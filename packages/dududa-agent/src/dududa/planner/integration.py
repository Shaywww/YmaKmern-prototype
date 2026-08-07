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
    planner.register_pattern(
        ("查课", "课程查询", "课程信息", "什么课", "课程评价"),
        {"name": "course_lookup", "goal": "Find course information",
         "steps": [{"step_id": "s1", "capability_id": "mcp.course_schedule",
                     "arguments": {"action": "search"}, "purpose": "Search course database",
                     "expected_output": "Course details"}]},
    )
    planner.register_pattern(
        ("考试", "期末", "期中", "exam", "什么时候考"),
        {"name": "exam_lookup", "goal": "Find exam information",
         "steps": [{"step_id": "s1", "capability_id": "mcp.exam_schedule",
                     "arguments": {"action": "get_all_exams"}, "purpose": "Get exam schedule",
                     "expected_output": "Exam timetable"}]},
    )
    planner.register_pattern(
        ("课表", "schedule", "课程表", "什么课"),
        {"name": "schedule_lookup", "goal": "Get course schedule",
         "steps": [{"step_id": "s1", "capability_id": "mcp.course_schedule",
                     "arguments": {"action": "get_personal_schedule"}, "purpose": "Get personal schedule"}]},
    )
    planner.register_pattern(
        ("选课", "培养方案", "学分", "毕业要求"),
        {"name": "program_lookup", "goal": "Check degree requirements",
         "steps": [{"step_id": "s1", "capability_id": "mcp.training_program",
                     "arguments": {"action": "get_program"}, "purpose": "Get training program"}]},
    )
    planner.register_pattern(
        ("活动", "讲座", "竞赛", "第二课堂", "社团"),
        {"name": "activity_lookup", "goal": "Find campus activities",
         "steps": [{"step_id": "s1", "capability_id": "mcp.second_classroom",
                     "arguments": {"action": "search"}, "purpose": "Search activities"}]},
    )
    planner.register_pattern(
        ("通知", "公告", "news", "notice"),
        {"name": "notice_lookup", "goal": "Find campus notices",
         "steps": [{"step_id": "s1", "capability_id": "mcp.campus_notice",
                     "arguments": {"action": "search"}, "purpose": "Search notices"}]},
    )

    # Complex multi-step patterns
    from .planner import COMPLEX_PATTERNS
    planner.register_pattern(
        ("对比", "比较", "哪个好", "选哪个", "区别", "vs"),
        COMPLEX_PATTERNS["course_compare"],
    )
    planner.register_pattern(
        ("综合查询", "全查", "都查", "各方面", "相关信息"),
        COMPLEX_PATTERNS["multi_source_lookup"],
    )
    planner.register_pattern(
        ("考试时间", "期末安排", "考试安排", "考表"),
        COMPLEX_PATTERNS["course_with_exam"],
    )
    planner.register_pattern(
        ("学期规划", "学期安排", "这学期", "下学期", "新学期"),
        COMPLEX_PATTERNS["semester_planning"],
    )
    planner.register_pattern(
        ("毕业", "学分够了", "还差多少", "培养方案完成", "毕业要求"),
        COMPLEX_PATTERNS["program_check"],
    )

    planner.register_pattern(
        ("成绩", "分数", "绩点"),
        {"name": "grade_lookup", "goal": "Get student grades",
         "steps": [{"step_id": "s1", "capability_id": "mcp.academic_affairs",
                     "arguments": {"action": "get_grade"}, "purpose": "Get grades"}]},
    )
    planner.register_pattern(
        ("放假", "校历", "节假日", "什么时候放"),
        {"name": "holiday_lookup", "goal": "Get academic calendar holidays",
         "steps": [{"step_id": "s1", "capability_id": "mcp.academic_calendar",
                     "arguments": {"action": "get_holidays"}, "purpose": "Get holidays"}]},
    )

    # 日期/时间（文档 2.5.x 时钟能力）：注册在最后，考试/课表等模式优先
    planner.register_pattern(
        ("几点", "时间", "几号", "星期几", "日期", "什么时候了", "现在是", "现在几"),
        {"name": "time_lookup", "goal": "Get current date and time",
         "steps": [{"step_id": "s1", "capability_id": "mcp.clock",
                     "arguments": {"action": "get_now"},
                     "purpose": "Get current local time",
                     "expected_output": "Current date/time"}]},
    )

    # 联网搜索（mcp.web_search）：通用「搜/查/找」命令；q 由 {query} 占位符填充。
    # 注册在最后：课表/考试/时间等专属模式优先于通用搜索。
    planner.register_pattern(
        ("搜", "百度", "百度一下", "search", "find"),
        {"name": "web_search", "goal": "Search the web for the requested topic",
         "steps": [{"step_id": "s1", "capability_id": "mcp.web_search",
                    "arguments": {"action": "search", "q": "{query}"},
                    "purpose": "Search the web",
                    "expected_output": "Top ranked web results with titles, links and snippets"}]},
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
    # 百科/名词查询（招生/录取/是什么…）-> 联网搜索
    planner.register_pattern(
        ("招生", "录取", "百科", "是什么", "什么是", "啥是", "啥叫"),
        {"name": "definition_lookup", "goal": "Look up facts about a noun or topic",
         "steps": [{"step_id": "s1", "capability_id": "mcp.web_search",
                     "arguments": {"action": "search", "q": "{query}"},
                     "purpose": "Search the web for facts",
                     "expected_output": "Top ranked web results with titles and snippets"}]},
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
