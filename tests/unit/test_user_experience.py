import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dududa.application.user_experience import (
    ConversationTaskRegistry,
    UserExperienceStore,
    make_support_id,
)
from dududa.core.memory import (
    JSONMemoryRepository,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    reset_memory_access_mode,
    set_memory_access_mode,
)


class Event:
    def __init__(self, actor="u1", session="s1", group=None):
        self.actor = actor
        self.session = session
        self.message_obj = SimpleNamespace(group=group)
        self.unified_msg_origin = f"qq:FriendMessage:{actor}"

    def get_platform_name(self):
        return "qq"

    def get_sender_id(self):
        return self.actor

    def get_session_id(self):
        return self.session


def _scope(actor="u1"):
    return MemoryScope(
        memory_type=MemoryType.SHORT_TERM,
        platform="qq",
        bot_id="bot",
        conversation_id="s1",
        actor_id=actor,
    )


def test_support_id_is_short_stable_shape_without_error_text():
    support_id = make_support_id("flow", "secret-token")
    assert support_id.startswith("FLOW-")
    assert len(support_id) == 13
    assert "secret" not in support_id.lower()


def test_first_private_welcome_is_opt_in_state_not_add_friend_push(tmp_path: Path):
    store = UserExperienceStore(str(tmp_path / "ux.json"))
    event = Event()
    assert store.should_welcome(event) is True
    store.mark_welcomed(event)
    assert store.should_welcome(event) is False
    assert UserExperienceStore(str(tmp_path / "ux.json")).should_welcome(event) is False
    assert store.should_welcome(Event(group="g1")) is False


def test_memory_modes_control_read_and_write(tmp_path: Path):
    repo = JSONMemoryRepository(str(tmp_path / "memory.json"))
    scope = _scope()
    repo.write(MemoryRecord(scope=scope, content="existing"))

    token = set_memory_access_mode("paused")
    try:
        repo.write(MemoryRecord(scope=scope, content="not saved"))
        assert [r.content for r in repo.query(scope)] == ["existing"]
    finally:
        reset_memory_access_mode(token)

    token = set_memory_access_mode("temporary")
    try:
        repo.write(MemoryRecord(scope=scope, content="also not saved"))
        assert repo.query(scope) == ()
    finally:
        reset_memory_access_mode(token)

    assert repo.count(scope) == 1


def test_json_delete_and_delete_many_are_persistent(tmp_path: Path):
    path = tmp_path / "memory.json"
    repo = JSONMemoryRepository(str(path))
    first = MemoryRecord(scope=_scope(), content="one")
    second = MemoryRecord(scope=_scope(), content="two")
    repo.write(first)
    repo.write(second)
    assert repo.delete(first.record_id)
    assert repo.delete_many((second.record_id,)) == 1
    assert JSONMemoryRepository(str(path)).count() == 0


def test_subscription_requires_opt_in_origin_and_respects_quiet_and_limit(tmp_path: Path):
    store = UserExperienceStore(str(tmp_path / "ux.json"))
    event = Event()
    key = store.key_for_event(event)
    assert store.eligible_subscribers("更新", datetime(2026, 8, 13, 12, 0)) == ()
    store.subscribe(event, "更新")
    assert store.eligible(key, "更新", datetime(2026, 8, 13, 12, 0))
    assert not store.eligible(key, "更新", datetime(2026, 8, 13, 23, 0))
    store.record_delivery(key, datetime(2026, 8, 13, 12, 0))
    assert not store.eligible(key, "更新", datetime(2026, 8, 13, 13, 0))
    assert store.eligible(key, "更新", datetime(2026, 8, 14, 12, 0))
    store.unsubscribe(event, "更新")
    assert store.get(key)["origin"] == ""


def test_subscription_uses_hashed_index_and_stores_route_only_after_opt_in(tmp_path: Path):
    path = tmp_path / "ux.json"
    store = UserExperienceStore(str(path))
    event = Event(actor="123456789")
    store.mark_welcomed(event)
    before = path.read_text(encoding="utf-8")
    assert "qq:FriendMessage:123456789" not in before
    store.subscribe(event, "更新")
    data = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert "123456789" not in data["users"]
    assert list(data["users"]) == [store.key_for_event(event)]
    # The AstrBot route is the minimum delivery address and is persisted only
    # after explicit opt-in so an active message can reach this subscriber.
    assert data["users"][store.key_for_event(event)]["origin"] == "qq:FriendMessage:123456789"


@pytest.mark.asyncio
async def test_task_registry_prevents_parallel_work_and_can_cancel():
    registry = ConversationTaskRegistry()
    blocker = asyncio.Event()

    async def work():
        await blocker.wait()

    first = asyncio.create_task(work())
    second = asyncio.create_task(work())
    try:
        assert registry.register("session", first)
        assert not registry.register("session", second)
        registry.mark_phase("session", "tools")
        assert registry.running("session").phase == "tools"
        assert registry.cancel("session")
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
