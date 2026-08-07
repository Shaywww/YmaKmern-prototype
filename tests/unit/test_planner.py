"""Test Planner/Executor system."""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from dududa.planner.planner import ToolPlanner, PlanningContext
from dududa.planner.dependency import DependencyResolver, CircularDependencyError
from dududa.planner.executor import ToolExecutor, ExecutionContext, StepResult
from dududa.planner.recovery import ErrorRecovery, ErrorContext, RecoveryAction
from dududa.planner.integration import integrate_with_orchestrator, ToolChainIntegration
from dududa.core.capability import Capability, CapabilityRegistry, ProviderType, CapProvider, ToolObservation

class StubProvider(CapProvider):
    def __init__(self, fail=False, data="stub_data"):
        self.fail = fail; self.data = data; self.calls = []
    async def execute(self, cap, args):
        self.calls.append(args)
        if self.fail: raise RuntimeError("stub failure")
        return ToolObservation(step_id="s", capability_id=cap.capability_id, success=True, data=self.data)
    def health(self): return not self.fail

def _make_registry():
    reg = CapabilityRegistry()
    for cid in ["mcp.course_schedule", "mcp.exam_schedule", "mcp.academic_calendar", "mcp.second_classroom", "mcp.campus_notice"]:
        cap = Capability(capability_id=cid, name=cid, description=f"Mock {cid}", provider=ProviderType.MCP)
        reg.register(cap, StubProvider())
    return reg

class TestWebSearchPattern:
    """通用「搜/查/找」命令 -> mcp.web_search，q 提取搜索词。"""

    def _reg_with_search(self):
        reg = _make_registry()
        cap = Capability(capability_id="mcp.web_search", name="web_search",
                         description="Web search returning top ranked results",
                         provider=ProviderType.MCP)
        reg.register(cap, StubProvider())
        return reg

    def _plan(self, text):
        from dududa.planner.planner import PlanningContext
        reg = self._reg_with_search()
        integration = integrate_with_orchestrator(None, reg)
        cands = reg.filter_candidates(())
        return integration.planner.plan(PlanningContext(
            user_intent=text, available_capabilities=cands,
            max_steps=4, permissions=()))

    def test_search_command_plans_web_search(self):
        plan = self._plan("帮我搜一下USTC")
        assert plan is not None and plan.steps
        assert plan.steps[0].capability_id == "mcp.web_search"
        assert plan.steps[0].arguments.get("action") == "search"
        assert plan.steps[0].arguments.get("q") == "USTC"

    def test_search_with_mention_and_particles(self):
        plan = self._plan("@bot 百度一下量子计算呗")
        assert plan.steps[0].capability_id == "mcp.web_search"
        assert plan.steps[0].arguments.get("q") == "量子计算"

    def test_chinese_query_extraction(self):
        plan = self._plan("帮我搜一下数据结构")
        assert plan.steps[0].capability_id == "mcp.web_search"
        assert plan.steps[0].arguments.get("q") == "数据结构"

    def test_chacha_not_hijack_course_query(self):
        plan = self._plan("帮我查一下数据结构")
        assert plan.steps[0].capability_id == "mcp.course_schedule"

    def test_course_pattern_still_priority(self):
        plan = self._plan("帮我查一下课表")
        assert plan.steps[0].capability_id == "mcp.course_schedule"

    def test_exam_pattern_still_priority(self):
        plan = self._plan("帮我搜一下考试安排")
        assert plan.steps[0].capability_id == "mcp.exam_schedule"


class TestDependencyResolver:
    def test_simple_sort(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "first")
        s2 = PlannedStep("s2", "c2", {}, "second", depends_on=("s1",))
        sorted_steps = DependencyResolver.topological_sort([s2, s1])
        assert [s.step_id for s in sorted_steps] == ["s1", "s2"]

    def test_cycle_detection(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "a", depends_on=("s2",))
        s2 = PlannedStep("s2", "c2", {}, "b", depends_on=("s1",))
        with pytest.raises(CircularDependencyError):
            DependencyResolver.topological_sort([s1, s2])

    def test_execution_order_batches(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "a")
        s2 = PlannedStep("s2", "c2", {}, "b")
        s3 = PlannedStep("s3", "c3", {}, "c", depends_on=("s1",))
        batches = DependencyResolver.execution_order([s1, s2, s3])
        assert len(batches) == 2
        assert len(batches[0]) == 2  # s1 and s2 are independent
        assert len(batches[1]) == 1  # s3 depends on s1

    def test_cycle_detection_complex(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "a", depends_on=("s3",))
        s2 = PlannedStep("s2", "c2", {}, "b", depends_on=("s1",))
        s3 = PlannedStep("s3", "c3", {}, "c", depends_on=("s2",))
        cycles = DependencyResolver.detect_cycles([s1, s2, s3])
        assert len(cycles) >= 1

    def test_validate_ok(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "a")
        s2 = PlannedStep("s2", "c2", {}, "b", depends_on=("s1",))
        valid, errors = DependencyResolver.validate_dependencies([s1, s2])
        assert valid

    def test_validate_missing(self):
        from dududa.planner.planner import PlannedStep
        s1 = PlannedStep("s1", "c1", {}, "a", depends_on=("nonexistent",))
        valid, errors = DependencyResolver.validate_dependencies([s1])
        assert not valid

class TestToolPlanner:
    def test_lookup_intent(self):
        p = ToolPlanner()
        reg = _make_registry()
        candidates = reg.filter_candidates(permissions=())
        ctx = PlanningContext(user_intent="查询数据结构课程", available_capabilities=candidates)
        plan = p.plan(ctx)
        assert plan.goal
        assert len(plan.steps) >= 0

    def test_compare_intent(self):
        p = ToolPlanner()
        reg = _make_registry()
        candidates = reg.filter_candidates(permissions=())
        ctx = PlanningContext(user_intent="比较数据结构与操作系统", available_capabilities=candidates)
        plan = p.plan(ctx)
        assert plan.goal

    def test_pattern_matching(self):
        p = ToolPlanner()
        p.register_pattern(("查课",), {"name": "test", "goal": "test_pattern", "steps": [{"step_id": "s1", "capability_id": "mcp.course_schedule", "arguments": {}, "purpose": "test"}]})
        reg = _make_registry()
        candidates = reg.filter_candidates(permissions=())
        ctx = PlanningContext(user_intent="帮我查课", available_capabilities=candidates)
        plan = p.plan(ctx)
        assert plan.rationale == "Pattern: test"

class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_execute_empty_plan(self):
        from dududa.planner.planner import GeneratedPlan
        reg = _make_registry()
        executor = ToolExecutor(reg)
        plan = GeneratedPlan(goal="empty", steps=())
        results = await executor.execute_plan(plan)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_execute_single_step(self):
        from dududa.planner.planner import PlannedStep, GeneratedPlan
        reg = _make_registry()
        executor = ToolExecutor(reg)
        step = PlannedStep("s1", "mcp.course_schedule", {}, "test")
        plan = GeneratedPlan(goal="test", steps=(step,))
        results = await executor.execute_plan(plan)
        assert len(results) == 1
        assert results[0].success

    @pytest.mark.asyncio
    async def test_execute_with_failing_provider(self):
        from dududa.planner.planner import PlannedStep, GeneratedPlan
        reg = CapabilityRegistry()
        cap = Capability(capability_id="bad.svc", name="bad", description="failing", provider=ProviderType.MCP)
        reg.register(cap, StubProvider(fail=True))
        executor = ToolExecutor(reg)
        step = PlannedStep("s1", "bad.svc", {}, "test")
        plan = GeneratedPlan(goal="test", steps=(step,))
        results = await executor.execute_plan(plan)
        assert len(results) == 1
        assert not results[0].success

class TestErrorRecovery:
    def test_timeout_retry_first(self):
        recovery = ErrorRecovery()
        ctx = ErrorContext(step_id="s1", capability_id="c1", error_message="timeout", error_type="timeout", retries_used=0, max_retries=2)
        decision = recovery.decide(ctx)
        assert decision.action == RecoveryAction.RETRY

    def test_timeout_degrade_after_retries(self):
        recovery = ErrorRecovery()
        ctx = ErrorContext(step_id="s1", capability_id="c1", error_message="timeout", error_type="timeout", retries_used=3, max_retries=2, has_partial_data=True)
        decision = recovery.decide(ctx)
        assert decision.action == RecoveryAction.DEGRADE

    def test_permission_aborts_critical(self):
        recovery = ErrorRecovery()
        ctx = ErrorContext(step_id="s1", capability_id="c1", error_message="permission denied", error_type="permission", critical_step=True)
        decision = recovery.decide(ctx)
        assert decision.action == RecoveryAction.ABORT

    def test_schema_clarifies(self):
        recovery = ErrorRecovery()
        ctx = ErrorContext(step_id="s1", capability_id="c1", error_message="schema validation failed", error_type="schema")
        decision = recovery.decide(ctx)
        assert decision.action == RecoveryAction.CLARIFY

    def test_classify_error(self):
        recovery = ErrorRecovery()
        assert recovery.classify_error("Connection timeout after 30s") == "timeout"
        assert recovery.classify_error("Permission denied for user") == "permission"
        assert recovery.classify_error("Schema validation error: missing field") == "schema"
        assert recovery.classify_error("Provider internal error 500") == "provider"
        assert recovery.classify_error("Network unreachable: DNS failed") == "network"

class TestToolChainIntegration:
    @pytest.mark.asyncio
    async def test_plan_and_execute(self):
        reg = _make_registry()
        integration = integrate_with_orchestrator(None, reg)
        result = await integration.plan_and_execute(
            user_intent="查课", perception=None,
            candidates=reg.filter_candidates(permissions=()),
            permissions=(), budget=type("B", (), {"max_tool_steps": 4, "deadline_seconds": 30})(),
        )
        assert "plan" in result
        assert "results" in result
        assert "recovery" in result

    @pytest.mark.asyncio
    async def test_plan_and_execute_exam(self):
        reg = _make_registry()
        integration = integrate_with_orchestrator(None, reg)
        result = await integration.plan_and_execute(
            user_intent="什么时候考试", perception=None,
            candidates=reg.filter_candidates(permissions=()),
            permissions=(), budget=type("B", (), {"max_tool_steps": 4, "deadline_seconds": 30})(),
        )
        assert result["success_count"] >= 0
