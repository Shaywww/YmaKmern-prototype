# -*- coding: utf-8 -*-
"""Connector 契约 P0：幂等键 (platform, bot_id, message_id) + 跨会话回复拒绝。

覆盖：幂等键组成、TTL/有界淘汰、插件判重接线、Reply 组件提取、
Orchestrator 跨会话拒绝（同会话放行）、Handler 守卫。
"""
import asyncio
import sys
import time


import pytest

from dududa.core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef, Actor,
)
from dududa.core.idempotency import (
    MessageIdempotencyRegistry, make_idempotency_key,
)
from dududa.core.state import RunOutcome, SocialAction
from dududa.core.decision import (
    SocialDecisionEngine, SocialDecision, DecisionReason,
)
from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
from dududa.core.capability import CapabilityRegistry
from dududa.core.memory import InMemoryRepository
from dududa.runtime.orchestrator import RuntimeOrchestrator
from dududa.adapters.astrbot.types import (
    AstrMessageEvent, AstrSender, EventMessageType, AstrBotPlatform, Reply,
)
from dududa.adapters.astrbot.input_adapter import AstrBotInputAdapter
from dududa.application.dududa_handlers import (
    _dedupe_message, _cross_session_reply_dropped,
)


def _envelope(text="hi", conversation="g1", reply_to=None):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conversation, platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="u"),
        text=text, mentions=(), reply_to=reply_to,
    )


def _event(group="g1", components=()):
    e = AstrMessageEvent(
        message_str="hi", message_id="m1", session_id="s1",
        sender=AstrSender(user_id="u1", nickname="u"),
        group_id=group, _message_type=EventMessageType.GROUP_MESSAGE,
        _platform=AstrBotPlatform.AIOCQHTTP)
    e._components = list(components)
    return e


class _Engine(SocialDecisionEngine):
    def decide(self, perception=None, context=None, now=None):
        return SocialDecision(
            action=SocialAction.ANSWER,
            reason_codes=(DecisionReason.HIGH_RELEVANCE,),
            confidence=1.0,
        )


def _orch():
    return RuntimeOrchestrator(
        decision_engine=_Engine(),
        capability_registry=CapabilityRegistry(),
        memory_repo=InMemoryRepository(),
        delivery_manager=DeliveryManager(NoOpOutputAdapter()),
    )


class TestIdempotencyKey:
    def test_composition(self):
        assert make_idempotency_key("aiocqhttp", "bot1", "m1") == "aiocqhttp|bot1|m1"
        assert make_idempotency_key("", "", "") == "||"

    def test_platform_and_bot_are_part_of_key(self):
        reg = MessageIdempotencyRegistry()
        assert reg.check_and_register("qq", "b1", "m1") is True
        assert reg.check_and_register("qq", "b1", "m1") is False   # 同键重复
        assert reg.check_and_register("qq", "b2", "m1") is True    # 换 bot 不判重
        assert reg.check_and_register("qq", "b1", "m2") is True    # 换消息不判重
        assert reg.check_and_register("wx", "b1", "m1") is True    # 换平台不判重

    def test_empty_message_id_not_deduped(self):
        reg = MessageIdempotencyRegistry()
        assert reg.check_and_register("qq", "b1", "") is True
        assert reg.check_and_register("qq", "b1", "") is True

    def test_ttl_expiry(self):
        reg = MessageIdempotencyRegistry(ttl_seconds=0.05, max_keys=100)
        assert reg.check_and_register("qq", "b1", "m1") is True
        assert reg.check_and_register("qq", "b1", "m1") is False
        time.sleep(0.06)
        assert reg.check_and_register("qq", "b1", "m1") is True

    def test_bounded_eviction(self):
        reg = MessageIdempotencyRegistry(ttl_seconds=60.0, max_keys=3)
        for i in range(3):
            assert reg.check_and_register("qq", "b1", f"m{i}") is True
        assert reg.check_and_register("qq", "b1", "m0") is False  # 仍在窗口
        reg.check_and_register("qq", "b1", "m3")                  # 超上限淘汰最旧
        assert reg.check_and_register("qq", "b1", "m0") is True   # 最旧已被淘汰


class TestReplyExtraction:
    def test_cross_group_reply(self):
        env = AstrBotInputAdapter().to_envelope(
            _event(group="g1", components=[Reply(id="r1", group_id="g2")]))
        assert env.reply_to is not None
        assert env.reply_to.platform_message_id == "r1"
        assert env.reply_to.conversation.conversation_id == "g2"

    def test_same_group_reply(self):
        env = AstrBotInputAdapter().to_envelope(
            _event(group="g1", components=[Reply(id="r1")]))
        assert env.reply_to is not None
        assert env.reply_to.conversation.conversation_id == "g1"

    def test_no_reply(self):
        env = AstrBotInputAdapter().to_envelope(_event(group="g1"))
        assert env.reply_to is None


class TestCrossSessionReject:
    def test_cross_session_reply_ignored(self):
        env = _envelope(conversation="g1",
                        reply_to=_envelope(conversation="g2"))
        result = asyncio.run(_orch().run(env, run_id="r-x", trace_id="t-x"))
        assert result.outcome == RunOutcome.IGNORED

    def test_same_session_reply_allowed(self):
        env = _envelope(conversation="g1",
                        reply_to=_envelope(conversation="g1"))
        result = asyncio.run(_orch().run(env, run_id="r-s", trace_id="t-s"))
        assert result.outcome != RunOutcome.IGNORED
        assert result.outcome != RunOutcome.ABORTED


class TestDedupeWiring:
    class _Ev:
        message_id = "m1"

        def get_platform_name(self):
            return "aiocqhttp"

    class _Plugin:
        def __init__(self):
            self._idem = MessageIdempotencyRegistry()

        def _get_bot_id(self, event):
            return "bot1"

    def test_registry_path(self):
        p = self._Plugin()
        ev = self._Ev()
        assert _dedupe_message(p, ev, "m1") is False
        assert _dedupe_message(p, ev, "m1") is True
        assert _dedupe_message(p, ev, "m2") is False

    def test_legacy_fallback(self):
        class _Legacy:
            _processed = set()

        p = _Legacy()
        ev = self._Ev()
        assert _dedupe_message(p, ev, "m1") is False
        assert _dedupe_message(p, ev, "m1") is True
        assert _dedupe_message(p, ev, "m2") is False


class TestHandlerGuard:
    def test_cross_session_dropped(self):
        class _Plugin:
            input_adapter = AstrBotInputAdapter()

        e = _event(group="g1", components=[Reply(id="r1", group_id="g2")])
        assert _cross_session_reply_dropped(_Plugin(), e) is True

    def test_same_session_kept(self):
        class _Plugin:
            input_adapter = AstrBotInputAdapter()

        e = _event(group="g1", components=[Reply(id="r1")])
        assert _cross_session_reply_dropped(_Plugin(), e) is False

    def test_plugin_without_adapter_kept(self):
        class _Plugin:
            pass

        assert _cross_session_reply_dropped(_Plugin(), _event()) is False