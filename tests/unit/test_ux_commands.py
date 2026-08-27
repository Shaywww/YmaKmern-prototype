from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

import pytest

from dududa.application import dududa_commands
from dududa.application.user_experience import UserExperienceStore
from dududa.core.capability import Capability, CapabilityRegistry, CapProvider, ProviderType
from dududa.core.memory import JSONMemoryRepository, MemoryRecord, MemoryScope, MemoryType
from dududa.core.meme_library import MemeLibrary


class Event:
    unified_msg_origin = "qq:FriendMessage:u1"
    message_obj = SimpleNamespace(group=None)

    def get_platform_name(self): return "qq"
    def get_sender_id(self): return "u1"
    def get_session_id(self): return "s1"


class Provider(CapProvider):
    async def execute(self, capability, arguments):  # pragma: no cover
        return None
    def health(self): return True


def plugin(tmp_path: Path):
    event = Event()
    scope = MemoryScope(MemoryType.SHORT_TERM, "qq", "bot", "s1", "u1")
    memory = JSONMemoryRepository(str(tmp_path / "memory.json"))
    memory.write(MemoryRecord(scope=scope, content="用户喜欢简短回复"))
    cap_registry = CapabilityRegistry()
    cap_registry.register(Capability("chat", "智能对话", "聊天", provider=ProviderType.BUILTIN), Provider())
    return SimpleNamespace(
        ux_store=UserExperienceStore(str(tmp_path / "ux.json")),
        memory=memory,
        cap_registry=cap_registry,
        _make_scope=lambda _event: scope,
    ), event


@pytest.mark.asyncio
async def test_memory_command_lists_deletes_and_changes_mode(tmp_path):
    p, event = plugin(tmp_path)
    listed = await dududa_commands.cmd_memory_impl(p, event, "list")
    record_id = next(iter(p.memory._records))[:8]
    assert record_id in listed
    assert "用户喜欢简短回复" in listed
    assert "已暂停" in await dududa_commands.cmd_memory_impl(p, event, "paused")
    assert "已删除" in await dududa_commands.cmd_memory_impl(p, event, "delete", record_id)


@pytest.mark.asyncio
async def test_dynamic_help_uses_registry_health(tmp_path):
    p, _ = plugin(tmp_path)
    text = await dududa_commands.cmd_help_impl(p)
    assert "智能对话" in text
    assert "/dududa_memory" in text
    assert "/dududa_cancel" in text


@pytest.mark.asyncio
async def test_subscribe_is_explicit_and_reversible(tmp_path):
    p, event = plugin(tmp_path)
    assert "已订阅" in await dududa_commands.cmd_subscribe_impl(p, event, "add", "更新")
    assert "更新" in await dududa_commands.cmd_subscribe_impl(p, event, "list")
    assert "已退订" in await dududa_commands.cmd_subscribe_impl(p, event, "remove", "更新")


@pytest.mark.asyncio
async def test_broadcast_preview_never_sends_and_confirm_rechecks_opt_in(tmp_path, monkeypatch):
    p, event = plugin(tmp_path)
    p._authorize_manage = lambda *a, **kw: (SimpleNamespace(allowed=True), None)
    sent = []
    async def send(origin, text):
        sent.append((origin, text))
    p._send_subscription_message = send
    await dududa_commands.cmd_subscribe_impl(p, event, "add", "更新")
    preview = await dududa_commands.cmd_broadcast_prepare_impl(p, event, "更新", "版本说明")
    assert "推送预览" in preview
    assert sent == []
    broadcast_id = preview.rsplit(" ", 1)[-1]
    # User opts out between preview and confirmation: delivery must be skipped.
    await dududa_commands.cmd_subscribe_impl(p, event, "remove", "更新")
    result = await dududa_commands.cmd_broadcast_confirm_impl(p, event, broadcast_id)
    assert result == "推送完成：成功 0，失败 0。"
    assert sent == []


@pytest.mark.asyncio
async def test_group_meme_command_requires_review_and_scopes_entries(tmp_path):
    allowed = SimpleNamespace(allowed=True)
    p = SimpleNamespace(
        meme_library=MemeLibrary(str(tmp_path / "memes.json")),
        _authorize_manage=lambda *a, **kw: (allowed, None),
    )
    event = Event()
    event.message_obj = SimpleNamespace(group="g1")
    event.message_str = (
        "/dududa_meme add 轨交之神 | 夸专业课答题很强 | 轨信之神")
    added = await dududa_commands.cmd_group_meme_impl(p, event)
    assert "已加入本群梗库" in added

    event.message_str = "/dududa_meme list"
    listed = await dududa_commands.cmd_group_meme_impl(p, event)
    assert "轨交之神" in listed and "轨信之神" in listed
    assert p.meme_library.match("轨信之神", group_id="g1") is not None
    assert p.meme_library.match("轨信之神", group_id="g2") is None

