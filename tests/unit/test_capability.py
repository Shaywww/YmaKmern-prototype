"""测试 Capability Registry 与 ToolPlanValidator。"""
import sys
import pytest
from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilitySchema,
    ToolPlanValidator, ToolObservation, ValidatorAction,
    CapabilityRisk, ProviderType, CapProvider,
)
from dududa.core.state import ToolStep, ToolPlan, RuntimeBudget


class StubProvider(CapProvider):
    async def execute(self, capability, arguments):
        return ToolObservation(
            step_id="stub", capability_id=capability.capability_id,
            success=True, data="stub result",
        )
    def health(self): return True


class TestCapability:
    def test_healthy_by_default(self):
        cap = Capability(capability_id="test1", name="测试能力", description="用于测试的能力")
        assert cap.is_healthy

    def test_disabled(self):
        cap = Capability(capability_id="test1", name="测试", description="...", enabled=False)
        assert not cap.is_healthy

    def test_health_check_fails(self):
        cap = Capability(capability_id="test1", name="测试", description="...", health_check=lambda: False)
        assert not cap.is_healthy

    def test_health_check_passes(self):
        cap = Capability(capability_id="test1", name="测试", description="...", health_check=lambda: True)
        assert cap.is_healthy


class TestCapabilityRegistry:
    def test_register_and_get(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="能力1", description="...")
        reg.register(cap, StubProvider())
        assert reg.get("c1") is cap
        assert reg.get("nonexistent") is None

    def test_list_enabled(self):
        reg = CapabilityRegistry()
        cap1 = Capability(capability_id="c1", name="A", description="...")
        cap2 = Capability(capability_id="c2", name="B", description="...", enabled=False)
        reg.register(cap1, StubProvider())
        reg.register(cap2, StubProvider())
        assert len(reg.list_enabled()) == 1

    def test_filter_candidates_by_permission(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="需要权限", description="...", required_permissions=("admin",))
        reg.register(cap, StubProvider())
        candidates = reg.filter_candidates(permissions=("read_messages",))
        assert len(candidates) == 0

    def test_filter_candidates_with_permission(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="管理员能力", description="...", required_permissions=("admin",))
        reg.register(cap, StubProvider())
        candidates = reg.filter_candidates(permissions=("admin",))
        assert len(candidates) == 1

    def test_filter_by_risk(self):
        reg = CapabilityRegistry()
        cap_dangerous = Capability(capability_id="d1", name="危险", description="...", risk=CapabilityRisk.DANGEROUS)
        cap_safe = Capability(capability_id="s1", name="安全", description="...", risk=CapabilityRisk.READ_ONLY)
        reg.register(cap_dangerous, StubProvider())
        reg.register(cap_safe, StubProvider())
        candidates = reg.filter_candidates(permissions=(), risk_tolerance=CapabilityRisk.READ_ONLY)
        assert len(candidates) == 1
        assert candidates[0].capability.capability_id == "s1"

    def test_summaries(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="课程查询", description="查询科大课程信息")
        reg.register(cap, StubProvider())
        summaries = reg.summaries()
        assert any("课程查询" in s for s in summaries)


class TestToolPlanValidator:
    def test_empty_plan(self):
        v = ToolPlanValidator(CapabilityRegistry())
        plan = ToolPlan(goal="test", steps=())
        valid, errors = v.validate_plan(plan, RuntimeBudget())
        assert valid

    def test_unknown_capability(self):
        v = ToolPlanValidator(CapabilityRegistry())
        plan = ToolPlan(goal="test", steps=(ToolStep(step_id="s1", capability_id="nonexistent", arguments={}, purpose="test"),))
        valid, errors = v.validate_plan(plan, RuntimeBudget())
        assert not valid
        assert any("not registered" in e for e in errors)

    def test_step_limit(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="test", description="...")
        reg.register(cap, StubProvider())
        v = ToolPlanValidator(reg)
        steps = tuple(ToolStep(step_id=f"s{i}", capability_id="c1", arguments={}, purpose=f"step {i}") for i in range(10))
        plan = ToolPlan(goal="test", steps=steps)
        valid, errors = v.validate_plan(plan, RuntimeBudget(max_tool_steps=4))
        assert not valid
        assert any("max" in e.lower() for e in errors)


class TestValidationResult:
    def test_all_success(self):
        validator = ToolPlanValidator(CapabilityRegistry())
        obs = (ToolObservation(step_id="s1", capability_id="c1", success=True, data="result"),)
        result = validator.validate_results(obs)
        assert result.action == ValidatorAction.FINISH

    def test_mixed_results(self):
        validator = ToolPlanValidator(CapabilityRegistry())
        obs = (
            ToolObservation(step_id="s1", capability_id="c1", success=True, data="ok"),
            ToolObservation(step_id="s2", capability_id="c2", success=False, error="fail"),
        )
        result = validator.validate_results(obs)
        assert result.action == ValidatorAction.DEGRADE

    def test_all_failures(self):
        validator = ToolPlanValidator(CapabilityRegistry())
        obs = (ToolObservation(step_id="s1", capability_id="c1", success=False, error="err"),)
        result = validator.validate_results(obs)
        assert result.action == ValidatorAction.CLARIFY
