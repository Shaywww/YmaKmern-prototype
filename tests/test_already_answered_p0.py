# -*- coding: utf-8 -*-
"""P0: ALREADY_ANSWERED 接线 —— Orchestrator 内置 Connector 幂等判重。

覆盖：
- 无注册表 / 缺 platform_message_id / 换消息 / 换平台：不误判
- 同键 TTL 窗口内重复：IGNORED + decision_reason=already_answered（可审计）
- TTL 过期后放行
- 判重异常不阻断主流程
- PreprocessedEnvelope 先解包再判重
- 生产 _ProdOrchestrator 同步接线
"""
import asyncio
import sys
import time

sys.path.insert(0, "/opt/dududa20-prototype")

import pytest

from packages.core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef, Actor,
    PreprocessedEnvelope,
)
from packages.core.idempotency import MessageIdempotencyRegistry
from packages.core.state import RunOutcome, SocialAction
from packages.core.decision import (
    SocialDecisionEngine, SocialDecision, DecisionReason,
)
from packages.core.delivery import DeliveryManager, NoOpOutputAdapter
from packages.core.capability import CapabilityRegistry
from packages.core.memory import InMemoryRepository
from packages.core.context import ContextBuilder
from packages.core.renderer import Persona
from packages.runtime.orchestrator import RuntimeOrchestrator


def _envelope(text="hi", mid="m1", platform=Platform.QQ, conversation="g1"):
    return MessageEnvelope(
        platform=platform, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conversation, platform=platform,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=platform, display_name="u"),
        text=text, mentions=(), platform_message_id=mid,
    )


class _Engine(SocialDecisionEngine):
    def decide(self, perception=None, context=None, now=None):
        return SocialDecision(
            action=SocialAction.ANSWER,
            reason_codes=(DecisionReason.HIGH_RELEVANCE,),
            confidence=1.0,
        )


def _orch(registry=None):
    return RuntimeOrchestrator(
        decision_engine=_Engine(),
        capability_registry=CapabilityRegistry(),
        memory_repo=InMemoryRepository(),
        delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        idempotency_registry=registry,
    )


def _run(orch, env, run_id):
    return asyncio.run(orch.run(env, run_id=run_id, trace_id=f"t-{run_id}"))


class TestAlreadyAnsweredWiring:
    def test_first_message_processes(self):
        reg = MessageIdempotencyRegistry()
        r1 = _run(_orch(reg), _envelope(mid="m1"), "aa-1")
        assert r1.outcome == RunOutcome.SUCCEEDED
        assert r1.has_visible_output

    def test_duplicate_message_ignored_with_reason(self):
        reg = MessageIdempotencyRegistry()
        orch = _orch(reg)
        env = _envelope(mid="m1")
        r1 = _run(orch, env, "aa-2a")
        assert r1.outcome == RunOutcome.SUCCEEDED
        r2 = _run(orch, env, "aa-2b")
        assert r2.outcome == RunOutcome.IGNORED
        assert r2.reason_codes == (DecisionReason.ALREADY_ANSWERED.value,)
        assert not r2.has_visible_output
        assert r2.final_response is None

    def test_no_registry_no_dedup(self):
        orch = _orch()
        env = _envelope(mid="m1")
        r1 = _run(orch, env, "aa-3a")
        r2 = _run(orch, env, "aa-3b")
        assert r1.outcome == RunOutcome.SUCCEEDED
        assert r2.outcome == RunOutcome.SUCCEEDED

    def test_different_message_id_not_deduped(self):
        reg = MessageIdempotencyRegistry()
        orch = _orch(reg)
        assert _run(orch, _envelope(mid="m1"), "aa-4a").outcome == RunOutcome.SUCCEEDED
        assert _run(orch, _envelope(mid="m2"), "aa-4b").outcome == RunOutcome.SUCCEEDED

    def test_different_platform_not_deduped(self):
        reg = MessageIdempotencyRegistry()
        orch = _orch(reg)
        r1 = _run(orch, _envelope(mid="m1", platform=Platform.QQ), "aa-5a")
        r2 = _run(orch, _envelope(mid="m1", platform=Platform.WECHAT_WORK), "aa-5b")
        assert r1.outcome == RunOutcome.SUCCEEDED
        assert r2.outcome == RunOutcome.SUCCEEDED

    def test_missing_message_id_not_deduped(self):
        reg = MessageIdempotencyRegistry()
        orch = _orch(reg)
        env = _envelope(mid=None)
        assert _run(orch, env, "aa-6a").outcome == RunOutcome.SUCCEEDED
        assert _run(orch, env, "aa-6b").outcome == RunOutcome.SUCCEEDED

    def test_ttl_expiry_allows_reprocess(self):
        reg = MessageIdempotencyRegistry(ttl_seconds=0.05)
        orch = _orch(reg)
        env = _envelope(mid="m1")
        assert _run(orch, env, "aa-7a").outcome == RunOutcome.SUCCEEDED
        assert _run(orch, env, "aa-7b").outcome == RunOutcome.IGNORED
        time.sleep(0.06)
        assert _run(orch, env, "aa-7c").outcome == RunOutcome.SUCCEEDED

    def test_broken_registry_does_not_block(self):
        class _Broken:
            def check_and_register(self, *a, **k):
                raise RuntimeError("registry down")

        orch = _orch(_Broken())
        r = _run(orch, _envelope(mid="m1"), "aa-8")
        assert r.outcome == RunOutcome.SUCCEEDED

    def test_preprocessed_envelope_unwrapped_before_dedup(self):
        reg = MessageIdempotencyRegistry()
        orch = _orch(reg)
        pre = PreprocessedEnvelope(envelope=_envelope(mid="m1"))
        assert _run(orch, pre, "aa-9a").outcome == RunOutcome.SUCCEEDED
        assert _run(orch, pre, "aa-9b").outcome == RunOutcome.IGNORED


class TestProdOrchestratorWiring:
    """生产 _ProdOrchestrator 同源判重（event=None 走确定性草稿合成）。"""

    @staticmethod
    def _make_prod(registry=None):
        sys.path.insert(0, "/root/data/plugins/dududa20")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dududa_main_aa", "/root/data/plugins/dududa20/main.py")
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)

        class _FakePlugin:
            context_builder = ContextBuilder()

        orch = main._ProdOrchestrator(
            plugin=_FakePlugin(),
            decision_engine=main._ProdDecisionEngine(),
            capability_registry=CapabilityRegistry(),
            memory_repo=InMemoryRepository(),
            renderer=main.OCRenderer(persona=Persona(
                persona_id="t", version="1.0", name="嘟嘟哒")),
            planner_integration=None,
            idempotency_registry=registry,
        )
        return orch

    def test_prod_first_then_duplicate_ignored(self):
        orch = self._make_prod(MessageIdempotencyRegistry())
        env = _envelope(mid="m1")
        r1 = asyncio.run(orch.run(env, run_id="aa-p1", trace_id="t-aa-p1"))
        assert r1.outcome == RunOutcome.SUCCEEDED
        r2 = asyncio.run(orch.run(env, run_id="aa-p2", trace_id="t-aa-p2"))
        assert r2.outcome == RunOutcome.IGNORED
        assert r2.reason_codes == (DecisionReason.ALREADY_ANSWERED.value,)

    def test_prod_without_registry_no_dedup(self):
        orch = self._make_prod(None)
        env = _envelope(mid="m1")
        r1 = asyncio.run(orch.run(env, run_id="aa-p3", trace_id="t-aa-p3"))
        r2 = asyncio.run(orch.run(env, run_id="aa-p4", trace_id="t-aa-p4"))
        assert r1.outcome == RunOutcome.SUCCEEDED
        assert r2.outcome == RunOutcome.SUCCEEDED

