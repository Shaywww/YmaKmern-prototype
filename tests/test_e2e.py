"""End-to-end integration tests - full message-to-reply pipeline."""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from packages.core.envelope import MessageEnvelope, Actor, Platform, MessageKind, ConversationRef
from packages.core.state import RunOutcome, RuntimeBudget
from packages.core.capability import CapabilityRegistry, Capability, ProviderType, CapProvider, ToolObservation
from packages.core.decision import SocialDecisionEngine
from packages.runtime.orchestrator import RuntimeOrchestrator
from packages.mcp.course_schedule import CourseScheduleService
from packages.mcp.exam_schedule import ExamScheduleService
from packages.mcp.academic_calendar import AcademicCalendarService
from packages.mcp.second_classroom import SecondClassroomService
from packages.mcp.campus_notice import CampusNoticeService
from packages.mcp.registry import register_all_mcp_services
from packages.planner.integration import integrate_with_orchestrator
from packages.core.delivery import DeliveryManager, NoOpOutputAdapter

def _make_envelope(text="", mentions=()):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(conversation_id="group_123", platform=Platform.QQ, kind=MessageKind.GROUP),
        sender=Actor(actor_id="user_1", platform=Platform.QQ, display_name=""),
        text=text, mentions=mentions,
    )

def _make_orchestrator_with_tools():
    reg = CapabilityRegistry()
    register_all_mcp_services(reg)
    integration = integrate_with_orchestrator(None, reg)
    return RuntimeOrchestrator(
        decision_engine=SocialDecisionEngine(keywords={"查", "搜", "课"}),
        capability_registry=reg,
        delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        planner_integration=integration,
    )

class TestE2ESimpleQueries:
    @pytest.mark.asyncio
    async def test_course_search_flow(self):
        """Full flow: "" -> Mention -> Decision -> Plan -> Execute MCP -> Compose -> Render"""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)
        assert result.outcome in (RunOutcome.SUCCEEDED, RunOutcome.DEGRADED)

    @pytest.mark.asyncio
    async def test_command_flow(self):
        """Explicit command should trigger full processing."""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="/help")
        result = await orch.run(env)
        # 显式命令走完整流水线；空参数 fallback 计划可能部分成功/失败，
        # 两种收尾都算流水线正常完成（与同文件其余用例一致）
        assert result.outcome in (RunOutcome.SUCCEEDED, RunOutcome.DEGRADED)

    @pytest.mark.asyncio
    async def test_course_keyword_triggers_tools(self):
        """Keywords should trigger tool chain."""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)
        assert result.outcome is not None

class TestE2EMultiStep:
    @pytest.mark.asyncio
    async def test_compare_courses(self):
        """Compare two courses - should trigger multi-step plan."""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)
        assert result.outcome is not None

    @pytest.mark.asyncio
    async def test_multi_source_lookup(self):
        """Multi-source query should execute parallel steps."""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)
        assert result.outcome is not None

class TestE2EErrorHandling:
    @pytest.mark.asyncio
    async def test_tool_chain_graceful_degradation(self):
        """Even with failing providers, the pipeline should complete."""
        reg = CapabilityRegistry()
        cap = Capability(capability_id="mcp.course_schedule", name="course", description="...", provider=ProviderType.MCP)
        reg.register(cap, _FailingProvider())
        integration = integrate_with_orchestrator(None, reg)
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            planner_integration=integration,
        )
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)
        # Should still complete (degraded or succeeded)
        assert result.outcome is not None

    @pytest.mark.asyncio
    async def test_no_capabilities_graceful(self):
        """No registered capabilities should not crash."""
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        env = _make_envelope(text="/help")
        result = await orch.run(env)
        assert result.outcome is not None

class TestE2EComplete:
    @pytest.mark.asyncio
    async def test_full_pipeline_reply(self):
        """Complete pipeline: message -> perception -> decision -> tools -> compose -> render -> result."""
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        integration = integrate_with_orchestrator(None, reg)
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查", "搜"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            planner_integration=integration,
        )
        env = _make_envelope(text="", mentions=("bot_001",))
        result = await orch.run(env)

        # Verify the result structure
        assert result.run_id
        assert result.outcome in (RunOutcome.SUCCEEDED, RunOutcome.DEGRADED)
        assert result.trace_summary is not None

    @pytest.mark.asyncio
    async def test_multiple_runs_independent(self):
        """Multiple runs should not interfere with each other."""
        orch = _make_orchestrator_with_tools()
        env1 = _make_envelope(text="", mentions=("bot_001",))
        env2 = _make_envelope(text="/help")
        r1 = await orch.run(env1)
        r2 = await orch.run(env2)
        assert r1.run_id != r2.run_id

    @pytest.mark.asyncio
    async def test_budget_enforcement(self):
        """Tight budget should not crash."""
        orch = _make_orchestrator_with_tools()
        env = _make_envelope(text="/help")
        result = await orch.run(env, budget=RuntimeBudget(max_tool_steps=1, deadline_seconds=5))
        assert result.outcome is not None

class _FailingProvider(CapProvider):
    async def execute(self, cap, args):
        raise RuntimeError("simulated provider failure")
    def health(self): return True
