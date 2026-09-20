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
    stored_memory = []

    def store_memory(event, *contents, **kwargs):
        stored_memory.extend(contents)

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
        _store_memory=store_memory,
        stored_memory=stored_memory,
    )


@pytest.mark.asyncio
async def test_message_flow_shows_progress_without_unsolicited_welcome(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    async def inner(plugin, event, *args):
        dududa_handlers._mark_task_phase(plugin, event, "tools")
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
async def test_slow_ordinary_chat_never_shows_analysis_progress(
    tmp_path, monkeypatch
):
    p = plugin(tmp_path)

    async def inner(plugin, event, *args):
        dududa_handlers._mark_task_phase(plugin, event, "compose")
        await asyncio.sleep(0.03)
        return "我就回了一句啊。"

    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(
        dududa_handlers, "_prune_stale_deliveries",
        lambda plugin: asyncio.sleep(0))
    event = Event("chat")
    event.message_str = "你攻击性太强了"

    reply = await dududa_handlers.run_message_flow(p, event)

    assert event.sent == []
    assert p.stored_memory == []
    assert reply == "我就回了一句啊。"


@pytest.mark.asyncio
async def test_newer_message_cancels_stale_reply_and_replaces_turn(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def inner(plugin, event, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return "过时回答"
        return f"最新：{event.message_str}"

    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(dududa_handlers, "_prune_stale_deliveries", lambda plugin: asyncio.sleep(0))
    first_event = Event("m1")
    first_event.message_str = "我忘了"
    first = asyncio.create_task(
        dududa_handlers.run_message_flow(p, first_event))
    await entered.wait()
    second_event = Event("m2")
    second_event.message_str = "保存你人设，刚才改的没了"
    second = asyncio.create_task(
        dududa_handlers.run_message_flow(p, second_event))
    await asyncio.sleep(0)
    release.set()

    assert await first == ""
    replacement = await second
    assert "我忘了" in replacement
    assert "保存你人设" in replacement
    assert calls == 2


@pytest.mark.asyncio
async def test_adjacent_bubbles_merge_before_generation(tmp_path, monkeypatch):
    p = plugin(tmp_path)
    p.turn_merge_delay = 0.03
    p.turn_merge_max_delay = 0.08
    seen = []

    async def inner(plugin, event, *args):
        seen.append(event.message_str)
        return event.message_str

    monkeypatch.setattr(dududa_handlers, "_run_flow_inner", inner)
    monkeypatch.setattr(
        dududa_handlers, "_prune_stale_deliveries",
        lambda plugin: asyncio.sleep(0))
    first_event = Event("m1")
    first_event.message_str = "我忘了"
    second_event = Event("m2")
    second_event.message_str = "保存你人设"

    first = asyncio.create_task(
        dududa_handlers.run_message_flow(p, first_event))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        dududa_handlers.run_message_flow(p, second_event))

    assert await second == ""
    assert await first == "我忘了\n保存你人设"
    assert seen == ["我忘了\n保存你人设"]


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
