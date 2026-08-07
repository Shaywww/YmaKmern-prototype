# -*- coding: utf-8 -*-
"""时钟能力（mcp.clock）：用系统时钟回答日期/时间，不靠 LLM 猜测。

覆盖：服务实时性、注册计数、Planner 模式（时间 vs 考试优先级）、E2E 工具链。
"""
import os
import sys
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
# 测试与生产运行时数据隔离：access 策略指向不存在的路径 = legacy allow，
# 不受 data/mcp_access.json（生产 default deny）影响（文档 2.5.6）。
os.environ.setdefault(
    "DUDUDA_MCP_ACCESS", "/tmp/dududa-test-mcp-access-absent.json")
from datetime import datetime, timedelta, timezone

import pytest

from dududa.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from dududa.core.state import SocialAction, RunOutcome, RuntimeState
from dududa.core.decision import (
    SocialDecisionEngine, SocialDecision, DecisionReason,
)
from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk,
)
from dududa.core.memory import InMemoryRepository
from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
from dududa.mcp.clock_service import ClockService
from dududa.mcp.registry import create_all_services, register_all_mcp_services
from dududa.planner.integration import integrate_with_orchestrator
from dududa.runtime.orchestrator import RuntimeOrchestrator

_CST = timezone(timedelta(hours=8))


def _envelope(text):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="group_123", platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="user_1", platform=Platform.QQ,
                     display_name="小明"),
        text=text, mentions=("bot_001",),
    )


class _ForceToolsEngine(SocialDecisionEngine):
    """强制 USE_TOOLS：确定性触发工具链。"""

    def decide(self, perception=None, context=None, now=None):
        return SocialDecision(
            action=SocialAction.USE_TOOLS,
            reason_codes=(DecisionReason.KEYWORD_MATCH,),
            confidence=1.0,
        )


def _orch_with_clock():
    reg = CapabilityRegistry()
    register_all_mcp_services(reg)
    return RuntimeOrchestrator(
        decision_engine=_ForceToolsEngine(),
        capability_registry=reg,
        memory_repo=InMemoryRepository(),
        delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        planner_integration=integrate_with_orchestrator(None, reg),
    )


class _StubProvider:
    """测试用最小 provider：health OK，execute 不会被调用。"""

    def health(self):
        return True

    async def execute(self, capability, arguments):
        return None


class TestCandidateCutoff:
    @pytest.mark.asyncio
    async def test_production_shape_keeps_clock_in_candidates(self):
        """生产注册表（3 内置 + 9 MCP = 12 项）超过默认 top_k=8 时，
        mcp.clock 不得被候选截断（曾导致「现在几点」降级为闲聊）。"""
        reg = CapabilityRegistry()
        for i in range(3):
            reg.register(
                Capability(capability_id=f"builtin_{i}", name=f"内置{i}",
                           description="builtin",
                           risk=CapabilityRisk.READ_ONLY),
                _StubProvider())
        register_all_mcp_services(reg)
        assert len(reg.list_enabled()) == 15
        orch = RuntimeOrchestrator(
            decision_engine=_ForceToolsEngine(),
            capability_registry=reg,
            memory_repo=InMemoryRepository(),
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            planner_integration=integrate_with_orchestrator(None, reg),
        )
        state = RuntimeState(
            envelope=_envelope("现在几点"), run_id="r-cut", trace_id="t-cut")
        listed = orch._phase_list_capabilities(state)
        ids = [c.capability.capability_id for c in listed.capability_candidates]
        assert "mcp.clock" in ids
        plan = orch._plan(listed, listed.capability_candidates, 4, ())
        assert plan is not None and plan.steps
        assert plan.steps[0].capability_id == "mcp.clock"



class TestClockService:
    @pytest.mark.asyncio
    async def test_get_now_returns_real_time(self):
        svc = ClockService()
        r = await svc.get_now()
        assert r.success
        assert r.cached is False  # 实时数据不走缓存
        assert datetime.now(_CST).strftime("%Y-%m-%d") in str(r.data)
        assert "星期" in str(r.data)
        assert "UTC+8" in str(r.data)

    @pytest.mark.asyncio
    async def test_get_date_and_get_time(self):
        svc = ClockService()
        d = await svc.get_date()
        t = await svc.get_time()
        assert d.success and datetime.now(_CST).strftime("%Y-%m-%d") in str(d.data)
        assert t.success and ":" in str(t.data)


class TestClockCapability:
    def test_registry_has_clock(self):
        services = create_all_services()
        assert "clock" in services
        reg = CapabilityRegistry()
        n = register_all_mcp_services(reg)
        assert n == 12
        assert reg.get("mcp.clock") is not None

    @pytest.mark.asyncio
    async def test_now_question_uses_clock(self):
        """「现在几点」走工具链 -> mcp.clock -> 真实时间。"""
        orch = _orch_with_clock()
        result = await orch.run(_envelope("现在几点"))
        assert result.outcome == RunOutcome.SUCCEEDED
        clocks = [o for o in orch._last_state.tool_observations
                  if o.capability_id == "mcp.clock"]
        assert clocks and clocks[0].success
        assert datetime.now(_CST).strftime("%Y-%m-%d") in str(clocks[0].data)

    @pytest.mark.asyncio
    async def test_date_question_uses_clock(self):
        orch = _orch_with_clock()
        result = await orch.run(_envelope("今天是星期几"))
        assert result.outcome == RunOutcome.SUCCEEDED
        caps = {o.capability_id for o in orch._last_state.tool_observations}
        assert "mcp.clock" in caps

    @pytest.mark.asyncio
    async def test_exam_question_keeps_exam_priority(self):
        """「考试时间」仍走考试能力，不被时钟截胡。"""
        orch = _orch_with_clock()
        result = await orch.run(_envelope("考试时间"))
        assert result.outcome == RunOutcome.SUCCEEDED
        caps = {o.capability_id for o in orch._last_state.tool_observations}
        assert "mcp.exam_schedule" in caps
        assert "mcp.clock" not in caps

    @pytest.mark.asyncio
    async def test_base_perceive_flags_time(self):
        """基础感知：时间关键词 -> needs_tools。"""
        orch = _orch_with_clock()
        result = await orch.run(_envelope("现在几点"))
        assert result.outcome == RunOutcome.SUCCEEDED
        # 基础 _phase_perceive 已把时间问句标记为 needs_tools
        assert orch._last_state.perception is not None

    @pytest.mark.asyncio
    async def test_plan_pattern_time_lookup(self):
        """Planner 对「现在几点了」命中 time_lookup 单步。"""
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        chain = integrate_with_orchestrator(None, reg)
        from dududa.planner.planner import PlanningContext
        plan = chain.planner.plan(PlanningContext(
            user_intent="现在几点了",
            available_capabilities=reg.filter_candidates(permissions=()),
            max_steps=4, permissions=()))
        assert plan.goal == "Get current date and time"
        assert plan.steps and plan.steps[0].capability_id == "mcp.clock"
        assert plan.steps[0].arguments.get("action") == "get_now"
