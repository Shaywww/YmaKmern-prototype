# -*- coding: utf-8 -*-
"""版本化 Eval 库：五组件 fixture + 阈值比对（文档 2.5.10 / Phase 9 前半）。

组件：perception / social_decision / tool_runtime / memory_writegate / oc_render。
每个 run_* 返回 metric dict；check() 按 thresholds.json 判定。
"""
import json
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path

from tests.path_config import PLUGIN_MAIN, configure_import_paths

configure_import_paths()

_EVAL_DIR = Path(__file__).resolve().parent
_FIXTURES = _EVAL_DIR / "fixtures"


def load_fixture(name: str) -> dict:
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def load_thresholds() -> dict:
    with open(_EVAL_DIR / "thresholds.json", encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class EvalCheckResult:
    status: str  # pass | fail | skip
    failures: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def check(component: str, metric: dict, thresholds: dict) -> EvalCheckResult:
    """Compare metrics with typed thresholds and explicit sample-size skips."""
    t = thresholds.get(component, {})
    failures = []
    skipped = []
    evaluated = 0
    for key, bound in t.items():
        value = metric.get(key)
        if isinstance(bound, bool):
            evaluated += 1
            if value is not bound:
                failures.append(f"{component}.{key}: 期望 {bound}，实际 {value}")
        elif isinstance(bound, dict):
            threshold_class = bound.get("class", "deterministic")
            if threshold_class == "statistical":
                sample_key = bound.get("sample_key", "cases")
                minimum = int(bound.get("min_samples", 1))
                samples = metric.get(sample_key)
                if not isinstance(samples, int) or samples < minimum:
                    skipped.append(
                        f"{component}.{key}: {sample_key}={samples} < {minimum}")
                    continue
            evaluated += 1
            for op, target in bound.items():
                if op in {"class", "min_samples", "sample_key"}:
                    continue
                if op == "gte" and not (isinstance(value, (int, float)) and value >= target):
                    failures.append(f"{component}.{key}: {value} < {target}")
                elif op == "eq" and value != target:
                    failures.append(f"{component}.{key}: 期望 {target}，实际 {value}")
        elif isinstance(bound, (int, float)):
            evaluated += 1
            if not (isinstance(value, (int, float)) and value >= bound):
                failures.append(f"{component}.{key}: {value} < {bound}")
    if failures:
        status = "fail"
    elif skipped and not evaluated:
        status = "skip"
    else:
        status = "pass"
    return EvalCheckResult(status, tuple(failures), tuple(skipped))


# ---- 插件构造（与 tests/test_social_decision_alignment.py 同模式） ----

def _load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dududa_main_eval", str(PLUGIN_MAIN))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    _tmp = tempfile.mkdtemp(prefix="dududa_eval_")
    main.MEMORY_FILE = str(Path(_tmp) / "memory.json")
    main.CONFIRM_FILE = str(Path(_tmp) / "confirmations.json")
    main.GROUP_POLICY_FILE = str(Path(_tmp) / "group_policy.json")
    try:
        ctx = main.star.Context()
    except TypeError:
        from unittest import mock
        ctx = mock.Mock()
    return main.Main(ctx)


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", bot="bot1",
                 at=True, session=None):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = session if session is not None else (group or f"private_{user}")
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message" if group else "private_message"
        self._components = []
        self.is_at_or_wake_command = at

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


# ---- 组件 Eval ----

def run_perception() -> dict:
    plugin = _load_plugin()
    fx = load_fixture("perception_cases.json")
    total = passed = 0
    greeting_misfires = 0
    details = []
    for case in fx["cases"]:
        ev = _FakeEvent(case["text"], group=case.get("group", "g1"),
                        at=case.get("at", True))
        acts = [a.act_type for a in plugin._perceive(ev).speech_acts]
        act_set = set(acts)
        ok = (set(case["expect"]).issubset(act_set)
              and not (act_set & set(case.get("not_expect", []))))
        total += 1
        passed += int(ok)
        if "greeting" in case.get("not_expect", []) and "greeting" in act_set:
            greeting_misfires += 1
        details.append({"case": case["text"], "acts": acts, "passed": ok})
    return {
        "version": fx.get("version"),
        "cases": total, "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "greeting_misfires": greeting_misfires,
        "details": details,
    }


def run_social_decision() -> dict:
    plugin = _load_plugin()
    fx = load_fixture("social_decision_cases.json")
    total = passed = 0
    actions = set()
    details = []
    for i, case in enumerate(fx["cases"]):
        ev = _FakeEvent(case["text"], group=case.get("group", "g1"),
                        at=case.get("at", True),
                        session=case.get("session", f"ev{i}"))
        action, reason = plugin._social_decision(ev)
        actions.add(action.value)
        ok = action.value == case["expect"]
        total += 1
        passed += int(ok)
        details.append({"case": case["text"], "action": action.value,
                        "reason": reason, "expected": case["expect"],
                        "passed": ok})
    return {
        "version": fx.get("version"),
        "cases": total, "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "distinct_actions": len(actions),
        "actions": sorted(actions),
        "details": details,
    }


def run_social_decision_policy() -> dict:
    """should-reply 策略 Eval（文档 2.5.4/2.5.9）：打断成本 + 群聊隐私门。"""
    plugin = _load_plugin()
    fx = load_fixture("social_decision_policy_cases.json")
    store = plugin.group_policy
    total = passed = 0
    details = []
    for i, case in enumerate(fx["cases"]):
        setup = case.get("setup", {})
        gid = case.get("group", "g1")
        store.set(gid, mode=setup.get("mode", "normal"),
                  reply_rate=setup.get("reply_rate", 1.0),
                  meme_rate=setup.get("meme_rate", 1.0),
                  interruption_cost=setup.get("interruption_cost", 0.0))
        ev = _FakeEvent(case["text"], group=gid,
                        at=case.get("at", True),
                        session=case.get("session", f"pe{i}"))
        action, reason = plugin._social_decision(ev)
        ok = action.value == case["expect"]
        total += 1
        passed += int(ok)
        details.append({"case": case.get("name", case["text"]),
                        "action": action.value, "reason": reason,
                        "expected": case["expect"], "passed": ok})
    return {
        "version": fx.get("version"),
        "cases": total, "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "details": details,
    }


async def run_tool_runtime() -> dict:
    from dududa.core.capability import (
        Capability, CapabilityRegistry, CapabilityRisk, ProviderType,
        CapProvider, ToolObservation,
    )
    from dududa.core.memory import InMemoryRepository
    from dududa.core.state import RuntimeBudget, RunOutcome, RuntimeState
    from dududa.planner.executor import ToolExecutor, ExecutionContext
    from dududa.planner.planner import PlannedStep, GeneratedPlan
    from dududa.runtime.orchestrator import RuntimeOrchestrator

    def cap(cid, idempotent=False):
        return Capability(
            capability_id=cid, name=cid, description=f"Mock {cid}",
            provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY,
            required_permissions=(), idempotent=idempotent,
        )

    class OkProvider(CapProvider):
        def __init__(self):
            self.calls = []

        async def execute(self, c, args):
            self.calls.append(c.capability_id)
            return ToolObservation(step_id="s", capability_id=c.capability_id,
                                   success=True, data="stub")

        def health(self):
            return True

    class FlakyProvider(CapProvider):
        def __init__(self, fail_times):
            self.fail_times = fail_times
            self.calls = 0

        async def execute(self, c, args):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError("stub failure")
            return ToolObservation(step_id="s", capability_id=c.capability_id,
                                   success=True, data="ok")

        def health(self):
            return True

    fx = load_fixture("tool_runtime_cases.json")
    by_name = {c["name"]: c for c in fx["cases"]}

    hc = by_name["hard_cap"]
    reg = CapabilityRegistry()
    okp = OkProvider()
    for i in range(hc["plan_steps"]):
        reg.register(cap(f"cap{i}.tool"), okp)
    plan10 = GeneratedPlan(
        goal="t",
        steps=tuple(PlannedStep(f"s{i}", f"cap{i}.tool", {}, "p")
                    for i in range(hc["plan_steps"])),
    )
    results = await ToolExecutor(reg).execute_plan(
        plan10, ExecutionContext(max_steps=hc["max_steps"]))
    hard_cap_ok = (len(results) == hc["expect_executed"]
                   and len(okp.calls) <= hc["expect_executed"])

    ri = by_name["retry_idempotent"]
    reg2 = CapabilityRegistry()
    flaky = FlakyProvider(fail_times=ri["fail_times"])
    reg2.register(cap("retry.tool", idempotent=True), flaky)
    plan2 = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "retry.tool", {}, "p"),))
    await ToolExecutor(reg2).execute_plan(
        plan2, ExecutionContext(max_retries_per_step=ri["max_retries"]))
    retry_ok = flaky.calls == ri["expect_attempts"]

    rn = by_name["retry_non_idempotent"]
    reg3 = CapabilityRegistry()
    flaky3 = FlakyProvider(fail_times=rn["fail_times"])
    reg3.register(cap("flaky.tool", idempotent=False), flaky3)
    plan3 = GeneratedPlan(goal="t", steps=(PlannedStep("s1", "flaky.tool", {}, "p"),))
    await ToolExecutor(reg3).execute_plan(plan3)
    non_idem_ok = flaky3.calls == rn["expect_attempts"]

    orch = RuntimeOrchestrator(memory_repo=InMemoryRepository())
    state = RuntimeState(budget=RuntimeBudget(max_tool_steps=4,
                                              deadline_seconds=20))
    new_state = await orch._phase_tool_chain(state)
    degrade_ok = new_state.outcome == RunOutcome.DEGRADED

    return {
        "version": fx.get("version"),
        "hard_cap_ok": hard_cap_ok,
        "retry_ok": retry_ok,
        "non_idempotent_ok": non_idem_ok,
        "degrade_ok": degrade_ok,
        "executed_steps": len(results),
    }

class _NoopProvider:
    """Eval 用无副作用 Provider（检索只读，不执行）。"""

    async def execute(self, capability, arguments):
        return None

    def health(self):
        return True


def run_capability_retrieval() -> dict:
    """Capability Top-K 检索 Eval（文档 2.5.5/2.5.10 Phase 9）。

    指标：
    - recall_at_k：确定性意图命中下的 Top-K 召回率；
    - wrong_exposure_rate：越权/禁止副作用/不健康等不应出现的能力泄漏率；
    - arg_accuracy：ToolPlanValidator 对合法/缺参/未知/超预算计划的判定正确率。
    """
    from dududa.core.capability import (
        Capability, CapabilityRegistry, CapabilityQuery, CapabilityRisk,
        CapabilitySchema, ProviderType, ToolPlanValidator,
    )
    from dududa.core.state import RuntimeBudget
    from dududa.planner.planner import GeneratedPlan, PlannedStep

    fx = load_fixture("capability_retrieval_cases.json")

    # 1) Recall@K：intent token 进 description，确定性打分
    recall_scores = []
    for q in fx["recall_queries"]:
        reg = CapabilityRegistry()
        for c in q["capabilities"]:
            reg.register(
                Capability(
                    capability_id=c["id"], name=c.get("name", c["id"]),
                    description=c["description"], provider=ProviderType.BUILTIN,
                    risk=CapabilityRisk.READ_ONLY,
                ),
                _NoopProvider(),
            )
        candidates = reg.retrieve(
            CapabilityQuery(intent=q["intent"], top_k=q["top_k"]),
            permissions=(),
        )
        retrieved = {c.capability.capability_id for c in candidates}
        relevant = set(q["relevant"])
        recall = (len(retrieved & relevant) / len(relevant)) if relevant else 0.0
        recall_scores.append(recall)
    recall_at_k = round(sum(recall_scores) / len(recall_scores), 4)

    # 2) 错误能力暴露率：不应出现的能力（越权/副作用/不健康）不得泄漏
    leaks = total_checks = 0
    for case in fx["exposure_cases"]:
        reg = CapabilityRegistry()
        for c in case["capabilities"]:
            reg.register(
                Capability(
                    capability_id=c["id"], name=c.get("name", c["id"]),
                    description=c.get("description", c["id"]),
                    provider=ProviderType.BUILTIN,
                    risk=CapabilityRisk(c.get("risk", "read_only")),
                    required_permissions=tuple(c.get("required_permissions", ())),
                    side_effects=tuple(c.get("side_effects", ())),
                    health_check=(lambda: False) if c.get("unhealthy") else None,
                ),
                _NoopProvider(),
            )
        candidates = reg.retrieve(
            CapabilityQuery(
                intent=case.get("intent", ""),
                max_risk=CapabilityRisk(case.get("max_risk", "side_effect")),
                forbidden_side_effects=tuple(case.get("forbidden_side_effects", ())),
                top_k=case.get("top_k", 8),
            ),
            permissions=tuple(case.get("permissions", ())),
        )
        retrieved = {c.capability.capability_id for c in candidates}
        for bad in case["must_not_expose"]:
            total_checks += 1
            if bad in retrieved:
                leaks += 1
    wrong_exposure_rate = round(leaks / total_checks, 4) if total_checks else 0.0

    # 3) 参数正确率：ToolPlanValidator 判定正确率
    reg = CapabilityRegistry()
    schema = CapabilitySchema(input_schema={"required": ["action"]})
    reg.register(
        Capability(
            capability_id="mcp.course_schedule", name="课表",
            description="查询课程表", provider=ProviderType.MCP,
            risk=CapabilityRisk.READ_ONLY, schema=schema,
        ),
        _NoopProvider(),
    )
    validator = ToolPlanValidator(reg)
    correct = total = 0
    for case in fx["arg_cases"]:
        budget = RuntimeBudget(max_tool_steps=case.get("max_steps", 8))
        steps = tuple(
            PlannedStep(f"s{i}", case["capability"],
                        dict(case.get("args", {})), "p")
            for i in range(case.get("plan_steps", 1)))
        plan = GeneratedPlan(goal=case.get("name", "t"), steps=steps)
        valid, _ = validator.validate_plan(plan, budget)
        total += 1
        correct += int(valid == case["expect_valid"])
    arg_accuracy = round(correct / total, 4) if total else 0.0

    return {
        "version": fx.get("version"),
        "recall_at_k": recall_at_k,
        "wrong_exposure_rate": wrong_exposure_rate,
        "arg_accuracy": arg_accuracy,
        "recall_queries": len(recall_scores),
        "exposure_checks": total_checks,
        "arg_cases": total,
    }


def run_memory_writegate() -> dict:
    from dududa.core.memory import (
        InMemoryRepository, MemoryRecord, MemoryScope, MemoryType,
        MemoryCandidate, WriteGate, WriteGateDecision,
    )
    fx = load_fixture("memory_writegate_cases.json")
    repo = InMemoryRepository()
    gate = WriteGate(repo)
    total = passed = 0
    details = []
    for case in fx["cases"]:
        if case.get("pre_seed"):
            repo.write(MemoryRecord(
                scope=MemoryScope(memory_type=MemoryType.SHORT_TERM,
                                  platform="qq", bot_id="b",
                                  conversation_id="c1", actor_id="u1"),
                content=case["pre_seed"], source="message",
                evidence=("seed",),
            ))
        record = MemoryRecord(
            scope=MemoryScope(memory_type=MemoryType.SHORT_TERM,
                              platform="qq", bot_id="b",
                              conversation_id="c1", actor_id="u1"),
            content=case["content"],
            source=case.get("source", "message"),
            evidence=tuple(case.get("evidence", ())),
            ttl_seconds=case.get("ttl_seconds", 86400),
        )
        decision = gate.evaluate(MemoryCandidate(proposed_record=record))
        expect_allow = case["expect"] == "allow"
        ok = (decision == WriteGateDecision.ALLOW) == expect_allow
        total += 1
        passed += int(ok)
        details.append({"case": case.get("name", case["content"]),
                        "decision": decision.value,
                        "expected": case["expect"], "passed": ok})

    plugin = _load_plugin()
    ev = _FakeEvent("测试记忆", group="g1", user="u1")
    plugin._store_memory(ev, fx["integration"]["restricted"])
    plugin._store_memory(ev, fx["integration"]["normal"])
    scope = plugin._make_scope(ev)
    records = plugin.memory.query(scope, limit=50)
    blocked = not any("sk-" in r.content for r in records)
    stored = any(r.content == fx["integration"]["normal"] for r in records)
    return {
        "version": fx.get("version"),
        "cases": total, "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "integration_restricted_blocked": blocked,
        "integration_normal_stored": stored,
        "details": details,
    }


def run_oc_render() -> dict:
    from dududa.core.renderer import OCRenderer, DraftResponse, FactAnchor
    fx = load_fixture("oc_render_cases.json")
    renderer = OCRenderer()
    total = passed = 0
    details = []
    for case in fx["cases"]:
        draft = DraftResponse(
            text=case["text"],
            fact_anchors=tuple(
                FactAnchor(field=a["field"], value=a["value"],
                           source=a.get("source", ""))
                for a in case["anchors"]),
        )
        final = renderer.render(draft)
        retained = all(a["value"] in final.text for a in case["anchors"])
        total += 1
        passed += int(retained)
        details.append({"case": case["text"], "retained": retained})
    return {
        "version": fx.get("version"),
        "cases": total, "passed": passed,
        "anchor_retention": round(passed / total, 4) if total else 0.0,
        "details": details,
    }


async def run_all() -> dict:
    return {
        "perception": run_perception(),
        "social_decision": run_social_decision(),
        "social_decision_policy": run_social_decision_policy(),
        "tool_runtime": await run_tool_runtime(),
        "memory_writegate": run_memory_writegate(),
        "oc_render": run_oc_render(),
    }
