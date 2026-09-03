import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from dududa.application import dududa_handlers
from dududa.application.user_experience import (
    ConversationTaskRegistry,
    UserExperienceStore,
)
from dududa.core.idempotency import MessageIdempotencyRegistry


class Event:
    def __init__(self, message_id="m1", actor="u1", session="s1"):
        self.message_obj = SimpleNamespace(message_id=message_id, group=None)
        self.message_str = "请认真回答"
        self.actor = actor
        self.session = session
        self.sent = []

    def get_messages(self): return [SimpleNamespace()]
    def get_platform_name(self): return "qq"
    def get_sender_id(self): return self.actor
    def get_session_id(self): return self.session
    def plain_result(self, text): return text
    async def send(self, result): self.sent.append(result)


def plugin(tmp_path: Path):
    async def progress(event, text):
        await event.send(text)
    return SimpleNamespace(
        enabled=True,
        _last_file_ts=0.0,
        _pending_deliveries={},
        _idem=MessageIdempotencyRegistry(),
        _is_self_message=lambda event: False,
        _get_bot_id=lambda event: "bot",
        ux_store=UserExperienceStore(str(tmp_path / "ux.json")),
        ux_tasks=ConversationTaskRegistry(),
        progress_delay=0.01,
        _send_progress=progress,
    )


@pytest.mark.asyncio
async def test_message_flow_shows_progress_without_unsolicited_welcome(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    async def inner(*args):
        await asyncio.sleep(0.03)
        return "最终答案"
    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(dududa_handlers, "_prune_stale_deliveries", lambda plugin: asyncio.sleep(0))
    first = Event("m1")
    reply = await dududa_handlers.run_message_flow(p, first)
    assert first.sent and "正在" in first.sent[0]
    assert reply == "最终答案"
    assert "第一次见面" not in reply
    second = Event("m2")
    reply2 = await dududa_handlers.run_message_flow(p, second)
    assert reply2 == "最终答案"


@pytest.mark.asyncio
async def test_casual_food_advice_never_shows_lookup_progress(
    tmp_path, monkeypatch
):
    p = plugin(tmp_path)

    async def inner(*args):
        await asyncio.sleep(0.03)
        return "来碗浆水面，清爽点。"

    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(
        dududa_handlers, "_prune_stale_deliveries",
        lambda plugin: asyncio.sleep(0))
    event = Event("lunch")
    event.message_str = "中午吃什么"

    reply = await dududa_handlers.run_message_flow(p, event)

    assert event.sent == []
    assert reply == "来碗浆水面，清爽点。"


@pytest.mark.asyncio
async def test_message_flow_rejects_parallel_and_cancel_stops_active(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    entered = asyncio.Event()
    async def inner(*args):
        entered.set()
        await asyncio.sleep(30)
        return "late"
    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(dududa_handlers, "_prune_stale_deliveries", lambda plugin: asyncio.sleep(0))
    first = asyncio.create_task(dududa_handlers.run_message_flow(p, Event("m1")))
    await entered.wait()
    duplicate = await dududa_handlers.run_message_flow(p, Event("m2"))
    assert "还在处理上一条" in duplicate
    assert "perception" not in duplicate
    key = p.ux_store.session_key(Event("cancel"))
    assert p.ux_tasks.cancel(key)
    assert "已取消" in await first


@pytest.mark.asyncio
async def test_message_flow_returns_support_id_on_unhandled_error(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    async def inner(*args):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(dududa_handlers, "_prune_stale_deliveries", lambda plugin: asyncio.sleep(0))
    reply = await dududa_handlers.run_message_flow(p, Event())
    assert "错误编号：FLOW-" in reply
    assert "provider exploded" not in reply
