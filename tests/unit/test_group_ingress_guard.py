# -*- coding: utf-8 -*-
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from dududa.core.group_ingress_guard import (
    GroupIngressGuard,
    IngressReason,
)


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def _evaluate(
    guard: GroupIngressGuard,
    *,
    group: str = "group-1",
    sender: str = "human-1",
    text: str = "hello",
    explicit: bool = False,
    media: bool = False,
):
    return guard.evaluate(
        group_id=group,
        sender_id=sender,
        text=text,
        explicit_at_bot=explicit,
        has_media=media,
    )


def test_configured_sender_is_always_dropped_but_unscoped_is_allowed():
    guard = GroupIngressGuard(ignored_sender_ids={" bot-1 ", ""})

    blocked = _evaluate(guard, sender="bot-1", explicit=True)
    assert blocked.allowed is False
    assert blocked.reason == IngressReason.CONFIGURED_SENDER

    private = _evaluate(guard, group="", sender="bot-1", explicit=True)
    assert private.allowed is True
    assert private.reason == IngressReason.UNSCOPED


def test_from_env_parses_static_ids_and_dynamic_thresholds():
    guard = GroupIngressGuard.from_env({
        "DUDUDA_GROUP_IGNORED_SENDER_IDS": " bot-1, bot-2 ,,",
        "DUDUDA_LOOP_REPEAT_THRESHOLD": "4",
        "DUDUDA_LOOP_BURST_THRESHOLD": "9",
        "DUDUDA_LOOP_MAX_KEYS": "32",
    })

    assert _evaluate(guard, sender="bot-2", explicit=True).reason == (
        IngressReason.CONFIGURED_SENDER)
    for _ in range(3):
        assert _evaluate(guard, sender="human", text="same").allowed is True
    assert _evaluate(guard, sender="human", text="same").reason == (
        IngressReason.REPEAT)


def test_repeat_normalization_quarantines_then_expires_by_ttl():
    clock = FakeClock()
    guard = GroupIngressGuard(
        clock=clock,
        repeat_window_seconds=2,
        repeat_threshold=3,
        burst_threshold=20,
        group_repeat_threshold=20,
        sender_quarantine_ttl_seconds=5,
    )

    variants = (
        "[At:123] LLM ERROR https://one.example/a/123456",
        "  llm   error   https://two.example/b/999999  ",
        "LLM ERROR https://three.example/c/888888",
    )
    assert _evaluate(guard, text=variants[0]).allowed is True
    assert _evaluate(guard, text=variants[1]).allowed is True
    tripped = _evaluate(guard, text=variants[2])
    assert tripped.allowed is False
    assert tripped.reason == IngressReason.REPEAT
    assert tripped.retry_after_seconds == pytest.approx(5.0)

    quarantined = _evaluate(guard, text="different text")
    assert quarantined.reason == IngressReason.SENDER_QUARANTINE

    clock.advance(5.1)
    recovered = _evaluate(guard, text="different text")
    assert recovered.allowed is True
    assert recovered.reason == IngressReason.ALLOW


def test_burst_quarantine_does_not_block_or_extend_an_explicit_at():
    clock = FakeClock()
    guard = GroupIngressGuard(
        clock=clock,
        repeat_threshold=20,
        burst_threshold=3,
        group_repeat_threshold=20,
        sender_quarantine_ttl_seconds=5,
    )

    assert _evaluate(guard, text="one").allowed is True
    assert _evaluate(guard, text="two").allowed is True
    tripped = _evaluate(guard, text="three")
    assert tripped.reason == IngressReason.BURST

    clock.advance(4.0)
    bypass = _evaluate(guard, text="please answer", explicit=True)
    assert bypass.allowed is True
    assert bypass.reason == IngressReason.EXPLICIT_AT

    still_blocked = _evaluate(guard, text="ambient")
    assert still_blocked.reason == IngressReason.SENDER_QUARANTINE
    assert still_blocked.retry_after_seconds == pytest.approx(1.0)

    clock.advance(1.1)
    assert _evaluate(guard, text="ambient after ttl").allowed is True


def test_cross_sender_repeat_opens_group_circuit_but_explicit_at_bypasses():
    clock = FakeClock()
    guard = GroupIngressGuard(
        clock=clock,
        repeat_threshold=10,
        burst_threshold=10,
        group_repeat_threshold=4,
        group_repeat_min_senders=2,
        group_circuit_ttl_seconds=5,
    )

    for sender in ("bot-a", "bot-b", "bot-a"):
        assert _evaluate(guard, sender=sender, text="same echo").allowed is True
    tripped = _evaluate(guard, sender="bot-b", text="same echo")
    assert tripped.allowed is False
    assert tripped.reason == IngressReason.GROUP_REPEAT

    ambient = _evaluate(guard, sender="human", text="ambient question")
    assert ambient.reason == IngressReason.GROUP_CIRCUIT
    direct = _evaluate(
        guard, sender="human", text="direct question", explicit=True)
    assert direct.allowed is True
    assert direct.reason == IngressReason.EXPLICIT_AT

    # Circuit scope is the group, not the global sender ID.
    other_group = _evaluate(
        guard, group="group-2", sender="human", text="ambient question")
    assert other_group.allowed is True

    clock.advance(5.1)
    recovered = _evaluate(guard, sender="human", text="after ttl")
    assert recovered.allowed is True


def test_empty_platform_artifact_is_not_counted_but_media_is_burst_traffic():
    guard = GroupIngressGuard(
        repeat_threshold=10,
        burst_threshold=2,
        group_repeat_threshold=10,
    )

    for _ in range(5):
        empty = _evaluate(guard, text="", media=False)
        assert empty.allowed is True
        assert empty.reason == IngressReason.EMPTY

    assert _evaluate(guard, text="", media=True).allowed is True
    media_burst = _evaluate(guard, text="", media=True)
    assert media_burst.allowed is False
    assert media_burst.reason == IngressReason.BURST


def test_every_state_table_is_bounded_and_expired_windows_are_swept():
    clock = FakeClock()
    guard = GroupIngressGuard(
        clock=clock,
        repeat_window_seconds=2,
        repeat_threshold=10,
        burst_window_seconds=2,
        burst_threshold=10,
        group_repeat_window_seconds=2,
        group_repeat_threshold=10,
        max_keys=3,
    )

    for index in range(20):
        assert _evaluate(
            guard,
            group=f"group-{index}",
            sender=f"sender-{index}",
            text=f"unique-{index}",
        ).allowed is True

    stats = guard.stats()
    assert stats.sender_windows <= 3
    assert stats.repeat_windows <= 3
    assert stats.group_repeat_windows <= 3

    clock.advance(2.1)
    expired = guard.stats()
    assert expired.sender_windows == 0
    assert expired.repeat_windows == 0
    assert expired.group_repeat_windows == 0


def test_quarantine_and_group_circuit_tables_are_bounded():
    guard = GroupIngressGuard(
        repeat_threshold=20,
        burst_threshold=2,
        group_repeat_threshold=2,
        group_repeat_min_senders=2,
        max_keys=3,
    )

    for index in range(10):
        group = f"burst-group-{index}"
        sender = f"burst-sender-{index}"
        _evaluate(guard, group=group, sender=sender, text="first")
        _evaluate(guard, group=group, sender=sender, text="second")

    for index in range(10):
        group = f"echo-group-{index}"
        _evaluate(guard, group=group, sender="a", text="echo")
        _evaluate(guard, group=group, sender="b", text="echo")

    stats = guard.stats()
    assert stats.sender_quarantines <= 3
    assert stats.group_circuits <= 3


def test_evaluate_is_atomic_under_concurrent_burst():
    guard = GroupIngressGuard(
        repeat_threshold=100,
        burst_threshold=6,
        group_repeat_threshold=100,
    )

    def evaluate(index: int):
        return _evaluate(guard, text=f"message-{index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        decisions = list(pool.map(evaluate, range(32)))

    assert sum(decision.allowed for decision in decisions) == 5
    stats = guard.stats()
    assert stats.evaluated == 32
    assert stats.allowed == 5
    assert stats.dropped == 27
    assert stats.sender_quarantines == 1


def test_stats_expose_only_aggregate_counts_without_raw_values():
    raw_sender = "raw-sender-778899"
    raw_message = "private diagnostic text that must not appear"
    guard = GroupIngressGuard(ignored_sender_ids={"known-bot-123"})
    _evaluate(guard, sender=raw_sender, text=raw_message)

    stats = guard.stats()
    values = asdict(stats)
    assert values
    assert all(isinstance(value, int) for value in values.values())
    rendered = repr(stats)
    assert raw_sender not in rendered
    assert raw_message not in rendered
    assert "known-bot-123" not in rendered


@pytest.mark.parametrize(
    "kwargs",
    (
        {"repeat_window_seconds": 0},
        {"repeat_threshold": 1},
        {"burst_window_seconds": float("inf")},
        {"burst_threshold": 1},
        {"group_repeat_min_senders": 5, "group_repeat_threshold": 4},
        {"sender_quarantine_ttl_seconds": -1},
        {"group_circuit_ttl_seconds": float("nan")},
        {"max_keys": 0},
    ),
)
def test_invalid_configuration_fails_fast(kwargs):
    with pytest.raises((TypeError, ValueError)):
        GroupIngressGuard(**kwargs)
