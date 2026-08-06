# -*- coding: utf-8 -*-
"""Phase 4 拆分：管理命令应用用例。

每个 impl 接收 plugin（Main 适配器实例）与事件对象，返回要展示的文本；
副作用（切人格、开关、清记忆）在 impl 内完成。
"""
import json as _json
import logging

from packages.safeguards.security import AuthorizationDecision
from packages.core.renderer import OCRenderer

from packages.application.dududa_log import get_logger as _get_logger
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
    """统一 MCP Client 状态（启用 DUDUDA_MCP_CLIENT=1 后生效）。"""
    factory = getattr(plugin, "mcp_client", None)
    if factory is None:
        return "统一 MCP Client 未启用（需设置 DUDUDA_MCP_CLIENT=1）"
    try:
        tools = await factory.list_tools()
        return f"MCP client: {factory.health()} | 发现工具: {len(tools)}"
    except Exception as e:
        return f"MCP client 异常: {e}"


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
