# -*- coding: utf-8 -*-
"""P0：Tool Runtime cancellation + 每步重新授权（文档 2.5.5 / 2.4.12）。

覆盖：ExecutionContext 取消三来源（request_cancel / asyncio.Event / deadline）、
取消后不开始下一步、batch 迟到结果整批标记取消、重试中取消、
Orchestrator run(入口取消) -> CANCELLED、_phase_tool_chain 取消传播、
_execute_direct 直连退化路径的取消 + 每步重新授权语义。
"""
import asyncio
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

sys.path.insert(0, "/opt/dududa20-prototype")

import pytest

from packages.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, ProviderType,
    CapProvider, CapabilityQuery, ToolObservation,
)
from packages.core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef, Actor,
)
from packages.core.state import RuntimeBudget, RunOutcome, RuntimeState
from packages.core.memory import InMemoryRepository
from packages.planner.planner import PlannedStep, GeneratedPlan
from packages.planner.executor import ToolExecutor, ExecutionContext
from packages.runtime.orchestrator import RuntimeOrchestrator


class StubProvider(CapProvider):
    def __init__(self, data="stub_data", fail=False, event=None):
        self.data = data
        self.fail = fail
        self.event = event
        self.calls = []

    async def execute(self, cap, args):
        self.calls.append(cap.capability_id)
        if self.event is not None:
            self.event.set()
        if self.fail:
            raise RuntimeError("stub failure")
        return ToolObservation(step_id="s", capability_id=cap.capability_id,
                               success=True, data=self.data)

    def health(self):
        return not self.fail


def _cap(cid, risk=CapabilityRisk.READ_ONLY, perms=(), idempotent=False):
    return Capability(
        capability_id=cid, name=cid, description=f"Mock {cid}",
        provider=ProviderType.BUILTIN, risk=risk,
        required_permissions=perms, idempotent=idempotent,
    )


def _plan(*steps):
    return GeneratedPlan(goal="t", steps=tuple(steps))


def _envelope(text="hi", conversation="g1"):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conversation, platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="u"),
        text=text, mentions=(), reply_to=None,
    )


class TestExecutionContextCancellation:
    def test_not_cancelled_by_default(self):
        ctx = ExecutionContext()
        assert not ctx.cancelled
        assert ctx.can_execute_step

    def test_request_cancel(self):
        ctx = ExecutionContext()
        ctx.request_cancel()
        assert ctx.cancelled
        assert not ctx.can_execute_step
        assert not ctx.can_retry

    @pytest.mark.asyncio
    async def test_event_cancellation(self):
        ev = asyncio.Event()
        ctx = ExecutionContext(cancellation=ev)
        assert not ctx.cancelled
        ev.set()
        assert ctx.cancelled
        assert not ctx.can_execute_step

    def test_deadline_expiry_cancels(self):
        ctx = ExecutionContext(deadline_seconds=30)
        ctx.created_at = time.time() - 31
        assert ctx.cancelled
        assert not ctx.can_execute_step


class TestExecutorCancellation:
    @pytest.mark.asyncio
    async def test_cancel_between_steps_prevents_next(self):
        ev = asyncio.Event()
        reg = CapabilityRegistry()
        provider = StubProvider(event=ev)
        reg.register(_cap("cap.a"), provider)
        reg.register(_cap("cap.b"), provider)
        executor = ToolExecutor(reg)
        plan = _plan(
            PlannedStep("s1", "cap.a", {}, "p"),
            PlannedStep("s2", "cap.b", {}, "p", depends_on=("s1",)),
        )
        results = await executor.execute_plan(
            plan, ExecutionContext(max_steps=8, cancellation=ev))
        assert len(results) == 1
        assert provider.calls == ["cap.a"]
        # 迟到结果不推进状态：s1 完成时取消已到达 -> 整批标记取消
        assert results[0].cancelled
        assert not results[0].completed
        assert results[0].error == "Cancelled"

    @pytest.mark.asyncio
    async def test_batch_cancel_marks_all_late(self):
        ev = asyncio.Event()
        reg = CapabilityRegistry()
        provider = StubProvider(event=ev)
        reg.register(_cap("cap.a"), provider)
        reg.register(_cap("cap.b"), provider)
        executor = ToolExecutor(reg)
        plan = _plan(
            PlannedStep("s1", "cap.a", {}, "p"),
            PlannedStep("s2", "cap.b", {}, "p"),
        )
        results = await executor.execute_plan(
            plan, ExecutionContext(max_steps=8, cancellation=ev))
        assert len(results) == 2
        assert all(r.cancelled for r in results)
        assert all(not r.completed for r in results)
        assert all(r.error == "Cancelled" for r in results)

    @pytest.mark.asyncio
    async def test_cancel_during_retry_returns_cancelled(self):
        ev = asyncio.Event()
        reg = CapabilityRegistry()
        provider = StubProvider(fail=True, event=ev)
        reg.register(_cap("cap.r", idempotent=True), provider)
        executor = ToolExecutor(reg)
        plan = _plan(PlannedStep("s1", "cap.r", {}, "p"))
        results = await executor.execute_plan(
            plan, ExecutionContext(max_retries_per_step=2, cancellation=ev))
        r = results[0]
        assert r.cancelled
        assert not r.completed
        assert r.error == "Cancelled"


class TestOrchestratorCancellation:
    @pytest.mark.asyncio
    async def test_run_entry_cancellation(self):
        orch = RuntimeOrchestrator(memory_repo=InMemoryRepository())
        ev = asyncio.Event()
        ev.set()
        result = await orch.run(_envelope(), cancellation=ev)
        assert result.outcome == RunOutcome.CANCELLED
        assert result.reason_codes == ("cancelled_at_entry",)

    @pytest.mark.asyncio
    async def test_phase_tool_chain_cancel_mid_execution(self):
        ev = asyncio.Event()
        reg = CapabilityRegistry()
        provider = StubProvider(event=ev)
        reg.register(_cap("cap.a"), provider)
        reg.register(_cap("cap.b"), provider)
        orch = RuntimeOrchestrator(
            memory_repo=InMemoryRepository(),
            capability_registry=reg,
            planner_integration=SimpleNamespace(
                planner=None, executor=ToolExecutor(reg)),
        )
        orch._pending_cancellation = ev
        candidates = reg.retrieve(CapabilityQuery(intent="t", top_k=8))
        state = RuntimeState(
            budget=RuntimeBudget(max_tool_steps=8, deadline_seconds=30),
            capability_candidates=candidates,
        )
        new_state = await orch._phase_tool_chain(state)
        assert new_state.outcome == RunOutcome.CANCELLED
        assert new_state.tool_observations
        assert all(getattr(o, "cancelled", False)
                   for o in new_state.tool_observations)

    @pytest.mark.asyncio
    async def test_execute_direct_reauthorize_no_permission(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        cap = _cap("admin.tool")
        reg.register(cap, provider)
        candidates = reg.retrieve(CapabilityQuery(intent="t", top_k=8))
        # 计划生成后权限收紧：执行时每步重新授权必须拒绝（文档 2.4.12）
        reg.register(replace(cap, required_permissions=("manage",)), provider)
        orch = RuntimeOrchestrator(
            memory_repo=InMemoryRepository(), capability_registry=reg)
        state = RuntimeState(
            budget=RuntimeBudget(max_tool_steps=8, deadline_seconds=30),
            capability_candidates=candidates,
        )
        new_state = await orch._phase_tool_chain(state)
        assert provider.calls == []
        obs = new_state.tool_observations
        assert len(obs) == 1
        assert not obs[0].success
        assert not obs[0].cancelled
        assert "permissions" in (obs[0].error or "")

    @pytest.mark.asyncio
    async def test_execute_direct_cancel_mid_execution(self):
        ev = asyncio.Event()
        reg = CapabilityRegistry()
        provider = StubProvider(event=ev)
        reg.register(_cap("cap.a"), provider)
        reg.register(_cap("cap.b"), provider)
        orch = RuntimeOrchestrator(
            memory_repo=InMemoryRepository(), capability_registry=reg)
        orch._pending_cancellation = ev
        candidates = reg.retrieve(CapabilityQuery(intent="t", top_k=8))
        state = RuntimeState(
            budget=RuntimeBudget(max_tool_steps=8, deadline_seconds=30),
            capability_candidates=candidates,
        )
        new_state = await orch._phase_tool_chain(state)
        assert new_state.outcome == RunOutcome.CANCELLED
        assert new_state.tool_observations
        assert all(getattr(o, "cancelled", False)
                   for o in new_state.tool_observations)
