# -*- coding: utf-8 -*-
"""QQ split-at tracker isolation, consumption and resource bounds."""

from types import SimpleNamespace

import pytest

from dududa.application import dududa_handlers as h


def _event(*, platform="qq", group="g1", sender="u1", bot="b1"):
    sender_obj = SimpleNamespace(user_id=sender)
    message_obj = SimpleNamespace(
        group_id=group,
        self_id=bot,
        sender=sender_obj,
    )
    return SimpleNamespace(
        platform=platform,
        group_id=group,
        self_id=bot,
        sender=sender_obj,
        message_obj=message_obj,
        get_platform_name=lambda: platform,
        get_group_id=lambda: group,
        get_sender_id=lambda: sender,
        get_self_id=lambda: bot,
    )


@pytest.fixture(autouse=True)
def _empty_tracker():
    with h._AT_ONLY_LOCK:
        h._AT_ONLY_TS.clear()
        h._RECENT_GROUP_TEXT.clear()
    yield
    with h._AT_ONLY_LOCK:
        h._AT_ONLY_TS.clear()
        h._RECENT_GROUP_TEXT.clear()


def test_recent_window_is_consumed_once():
    event = _event()

    h._mark_at_only_ts(event)

    assert h._recent_at_only(event) is True
    assert h._recent_at_only(event) is False


def test_window_is_isolated_by_sender():
    sender_a = _event(sender="u1")
    sender_b = _event(sender="u2")

    h._mark_at_only_ts(sender_a)

    assert h._recent_at_only(sender_b) is False
    assert h._recent_at_only(sender_a) is True


@pytest.mark.parametrize("changed", [
    {"platform": "other"},
    {"group": "g2"},
    {"bot": "b2"},
])
def test_window_is_isolated_by_platform_group_and_bot(changed):
    original = _event()
    h._mark_at_only_ts(original)

    assert h._recent_at_only(_event(**changed)) is False
    assert h._recent_at_only(original) is True


@pytest.mark.parametrize("missing", ["platform", "group", "sender", "bot"])
def test_incomplete_identity_is_not_tracked(missing):
    values = {"platform": "qq", "group": "g1", "sender": "u1", "bot": "b1"}
    values[missing] = ""
    event = _event(**values)

    h._mark_at_only_ts(event)

    assert h._recent_at_only(event) is False
    assert h._AT_ONLY_TS == {}


def test_message_object_fallback_builds_same_scoped_key():
    event = SimpleNamespace(
        platform="qq",
        message_obj=SimpleNamespace(
            group_id="g1",
            self_id="b1",
            sender=SimpleNamespace(user_id="u1"),
        ),
    )

    assert h._at_only_key(event) == ("qq", "g1", "u1", "b1")
    h._mark_at_only_ts(event)
    assert h._recent_at_only(event) is True


def test_window_expires_and_is_pruned(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    event = _event()
    h._mark_at_only_ts(event)

    clock[0] += h._AT_ONLY_WINDOW_SECONDS

    assert h._recent_at_only(event) is False
    assert h._AT_ONLY_TS == {}


def test_mark_prunes_other_expired_windows(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    old = _event(sender="old")
    fresh = _event(sender="fresh")
    h._mark_at_only_ts(old)

    clock[0] += h._AT_ONLY_WINDOW_SECONDS + 0.1
    h._mark_at_only_ts(fresh)

    assert h._at_only_key(old) not in h._AT_ONLY_TS
    assert list(h._AT_ONLY_TS) == [h._at_only_key(fresh)]


def test_capacity_evicts_oldest_window(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(h, "_AT_ONLY_MAX_ENTRIES", 3)
    events = [_event(sender=f"u{i}") for i in range(4)]

    for event in events:
        h._mark_at_only_ts(event)
        clock[0] += 1.0

    assert len(h._AT_ONLY_TS) == 3
    assert h._recent_at_only(events[0]) is False
    assert h._recent_at_only(events[1]) is True


def test_remarking_same_scope_refreshes_without_growing(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    event = _event()
    h._mark_at_only_ts(event)

    clock[0] += 4.0
    h._mark_at_only_ts(event)
    clock[0] += 2.0

    assert len(h._AT_ONLY_TS) == 1
    assert h._recent_at_only(event) is True


def test_text_before_at_window_is_consumed_once():
    event = _event()
    event.message_str = "查询一下西藏拉萨的天气"

    h._mark_recent_group_text(event)

    assert h._take_recent_group_text(event) == "查询一下西藏拉萨的天气"
    assert h._take_recent_group_text(event) == ""


def test_text_before_at_is_isolated_by_sender():
    first = _event(sender="u1")
    first.message_str = "帮我看看"
    h._mark_recent_group_text(first)

    assert h._take_recent_group_text(_event(sender="u2")) == ""
    assert h._take_recent_group_text(first) == "帮我看看"
