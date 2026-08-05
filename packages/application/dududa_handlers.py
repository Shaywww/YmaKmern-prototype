# -*- coding: utf-8 -*-
"""Phase 4 拆分：消息流处理用例（on_message / media / image / text）。

事件对象仍由 AstrBot 平台传入（窄接口访问），所有业务逻辑在此层完成；
Main 只做事件适配与结果发送。
"""
import logging
import random
import time

from packages.core.state import SocialAction, RuntimeState, RuntimePhase, RunOutcome, RuntimeBudget
from packages.core.delivery import DeliveryReceipt, DeliveryStatus

from packages.application.dududa_utils import (
    _detect_media, _has_media_in_raw, _contains_restricted,
    _redact_text, _file_ext, _parse_document, _IMAGE_EXTS,
)

logger = logging.getLogger("dududa20")

_REACT_EMOJIS = ["(\u30b7\u00b0\u3002\u00b0)\uff83", "(\u3002>\u3002<\u3002)",
                 "(\u3002\u30fb\u03c9\u30fb\u3002)", "(\u2267\u2207\u2266)"]


async def handle_media(plugin, event, url, name, is_image) -> str:
    ext = _file_ext(name)
    try:
        logger.info("Media: %s (%s) image=%s url=%s", name, ext, is_image, url[:50])
        if url.startswith("/"):
            import os as _os
            if _os.path.exists(url):
                with open(url, "rb") as f:
                    data = f.read()
            else:
                return "找不到文件"
        elif url.startswith("data:"):
            import base64
            _, encoded = url.split(",", 1) if "," in url else ("", url.split(":", 2)[-1])
            data = base64.b64decode(encoded)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                r = await c.get(url)
                if r.status_code != 200:
                    return "下载失败..."
                data = r.content

        if is_image or ext in _IMAGE_EXTS:
            return await handle_image(plugin, event, data, name, ext)

        text = _parse_document(data, name)
        if not text: return "无法解析文件格式~"
        if _contains_restricted(text):
            logger.warning("File contains restricted content: %s", name)
            return "文件里包含敏感信息（密码/Token/登录态），我不能处理哦。"
        text = _redact_text(text)
        pre = plugin.input_adapter.to_preprocessed(event)
        p = plugin.personas.active
        system = (
            f"你是{p.display_name}，自称{p.first_person}。你就是嘟嘟哒。"
            "★ 你必须基于用户提供的文件内容如实回答。不准编造。"
            "回复用颜表情风格，但内容必须准确。"
        )
        user_msg = (
            f"用户发来文件《{name}》，完整内容：\n\n{text[:6000]}\n\n"
            f"用户说：{pre.combined_text if pre.combined_text.strip() else '请帮我看看这个文件'}\n\n"
            "请基于以上文件内容如实回复，不准编造。"
        )
        reply = await plugin._call_llm(system, user_msg, max_tokens=2048, temperature=0.3)
        plugin._store_memory(event,
            f"[文件《{name}》]:\n{text[:3000]}",
            f"[嘟嘟哒]: {reply[:500]}" if reply else "",
            msg_type="file")
        plugin._last_file_ts = time.time()
        return reply or "生成失败..."
    except Exception as e:
        logger.exception("Media error: %s", e)
        return "文件处理出错，稍后再试吧..."


async def handle_image(plugin, event, data, name, ext) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(data).decode()
    mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
                 "gif":"image/gif","webp":"image/webp","bmp":"image/bmp"}
    mime = mime_map.get(ext, "image/png")
    pre = plugin.input_adapter.to_preprocessed(event)
    p = plugin.personas.active
    user_text = pre.combined_text if pre.combined_text.strip() else "请描述这张图片的内容。如果图片里有文字，请把文字完整提取出来。"
    if _contains_restricted(user_text):
        logger.warning("Restricted content blocked from vision")
        return "这类敏感信息我不能处理哦，请不要发送密码、Token 或登录凭证。"
    user_text = _redact_text(user_text)
    system = (
        f"你是{p.display_name}，自称{p.first_person}。你就是嘟嘟哒。"
        "用户发来一张图片。请详细描述图片内容。"
        "★ 如果图片里有文字，必须完整提取。回复用颜表情风格，但内容必须准确。"
    )
    reply = await plugin._call_vision(system, user_text, b64, mime)
    plugin._store_memory(event,
        f"[图片《{name}》]:\n{reply[:3000]}",
        f"[嘟嘟哒]: {reply[:500]}" if reply else "",
        msg_type="image")
    plugin._last_file_ts = time.time()
    return reply or "(｡•́︿•̀｡) 图片读不出来..."


async def handle_text(plugin, event) -> str:
    try:
        preprocessed = plugin.input_adapter.to_preprocessed(event)
        if not preprocessed or not preprocessed.combined_text.strip(): return ""
        if _contains_restricted(preprocessed.combined_text):
            logger.warning("Restricted content blocked from LLM/memory")
            return "这类敏感信息我不能处理哦，请不要发送密码、Token、Cookie 或登录凭证。"
        perception = plugin._perceive(event)
        try:
            envelope = plugin.input_adapter.to_envelope(event)
        except Exception:
            envelope = getattr(preprocessed, "envelope", None)
        result = None
        reply = ""
        if envelope is not None:
            try:
                # P4: 文本路径走生产 Orchestrator（工具链 + 投递回执）
                result = await plugin.runtime.run(
                    envelope,
                    budget=RuntimeBudget(max_tool_steps=4, max_tool_retries=2,
                                          deadline_seconds=40),
                    perception=perception,
                    event=event,
                )
                if result.final_response and result.final_response.text:
                    reply = result.final_response.text
            except Exception as e:
                logger.warning("Runtime run failed: %s", e)
        if not reply:
            p = plugin.personas.active
            reply = await plugin._call_llm(
                f"你是{p.display_name}，自称{p.first_person}。你就是嘟嘟哒。用颜表情风格，短回复。",
                preprocessed.combined_text, max_tokens=1024, temperature=0.5)
        user_snippet = f"[用户]: {preprocessed.combined_text[:300]}"
        bot_snippet = f"[嘟嘟哒]: {reply[:300]}" if reply else ""
        if result is not None:
            try:
                # 投递回执：消息已发送，确认后 Orchestrator 才落盘 bot 记忆
                receipt = DeliveryReceipt(run_id=result.run_id,
                                          status=DeliveryStatus.SUCCEEDED)
                await plugin.runtime.acknowledge_delivery(receipt)
                plugin._store_memory(event, user_snippet)
                return reply or ""
            except Exception as e:
                logger.warning("Delivery ack failed: %s", e)
        plugin._store_memory(event, user_snippet, bot_snippet)
        return reply or ""
    except Exception as e:
        logger.exception("Text error: %s", e)
        return "诶呀，短路了一下..."


async def run_message_flow(plugin, event) -> str | None:
    """on_message 主流程（原 Main.on_message 逻辑）。

    返回要发送的文本；None 表示不回复。
    """
    if not plugin.enabled: return None
    if plugin._is_self_message(event): return None
    msgs = event.get_messages()
    if not msgs:
        if time.time() - plugin._last_file_ts < 3: return None
        if _has_media_in_raw(event): return None
    msg_id = ""
    try: msg_id = str(event.message_obj.message_id)
    except Exception: pass
    if not msg_id: msg_id = str(id(event))
    if msg_id in plugin._processed: return None
    plugin._processed.add(msg_id)
    if len(plugin._processed) > 2000: plugin._processed.clear()
    if plugin._should_ignore(event): return None
    state = RuntimeState()
    action, reason = plugin._social_decision(event)
    if action == SocialAction.IGNORE:
        state = state.transition(RuntimePhase.DECIDED,
                                 social_decision=SocialAction.IGNORE,
                                 decision_reason=reason,
                                 outcome=RunOutcome.IGNORED)
        return None
    if action == SocialAction.REACT:
        state = state.transition(RuntimePhase.COMPLETED,
                                 outcome=RunOutcome.SUCCEEDED)
        return random.choice(_REACT_EMOJIS)
    state = state.transition(RuntimePhase.DECIDED,
                             social_decision=SocialAction.ANSWER,
                             decision_reason=reason)
    state = state.transition(RuntimePhase.VALIDATED)
    perception = plugin._perceive(event)
    state = state.transition(RuntimePhase.PERCEIVED, perception=perception)
    try:
        envelope = plugin.input_adapter.to_envelope(event)
        ctx_snapshot = plugin.context_builder.build(envelope)
        state = state.transition(RuntimePhase.CONTEXT_BUILT, context_snapshot=ctx_snapshot)
    except Exception:
        pass
    file_url, file_name, is_image = _detect_media(event)
    if file_url:
        reply = await handle_media(plugin, event, file_url, file_name, is_image)
        if reply:
            event.stop_event()
            return reply
        return None
    reply = await handle_text(plugin, event)
    return reply or None
