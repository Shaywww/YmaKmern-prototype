# -*- coding: utf-8 -*-
"""P1 第 1 项：Tool Runtime 闭环测试（文档 2.5.5）。

覆盖：Top-K 权限/风险预过滤与排序、重复调用检测、
执行时重新授权（禁用/权限变更）、非幂等不重试、
步数硬上限（默认 4 / 全局 8）、无候选降级。
"""
import sys
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
import pytest

from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, ProviderType,
    CapProvider, CapabilityQuery, ToolObservation,
)
from dududa.core.state import RuntimeBudget, RunOutcome, RuntimeState
from dududa.core.memory import InMemoryRepository
from dududa.planner.planner import PlannedStep, GeneratedPlan
from dududa.planner.executor import ToolExecutor, ExecutionContext
from dududa.runtime.orchestrator import RuntimeOrchestrator


class StubProvider(CapProvider):
    def __init__(self, fail=False, data="stub_data"):
        self.fail = fail
        self.data = data
        self.calls = []

    async def execute(self, cap, args):
        self.calls.append((cap.capability_id, args))
        if self.fail:
            raise RuntimeError("stub failure")
        return ToolObservation(step_id="s", capability_id=cap.capability_id,
                               success=True, data=self.data)

    def health(self):
        return not self.fail


def _cap(cid, risk=CapabilityRisk.READ_ONLY, perms=(), idempotent=False,
         desc=None):
    return Capability(
        capability_id=cid, name=cid, description=desc or f"Mock {cid}",
        provider=ProviderType.BUILTIN, risk=risk,
        required_permissions=perms, idempotent=idempotent,
    )


class TestTopKRetrieval:
    def test_permission_prefilter(self):
        reg = CapabilityRegistry()
        reg.register(_cap("safe.tool"), StubProvider())
        reg.register(_cap("danger.tool", risk=CapabilityRisk.DANGEROUS,
                          perms=("manage",)), StubProvider())
        cands = reg.retrieve(CapabilityQuery(intent="tool", top_k=8))
        ids = {c.capability.capability_id for c in cands}
        assert "safe.tool" in ids
        assert "danger.tool" not in ids
        cands2 = reg.retrieve(CapabilityQuery(
            intent="tool", top_k=8,
            max_risk=CapabilityRisk.DANGEROUS),
            permissions=("manage",))
        ids2 = {c.capability.capability_id for c in cands2}
        assert "danger.tool" in ids2

    def test_risk_prefilter(self):
        reg = CapabilityRegistry()
        reg.register(_cap("r0.tool", risk=CapabilityRisk.READ_ONLY), StubProvider())
        reg.register(_cap("r1.tool", risk=CapabilityRisk.SIDE_EFFECT), StubProvider())
        reg.register(_cap("r2.tool", risk=CapabilityRisk.DANGEROUS), StubProvider())
        cands = reg.retrieve(CapabilityQuery(
            intent="tool", top_k=8, max_risk=CapabilityRisk.READ_ONLY))
        ids = {c.capability.capability_id for c in cands}
        assert ids == {"r0.tool"}

    def test_relevance_ranking(self):
        reg = CapabilityRegistry()
        reg.register(_cap("mcp.course_schedule", desc="课程查询 课表"),
                     StubProvider())
        reg.register(_cap("mcp.campus_notice", desc="校园通知 公告"),
                     StubProvider())
        cands = reg.retrieve(CapabilityQuery(intent="course", goal="查课程",
                                             top_k=8))
        assert cands[0].capability.capability_id == "mcp.course_schedule"
        assert cands[0].rank == 1

    def test_record_call_dedup(self):
        reg = CapabilityRegistry()
        assert reg.record_call("a.tool", "key1", window_seconds=60.0) is False
        assert reg.record_call("a.tool", "key1", window_seconds=60.0) is True
        assert reg.record_call("a.tool", "key2", window_seconds=60.0) is False


class TestExecutorClosure:
    @pytest.mark.asyncio
    async def test_reauthorize_disabled_capability(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        cap = _cap("svc.tool")
        from dataclasses import replace
        reg.register(replace(cap, enabled=False), provider)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "svc.tool", {}, "p"),))
        results = await executor.execute_plan(plan)
        assert not results[0].success
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_reauthorize_missing_permission(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        reg.register(_cap("admin.tool", perms=("manage",)), provider)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "admin.tool", {}, "p"),))
        results = await executor.execute_plan(plan, ExecutionContext(permissions=()))
        assert not results[0].success
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_non_idempotent_failure_no_retry(self):
        reg = CapabilityRegistry()
        provider = StubProvider(fail=True)
        reg.register(_cap("flaky.tool", idempotent=False), provider)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "flaky.tool", {}, "p"),))
        results = await executor.execute_plan(plan)
        assert not results[0].success
        assert len(provider.calls) == 1

    @pytest.mark.asyncio
    async def test_idempotent_retries_then_fails(self):
        reg = CapabilityRegistry()
        provider = StubProvider(fail=True)
        reg.register(_cap("retry.tool", idempotent=True), provider)
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "retry.tool", {}, "p"),))
        ctx = ExecutionContext(max_retries_per_step=2)
        results = await executor.execute_plan(plan, ctx)
        assert not results[0].success
        assert len(provider.calls) == 3  # 1 + 2 retries

    @pytest.mark.asyncio
    async def test_hard_cap_steps(self):
        reg = CapabilityRegistry()
        provider = StubProvider()
        for i in range(10):
            reg.register(_cap(f"cap{i}.tool"), provider)
        executor = ToolExecutor(reg)
        steps = tuple(PlannedStep(f"s{i}", f"cap{i}.tool", {}, "p") for i in range(10))
        plan = GeneratedPlan(goal="t", steps=steps)
        results = await executor.execute_plan(plan, ExecutionContext(max_steps=8))
        assert len(results) == 8
        assert provider.calls == [] or len(provider.calls) == 8


class TestOrchestratorDegrade:
    @pytest.mark.asyncio
    async def test_no_candidates_degrades(self):
        orch = RuntimeOrchestrator(memory_repo=InMemoryRepository())
        state = RuntimeState(budget=RuntimeBudget(max_tool_steps=4,
                                                  deadline_seconds=20))
        new_state = await orch._phase_tool_chain(state)
        assert new_state.outcome == RunOutcome.DEGRADED
        assert new_state.tool_observations == ()
