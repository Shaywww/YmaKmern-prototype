# -*- coding: utf-8 -*-
"""Phase 4 拆分：管理命令应用用例。

每个 impl 接收 plugin（Main 适配器实例）与事件对象，返回要展示的文本；
副作用（切人格、开关、清记忆）在 impl 内完成。
"""
import json as _json
import logging
import time

from dududa.safeguards.security import AuthorizationDecision
from dududa.core.renderer import OCRenderer
from dududa.core.group_policy import GROUP_MODES

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


# ---- 更新公告推送（Update Pusher）----

def _announce_report_lines(report: dict) -> str:
    if report.get("skipped") == "no_pending":
        return "当前没有待推送的公告（或已推送过）"
    if report.get("friends") is None:
        return ("推送未完成：" + str(report.get("error") or report.get("skipped"))
                + "（适配器未就绪或好友列表拉取失败，会自动重试）")
    lines = [
        f"公告 {report.get('version') or ''} 推送完成："
        f"好友 {report['friends']} 人，本次新推送 {report['pushed_new']} 人，"
        f"累计 {report['pushed_total']} 人",
    ]
    if report.get("failed"):
        shown = "、".join(f"{uid}({err})" for uid, err in report["failed"][:5])
        lines.append("失败（下次自动重试）: " + shown)
    return "\n".join(lines)


async def cmd_announce_impl(plugin, event, content) -> str:
    """管理员：写入更新公告并立即推送给机器人好友。

    用法: dududa_announce [版本号] <更新内容>
    例如: dududa_announce v4.4.0 新增天气查询与更新推送
    """
    if not content or not content.strip():
        return "用法: dududa_announce [版本号] <更新内容>\n例如: dududa_announce v4.4.0 新增天气查询"
    res, conf = plugin._authorize_manage(
        event, resource="notice", payload={"op": "announce"})
    if not res.allowed:
        return _deny_hint(res, conf)
    text = content.strip()
    version = ""
    first = text.split(None, 1)[0]
    if first and (first[:1].lower() == "v" or first[:1].isdigit()) \
            and any(ch.isdigit() for ch in first):
        version = first
        rest = text[len(first):].strip()
        if not rest:
            return "更新内容不能为空"
        text = rest
    store = getattr(plugin, "notice_store", None)
    if store is None:
        return "更新公告未装配（notice_store）"
    notice = {
        "version": version,
        "content": text,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pushed_at": None,
        "pushed_ids": [],
    }
    store.write(notice)
    pusher = getattr(plugin, "update_pusher", None)
    if pusher is None:
        return "公告已写入，但推送器未启用（DUDUDA_UPDATE_PUSH=0）"
    try:
        report = await pusher.push_pending()
    except Exception as e:
        logger.warning("Announce push failed: %s", e)
        return "公告已写入，推送异常: %s" % e
    return _announce_report_lines(report)


async def cmd_announce_status_impl(plugin) -> str:
    """查看当前公告状态（版本/内容/是否已推送/送达人数）。"""
    store = getattr(plugin, "notice_store", None)
    if store is None:
        return "更新公告未装配（notice_store）"
    notice = store.load()
    if notice is None:
        return "暂无更新公告"
    return "\n".join([
        f"版本: {notice.get('version') or '（无）'}",
        f"内容: {notice.get('content')}",
        f"创建: {notice.get('created_at')}",
        f"推送: {notice.get('pushed_at') or '未推送'}",
        f"已送达: {len(notice.get('pushed_ids') or [])} 人",
    ])
