# -*- coding: utf-8 -*-
from dududa.core.group_ambient import GroupAmbientTracker
from datetime import datetime


def _fill(tracker, *, group="g1", start=1000.0, count=14):
    for i in range(count):
        tracker.observe(
            group_id=group, sender_id=f"u{i % 3}", text=f"普通讨论 {i}",
            now=start + i,
        )


def test_default_threshold_requires_busy_group_and_current_question():
    tracker = GroupAmbientTracker()
    _fill(tracker)
    decision = tracker.observe(
        group_id="g1", sender_id="u3", text="明天几点上课？", now=1014.0)
    assert decision.should_reply is True
    assert decision.message_count == 15
    assert decision.unique_senders == 4


def test_statement_does_not_trigger_after_threshold():
    tracker = GroupAmbientTracker()
    _fill(tracker)
    decision = tracker.observe(
        group_id="g1", sender_id="u3", text="明天上午上课", now=1014.0)
    assert decision.should_reply is False
    assert decision.reason == "latest_not_question"


def test_cooldown_and_daily_limit_are_per_group():
    tracker = GroupAmbientTracker(
        min_messages=2, min_unique_senders=2,
        cooldown_seconds=60, daily_limit=2)
    tracker.observe(group_id="g1", sender_id="u1", text="讨论", now=1000)
    first = tracker.observe(
        group_id="g1", sender_id="u2", text="这个怎么处理？", now=1001)
    blocked = tracker.observe(
        group_id="g1", sender_id="u3", text="那个怎么办？", now=1002)
    second = tracker.observe(
        group_id="g1", sender_id="u3", text="那个怎么办？", now=1062)
    limited = tracker.observe(
        group_id="g1", sender_id="u4", text="还有什么办法？", now=1123)
    assert first.should_reply is True
    assert blocked.reason == "cooldown"
    assert second.should_reply is True
    assert limited.reason == "daily_limit"


def test_commands_recalls_and_ambiguous_short_text_are_ignored():
    tracker = GroupAmbientTracker(min_messages=2, min_unique_senders=2)
    assert tracker.observe(
        group_id="g", sender_id="u1", text="/help", now=1).reason == "ignored_message"
    assert tracker.observe(
        group_id="g", sender_id="u1", text="小明撤回了一条消息", now=2).reason == "ignored_message"
    assert tracker.is_clear_question("啥") is False


def test_explicit_emotional_bid_can_trigger_without_busy_group():
    tracker = GroupAmbientTracker(
        min_messages=15, min_unique_senders=3,
        cooldown_seconds=60, daily_limit=2)
    decision = tracker.observe(
        group_id="g", sender_id="u1", text="今天真的好烦，快崩溃了",
        now=1000)
    assert decision.should_reply is True
    assert decision.reason == "emotional_checkin"


def test_generic_negative_word_does_not_trigger_emotional_reply():
    tracker = GroupAmbientTracker()
    decision = tracker.observe(
        group_id="g", sender_id="u1", text="这个方案不好", now=1000)
    assert decision.should_reply is False


def test_window_prunes_old_messages():
    tracker = GroupAmbientTracker(
        window_seconds=30, min_messages=2, min_unique_senders=2)
    tracker.observe(group_id="g", sender_id="u1", text="讨论", now=1)
    decision = tracker.observe(
        group_id="g", sender_id="u2", text="现在怎么办？", now=32)
    assert decision.should_reply is False
    assert decision.message_count == 1


def test_cooldown_and_daily_quota_survive_restart(tmp_path):
    path = str(tmp_path / "ambient-state.json")
    first = GroupAmbientTracker(
        min_messages=2, min_unique_senders=2,
        cooldown_seconds=60, daily_limit=2, state_path=path)
    first.observe(group_id="g", sender_id="u1", text="讨论", now=1000)
    assert first.observe(
        group_id="g", sender_id="u2", text="怎么办？", now=1001).should_reply

    restarted = GroupAmbientTracker(
        min_messages=2, min_unique_senders=2,
        cooldown_seconds=60, daily_limit=2, state_path=path)
    restarted.observe(group_id="g", sender_id="u1", text="讨论", now=1002)
    blocked = restarted.observe(
        group_id="g", sender_id="u2", text="为什么？", now=1003)
    assert blocked.reason == "cooldown"
    assert restarted.status("g", now=1003)["daily_used"] == 1


def test_native_scenes_share_cooldown_and_daily_quota():
    tracker = GroupAmbientTracker(cooldown_seconds=60, daily_limit=2)
    first = tracker.reserve_scene(
        group_id="g", reason="new_member", now=1000)
    blocked = tracker.reserve_scene(
        group_id="g", reason="poll", now=1001)
    second = tracker.reserve_scene(
        group_id="g", reason="red_packet", now=1061)
    limited = tracker.reserve_scene(
        group_id="g", reason="poll", now=1122)
    assert first.should_reply is True
    assert blocked.reason == "cooldown"
    assert second.should_reply is True
    assert limited.reason == "daily_limit"


def test_late_night_checkin_requires_prior_activity_and_long_silence():
    tracker = GroupAmbientTracker(
        cooldown_seconds=60, daily_limit=2,
        late_night_silence_seconds=1800)
    late = datetime(2026, 8, 27, 1, 0).timestamp()
    cold_start = tracker.observe(
        group_id="g", sender_id="u1", text="居然还有人", now=late)
    too_soon = tracker.observe(
        group_id="g", sender_id="u2", text="我也在", now=late + 60)
    after_silence = tracker.observe(
        group_id="g", sender_id="u3", text="突然想起来一件事",
        now=late + 1861)
    assert cold_start.should_reply is False
    assert too_soon.should_reply is False
    assert after_silence.should_reply is True
    assert after_silence.reason == "late_night_checkin"


def test_late_night_sleep_closing_message_stays_silent():
    tracker = GroupAmbientTracker(late_night_silence_seconds=1800)
    late = datetime(2026, 8, 27, 1, 0).timestamp()
    tracker.observe(group_id="g", sender_id="u1", text="我还在", now=late)
    decision = tracker.observe(
        group_id="g", sender_id="u2", text="晚安，我先睡了",
        now=late + 1801)
    assert decision.should_reply is False
