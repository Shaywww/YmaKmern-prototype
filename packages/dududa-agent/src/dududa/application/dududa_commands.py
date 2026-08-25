# -*- coding: utf-8 -*-
"""Phase 4 拆分：管理命令应用用例。

每个 impl 接收 plugin（Main 适配器实例）与事件对象，返回要展示的文本；
副作用（切人格、开关、清记忆）在 impl 内完成。
"""
import json as _json
import logging
from uuid import uuid4

from dududa.safeguards.security import AuthorizationDecision
from dududa.core.renderer import OCRenderer
from dududa.core.group_policy import GROUP_MODES
from dududa.core.memory import MemoryType, ScopeSelector
from dududa.application.user_experience import MEMORY_MODES, make_support_id

from dududa.application.dududa_log import get_logger as _get_logger
logger = _get_logger("dududa20")


def _deny_hint(res, conf) -> str:
    if (res.decision == AuthorizationDecision.REQUIRE_CONFIRMATION
            and conf is not None):
        return (f"该操作需要管理员确认，确认码: {conf.confirmation_id}"
                "（管理员回复 dududa_confirm 确认码 后，发起者重试即可）")
    return "权限不足（需要管理员）"


async def cmd_status_impl(plugin) -> str:
    try:
        n = plugin.memory.count()
    except Exception:
        n = "?"
    return f"嘟嘟哒 2.0 | 人格: {plugin.personas.active_id} | 记忆: {n}"


async def cmd_mcp_impl(plugin) -> str:
    """MCP 状态：访问策略（按群/按人）+ 服务熔断 + 统一 Client（可选）。"""
    from dududa.mcp.access import mcp_access
    from dududa.mcp.registry import breaker_status

    acc = mcp_access.status()
    lines = [
        f"访问策略: default={acc['default_policy']}",
        f"  文件: {acc['path']} (exists={acc['exists']})",
        f"  群 allow={list(acc['groups'].get('allow', ()))} deny={list(acc['groups'].get('deny', ()))}",
        f"  人 allow={list(acc['users'].get('allow', ()))} deny={list(acc['users'].get('deny', ()))}",
    ]
    st = breaker_status()
    if st:
        lines.append("熔断: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    factory = getattr(plugin, "mcp_client", None)
    if factory is None:
        lines.append("统一 MCP Client 未启用（需设置 DUDUDA_MCP_CLIENT=1）")
    else:
        try:
            tools = await factory.list_tools()
            lines.append(f"统一 MCP Client: {factory.health()} | 发现工具: {len(tools)}")
        except Exception as e:
            lines.append(f"MCP client 异常: {e}")
    return "\n".join(lines)


async def cmd_health_impl(plugin) -> str:
    try:
        with open("/tmp/health_status.json") as f:
            s = _json.load(f)
        svc = s.get("services", {})
        return "\n".join([
            f"sign: {'OK' if svc.get('sign_server')=='ok' else 'DOWN'}",
            f"bot:  {'OK' if svc.get('astrbot')=='ok' else 'DOWN'}",
            f"内存: {s.get('memory','?')}",
        ])
    except Exception as e:
        logger.warning("Health read: %s", e)
        return "无法获取状态"


async def cmd_persona_impl(plugin, event, target) -> str:
    if not target:
        return f"可用: {', '.join(plugin.personas.list_all())}"
    res, conf = plugin._authorize_manage(
        event, resource="persona", payload={"target": target})
    if not res.allowed:
        return _deny_hint(res, conf)
    if plugin.personas.switch(target):
        plugin.renderer.set_persona(plugin.personas.active)
        plugin.oc_renderer = OCRenderer(persona=plugin._persona_to_oc(plugin.personas.active))
        if getattr(plugin, "runtime", None) is not None:
            plugin.runtime._renderer = plugin.oc_renderer
        return f"切换: {target}"
    return f"不存在: {target}"


async def cmd_confirm_impl(plugin, event, confirmation_id) -> str:
    """管理员批准高风险操作确认（绑定发起者/会话/操作内容，单次使用）。"""
    if not confirmation_id:
        return "用法: dududa_confirm <确认码>"
    conf = plugin.confirmations.get(confirmation_id)
    if conf is None or conf.is_expired or conf.is_consumed:
        return "确认码不存在或已失效"
    approver = plugin._actor_for(event)
    if approver.is_muted() or approver.role not in ("owner", "admin"):
        return "只有管理员可以确认"
    if not plugin._same_scope_prefix(conf.scope_key, plugin._scope_key(event)):
        return "只能在发起者所在的会话中确认"
    if plugin.confirmations.approve(confirmation_id):
        plugin._save_confirmations()
        return "已确认，请让发起者重试原操作（一次性，10分钟内有效）"
    return "确认失败：已过期或已使用"


async def cmd_off_impl(plugin, event) -> str:
    res, conf = plugin._authorize_manage(
        event, resource="switch", payload={"op": "off"})
    if not res.allowed:
        return _deny_hint(res, conf)
    plugin.enabled = False
    return "zzz..."


async def cmd_on_impl(plugin, event) -> str:
    res, conf = plugin._authorize_manage(
        event, resource="switch", payload={"op": "on"})
    if not res.allowed:
        return _deny_hint(res, conf)
    plugin.enabled = True
    return "已唤醒！"


async def cmd_forget_impl(plugin, event) -> str:
    res, conf = plugin._authorize_manage(
        event, resource="memory", payload={"op": "purge"})
    if not res.allowed:
        return _deny_hint(res, conf)
    try:
        n = plugin.memory.purge_expired()
        return f"已清除 {n} 条"
    except Exception as e:
        logger.warning("Forget: %s", e)
        return "清除失败"


def _ux(plugin):
    store = getattr(plugin, "ux_store", None)
    if store is None:
        raise RuntimeError("用户体验设置尚未装配")
    return store


def _memory_records(plugin, event, limit=50):
    scope = plugin._make_scope(event)
    selector = ScopeSelector(
        platform=scope.platform,
        bot_id=scope.bot_id,
        conversation_id=scope.conversation_id,
        actor_id=scope.actor_id,
    )
    query = getattr(plugin.memory, "query_selector", None)
    if query is None:
        return ()
    return query(selector, limit=limit)


async def cmd_memory_impl(plugin, event, action="status", record_id=None) -> str:
    """Self-service memory controls.  A user can only see/delete own scoped data."""
    action = (action or "status").strip().lower()
    aliases = {"开启": "active", "恢复": "active", "暂停": "paused",
               "临时": "temporary", "查看": "list", "清除": "clear",
               "状态": "status", "删除": "delete"}
    action = aliases.get(action, action)
    store = _ux(plugin)
    if action in MEMORY_MODES:
        store.set_memory_mode(event, action)
        hints = {
            "active": "记忆已开启：会读取并保存与你相关的记忆。",
            "paused": "记忆已暂停：仍可读取已有记忆，但不再新增。",
            "temporary": "已进入临时对话：不读取、也不保存记忆。",
        }
        return hints[action]
    mode = store.memory_mode(event)
    records = _memory_records(plugin, event)
    if action == "status":
        return (f"记忆模式: {mode} | 当前会话中你的记忆: {len(records)} 条\n"
                "用法: /dududa_memory list|active|paused|temporary|delete <ID>|clear")
    if action == "list":
        if not records:
            return f"记忆模式: {mode}\n当前会话还没有与你相关的可见记忆。"
        lines = [f"记忆模式: {mode} | 最近 {min(len(records), 10)} 条："]
        for record in records[:10]:
            content = " ".join((record.content or "").split())[:100]
            lines.append(f"- {record.record_id[:8]}  {content}")
        lines.append("删除单条: /dududa_memory delete <前8位ID>")
        return "\n".join(lines)
    if action == "delete":
        needle = (record_id or "").strip().lower()
        if len(needle) < 6:
            return "请提供列表中的记忆 ID，例如 /dududa_memory delete a1b2c3d4"
        matches = [r for r in records if r.record_id.lower().startswith(needle)]
        if len(matches) != 1:
            return "未找到唯一匹配的记忆，请重新查看列表后再试。"
        return "已删除 1 条记忆。" if plugin.memory.delete(matches[0].record_id) else "删除失败。"
    if action == "clear":
        ids = tuple(r.record_id for r in records)
        delete_many = getattr(plugin.memory, "delete_many", None)
        if delete_many is not None:
            count = delete_many(ids)
        else:
            count = sum(1 for rid in ids if plugin.memory.delete(rid))
        return f"已清除当前会话中与你相关的 {count} 条记忆。"
    return "未知操作。用法: /dududa_memory list|active|paused|temporary|delete <ID>|clear"


async def cmd_cancel_impl(plugin, event) -> str:
    registry = getattr(plugin, "ux_tasks", None)
    if registry is None:
        return "当前没有可取消的任务。"
    key = _ux(plugin).session_key(event)
    active = registry.running(key)
    if active is None:
        return "当前没有正在处理的任务。"
    active.task.cancel()
    return f"已请求取消当前任务（阶段: {active.phase}）。"


async def cmd_subscribe_impl(plugin, event, action="list", topic="更新") -> str:
    action = (action or "list").strip().lower()
    topic = (topic or "更新").strip()[:24]
    store = _ux(plugin)
    if action in ("add", "on", "订阅"):
        topics = store.subscribe(event, topic)
        return (f"已订阅「{topic}」。只有明确订阅的用户会收到消息。\n"
                f"当前订阅: {', '.join(topics)}")
    if action in ("remove", "off", "退订"):
        topics = store.unsubscribe(event, topic)
        return f"已退订「{topic}」。当前订阅: {', '.join(topics) if topics else '无'}"
    if action in ("quiet", "免打扰"):
        try:
            value = store.set_quiet_hours(event, topic)
            return f"免打扰时间已设置为 {value}。"
        except ValueError:
            return "格式错误。示例: /dududa_subscribe quiet 22:30-08:00"
    value = store.get(store.key_for_event(event))
    topics = value.get("subscriptions", [])
    return (f"当前订阅: {', '.join(topics) if topics else '无'}\n"
            f"免打扰: {value.get('quiet_hours')} | 每日最多 {value.get('daily_limit')} 条\n"
            "用法: /dududa_subscribe add 更新 | remove 更新 | quiet 22:30-08:00")


async def cmd_help_impl(plugin) -> str:
    registry = getattr(plugin, "cap_registry", None)
    capabilities = registry.list_enabled() if registry is not None else ()
    available = []
    unavailable = []
    for capability in capabilities:
        healthy = capability.is_healthy
        provider = registry.get_provider(capability.capability_id)
        if healthy and provider is not None:
            try:
                healthy = bool(provider.health())
            except Exception:
                healthy = False
        (available if healthy else unavailable).append(capability.name)
    lines = [
        "我是嘟嘟哒，可以聊天、查资料、看图片和读取常见文件。",
        "你可以试试：",
        "- 帮我解释一下量子纠缠",
        "- 总结这张图片/这个文件",
        "- 查一下今天的天气或新闻",
        "- 查一下评课社区里的微积分I（张瑞）",
        f"当前可用能力（{len(available)}）: {', '.join(available[:12]) or '基础对话'}",
    ]
    if unavailable:
        lines.append(f"暂不可用: {', '.join(unavailable[:8])}")
    lines.extend([
        "常用命令:",
        "/dududa_memory — 查看和控制记忆",
        "/dududa_subscribe — 自主管理订阅",
        "/dududa_cancel — 取消正在处理的任务",
        "/dududa_feedback — 提交脱敏改进反馈（不会自动修改机器人）",
        "/dududa_help — 查看这份动态帮助",
        "请不要发送密码、Token、Cookie 等敏感信息。",
        "主动订阅后会保存必要的会话路由；退订后不再发送。",
    ])
    return "\n".join(lines)


async def cmd_feedback_impl(plugin, summary: str = "") -> str:
    """用户主动提交改进线索；不保存身份、会话或原始附件。"""
    summary = (summary or "").strip()
    if not summary:
        return ("用法: /dududa_feedback <问题说明>\n"
                "反馈会先脱敏，只进入人工审核队列，不会自动修改或部署机器人。")
    evolution = getattr(plugin, "evolution", None)
    if evolution is None:
        from dududa.evolution import ShadowEvolution
        evolution = plugin.evolution = ShadowEvolution()
    try:
        item = evolution.add_experience(
            summary, source="user_feedback", signal_type="explicit_feedback")
    except ValueError:
        return "请补充具体的问题说明后再提交。"
    except Exception as exc:
        logger.warning("Shadow feedback unavailable: %s", exc)
        return "反馈队列当前不可用，请稍后再试。"
    if item.get("duplicate"):
        return (f"这条问题已经记录过啦（编号 {item['experience_id']}）。"
                "它仍只在人工审核队列中。")
    try:
        evolution.analyze()
    except Exception as exc:
        logger.warning("Shadow candidate analysis deferred: %s", exc)
    return (f"已记录脱敏改进反馈（编号 {item['experience_id']}）。"
            "它只会用于生成待审核候选，不会自动修改、启用或部署机器人。")


async def cmd_broadcast_prepare_impl(plugin, event, topic=None, message=None) -> str:
    topic = (topic or "").strip()[:24]
    message = (message or "").strip()
    if not topic or not message:
        return "用法: /dududa_broadcast <主题> <消息正文>"
    res, conf = plugin._authorize_manage(
        event, resource="subscriber_broadcast", payload={"topic": topic})
    if not res.allowed:
        return _deny_hint(res, conf)
    recipients = _ux(plugin).eligible_subscribers(topic)
    broadcast_id = uuid4().hex[:8]
    pending = getattr(plugin, "_pending_broadcasts", None)
    if pending is None:
        plugin._pending_broadcasts = pending = {}
    pending[broadcast_id] = {
        "topic": topic, "message": message[:1500],
        "recipients": recipients, "created": __import__("time").time(),
    }
    return (f"推送预览 [{topic}]：\n{message[:500]}\n\n"
            f"符合订阅、免打扰和频率限制的接收者: {len(recipients)}\n"
            f"确认发送: /dududa_broadcast_confirm {broadcast_id}")


async def cmd_broadcast_confirm_impl(plugin, event, broadcast_id=None) -> str:
    broadcast_id = (broadcast_id or "").strip()
    res, conf = plugin._authorize_manage(
        event, resource="subscriber_broadcast", payload={"id": broadcast_id})
    if not res.allowed:
        return _deny_hint(res, conf)
    pending = getattr(plugin, "_pending_broadcasts", {})
    item = pending.pop(broadcast_id, None)
    if not item:
        return "推送预览不存在或已失效。"
    now = __import__("time").time()
    if now - float(item.get("created", 0)) > 600:
        return "推送预览已过期，请重新生成。"
    sender = getattr(plugin, "_send_subscription_message", None)
    if sender is None:
        return f"发送失败，错误编号 {make_support_id('send', 'adapter_missing')}"
    sent = 0
    failed = 0
    for key, origin in item["recipients"]:
        # Preview recipients are only a snapshot.  Re-check immediately before
        # delivery so an unsubscribe or quiet-hour transition always wins.
        if not _ux(plugin).eligible(key, item["topic"]):
            continue
        try:
            await sender(origin, f"【嘟嘟哒·{item['topic']}】\n{item['message']}\n\n退订: /dududa_subscribe remove {item['topic']}")
            _ux(plugin).record_delivery(key)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("Opt-in broadcast failed (%s): %s", key[:8], exc)
    return f"推送完成：成功 {sent}，失败 {failed}。"


# ---- 群策略（文档 2.5.2 / 2.5.4）：mode / reply_rate / meme_rate ----

def _group_store(plugin):
    store = getattr(plugin, "group_policy", None)
    if store is None:
        raise RuntimeError("群策略存储未装配（group_policy）")
    return store


async def cmd_group_impl(plugin, event, target=None) -> str:
    """查看群策略（默认当前群）。"""
    gid = (target or "").strip()
    if not gid:
        try:
            gid = str(getattr(event.message_obj, "group", None) or "")
        except Exception:
            gid = ""
    if not gid:
        return "用法: dududa_group [群号]"
    policy = _group_store(plugin).get(gid)
    if policy is None:
        return f"群 {gid}: 未设置（normal / reply_rate=0 / meme_rate=1）"
    return (f"群 {gid}: mode={policy.mode} reply_rate={policy.reply_rate} "
            f"meme_rate={policy.meme_rate} interrupt_cost={policy.interruption_cost}")


async def cmd_group_mode_impl(plugin, event, group_id=None, mode=None) -> str:
    """设置群模式：normal（默认）/ silent（只回@和命令）/ off（沉默）。"""
    gid = (group_id or "").strip()
    mode = (mode or "").strip().lower()
    if not gid or not mode:
        return "用法: dududa_mode <群号> <normal|silent|off>"
    if mode not in GROUP_MODES:
        return "mode 无效（应为 normal/silent/off）"
    res, conf = plugin._authorize_manage(
        event, resource="group_policy", payload={"mode": mode, "group": gid})
    if not res.allowed:
        return _deny_hint(res, conf)
    policy = _group_store(plugin).set(gid, mode=mode)
    return (f"群 {gid} mode 已设置: {policy.mode} "
            f"(reply_rate={policy.reply_rate} meme_rate={policy.meme_rate})")


def _parse_rate(value) -> float:
    v = float(value)
    if not (0.0 <= v <= 1.0):
        raise ValueError
    return v


async def cmd_group_reply_rate_impl(plugin, event, group_id=None, rate=None) -> str:
    """设置被动参与概率 0~1（未 @ 时按此概率回应群消息）。"""
    gid = (group_id or "").strip()
    if not gid or rate is None:
        return "用法: dududa_reply_rate <群号> <0~1>"
    try:
        parsed = _parse_rate(rate)
    except (TypeError, ValueError):
        return "reply_rate 无效（应为 0~1 的数字）"
    res, conf = plugin._authorize_manage(
        event, resource="group_policy", payload={"reply_rate": parsed, "group": gid})
    if not res.allowed:
        return _deny_hint(res, conf)
    policy = _group_store(plugin).set(gid, reply_rate=parsed)
    return (f"群 {gid} reply_rate 已设置: {policy.reply_rate} "
            f"(mode={policy.mode} meme_rate={policy.meme_rate})")


async def cmd_group_meme_rate_impl(plugin, event, group_id=None, rate=None) -> str:
    """设置表情回复比例 0~1（问候/轻松消息走 REACT 的概率，未命中回文本）。"""
    gid = (group_id or "").strip()
    if not gid or rate is None:
        return "用法: dududa_meme_rate <群号> <0~1>"
    try:
        parsed = _parse_rate(rate)
    except (TypeError, ValueError):
        return "meme_rate 无效（应为 0~1 的数字）"
    res, conf = plugin._authorize_manage(
        event, resource="group_policy", payload={"meme_rate": parsed, "group": gid})
    if not res.allowed:
        return _deny_hint(res, conf)
    policy = _group_store(plugin).set(gid, meme_rate=parsed)
    return (f"群 {gid} meme_rate 已设置: {policy.meme_rate} "
            f"(mode={policy.mode} reply_rate={policy.reply_rate})")



async def cmd_group_interrupt_cost_impl(plugin, event, group_id=None, cost=None) -> str:
    """设置打断成本 0~1（被动参与概率乘 (1-cost)）。"""
    gid = (group_id or "").strip()
    if not gid or cost is None:
        return "用法: dududa_interrupt_cost <群号> <0~1>"
    try:
        parsed = _parse_rate(cost)
    except (TypeError, ValueError):
        return "interrupt_cost 无效（应为 0~1 的数字）"
    res, conf = plugin._authorize_manage(
        event, resource="group_policy",
        payload={"interruption_cost": parsed, "group": gid})
    if not res.allowed:
        return _deny_hint(res, conf)
    policy = _group_store(plugin).set(gid, interruption_cost=parsed)
    return (f"群 {gid} interrupt_cost 已设置: {policy.interruption_cost} "
            f"(mode={policy.mode} reply_rate={policy.reply_rate} meme_rate={policy.meme_rate})")


async def cmd_style_impl(plugin, event) -> str:
    """查看当前用户在本 Persona 下的 style 偏好（文档 2.5.8 四维隔离）。"""
    store = getattr(plugin, "style_store", None)
    if store is None:
        return "style 存储未装配（style_store）"
    try:
        platform = "qq"
        bot_id = "dududa"
        getter = getattr(plugin, "_get_bot_id", None)
        if getter is not None:
            try:
                bot_id = getter(event) or "dududa"
            except Exception:
                bot_id = "dududa"
        user = str(event.get_sender_id())
        persona = getattr(getattr(plugin, "personas", None), "active_id",
                          "dududa_default")
    except Exception:
        return "无法读取当前会话信息"
    style = store.get(platform, bot_id, user, persona)
    if style is None:
        return ("还没有记录你的风格偏好～告诉我“以后叫我XX”“回复简短点”"
                "“说话随意点”“多用表情”就能记住哦")
    return style.display()
