"""iCourse MCP 按群/按人选择性切换 + Server Registry 熔断（文档 2.5.6）。"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dududa.mcp.access import (
    MCPAccessPolicy, ICOURSE_SERVICE_IDS, is_icourse_capability,
)
from dududa.mcp.registry import (
    MCPProvider, ServerCircuitBreaker, breaker_status,
    create_all_services, register_all_mcp_services,
)
from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, CapabilitySchema,
    ProviderType,
)
from dududa.core.envelope import (
    MessageEnvelope, Actor, ConversationRef, MessageKind, Platform,
)
from dududa.core.state import RuntimeState, RuntimeBudget


def _write_cfg(path, **over):
    cfg = {
        "default_policy": "deny",
        "groups": {"allow": [], "deny": []},
        "users": {"allow": [], "deny": []},
    }
    cfg.update(over)
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _cap(cid):
    return Capability(
        capability_id=cid, name=cid, description="test",
        provider=ProviderType.MCP, risk=CapabilityRisk.READ_ONLY,
        schema=CapabilitySchema(input_schema={"type": "object"}),
    )


class TestICourseSet:
    def test_set_contains_seven_services(self):
        assert len(ICOURSE_SERVICE_IDS) == 7
        for svc in ("course_schedule", "exam_schedule", "academic_calendar",
                    "training_program", "second_classroom", "campus_notice",
                    "academic_affairs"):
            assert is_icourse_capability(f"mcp.{svc}")

    def test_clock_not_icourse(self):
        assert not is_icourse_capability("mcp.clock")
        assert not is_icourse_capability("chat")


class TestAccessPolicy:
    def test_non_icourse_always_allowed(self, tmp_path):
        p = MCPAccessPolicy(config_path=str(tmp_path / "missing.json"))
        assert p.is_allowed("mcp.clock", "g1", "u1")
        assert p.is_allowed("chat", "g1", "u1")

    def test_no_config_allows_icourse_legacy(self, tmp_path):
        # 无配置文件：与历史行为一致，不限制 iCourse
        p = MCPAccessPolicy(config_path=str(tmp_path / "missing.json"))
        assert p.is_allowed("mcp.course_schedule", "g1", "u1")
        assert p.deny_reason("mcp.course_schedule", "g1", "u1")[1] == "no_config_allow"

    def test_config_default_deny_icourse(self, tmp_path):
        # 配置文件存在即启用策略：default deny（fail closed）
        cfg = _write_cfg(tmp_path / "c.json")
        p = MCPAccessPolicy(config_path=cfg)
        assert not p.is_allowed("mcp.course_schedule", "g1", "u1")
        assert p.deny_reason("mcp.course_schedule", "g1", "u1")[1] == "default_deny"
        assert p.status()["configured"] is True

    def test_user_allow_overrides_group_deny(self, tmp_path):
        cfg = _write_cfg(tmp_path / "c.json",
                         users={"allow": ["u1"], "deny": []},
                         groups={"allow": [], "deny": ["g1"]})
        p = MCPAccessPolicy(config_path=cfg)
        assert p.is_allowed("mcp.course_schedule", "g1", "u1")

    def test_user_deny_wins(self, tmp_path):
        cfg = _write_cfg(tmp_path / "c.json",
                         users={"allow": ["u1"], "deny": ["u1"]})
        p = MCPAccessPolicy(config_path=cfg)
        assert not p.is_allowed("mcp.exam_schedule", "g1", "u1")
        assert p.deny_reason("mcp.exam_schedule", "g1", "u1")[1] == "user_deny"

    def test_group_allow_and_deny(self, tmp_path):
        cfg = _write_cfg(tmp_path / "c.json",
                         groups={"allow": ["g1"], "deny": ["g2"]})
        p = MCPAccessPolicy(config_path=cfg)
        assert p.is_allowed("mcp.course_schedule", "g1", "u9")
        assert not p.is_allowed("mcp.course_schedule", "g2", "u9")

    def test_group_prefix_normalized(self, tmp_path):
        cfg = _write_cfg(tmp_path / "c.json",
                         groups={"allow": ["123"], "deny": []})
        p = MCPAccessPolicy(config_path=cfg)
        assert p.is_allowed("mcp.course_schedule", "group_123", "u9")

    def test_default_allow_policy(self, tmp_path):
        cfg = _write_cfg(tmp_path / "c.json", default_policy="allow")
        p = MCPAccessPolicy(config_path=cfg)
        assert p.is_allowed("mcp.course_schedule", "g9", "u9")

    def test_hot_reload_on_mtime(self, tmp_path):
        path = tmp_path / "c.json"
        _write_cfg(path)
        p = MCPAccessPolicy(config_path=str(path))
        assert not p.is_allowed("mcp.course_schedule", "g1", "u1")
        _write_cfg(path, users={"allow": ["u1"], "deny": []})
        os.utime(path, (time.time() + 5, time.time() + 5))
        assert p.is_allowed("mcp.course_schedule", "g1", "u1")

    def test_ensure_seed_writes_owner_allow(self, tmp_path):
        path = tmp_path / "mcp_access.json"
        p = MCPAccessPolicy(config_path=str(path))
        assert p.ensure_seed(owner_ids=("u1", "u2"))
        assert not p.ensure_seed(owner_ids=("u3",))  # 幂等：已存在不再写
        p2 = MCPAccessPolicy(config_path=str(path))
        assert p2.is_allowed("mcp.course_schedule", "g1", "u2")
        assert not p2.is_allowed("mcp.course_schedule", "g1", "u3")

    def test_broken_config_falls_back_to_deny(self, tmp_path):
        path = tmp_path / "bad.json"
        Path(path).write_text("{not json", encoding="utf-8")
        p = MCPAccessPolicy(config_path=str(path))
        assert not p.is_allowed("mcp.course_schedule", "g1", "u1")
        assert p.status()["load_error"]


class TestServerCircuitBreaker:
    def test_closed_by_default(self):
        b = ServerCircuitBreaker(threshold=3, reset_seconds=30.0)
        assert b.state("s1") == "closed"
        assert b.allow("s1")

    def test_open_after_threshold(self):
        b = ServerCircuitBreaker(threshold=3, reset_seconds=30.0)
        for _ in range(3):
            b.record_failure("s1")
        assert b.state("s1") == "open"
        assert not b.allow("s1")

    def test_success_resets_failures(self):
        b = ServerCircuitBreaker(threshold=2, reset_seconds=30.0)
        b.record_failure("s1")
        b.record_success("s1")
        for _ in range(2):
            b.record_failure("s1")
        assert b.state("s1") == "open"
        b.record_success("s1")
        assert b.state("s1") == "closed"

    def test_half_open_probe_then_close(self):
        b = ServerCircuitBreaker(threshold=1, reset_seconds=0.05)
        b.record_failure("s1")
        assert not b.allow("s1")
        time.sleep(0.06)
        assert b.allow("s1")       # 冷却后放行一个探针
        assert not b.allow("s1")   # 探针期间只放行一次
        b.record_success("s1")     # 探针成功 -> closed
        assert b.state("s1") == "closed"
        assert b.allow("s1")

    def test_probe_failure_reopens(self):
        b = ServerCircuitBreaker(threshold=1, reset_seconds=0.05)
        b.record_failure("s1")
        time.sleep(0.06)
        assert b.allow("s1")
        b.record_failure("s1")
        assert b.state("s1") == "open"
        assert not b.allow("s1")


class TestMCPProviderBreaker:
    class _BoomService:
        name = "boom_test"
        config = SimpleNamespace(service_name="boom_test")

        async def search(self, **kw):
            raise RuntimeError("upstream down")

        def check_health(self):
            return "healthy"

    class _OkService:
        name = "ok_test"
        config = SimpleNamespace(service_name="ok_test")

        async def search(self, **kw):
            from dududa.mcp.base import ServiceResult
            return ServiceResult.ok([1], "mock")

        def check_health(self):
            return "healthy"

    @pytest.mark.asyncio
    async def test_failures_trip_and_fast_fail(self):
        from dududa.mcp import registry as reg
        prov = MCPProvider(self._BoomService(), server_id="boom_test")
        for _ in range(3):
            obs = await prov.execute(_cap("mcp.boom_test"), {"action": "search"})
            assert not obs.success
        assert reg.breaker.state("boom_test") == "open"
        assert not prov.health()
        obs = await prov.execute(_cap("mcp.boom_test"), {"action": "search"})
        assert not obs.success
        assert "breaker" in (obs.error or "").lower()
        reg.breaker.record_success("boom_test")

    @pytest.mark.asyncio
    async def test_success_recovers_breaker(self):
        from dududa.mcp import registry as reg
        prov = MCPProvider(self._OkService(), server_id="ok_test")
        obs = await prov.execute(_cap("mcp.ok_test"), {"action": "search"})
        assert obs.success
        assert reg.breaker.state("ok_test") == "closed"


class TestRegistryHealthWiring:
    def test_breaker_open_excluded_from_healthy(self):
        from dududa.mcp import registry as reg
        reg2 = CapabilityRegistry()
        n = register_all_mcp_services(reg2)
        assert n == 9
        try:
            for _ in range(3):
                reg.breaker.record_failure("course_schedule")
            ids = {c.capability_id for c in reg2.list_healthy()}
            assert "mcp.course_schedule" not in ids
            assert "mcp.clock" in ids
            st = breaker_status()
            assert st.get("course_schedule") == "open"
        finally:
            reg.breaker.record_success("course_schedule")


class TestOrchestratorScopeGating:
    def _orch(self, policy, monkeypatch, with_clock=True):
        import dududa.runtime.orchestrator as orch_mod
        from dududa.runtime.orchestrator import RuntimeOrchestrator
        from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        if not with_clock:
            reg.unregister("mcp.clock")
        monkeypatch.setattr(orch_mod, "mcp_access", policy)
        return RuntimeOrchestrator(
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )

    def _state(self, conv, actor):
        env = MessageEnvelope(
            platform=Platform.QQ,
            kind=MessageKind.GROUP,
            conversation=ConversationRef(
                conversation_id=conv, platform=Platform.QQ,
                kind=MessageKind.GROUP),
            sender=Actor(
                actor_id=actor, platform=Platform.QQ, display_name="t"),
            text="帮我查一下课程",
        )
        return RuntimeState(envelope=env, budget=RuntimeBudget())

    def test_candidates_filtered_for_denied_scope(self, tmp_path, monkeypatch):
        policy = MCPAccessPolicy(config_path=_write_cfg(
            tmp_path / "c.json", groups={"allow": ["g1"], "deny": []}))
        orch = self._orch(policy, monkeypatch)
        new_state = orch._phase_list_capabilities(self._state("g1", "u1"))
        ids = {c.capability.capability_id
               for c in new_state.capability_candidates}
        assert "mcp.course_schedule" in ids
        new_state = orch._phase_list_capabilities(self._state("g2", "u1"))
        ids = {c.capability.capability_id
               for c in new_state.capability_candidates}
        assert "mcp.course_schedule" not in ids
        assert "mcp.clock" in ids

    def test_plan_pruned_for_denied_scope(self, tmp_path, monkeypatch):
        from dududa.planner.planner import GeneratedPlan, PlannedStep
        policy = MCPAccessPolicy(config_path=_write_cfg(tmp_path / "c.json"))
        orch = self._orch(policy, monkeypatch)
        plan = GeneratedPlan(goal="t", steps=(
            PlannedStep(step_id="s1", capability_id="mcp.course_schedule",
                        arguments={}, purpose="p"),
            PlannedStep(step_id="s2", capability_id="mcp.clock",
                        arguments={}, purpose="p"),
        ))
        pruned = orch._scope_prune_plan(self._state("g9", "u9"), plan)
        assert pruned is not None
        assert [s.capability_id for s in pruned.steps] == ["mcp.clock"]
        all_denied = GeneratedPlan(goal="t", steps=(
            PlannedStep(step_id="s1", capability_id="mcp.course_schedule",
                        arguments={}, purpose="p"),))
        assert orch._scope_prune_plan(self._state("g9", "u9"), all_denied) is None

    @pytest.mark.asyncio
    async def test_full_run_scope_gating(self, tmp_path, monkeypatch):
        """按群/按人：拒绝范围不执行工具；放行范围正常执行。"""
        import dududa.runtime.orchestrator as orch_mod
        from dududa.runtime.orchestrator import RuntimeOrchestrator
        from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
        from dududa.core.decision import (
            SocialDecisionEngine, SocialDecision, DecisionReason,
        )
        from dududa.core.state import RunOutcome, SocialAction
        from dududa.planner.integration import integrate_with_orchestrator

        class _ForceTools(SocialDecisionEngine):
            def decide(self, perception=None, context=None, now=None):
                return SocialDecision(
                    action=SocialAction.USE_TOOLS,
                    reason_codes=(DecisionReason.EXPLICIT_COMMAND,),
                    confidence=1.0, should_use_tools=True)

        def _make_orch(policy_path):
            reg = CapabilityRegistry()
            register_all_mcp_services(reg)
            reg.unregister("mcp.clock")
            reg.unregister("mcp.web_search")  # 只留 iCourse 服务，便于断言
            monkeypatch.setattr(orch_mod, "mcp_access",
                                MCPAccessPolicy(config_path=policy_path))
            return RuntimeOrchestrator(
                decision_engine=_ForceTools(),
                capability_registry=reg,
                delivery_manager=DeliveryManager(NoOpOutputAdapter()),
                planner_integration=integrate_with_orchestrator(None, reg),
            )

        # 负例：g2 未放行 -> 无候选 -> 不执行任何工具，纯对话回复
        denied_path = _write_cfg(tmp_path / "deny.json",
                                 groups={"allow": ["g1"], "deny": []})
        orch = _make_orch(denied_path)
        result = await orch.run(self._state("g2", "u9"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert orch._last_state.tool_observations == ()

        # 正例：g1 已放行 -> 工具链正常执行 course_schedule
        orch = _make_orch(denied_path)
        result = await orch.run(self._state("g1", "u9"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert any(
            obs.capability_id == "mcp.course_schedule" and obs.success
            for obs in orch._last_state.tool_observations
        )
