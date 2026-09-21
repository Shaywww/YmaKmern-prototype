"""Integration with RuntimeOrchestrator - hooks into TOOLS_PLANNED/TOOLS_EXECUTED/VALIDATED_TOOLS phases."""
from __future__ import annotations
from typing import Any, Optional
from .planner import ToolPlanner, PlanningContext
from .executor import ToolExecutor, ExecutionContext
from .recovery import ErrorRecovery, ErrorContext, RecoveryAction

def integrate_with_orchestrator(orchestrator, capability_registry=None):
    """Replace orchestrator stub tool chain with real Planner/Executor.
    Call this before running the orchestrator to enable real tool execution.
    """
    planner = ToolPlanner()
    executor = ToolExecutor(capability_registry)
    recovery = ErrorRecovery()

    # Register common intent patterns
    # 日期/时间能力。
    planner.register_pattern(
        ("几点", "时间", "几号", "星期几", "日期", "什么时候了", "现在是", "现在几"),
        {"name": "time_lookup", "goal": "Get current date and time",
         "steps": [{"step_id": "s1", "capability_id": "mcp.clock",
                     "arguments": {"action": "get_now"},
                     "purpose": "Get current local time",
                     "expected_output": "Current date/time"}]},
    )

    # 天气（mcp.weather）：城市由生产 _enrich_plan_args 提取，默认合肥
    planner.register_pattern(
        ("天气", "气温", "温度", "下雨", "下雪", "多云", "晴", "预报",
         "冷不冷", "热不热", "weather", "forecast"),
        {"name": "weather_lookup", "goal": "Get current weather and 3-day forecast for a city",
         "steps": [{"step_id": "s1", "capability_id": "mcp.weather",
                     "arguments": {"action": "search", "q": "{query}"},
                     "purpose": "Get weather",
                     "expected_output": "Current weather and forecast"}]},
    )
    planner.register_pattern(
        ("新闻", "资讯", "热点", "热搜", "报道", "消息"),
        {"name": "news_lookup", "goal": "Get latest news aggregation",
         "steps": [{"step_id": "s1", "capability_id": "mcp.news",
                     "arguments": {"action": "search", "q": "{query}"},
                     "purpose": "Get latest news",
                     "expected_output": "Recent news items with titles and links"}]},
    )
    planner.register_pattern(
        ("翻译", "译成", "translate"),
        {"name": "translate_lookup", "goal": "Translate text between Chinese and English",
         "steps": [{"step_id": "s1", "capability_id": "mcp.translate",
                     "arguments": {"action": "search", "text": "{query}"},
                     "purpose": "Translate text",
                     "expected_output": "Translation result"}]},
    )
    # 百科/名词查询 -> 联网搜索
    planner.register_pattern(
        ("百科", "是什么", "什么是", "啥是", "啥叫"),
        {"name": "definition_lookup", "goal": "Look up facts about a noun or topic",
         "steps": [{"step_id": "s1", "capability_id": "mcp.web_search",
                     "arguments": {"action": "search", "q": "{query}"},
                     "purpose": "Search the web for facts",
                     "expected_output": "Top ranked web results with titles and snippets"}]},
    )
    # 通用联网搜索放在专用能力之后注册。一个请求同时含「查」和
    # 「天气/时间/新闻」时，专用能力优先；其余查询落到网页搜索。
    planner.register_pattern(
        ("搜", "搜索", "百度", "查", "查询", "找", "search", "find"),
        {"name": "web_search", "goal": "Search the web for the requested topic",
         "steps": [{"step_id": "s1", "capability_id": "mcp.web_search",
                    "arguments": {"action": "search", "q": "{query}"},
                    "purpose": "Search the web",
                    "expected_output": "Top ranked web results with titles, links and snippets"}]},
    )

    return ToolChainIntegration(planner, executor, recovery, capability_registry)

class ToolChainIntegration:
    """Wraps Planner/Executor/Recovery into a cohesive tool chain."""

    def __init__(self, planner: ToolPlanner, executor: ToolExecutor, recovery: ErrorRecovery, registry=None):
        self.planner = planner
        self.executor = executor
        self.recovery = recovery
        self.registry = registry

    async def plan_and_execute(self, user_intent: str, perception: Any,
                                candidates: tuple, permissions: tuple,
                                budget: Any) -> dict:
        context = PlanningContext(
            user_intent=user_intent,
            available_capabilities=candidates,
            max_steps=min(budget.max_tool_steps, 4),
            permissions=permissions,
        )

        # Phase 1: Plan
        plan = self.planner.plan(context)

        # Phase 2: Execute
        exec_ctx = ExecutionContext(
            max_steps=budget.max_tool_steps,
            max_retries_per_step=budget.max_tool_retries if hasattr(budget, 'max_tool_retries') else 2,
            deadline_seconds=budget.deadline_seconds if hasattr(budget, 'deadline_seconds') else 30.0,
            # Doc 2.4.12: executor re-checks latest permissions/actor/scope per step
            permissions=permissions,
            actor=getattr(perception, "actor_id", "") if perception else "",
            conversation_scope=getattr(perception, "conversation_id", "") if perception else "",
        )
        results = await self.executor.execute_plan(plan, exec_ctx)

        # Phase 3: Recovery for failed steps
        recovery_results = []
        for r in results:
            if not r.success:
                err_ctx = ErrorContext(
                    step_id=r.step_id, capability_id="",
                    error_message=r.error or "unknown",
                    error_type=self.recovery.classify_error(r.error or ""),
                    retries_used=r.retries_used,
                )
                decision = self.recovery.decide(err_ctx)
                recovery_results.append({"step_id": r.step_id, "decision": decision.action.value, "reason": decision.reason})
            else:
                recovery_results.append({"step_id": r.step_id, "decision": "completed", "reason": "success"})

        return {
            "plan": {"goal": plan.goal, "steps": len(plan.steps), "rationale": plan.rationale},
            "results": [{"step_id": r.step_id, "success": r.success, "latency_ms": r.latency_ms} for r in results],
            "recovery": recovery_results,
            "all_success": all(r.success for r in results),
            "success_count": sum(1 for r in results if r.success),
            "total_steps": len(results),
        }
