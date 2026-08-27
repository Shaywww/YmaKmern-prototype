"""Smart ToolPlanner - intent analysis to ToolPlan generation."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

import re as _re

_COMMAND_STRIP = _re.compile(
    r"^(?:帮我|请你|麻烦你|麻烦|给我|帮我一下|帮我查|你好|嗨|嘿|bot|机器人)")
_ACTION_STRIP = _re.compile(
    r"^(?:搜索一下|搜一搜|搜一下|搜索|搜|查找一下|查一下|查一查|查查|查找|查|"
    r"百度一下|百度|找一下|找找|找|看看)")
_TRAIL_STRIP = _re.compile(r"(?:一下|一下下|看看|一查|一找|呗|吧|呀|啊|哦|呢|嘛|的|什么)$")


def _clean_query(text: str) -> str:
    """从自然语言命令中提取搜索词（@/命令前缀/语气词全剔除）。"""
    q = _re.sub(r"@\S+", " ", text or "")
    q = _COMMAND_STRIP.sub("", q.strip()).strip()
    q = _ACTION_STRIP.sub("", q.strip()).strip()
    q = _TRAIL_STRIP.sub("", q.strip()).strip()
    return q or (text or "").strip()


@dataclass
class PlanningContext:
    user_intent: str = ""
    perception_summary: str = ""
    available_capabilities: tuple = ()
    max_steps: int = 4
    permissions: tuple = ()
    conversation_context: str = ""

@dataclass
class PlannedStep:
    step_id: str
    capability_id: str
    arguments: dict[str, Any]
    purpose: str
    depends_on: tuple[str, ...] = ()
    expected_output: str = ""
    completion_criteria: tuple[str, ...] = ()
    priority: int = 0  # Lower = higher priority | 0 = highest
    estimated_tokens: int = 100
    is_critical: bool = False  # If this fails, the whole plan fails

@dataclass
class GeneratedPlan:
    goal: str
    steps: tuple[PlannedStep, ...]
    schema_version: str = "1.0"
    plan_id: str = field(default_factory=lambda: uuid4().hex)
    fallback_steps: tuple[PlannedStep, ...] = ()  # Degraded path
    rationale: str = ""


# ===== Complex multi-step patterns =====
COMPLEX_PATTERNS = {
    "course_compare": {
        "name": "course_compare",
        "goal": "Compare two or more courses",
        "steps": [
            {"step_id": "s1", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Find first course"},
            {"step_id": "s2", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Find second course", "depends_on": []},
            {"step_id": "s3", "capability_id": "mcp.exam_schedule", "arguments": {"action": "get_all_exams"}, "purpose": "Get exam info", "depends_on": []},
        ],
    },
    "multi_source_lookup": {
        "name": "multi_source_lookup",
        "goal": "Look up information from multiple sources",
        "steps": [
            {"step_id": "s1", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Search courses"},
            {"step_id": "s2", "capability_id": "mcp.second_classroom", "arguments": {"action": "search"}, "purpose": "Search related activities", "depends_on": []},
            {"step_id": "s3", "capability_id": "mcp.campus_notice", "arguments": {"action": "search"}, "purpose": "Check related notices", "depends_on": []},
        ],
    },
    "course_with_exam": {
        "name": "course_with_exam",
        "goal": "Find course details and exam schedule",
        "steps": [
            {"step_id": "s1", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Get course info"},
            {"step_id": "s2", "capability_id": "mcp.exam_schedule", "arguments": {"action": "get_all_exams"}, "purpose": "Get exam schedule", "depends_on": ["s1"]},
        ],
    },
    "semester_planning": {
        "name": "semester_planning",
        "goal": "Plan semester: calendar + courses + activities",
        "steps": [
            {"step_id": "s1", "capability_id": "mcp.academic_calendar", "arguments": {"action": "get_semester"}, "purpose": "Get semester dates"},
            {"step_id": "s2", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Search public semester offerings", "depends_on": []},
            {"step_id": "s3", "capability_id": "mcp.second_classroom", "arguments": {"action": "get_upcoming"}, "purpose": "Get upcoming activities", "depends_on": []},
            {"step_id": "s4", "capability_id": "mcp.academic_calendar", "arguments": {"action": "get_holidays"}, "purpose": "Get holidays", "depends_on": []},
        ],
    },
    "program_check": {
        "name": "program_check",
        "goal": "Check degree progress against program requirements",
        "steps": [
            {"step_id": "s1", "capability_id": "mcp.training_program", "arguments": {"action": "get_program"}, "purpose": "Get degree requirements"},
            {"step_id": "s2", "capability_id": "mcp.course_schedule", "arguments": {"action": "search"}, "purpose": "Search public course offerings", "depends_on": []},
        ],
    },
}

class ToolPlanner:
    """Generates multi-step ToolPlans from user intent and available capabilities."""

    def __init__(self):
        self._intent_patterns: dict[str, list[dict]] = {}

    def register_pattern(self, intent_keywords: tuple[str, ...], pattern: dict):
        """Register a planning pattern for specific intents."""
        for kw in intent_keywords:
            self._intent_patterns.setdefault(kw, []).append(pattern)

    def plan(self, context: PlanningContext) -> GeneratedPlan:
        capabilities = {c.capability.capability_id: c for c in context.available_capabilities}
        intent = context.user_intent.lower()

        # 1. Intent classification
        intent_type = self._classify_intent(intent)
        patterns = self._find_patterns(intent)

        # 2. Pattern-based planning
        if patterns:
            return self._plan_from_patterns(patterns, capabilities, context)

        # 3. Heuristic planning
        return self._plan_heuristic(intent_type, capabilities, context)

    def _classify_intent(self, intent: str) -> str:
        if any(w in intent for w in ("查", "搜", "找", "search", "find", "lookup")):
            return "lookup"
        if any(w in intent for w in ("比较", "对比", "compare", "vs", "哪个")):
            return "compare"
        if any(w in intent for w in ("总结", "汇总", "summarize", "概括")):
            return "summarize"
        if any(w in intent for w in ("计算", "算", "calculate", "compute")):
            return "compute"
        if any(w in intent for w in ("提醒", "remind", "通知", "定时")):
            return "schedule"
        return "general"

    def _find_patterns(self, intent: str) -> list[dict]:
        patterns = []
        for kw, pats in self._intent_patterns.items():
            if kw in intent:
                patterns.extend(pats)
        return patterns

    def _plan_from_patterns(self, patterns: list[dict], capabilities: dict, context: PlanningContext) -> GeneratedPlan:
        best_pattern = patterns[0] if patterns else {}
        steps = []
        for sp in best_pattern.get("steps", []):
            cap_id = sp.get("capability_id", "")
            if cap_id in capabilities or not sp.get("required", True):
                steps.append(PlannedStep(
                    step_id=sp.get("step_id", uuid4().hex[:8]),
                    capability_id=cap_id,
                    arguments={
                        k: (v.replace("{query}", _clean_query(context.user_intent))
                            if isinstance(v, str) and "{query}" in v else v)
                        for k, v in (sp.get("arguments", {}) or {}).items()
                    },
                    purpose=sp.get("purpose", ""),
                    depends_on=tuple(sp.get("depends_on", [])),
                    expected_output=sp.get("expected_output", ""),
                ))
        steps = self._resolve_dependencies(steps)
        return GeneratedPlan(
            goal=best_pattern.get("goal", context.user_intent),
            steps=tuple(steps),
            rationale=f"Pattern: {best_pattern.get('name', 'custom')}",
        )

    def _plan_heuristic(self, intent_type: str, capabilities: dict, context: PlanningContext) -> GeneratedPlan:
        caps = list(capabilities.values())
        steps = []

        # Check for complex patterns first
        intent = context.user_intent.lower()
        if any(w in intent for w in ("比较", "对比", "哪个好", "选哪个", "vs")):
            # Multi-step: compare two items
            items = self._extract_comparison_items(context.user_intent)
            for i, item in enumerate(items[:2]):
                best = self._pick_best_capability(caps, item)
                if best:
                    steps.append(PlannedStep(
                        step_id=f"s{i+1}", capability_id=best.capability.capability_id,
                        arguments={"keyword": item, "action": "search"},
                        purpose=f"Look up {item}",
                    ))
            # Add third step for exam/calendar context
            for cap_id in ("mcp.exam_schedule", "mcp.academic_calendar"):
                if cap_id in capabilities:
                    steps.append(PlannedStep(
                        step_id=f"s{len(steps)+1}", capability_id=cap_id,
                        arguments={"action": "get_all_exams"} if "exam" in cap_id else {"action": "get_semester"},
                        purpose="Get context info",
                    ))
                    break

        elif intent_type == "lookup":
            # Simple lookup: 1 step
            best = self._pick_best_capability(caps, context.user_intent)
            if best:
                steps.append(PlannedStep(
                    step_id="s1", capability_id=best.capability.capability_id,
                    arguments={"keyword": context.user_intent, "action": "search"},
                    purpose=f"Search for {context.user_intent}",
                ))

        elif intent_type == "compare":
            # Compare: find both then compare
            items = self._extract_comparison_items(context.user_intent)
            for i, item in enumerate(items[:2]):
                best = self._pick_best_capability(caps, item)
                if best:
                    steps.append(PlannedStep(
                        step_id=f"s{i+1}", capability_id=best.capability.capability_id,
                        arguments={"keyword": item, "action": "search"},
                        purpose=f"Look up {item}",
                    ))

        elif intent_type == "summarize":
            # Summarize: gather data then summarize
            best = self._pick_best_capability(caps, context.user_intent)
            if best:
                steps.append(PlannedStep(
                    step_id="s1", capability_id=best.capability.capability_id,
                    arguments={"keyword": context.user_intent, "action": "search"},
                    purpose=f"Gather data about {context.user_intent}",
                ))

        steps = self._resolve_dependencies(steps)
        return GeneratedPlan(
            goal=context.user_intent,
            steps=tuple(steps[:context.max_steps]),
            rationale=f"Heuristic: {intent_type}",
        )

    def _pick_best_capability(self, caps: list, intent: str) -> Optional[Any]:
        if not caps:
            return None
        scored = []
        for c in caps:
            score = 0
            desc = (c.capability.description + " " + c.capability.name).lower()
            for w in intent.lower().split():
                if w in desc:
                    score += 1
            if c.capability.risk == "read_only":
                score += 2
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return scored[0][1] if scored else None

    def _extract_comparison_items(self, intent: str) -> list[str]:
        for sep in ("和", "与", "vs", "对比", "比较"):
            if sep in intent:
                parts = intent.split(sep, 1)
                return [p.strip() for p in parts if p.strip()]
        return [intent]

    def _resolve_dependencies(self, steps: list[PlannedStep]) -> list[PlannedStep]:
        step_ids = {s.step_id for s in steps}
        resolved = []
        for s in steps:
            valid_deps = tuple(d for d in s.depends_on if d in step_ids)
            resolved.append(PlannedStep(
                step_id=s.step_id, capability_id=s.capability_id,
                arguments=s.arguments, purpose=s.purpose,
                depends_on=valid_deps, expected_output=s.expected_output,
                completion_criteria=s.completion_criteria,
                priority=s.priority, estimated_tokens=s.estimated_tokens,
                is_critical=s.is_critical,
            ))
        return resolved
