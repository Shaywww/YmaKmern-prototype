# -*- coding: utf-8 -*-
"""Phase 4 拆分：消息流处理用例（on_message / media / image / text）。

事件对象仍由 AstrBot 平台传入（窄接口访问），所有业务逻辑在此层完成；
Main 只做事件适配与结果发送。
"""
import logging
import random
import time
from uuid import uuid4

from packages.core.state import SocialAction, RuntimeState, RuntimePhase, RunOutcome, RuntimeBudget
from packages.core.delivery import DeliveryReceipt, DeliveryStatus
from packages.core.trace_recorder import trace_recorder

from packages.application.dududa_utils import (
    _detect_media, _has_media_in_raw, _contains_restricted,
    _redact_text, _file_ext, _parse_document, _IMAGE_EXTS,
)

from packages.application.dududa_log import get_logger as _get_logger
logger = _get_logger("dududa20")

_REACT_EMOJIS = ["(\u30b7\u00b0\u3002\u00b0)\uff83", "(\u3002>\u3002<\u3002)",
                 "(\u3002\u30fb\u03c9\u30fb\u3002)", "(\u2267\u2207\u2266)"]


async def handle_media(plugin, event, url, name, is_image,
                       run_id="", trace_id="") -> str:
    ext = _file_ext(name)
    try:
        logger.info("Media | run_id=%s trace_id=%s: %s (%s) image=%s url=%s",
                    run_id, trace_id, name, ext, is_image, url[:50])
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
        reply = await plugin._call_llm(system, user_msg, max_tokens=2048,
                                       temperature=0.3,
                                       run_id=run_id, trace_id=trace_id)
        plugin._store_memory(event,
            f"[文件《{name}》]:\n{text[:3000]}",
            f"[嘟嘟哒]: {reply[:500]}" if reply else "",
            msg_type="file", run_id=run_id, trace_id=trace_id)
        plugin._last_file_ts = time.time()
        return reply or "生成失败..."
    except Exception as e:
        logger.exception("Media error: %s", e)
        return "文件处理出错，稍后再试吧..."


async def handle_image(plugin, event, data, name, ext,
                         run_id="", trace_id="") -> str:
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
    reply = await plugin._call_vision(system, user_text, b64, mime,
                                       run_id=run_id, trace_id=trace_id)
    plugin._store_memory(event,
        f"[图片《{name}》]:\n{reply[:3000]}",
        f"[嘟嘟哒]: {reply[:500]}" if reply else "",
        msg_type="image", run_id=run_id, trace_id=trace_id)
    plugin._last_file_ts = time.time()
    return reply or "(｡•́︿•̀｡) 图片读不出来..."


async def handle_text(plugin, event, run_id="", trace_id="") -> str:
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
                    run_id=run_id or None,
                    trace_id=trace_id or None,
                )
                logger.info("Flow runtime: ok=%s reply=%r",
                            bool(result and result.final_response and result.final_response.text),
                            (result.final_response.text if result and result.final_response and result.final_response.text else "")[:60])
                if result.final_response and result.final_response.text:
                    reply = result.final_response.text
            except Exception as e:
                logger.warning("Runtime run failed: %s", e)
        if not reply:
            logger.info("Flow fallback LLM")
            p = plugin.personas.active
            reply = await plugin._call_llm(
                f"你是{p.display_name}，自称{p.first_person}。你就是嘟嘟哒。用颜表情风格，短回复。"
                "如果用户只发来一个词或短名词（如 USTC、AstrBot），视为在询问它的含义，直接解释，不要当打招呼。",
                preprocessed.combined_text, max_tokens=1024, temperature=0.5,
                run_id=run_id, trace_id=trace_id)
        user_snippet = f"[用户]: {preprocessed.combined_text[:300]}"
        bot_snippet = f"[嘟嘟哒]: {reply[:300]}" if reply else ""
        if result is not None:
            try:
                # 投递回执：消息已发送，确认后 Orchestrator 才落盘 bot 记忆
                receipt = DeliveryReceipt(run_id=result.run_id,
                                          status=DeliveryStatus.SUCCEEDED)
                await plugin.runtime.acknowledge_delivery(receipt)
                plugin._store_memory(event, user_snippet,
                                       run_id=run_id, trace_id=trace_id)
                return reply or ""
            except Exception as e:
                logger.warning("Delivery ack failed: %s", e)
        plugin._store_memory(event, user_snippet, bot_snippet,
                               run_id=run_id, trace_id=trace_id)
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
    run_id, trace_id = uuid4().hex, uuid4().hex
    _msg_snip = str(getattr(event, "message_str", "") or "")[:80]
    logger.info("Flow start | run_id=%s trace_id=%s msg=%r",
                run_id, trace_id, _msg_snip)
    _flow_ts = time.time()
    try:
        _session = str(event.get_session_id())
    except Exception:
        _session = ""
    trace_recorder.record(event="flow_start", run_id=run_id, trace_id=trace_id,
                          msg=_msg_snip, session=_session)
    try:
        reply = await _run_flow_inner(
            plugin, event, msgs, run_id, trace_id)
        trace_recorder.record(event="flow_end", run_id=run_id, trace_id=trace_id,
                              duration_ms=int((time.time() - _flow_ts) * 1000),
                              reply=(reply or "")[:200])
        return reply
    except Exception as e:
        logger.exception("Flow error | run_id=%s trace_id=%s: %s",
                         run_id, trace_id, e)
        trace_recorder.record(event="flow_error", run_id=run_id, trace_id=trace_id,
                              duration_ms=int((time.time() - _flow_ts) * 1000),
                              error=str(e)[:300])
        return None


async def _run_flow_inner(plugin, event, msgs, run_id, trace_id):
    """run_message_flow 主体：所有分支带 run_id/trace_id 落日志（P1-3 Trace）。"""
    if _is_at_only(event, msgs):
        # 纯@：优先配对同人 60s 内刚发的图（QQ 拆条），没图才回通用短句
        _at_paired = _take_paired_media(plugin, event)
        if _at_paired:
            _at_reply = await handle_media(
                plugin, event, _at_paired[0], _at_paired[1], _at_paired[2],
                run_id=run_id, trace_id=trace_id)
            _drop_stash_file(_at_paired[0])
            if _at_reply:
                event.stop_event()
                logger.info("Flow end | run_id=%s trace_id=%s reply=%r",
                            run_id, trace_id, _at_reply[:80])
                return _at_reply
            return None
        _r = random.choice(_AT_ONLY_REPLIES)
        logger.info("Flow end | run_id=%s trace_id=%s reply=%r",
                    run_id, trace_id, _r[:80])
        return _r
    if plugin._should_ignore(event): return None
    if _stash_group_media(plugin, event, msgs):
        return None
    state = RuntimeState(run_id=run_id, trace_id=trace_id)
    action, reason = plugin._social_decision(event)
    logger.info("Flow decision | run_id=%s trace_id=%s: %s (%s)",
                run_id, trace_id, action, reason)
    if action == SocialAction.IGNORE:
        state = state.transition(RuntimePhase.DECIDED,
                                 social_decision=SocialAction.IGNORE,
                                 decision_reason=reason,
                                 outcome=RunOutcome.IGNORED)
        return None
    if action == SocialAction.REACT:
        state = state.transition(RuntimePhase.COMPLETED,
                                 outcome=RunOutcome.SUCCEEDED)
        _r = random.choice(_REACT_EMOJIS)
        logger.info("Flow react | run_id=%s trace_id=%s: %r",
                    run_id, trace_id, _r)
        return _r
    state = state.transition(RuntimePhase.DECIDED,
                             social_decision=action,
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
        reply = await handle_media(plugin, event, file_url, file_name, is_image,
                                   run_id=run_id, trace_id=trace_id)
        if reply:
            event.stop_event()
            logger.info("Flow end | run_id=%s trace_id=%s reply=%r",
                        run_id, trace_id, reply[:80])
            return reply
        return None
    if not file_url:
        paired = _take_paired_media(plugin, event)
        if paired:
            reply = await handle_media(plugin, event, paired[0], paired[1], paired[2],
                                       run_id=run_id, trace_id=trace_id)
            _drop_stash_file(paired[0])
            if reply:
                event.stop_event()
                logger.info("Flow end | run_id=%s trace_id=%s reply=%r",
                            run_id, trace_id, reply[:80])
                return reply
            return None
    reply = await handle_text(plugin, event, run_id=run_id, trace_id=trace_id)
    logger.info("Flow end | run_id=%s trace_id=%s reply=%r",
                run_id, trace_id, (reply or "")[:80])
    return reply or None

_IMAGE_ASK_KEYWORDS = ("图", "照片", "这张", "这个", "什么", "怎么样", "啥",
                       "截图", "截屏", "画面", "内容", "文件", "文档")


def _stash_dir() -> str:
    import os as _os
    return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), "data", "stash")


def _preserve_media(url: str) -> str:
    """本地路径的媒体复制到自管目录，防止 AstrBot 清理 temp 后配对失败。"""
    import os as _os
    import shutil as _shutil
    if not url.startswith("/"):
        return url
    try:
        if not _os.path.exists(url):
            return url
        d = _stash_dir()
        _os.makedirs(d, exist_ok=True)
        dst = _os.path.join(d, "%d_%s" % (int(time.time() * 1000),
                                          _os.path.basename(url)))
        _shutil.copy2(url, dst)
        return dst
    except Exception:
        return url


def _drop_stash_file(path: str) -> None:
    import os as _os
    try:
        if path and path.startswith(_stash_dir()) and _os.path.exists(path):
            _os.remove(path)
    except Exception:
        pass


def _remote_media_url(event) -> str:
    """从原始 OneBot 消息里找图片/文件的远程 URL（本地文件缺失时的兜底）。"""
    raw = getattr(event, "raw_message", None)
    if raw is None:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is None:
        return ""
    for attr in ("message", "json"):
        msg = getattr(raw, attr, None)
        if callable(msg):
            msg = msg()
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict) and item.get("type") in ("image", "file"):
                    u = str(item.get("url", "") or "")
                    if u.startswith("http"):
                        return u
    return ""


_AT_ONLY_REPLIES = (
    "在呢在呢～叫我有什么事呀？(｡･ω･｡)",
    "来啦来啦～想聊什么都可以哦～(≧▽≦)",
    "在的在的～要帮忙还是唠嗑呀？(◕‿◕)",
)


def _is_at_only(event, msgs) -> bool:
    """@ 了机器人但没有任何文本/媒体（QQ 拆条：@ 和图片分开发）。"""
    import re as _re
    text = str(getattr(event, "message_str", "") or "")
    cleaned = _re.sub(r"\[At:\d+\]", "", text).strip()
    for c in msgs:
        t = str(getattr(c, "type", ""))
        if "Image" in t or "File" in t or "Record" in t:
            return False
        if "At" not in t:
            cleaned += " " + str(getattr(c, "text", "") or "")
    cleaned = cleaned.strip()
    if cleaned:
        return False
    if "[At:" in text:
        return True
    return bool(getattr(event, "is_at_or_wake_command", False))


def _stash_group_media(plugin, event, msgs) -> bool:
    """未@ 的群聊图片/文件：静默暂存 60s，返回 True 表示吞掉本消息。"""
    if getattr(event, "is_at_or_wake_command", False):
        return False
    try:
        gid = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
        if not gid:
            return False
        if not any("Image" in str(getattr(c, "type", "")) or "File" in str(getattr(c, "type", ""))
                   for c in msgs):
            return False
        f_url, f_name, f_img = _detect_media(event)
        if not f_url:
            return False
        f_url = _preserve_media(f_url)
        if not f_url.startswith("/"):
            remote = _remote_media_url(event)
            if remote:
                f_url = remote
        slot = getattr(plugin, "_recent_media", None)
        if slot is None:
            slot = plugin._recent_media = {}
        now = time.time()
        for k in [k for k, v in slot.items() if now - v[0] > 60]:
            _drop_stash_file(v[1])
            slot.pop(k, None)
        sender = str(event.get_sender_id())
        slot[(gid, sender)] = (now, f_url, f_name, f_img)
        logger.info("Flow stash: gid=%s url=%s", gid, f_url[:50])
        return True
    except Exception:
        return False


def _take_paired_media(plugin, event):
    """@ 消息没带图时，配对同群同人 60s 内发的图；空文本或提到图才配对。"""
    try:
        gid = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
        if not gid:
            return ()
        slot = getattr(plugin, "_recent_media", None)
        if not slot:
            return ()
        sender = str(event.get_sender_id())
        st = slot.get((gid, sender))
        if not st or time.time() - st[0] > 60:
            return ()
        text = str(getattr(event, "message_str", "") or "").strip()
        if text and not any(kw in text for kw in _IMAGE_ASK_KEYWORDS):
            return ()
        slot.pop((gid, sender), None)
        return (st[1], st[2], st[3])
    except Exception:
        return ()
