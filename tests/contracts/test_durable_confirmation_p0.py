from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""P0：持久确认 Durable Confirmation（文档 2.4.12 / 2.4.23 / 2.5.9）。

覆盖：工具路径确认的创建/批准/单次消费、绑定校验（Actor/Scope/action/
payload digest/所需权限/过期/重放）、find_pending 重试自动命中、
dump/restore 进程重启恢复、executor 接入（自动创建待确认、批准后重试放行、
token 显式携带）、legacy confirmed_ids 兼容、orchestrator/生产接线。
"""
import sys
from types import SimpleNamespace as _NS


import pytest

from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, ProviderType,
    CapProvider, CapabilityQuery, ToolObservation,
)
from dududa.core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef, Actor,
)
from dududa.core.state import RuntimeBudget, RunOutcome, RuntimeState, SocialAction
from dududa.core.memory import InMemoryRepository
from dududa.safeguards.security import (
    ConfirmationStore, AuthReason,
)
from dududa.planner.planner import PlannedStep, GeneratedPlan
from dududa.planner.executor import ToolExecutor, ExecutionContext
from dududa.runtime.orchestrator import RuntimeOrchestrator


class StubProvider(CapProvider):
    def __init__(self):
        self.calls = []

    async def execute(self, cap, args):
        self.calls.append(cap.capability_id)
        return ToolObservation(step_id="s", capability_id=cap.capability_id,
                               success=True, data="ok")

    def health(self):
        return True


def _cap(cid="need.confirm"):
    return Capability(
        capability_id=cid, name=cid, description=f"Mock {cid}",
        provider=ProviderType.BUILTIN, risk=CapabilityRisk.SIDE_EFFECT,
        requires_confirmation=True,
    )


def _envelope(text="hi", conversation="g1"):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conversation, platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="u"),
        text=text, mentions=(), reply_to=None,
    )


class TestConfirmationStoreToolPath:
    def test_create_for_actor_approve_single_use(self):
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {"q": "x"})
        assert conf.required_permission == "use_tool"
        r1 = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                  "need.confirm", {"q": "x"}, ("use_tool",))
        assert not r1.allowed
        assert r1.reason_codes == (AuthReason.CONFIRMATION_REQUIRED,)
        assert store.approve(conf.confirmation_id) is True
        r2 = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                  "need.confirm", {"q": "x"}, ("use_tool",))
        assert r2.allowed
        assert r2.reason_codes == (AuthReason.CONFIRMATION_OK,)
        assert store.get(conf.confirmation_id).is_consumed
        r3 = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                  "need.confirm", {"q": "x"}, ("use_tool",))
        assert not r3.allowed
        assert r3.reason_codes == (AuthReason.CONFIRMATION_REPLAYED,)

    def test_expired_denied(self):
        store = ConfirmationStore(ttl_seconds=-1)
        conf = store.create_for_actor("u1", "g1", "need.confirm", {})
        r = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                 "need.confirm", {}, ("use_tool",))
        assert not r.allowed
        assert r.reason_codes == (AuthReason.CONFIRMATION_EXPIRED,)

    def test_actor_scope_action_digest_bindings(self):
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {"a": 1})
        store.approve(conf.confirmation_id)
        cases = [
            (("u2", "g1", "need.confirm", {"a": 1}), AuthReason.CONFIRMATION_ACTOR_MISMATCH),
            (("u1", "g2", "need.confirm", {"a": 1}), AuthReason.CONFIRMATION_SCOPE_MISMATCH),
            (("u1", "g1", "other.tool", {"a": 1}), AuthReason.CONFIRMATION_ACTION_MISMATCH),
            (("u1", "g1", "need.confirm", {"a": 2}), AuthReason.CONFIRMATION_DIGEST_MISMATCH),
        ]
        for args, reason in cases:
            r = store.authorize_tool(conf.confirmation_id, *args, ("use_tool",))
            assert not r.allowed, args
            assert r.reason_codes == (reason,), args

    def test_role_permission_recheck(self):
        store = ConfirmationStore()
        conf = store.create_for_actor(
            "u1", "g1", "need.confirm", {}, required_permission="manage_config")
        store.approve(conf.confirmation_id)
        r1 = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                  "need.confirm", {}, ("use_tool",))
        assert not r1.allowed
        assert r1.reason_codes == (AuthReason.ROLE_TOO_LOW,)
        r2 = store.authorize_tool(conf.confirmation_id, "u1", "g1",
                                  "need.confirm", {}, ("manage_config",))
        assert r2.allowed

    def test_find_pending_binding(self):
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {"q": "x"})
        assert store.find_pending("u1", "g1", "need.confirm", {"q": "x"}) is conf
        assert store.find_pending("u2", "g1", "need.confirm", {"q": "x"}) is None
        assert store.find_pending("u1", "g2", "need.confirm", {"q": "x"}) is None
        assert store.find_pending("u1", "g1", "other", {"q": "x"}) is None
        assert store.find_pending("u1", "g1", "need.confirm", {"q": "y"}) is None

    def test_dump_restore_durable(self):
        store = ConfirmationStore()
        live = store.create_for_actor("u1", "g1", "need.confirm", {})
        spent = store.create_for_actor("u2", "g1", "need.confirm", {})
        store.approve(live.confirmation_id)
        store.approve(spent.confirmation_id)
        store.authorize_tool(spent.confirmation_id, "u2", "g1",
                             "need.confirm", {}, ("use_tool",))
        data = store.dump()   # 消费状态已持久化，dump 前不 prune
        fresh = ConfirmationStore()
        n = fresh.restore(data)
        assert n == 2
        r = fresh.authorize_tool(live.confirmation_id, "u1", "g1",
                                 "need.confirm", {}, ("use_tool",))
        assert r.allowed
        r2 = fresh.authorize_tool(spent.confirmation_id, "u2", "g1",
                                  "need.confirm", {}, ("use_tool",))
        assert not r2.allowed
        assert r2.reason_codes == (AuthReason.CONFIRMATION_REPLAYED,)


class TestExecutorConfirmation:
    @pytest.mark.asyncio
    async def test_store_grant_consumes_and_executes(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {})
        store.approve(conf.confirmation_id)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(
            permissions=("use_tool",), actor="u1", conversation_scope="g1",
            confirmation_store=store, confirmation_ids=(conf.confirmation_id,))
        results = await executor.execute_plan(plan, ctx)
        assert results[0].success
        assert provider.calls == ["need.confirm"]
        assert store.get(conf.confirmation_id).is_consumed

    @pytest.mark.asyncio
    async def test_no_token_auto_creates_pending_and_denies(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(
            permissions=("use_tool",), actor="u1", conversation_scope="g1",
            confirmation_store=store)
        results = await executor.execute_plan(plan, ctx)
        assert not results[0].success
        assert "requires confirmation" in results[0].error
        assert "(id=" in results[0].error
        assert provider.calls == []
        pending = store.find_pending("u1", "g1", "need.confirm", {})
        assert pending is not None
        assert not pending.approved

    @pytest.mark.asyncio
    async def test_retry_after_approval_auto_consumes(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(
            permissions=("use_tool",), actor="u1", conversation_scope="g1",
            confirmation_store=store)
        first = await executor.execute_plan(plan, ctx)
        assert not first[0].success
        pending = store.find_pending("u1", "g1", "need.confirm", {})
        assert pending is not None
        store.approve(pending.confirmation_id)
        second = await executor.execute_plan(plan, ctx)
        assert second[0].success
        assert provider.calls == ["need.confirm"]
        assert store.get(pending.confirmation_id).is_consumed
        third = await executor.execute_plan(plan, ctx)
        assert not third[0].success
        assert "(id=" in third[0].error

    @pytest.mark.asyncio
    async def test_wrong_actor_token_denied(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        conf = store.create_for_actor("u2", "g1", "need.confirm", {})
        store.approve(conf.confirmation_id)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(
            permissions=("use_tool",), actor="u1", conversation_scope="g1",
            confirmation_store=store, confirmation_ids=(conf.confirmation_id,))
        results = await executor.execute_plan(plan, ctx)
        assert not results[0].success
        assert "confirmation_actor_mismatch" in results[0].error
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_consumed_token_replay_denied(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {})
        store.approve(conf.confirmation_id)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(
            permissions=("use_tool",), actor="u1", conversation_scope="g1",
            confirmation_store=store, confirmation_ids=(conf.confirmation_id,))
        results = await executor.execute_plan(plan, ctx)
        assert results[0].success
        results2 = await executor.execute_plan(plan, ctx)
        assert not results2[0].success
        assert "confirmation_replayed" in results2[0].error
        assert len(provider.calls) == 1

    @pytest.mark.asyncio
    async def test_legacy_confirmed_ids_still_works(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "need.confirm", {}, "p"),))
        ctx = ExecutionContext(permissions=(), confirmed_ids=("need.confirm",))
        results = await executor.execute_plan(plan, ctx)
        assert results[0].success
        assert provider.calls == ["need.confirm"]


class TestOrchestratorConfirmation:
    @pytest.mark.asyncio
    async def test_chain_grant_with_state_confirmation_ids(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        conf = store.create_for_actor("u1", "g1", "need.confirm", {},
                                      required_permission="")
        store.approve(conf.confirmation_id)
        orch = RuntimeOrchestrator(
            memory_repo=InMemoryRepository(),
            capability_registry=reg,
            planner_integration=_NS(planner=None, executor=ToolExecutor(reg)),
            confirmation_store=store,
        )
        state = RuntimeState(
            envelope=_envelope(),
            budget=RuntimeBudget(max_tool_steps=4, deadline_seconds=30),
            capability_candidates=reg.retrieve(CapabilityQuery(intent="t", top_k=8)),
            confirmation_ids=(conf.confirmation_id,),
        )
        new_state = await orch._phase_tool_chain(state)
        assert new_state.outcome == RunOutcome.SUCCEEDED
        assert new_state.tool_observations and new_state.tool_observations[0].success
        assert provider.calls == ["need.confirm"]
        assert store.get(conf.confirmation_id).is_consumed

    @pytest.mark.asyncio
    async def test_chain_denial_creates_pending(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap(), provider)
        store = ConfirmationStore()
        orch = RuntimeOrchestrator(
            memory_repo=InMemoryRepository(),
            capability_registry=reg,
            planner_integration=_NS(planner=None, executor=ToolExecutor(reg)),
            confirmation_store=store,
        )
        state = RuntimeState(
            envelope=_envelope(),
            budget=RuntimeBudget(max_tool_steps=4, deadline_seconds=30),
            capability_candidates=reg.retrieve(CapabilityQuery(intent="t", top_k=8)),
        )
        new_state = await orch._phase_tool_chain(state)
        assert provider.calls == []
        obs = new_state.tool_observations
        assert len(obs) == 1
        assert "requires confirmation" in (obs[0].error or "")
        assert "(id=" in (obs[0].error or "")
        assert store.find_pending("u1", "g1", "need.confirm", {}) is not None

    @pytest.mark.asyncio
    async def test_run_accepts_confirmation_ids(self):
        from dududa.core.decision import (
            SocialDecisionEngine, SocialDecision, DecisionReason,
        )

        class _IgnoreEngine(SocialDecisionEngine):
            def decide(self, perception=None, context=None, now=None):
                return SocialDecision(
                    action=SocialAction.IGNORE,
                    reason_codes=(DecisionReason.LOW_RELEVANCE,),
                    confidence=1.0)

        orch = RuntimeOrchestrator(
            decision_engine=_IgnoreEngine(),
            capability_registry=CapabilityRegistry(),
            memory_repo=InMemoryRepository(),
            confirmation_store=ConfirmationStore(),
        )
        result = await orch.run(_envelope(), confirmation_ids=("t1", "t2"))
        assert result.outcome == RunOutcome.IGNORED


class TestProdWiring:
    @staticmethod
    def _make_main():
        sys.path.insert(0, str(PLUGIN_DIR))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dududa_main_conf", str(PLUGIN_MAIN))
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        try:
            ctx = main.star.Context()
        except TypeError:
            from unittest import mock
            ctx = mock.Mock()
        return main, main.Main(ctx)

    def test_prod_plugin_wires_confirmation_store(self):
        main, p = self._make_main()
        assert hasattr(p, "confirmations")
        assert isinstance(p.confirmations, ConfirmationStore)
        assert getattr(p.runtime, "_confirmation_store", None) is p.confirmations

    def test_prod_runtime_run_signature_has_confirmation_ids(self):
        import inspect
        from dududa.application.dududa_prod import _ProdOrchestrator
        sig = inspect.signature(_ProdOrchestrator.run)
        assert "confirmation_ids" in sig.parameters
