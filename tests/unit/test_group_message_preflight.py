# -*- coding: utf-8 -*-
"""Group ingress preflight regression tests.

These tests keep platform-specific event details at the edge and assert the
observable contract of ``run_message_flow``: messages that are not addressed
to Dududa must not allocate user-facing work, while legitimate passive-policy,
media-pairing and explicit-mention paths remain reachable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from dududa.application import dududa_handlers as h
from dududa.application.dududa_core import DududaCore
from dududa.core.group_policy import GroupPolicyStore
from dududa.core.group_ambient import GroupAmbientTracker
from dududa.core.idempotency import MessageIdempotencyRegistry
from dududa.core.perception import PerceptionResult
from dududa.core.state import SocialAction


BOT_ID = "3823883634"
GROUP_ID = "1059231626"


class _Plain:
    type = "ComponentType.Plain"

    def __init__(self, text: str):
        self.text = text


class _At:
    type = "ComponentType.At"
    text = ""

    def __init__(self, qq: str):
        self.qq = str(qq)


class _Image:
    type = "ComponentType.Image"
    text = ""

    def __init__(self, url: str, name: str = "photo.jpg"):
        self.url = url
        self.name = name
        self.file = url


class GroupEvent:
    """Small AstrBot-like group event with explicit sender/bot identity."""

    def __init__(
        self,
        text: str,
        *,
        message_id: str,
        sender_id: str = "10001",
        bot_id: str = BOT_ID,
        group_id: str = GROUP_ID,
        at: bool = False,
        components=None,
        raw_message=None,
    ):
        self.message_str = text
        self.message_id = message_id
        self.session_id = group_id
        self.group_id = group_id
        self.sender = SimpleNamespace(user_id=str(sender_id), nickname="tester")
        self.message_obj = SimpleNamespace(
            group=group_id,
            group_id=group_id,
            message_id=message_id,
            message_str=text,
            self_id=str(bot_id),
            sender=SimpleNamespace(user_id=str(sender_id), role="member"),
            raw_message=raw_message,
        )
        self.is_at_or_wake_command = at
        self._components = list(components) if components is not None else [
            _Plain(text)
        ]
        self.call_llm_markers = []
        self.sent = []
        self.stopped = False

    def get_platform_name(self):
        return "aiocqhttp"

    def get_message_type(self):
        return "group_message"

    def get_messages(self):
        return self._components

    def get_self_id(self):
        return self.message_obj.self_id

    def get_session_id(self):
        return self.session_id

    def get_sender_id(self):
        return self.sender.user_id

    def get_message_outline(self):
        return self.message_str

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value):
        self.call_llm_markers.append(value)


class _UXStoreSpy:
    def __init__(self):
        self.session_key_calls = 0
        self.memory_mode_calls = 0

    def session_key(self, event):
        self.session_key_calls += 1
        return f"{event.get_session_id()}:{event.get_sender_id()}"

    def memory_mode(self, event):
        self.memory_mode_calls += 1
        return "active"


class _TaskRegistrySpy:
    def __init__(self):
        self.register_calls = []
        self.finish_calls = []
        self.phases = []

    def register(self, key, task):
        self.register_calls.append((key, task))
        return True

    def running(self, key):
        return None

    def mark_phase(self, key, phase):
        self.phases.append((key, phase))

    def finish(self, key, task):
        self.finish_calls.append((key, task))


class _TraceSpy:
    def __init__(self):
        self.events = []

    def record(self, **fields):
        self.events.append(fields)


@dataclass(frozen=True)
class _IngressDecision:
    allowed: bool
    reason: str = "allowed"


class _ConfiguredSenderGuard:
    """Guard stub matching the production plugin's narrow interface."""

    def __init__(self, ignored_ids=()):
        self.ignored_ids = {str(value) for value in ignored_ids}
        self.calls = []

    def evaluate(self, **signal):
        self.calls.append(signal)
        sender = str(signal["sender_id"])
        if sender in self.ignored_ids:
            return _IngressDecision(False, "configured_sender")
        return _IngressDecision(True)


class _CircuitGuard:
    """A dynamic circuit that explicitly allows a real directed mention."""

    def __init__(self):
        self.calls = []

    def evaluate(self, **signal):
        self.calls.append(signal)
        if signal["explicit_at_bot"]:
            return _IngressDecision(True, "explicit_mention")
        return _IngressDecision(False, "group_circuit")


class FlowPlugin:
    """Minimal production-flow plugin with observable UX side effects."""

    def __init__(self, tmp_path, *, guard=None, with_guard=False):
        self.enabled = True
        self._last_file_ts = 0.0
        self._pending_deliveries = {}
        self._recent_media = {}
        self._idem = MessageIdempotencyRegistry()
        self.ux_store = _UXStoreSpy()
        self.ux_tasks = _TaskRegistrySpy()
        self.progress_delay = 60.0
        self.progress_messages = []
        self.group_policy = GroupPolicyStore(str(tmp_path / "group-policy.json"))
        self.group_ambient = GroupAmbientTracker()
        # Absence of the attribute is intentional in compatibility tests.
        if with_guard:
            self.group_ingress_guard = guard

    async def _send_progress(self, event, text):
        self.progress_messages.append(text)

    def _is_self_message(self, event):
        return str(event.get_sender_id()) == str(event.get_self_id())

    def _get_bot_id(self, event):
        return str(event.get_self_id())

    def _should_ignore(self, event):
        # Exercise the real production recipient gate, including future policy
        # fixes, rather than baking the expected result into the fake.
        return DududaCore._should_ignore(self, event)

    def _group_policy_for(self, event):
        return self.group_policy.get(str(event.group_id))

    def _group_policy_view(self, event):
        return self.group_policy.to_policy_view(str(event.group_id))

    def _social_decision(self, event):
        policy = self._group_policy_for(event)
        if not event.is_at_or_wake_command and not (
            policy is not None
            and policy.mode == "normal"
            and policy.reply_rate >= 1.0
        ):
            return SocialAction.IGNORE, "low_relevance"
        return SocialAction.DIRECT_REPLY, "direct_mention"


@pytest.fixture(autouse=True)
def _reset_split_at_tracker():
    h._AT_ONLY_TS.clear()
    h._RECENT_GROUP_TEXT.clear()
    yield
    h._AT_ONLY_TS.clear()
    h._RECENT_GROUP_TEXT.clear()


def _install_side_effect_spies(monkeypatch):
    trace = _TraceSpy()
    monkeypatch.setattr(h, "trace_recorder", trace)

    created_tasks = []
    real_create_task = asyncio.create_task

    def tracked_create_task(coro, *args, **kwargs):
        created_tasks.append(coro)
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(h.asyncio, "create_task", tracked_create_task)

    prune_calls = []

    async def tracked_prune(plugin):
        prune_calls.append(plugin)

    monkeypatch.setattr(h, "_prune_stale_deliveries", tracked_prune)
    return trace, created_tasks, prune_calls


def _assert_zero_flow_side_effects(plugin, trace, created_tasks, prune_calls):
    assert plugin.ux_store.session_key_calls == 0
    assert plugin.ux_store.memory_mode_calls == 0
    assert plugin.ux_tasks.register_calls == []
    assert plugin.ux_tasks.finish_calls == []
    assert plugin.progress_messages == []
    assert created_tasks == []
    assert prune_calls == []
    assert trace.events == []


@pytest.mark.asyncio
async def test_unmentioned_plain_group_message_is_dropped_before_ux_progress_and_trace(
    tmp_path, monkeypatch
):
    """Ambient group chatter must be a zero-work, zero-trace decision."""
    plugin = FlowPlugin(tmp_path)
    event = GroupEvent("大家先讨论一下", message_id="ambient-1", at=False)
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)
    inner_calls = []

    async def forbidden_inner(*args):
        inner_calls.append(args)
        return "不应该处理"

    monkeypatch.setattr(h, "_run_flow_inner", forbidden_inner)

    assert await h.run_message_flow(plugin, event) is None
    assert inner_calls == []
    _assert_zero_flow_side_effects(plugin, trace, created_tasks, prune_calls)
    assert event.call_llm_markers == [True]


@pytest.mark.asyncio
async def test_configured_bot_sender_is_silent_even_when_it_mentions_dududa(
    tmp_path, monkeypatch
):
    """A statically ignored bot ID has priority over its explicit At segment."""
    sender_id = "3296147894"
    guard = _ConfiguredSenderGuard({sender_id})
    plugin = FlowPlugin(tmp_path, guard=guard, with_guard=True)
    event = GroupEvent(
        f"[At:{BOT_ID}] 继续回复我",
        message_id="bot-at-1",
        sender_id=sender_id,
        at=True,
        components=[_At(BOT_ID), _Plain("继续回复我")],
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)
    inner_calls = []

    async def forbidden_inner(*args):
        inner_calls.append(args)
        return "不应该处理"

    monkeypatch.setattr(h, "_run_flow_inner", forbidden_inner)

    assert await h.run_message_flow(plugin, event) is None
    assert inner_calls == []
    assert len(guard.calls) == 1
    assert guard.calls[0]["sender_id"] == sender_id
    assert guard.calls[0]["explicit_at_bot"] is True
    _assert_zero_flow_side_effects(plugin, trace, created_tasks, prune_calls)


@pytest.mark.asyncio
async def test_dynamic_group_circuit_allows_human_explicit_at(
    tmp_path, monkeypatch
):
    """A human's exact At to this bot remains usable during a loop circuit."""
    guard = _CircuitGuard()
    plugin = FlowPlugin(tmp_path, guard=guard, with_guard=True)
    event = GroupEvent(
        f"[At:{BOT_ID}] 帮我解释一下",
        message_id="human-at-1",
        sender_id="10086",
        at=True,
        components=[_At(BOT_ID), _Plain("帮我解释一下")],
    )

    async def handled_inner(*args):
        return "可以正常回答～"

    monkeypatch.setattr(h, "_run_flow_inner", handled_inner)
    monkeypatch.setattr(h, "_prune_stale_deliveries", lambda plugin: asyncio.sleep(0))

    assert await h.run_message_flow(plugin, event) == "可以正常回答～"
    assert len(guard.calls) == 1
    signal = guard.calls[0]
    assert signal["group_id"] == GROUP_ID
    assert signal["sender_id"] == "10086"
    assert signal["explicit_at_bot"] is True
    assert signal["has_media"] is False


def test_directed_casual_sticker_routes_through_summary_then_deepseek(tmp_path):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    raw = {"message": [{"type": "image", "data": {
        "url": "https://example.test/wave.webp", "file": "wave.webp",
        "emoji_id": "wave", "summary": "[动画表情]",
    }}]}
    event = GroupEvent(
        f"[At:{BOT_ID}]", message_id="direct-sticker", sender_id="10087",
        at=True, components=[_At(BOT_ID)], raw_message=raw)

    assert h._preflight_group_message(
        plugin, event, event.get_messages()) is True
    assert h._semantic_media_candidate(event) == "directed_media"
    assert "尚未识别" in h._group_context_text(plugin, event)


@pytest.mark.asyncio
async def test_directed_media_reply_uses_summary_without_proactive_quota(
    tmp_path,
):
    plugin = FlowPlugin(tmp_path)
    plugin.personas = SimpleNamespace(active=SimpleNamespace(
        display_name="嘟嘟哒", first_person="嘟嘟哒"))
    plugin.group_context = h.GroupConversationTracker()
    plugin.group_context.add(
        group_id=GROUP_ID, sender_id="10088",
        content="表情包摘要：小狐狸挥手；表达打招呼",
        message_type="sticker", message_id="direct-summary")
    event = GroupEvent(
        "", message_id="direct-summary", sender_id="10088")
    calls = []

    async def compose(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return "哟，挥手收到啦～(≧▽≦)"

    plugin._call_llm = compose
    reply = await h._direct_group_media_reply(plugin, event)
    assert reply == "哟，挥手收到啦～(≧▽≦)"
    assert "小狐狸挥手" in calls[0][1]
    assert plugin.group_ambient.status(GROUP_ID)["daily_used"] == 0


@pytest.mark.asyncio
async def test_at_other_bot_is_dropped_even_when_framework_sets_wake(
    tmp_path, monkeypatch
):
    """A broad adapter wake for @someone-else must not wake Dududa."""
    plugin = FlowPlugin(tmp_path)
    event = GroupEvent(
        "[At:3690063766]",
        message_id="other-bot-at-1",
        sender_id="10086",
        at=True,
        components=[_At("3690063766")],
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    assert await h.run_message_flow(plugin, event) is None
    assert event.is_at_or_wake_command is False
    _assert_zero_flow_side_effects(plugin, trace, created_tasks, prune_calls)


def test_nickname_wakes_only_in_ambient_enabled_group(tmp_path):
    plugin = FlowPlugin(tmp_path)
    event = GroupEvent(
        "嘟嘟哒你觉得呢？", message_id="nickname-off", at=False)
    assert h._preflight_group_message(
        plugin, event, event.get_messages()) is False

    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    event = GroupEvent(
        "嘟嘟哒你觉得呢？", message_id="nickname-on", at=False)
    assert h._preflight_group_message(
        plugin, event, event.get_messages()) is True
    assert event.is_at_or_wake_command is True


@pytest.mark.asyncio
async def test_reply_rate_one_keeps_unmentioned_group_participation_reachable(
    tmp_path, monkeypatch
):
    """Moving recipient filtering earlier must not make reply_rate unreachable."""
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, mode="normal", reply_rate=1.0)
    event = GroupEvent("这个方案大家怎么看？", message_id="passive-1", at=False)

    async def fake_perception(plugin_, event_):
        return PerceptionResult()

    async def fake_handle_text(plugin_, event_, **kwargs):
        return "我也来补充一点～"

    monkeypatch.setattr(h, "_perceive_with_model", fake_perception)
    monkeypatch.setattr(h, "handle_text", fake_handle_text)
    monkeypatch.setattr(h, "_take_paired_media", lambda plugin_, event_: ())
    monkeypatch.setattr(h, "_prune_stale_deliveries", lambda plugin_: asyncio.sleep(0))

    assert await h.run_message_flow(plugin, event) == "我也来补充一点～"


def test_ambient_opt_in_promotes_only_busy_group_question(tmp_path):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    for i in range(14):
        event = GroupEvent(
            f"普通讨论 {i}", message_id=f"ambient-fill-{i}",
            sender_id=str(10000 + (i % 3)), at=False)
        assert h._preflight_group_message(
            plugin, event, event.get_messages()) is False

    question = GroupEvent(
        "明天几点集合？", message_id="ambient-question",
        sender_id="10003", at=False)
    assert h._preflight_group_message(
        plugin, question, question.get_messages()) is True
    assert question.is_at_or_wake_command is True
    assert h._is_ambient_wake(question) is True


def test_ambient_default_off_stays_silent(tmp_path):
    plugin = FlowPlugin(tmp_path)
    for i in range(14):
        event = GroupEvent(
            f"普通讨论 {i}", message_id=f"ambient-off-fill-{i}",
            sender_id=str(10000 + (i % 3)), at=False)
        assert h._preflight_group_message(
            plugin, event, event.get_messages()) is False
    question = GroupEvent(
        "明天几点集合？", message_id="ambient-off-question",
        sender_id="10003", at=False)
    assert h._preflight_group_message(
        plugin, question, question.get_messages()) is False
    assert h._is_ambient_wake(question) is False


def test_local_meme_match_only_opens_semantic_review_after_real_context(
    tmp_path,
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    messages = (
        GroupEvent("哈哈这个展开有点怪", message_id="meme-1",
                   sender_id="10051"),
        GroupEvent("确实越来越离谱了", message_id="meme-2",
                   sender_id="10052"),
    )
    for event in messages:
        assert h._preflight_group_message(
            plugin, event, event.get_messages()) is False

    candidate_event = GroupEvent(
        "这也太绝绝紫了", message_id="meme-3", sender_id="10051")
    assert h._preflight_group_message(
        plugin, candidate_event, candidate_event.get_messages()) is True
    candidate = h._semantic_candidate(candidate_event)
    assert candidate["key"] == "绝绝子"
    assert candidate_event.is_at_or_wake_command is True

    rendered = h._group_context_text(plugin, candidate_event)
    assert "成员1" in rendered and "成员2" in rendered
    assert "10051" not in rendered and "10052" not in rendered


@pytest.mark.asyncio
async def test_deepseek_is_final_meme_judge_and_fails_closed(tmp_path):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    for index, (sender, text) in enumerate((
        ("10061", "哈哈这展开太怪了"),
        ("10062", "已经开始离谱了"),
        ("10061", "这也太绝绝紫了"),
    )):
        event = GroupEvent(
            text, message_id=f"review-{index}", sender_id=sender)
        h._record_group_context(
            plugin, event, event.get_messages(), GROUP_ID, sender)

    calls = []

    async def approve(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return (
            '{"scene":"casual_meme","is_meme":true,'
            '"should_reply":true,"confidence":0.91,'
            '"reply":"这展开确实有点绝绝子了～(≧▽≦)"}'
        )

    plugin._call_llm = approve
    event = GroupEvent(
        "这也太绝绝紫了", message_id="review-final", sender_id="10061")
    reply = await h._semantic_meme_reply(
        plugin, event, {"key": "绝绝子", "tier": "basic",
                        "meaning": "强调很绝", "evidence": "拼音近似"})
    assert reply == "这展开确实有点绝绝子了～(≧▽≦)"
    assert calls[0][2]["skip_render"] is True
    assert "成员1" in calls[0][1] and "10061" not in calls[0][1]

    isolated = FlowPlugin(tmp_path / "rejected")
    isolated.group_context = plugin.group_context

    async def reject(system, user, **kwargs):
        return (
            '{"scene":"serious_discussion","is_meme":false,'
            '"should_reply":false,"confidence":0.96,"reply":""}'
        )

    isolated._call_llm = reject
    assert await h._semantic_meme_reply(
        isolated, event, {"key": "绝绝子"}) == ""


def test_second_distinct_sticker_opens_multimodal_review_only(tmp_path):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)

    def sticker_event(message_id, sender_id):
        raw = {
            "message": [{"type": "image", "data": {
                "url": f"https://example.test/{message_id}.webp",
                "file": f"{message_id}.webp", "emoji_id": message_id,
                "summary": "[动画表情]",
            }}]
        }
        return GroupEvent(
            "", message_id=message_id, sender_id=sender_id,
            components=[], raw_message=raw)

    first = sticker_event("sticker-a", "10071")
    assert h._preflight_group_message(
        plugin, first, first.get_messages()) is False

    second = sticker_event("sticker-b", "10072")
    assert h._preflight_group_message(
        plugin, second, second.get_messages()) is True
    assert h._semantic_media_candidate(second) == "sticker_chain"
    assert second.is_at_or_wake_command is True


@pytest.mark.asyncio
async def test_semantic_media_review_rejects_uncertain_scene(tmp_path):
    plugin = FlowPlugin(tmp_path)
    event = GroupEvent("", message_id="media-review", sender_id="10081")
    plugin.group_context = h.GroupConversationTracker()
    plugin.group_context.add(
        group_id=GROUP_ID, sender_id="10081",
        content="表情包摘要：一只猫歪头，表达疑惑",
        message_type="sticker", message_id="media-review")

    async def uncertain(system, user, **kwargs):
        return (
            '{"scene":"unknown","should_reply":false,'
            '"confidence":0.51,"reply":""}'
        )

    plugin._call_llm = uncertain
    assert await h._semantic_media_reply(
        plugin, event, "sticker_chain") == ""


@pytest.mark.asyncio
async def test_quiet_group_is_replaced_by_identity_free_topic_capsule(
    tmp_path, monkeypatch,
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_context = h.GroupConversationTracker(ttl_seconds=300)
    base = h.time.time() - 303
    for index, (sender, text) in enumerate((
        ("10091", "明天实验课是不是换教室了"),
        ("10092", "听说可能换到综合楼"),
        ("10091", "但是群通知还没发"),
    )):
        plugin.group_context.add(
            group_id=GROUP_ID, sender_id=sender, content=text,
            message_id=f"capsule-{index}", now=base + index)

    async def no_wait(_seconds):
        return None

    async def summarise(system, user, **kwargs):
        assert "10091" not in user and "10092" not in user
        assert kwargs["skip_render"] is True
        return (
            '{"topic":"实验课教室变更",'
            '"summary":"明天实验课可能换到综合楼，但还没有正式通知",'
            '"core_points":["可能换到综合楼","群通知尚未发布"],'
            '"unresolved":"等待正式通知确认","tone":"serious",'
            '"confidence":0.94}'
        )

    monkeypatch.setattr(h.asyncio, "sleep", no_wait)
    plugin._call_llm = summarise
    await h._summarize_quiet_group_topic(plugin, GROUP_ID, base + 2)

    assert plugin.group_context.snapshot(GROUP_ID) == ()
    capsules = plugin.group_context.topic_capsules(GROUP_ID)
    assert len(capsules) == 1
    assert capsules[0].topic == "实验课教室变更"
    assert "成员" not in capsules[0].summary


@pytest.mark.asyncio
async def test_warm_topic_only_attaches_after_high_confidence_continuity(
    tmp_path,
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_context = h.GroupConversationTracker()
    capsule = plugin.group_context.set_topic_capsule(
        group_id=GROUP_ID, topic="实验课教室变更",
        summary="实验课可能换到综合楼，但还没有正式通知",
        core_points=("可能换教室",), unresolved="等待通知",
        last_message_at=h.time.time() - 700)
    event = GroupEvent(
        "群通知还是没发吗？", message_id="continue-topic",
        sender_id="10101", at=True)

    async def approve(system, user, **kwargs):
        return (
            '{"continues_topic":true,"confidence":0.91,'
            f'"capsule_id":"{capsule.capsule_id}"}}'
        )

    plugin._call_llm = approve
    assert await h._prepare_topic_continuity(plugin, event) is True
    rendered = h._group_context_text(plugin, event)
    assert "实验课教室变更" in rendered
    assert "等待通知" in rendered

    rejected = FlowPlugin(tmp_path / "new-topic")
    rejected.group_context = h.GroupConversationTracker()
    rejected.group_context.set_topic_capsule(
        group_id=GROUP_ID, topic="实验课教室变更",
        summary="实验课可能换教室", last_message_at=h.time.time() - 700,
        capsule_id=capsule.capsule_id)

    async def uncertain(system, user, **kwargs):
        return (
            '{"continues_topic":false,"confidence":0.55,'
            '"capsule_id":""}'
        )

    rejected._call_llm = uncertain
    ambiguous = GroupEvent(
        "确实", message_id="ambiguous-topic", sender_id="10102", at=True)
    assert await h._prepare_topic_continuity(rejected, ambiguous) is False
    assert rejected.group_context.active_capsule(GROUP_ID) is None


def test_topic_capsule_never_wakes_an_unaddressed_message(tmp_path):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    plugin.group_context = h.GroupConversationTracker()
    plugin.group_context.set_topic_capsule(
        group_id=GROUP_ID, topic="实验课教室变更",
        summary="实验课可能换教室", last_message_at=h.time.time() - 700)
    event = GroupEvent(
        "群通知还是没发", message_id="silent-continuation",
        sender_id="10103", at=False)

    assert h._preflight_group_message(
        plugin, event, event.get_messages()) is False
    assert plugin.group_context.active_capsule(GROUP_ID) is None


@pytest.mark.asyncio
async def test_unmentioned_group_image_is_stashed_without_ux_progress_or_trace(
    tmp_path, monkeypatch
):
    """QQ split-image ingestion is useful, but it is not a conversational flow."""
    plugin = FlowPlugin(tmp_path)
    url = "https://example.test/course-table.jpg"
    event = GroupEvent(
        "",
        message_id="image-1",
        sender_id="10010",
        at=False,
        components=[_Image(url)],
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    assert await h.run_message_flow(plugin, event) is None
    stashed = plugin._recent_media[(GROUP_ID, "10010")]
    assert stashed[1:] == (url, "photo.jpg", True)
    _assert_zero_flow_side_effects(plugin, trace, created_tasks, prune_calls)


@pytest.mark.asyncio
async def test_new_member_notice_gets_opt_in_welcome_without_llm_work(
    tmp_path, monkeypatch
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    event = GroupEvent(
        "", message_id="join-1", sender_id="10020", components=[],
        raw_message={
            "post_type": "notice", "notice_type": "group_increase",
            "group_id": int(GROUP_ID), "user_id": 10020,
            "self_id": int(BOT_ID), "sub_type": "approve",
        },
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    reply = await h.run_message_flow(plugin, event)

    assert reply in h._GROUP_SCENE_REPLIES["new_member"]
    _assert_zero_flow_side_effects(
        plugin, trace, created_tasks, prune_calls)


@pytest.mark.asyncio
async def test_new_member_notice_stays_silent_when_ambient_is_off(
    tmp_path, monkeypatch
):
    plugin = FlowPlugin(tmp_path)
    event = GroupEvent(
        "", message_id="join-off", sender_id="10021", components=[],
        raw_message={
            "post_type": "notice", "notice_type": "group_increase",
            "group_id": int(GROUP_ID), "user_id": 10021,
            "self_id": int(BOT_ID),
        },
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    assert await h.run_message_flow(plugin, event) is None
    _assert_zero_flow_side_effects(
        plugin, trace, created_tasks, prune_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "segment", "reason"),
    [
        ("red-1", {"type": "redbag", "data": {"title": "恭喜发财"}},
         "red_packet"),
        ("poll-1", {"type": "json", "data": {
            "data": '{"app":"com.tencent.troopvote","prompt":"[群投票]去哪吃"}'
        }}, "poll"),
    ],
)
async def test_native_card_scenes_reply_without_starting_llm_work(
    tmp_path, monkeypatch, message_id, segment, reason
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    event = GroupEvent(
        "", message_id=message_id, components=[],
        raw_message={
            "post_type": "message", "message_type": "group",
            "group_id": int(GROUP_ID), "user_id": 10030,
            "self_id": int(BOT_ID), "message": [segment],
        },
    )
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    reply = await h.run_message_flow(plugin, event)

    assert reply in h._GROUP_SCENE_REPLIES[reason]
    _assert_zero_flow_side_effects(
        plugin, trace, created_tasks, prune_calls)


def test_ordinary_json_card_is_not_misclassified_as_poll():
    event = GroupEvent(
        "", message_id="json-share", components=[],
        raw_message={
            "post_type": "message", "message_type": "group",
            "message": [{"type": "json", "data": {
                "data": '{"app":"com.tencent.structmsg","prompt":"分享了一篇文章"}'
            }}],
        },
    )
    assert h._detect_group_scene(event, event.get_messages()) == ""


@pytest.mark.asyncio
async def test_topic_keyword_can_make_a_low_cost_fixed_reply(
    tmp_path, monkeypatch
):
    plugin = FlowPlugin(tmp_path)
    plugin.group_policy.set(GROUP_ID, ambient_enabled=True)
    plugin.group_ambient = GroupAmbientTracker(
        topic_reply_rate=1.0, topic_min_messages=2,
        topic_min_unique_senders=2)
    opening = GroupEvent(
        "刚回宿舍", message_id="topic-opening", sender_id="10040")
    assert h._preflight_group_message(
        plugin, opening, opening.get_messages()) is False

    event = GroupEvent(
        "说得我也想喝奶茶了", message_id="topic-milk-tea",
        sender_id="10041")
    trace, created_tasks, prune_calls = _install_side_effect_spies(monkeypatch)

    reply = await h.run_message_flow(plugin, event)

    assert reply in h._GROUP_SCENE_REPLIES["topic_milk_tea"]
    _assert_zero_flow_side_effects(
        plugin, trace, created_tasks, prune_calls)


def test_split_at_window_matches_same_sender():
    first = GroupEvent(
        f"[At:{BOT_ID}]",
        message_id="split-at-a",
        sender_id="10001",
        at=True,
        components=[_At(BOT_ID)],
    )
    continuation = GroupEvent(
        "帮我看一下",
        message_id="split-text-a",
        sender_id="10001",
        at=False,
    )

    h._mark_at_only_ts(first)

    assert h._recent_at_only(continuation) is True


def test_split_at_window_does_not_cross_senders_in_same_group():
    first = GroupEvent(
        f"[At:{BOT_ID}]",
        message_id="split-at-owner",
        sender_id="10001",
        at=True,
        components=[_At(BOT_ID)],
    )
    other_user = GroupEvent(
        "我只是路过说一句",
        message_id="split-text-other",
        sender_id="10002",
        at=False,
    )

    h._mark_at_only_ts(first)

    assert h._recent_at_only(other_user) is False
