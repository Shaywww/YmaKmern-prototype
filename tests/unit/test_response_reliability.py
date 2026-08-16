# -*- coding: utf-8 -*-
"""Regression tests for truthful replies when tools fail."""
from types import SimpleNamespace

import pytest

from dududa.application.dududa_prod import _ProdOrchestrator
from dududa.core.capability import CapabilityRegistry, ToolObservation
from dududa.core.state import RuntimeState
from dududa.mcp.registry import register_all_mcp_services


def _orchestrator(text: str):
    orch = object.__new__(_ProdOrchestrator)
    orch._pending_event = object()
    orch._plugin = SimpleNamespace(
        input_adapter=SimpleNamespace(
            to_preprocessed=lambda _event: SimpleNamespace(combined_text=text)))
    return orch


def test_web_search_declares_query_schema():
    registry = CapabilityRegistry()
    register_all_mcp_services(registry)
    schema = registry.get("mcp.web_search").schema.input_schema
    assert "q" in schema["properties"]
    assert "q" in schema["required"]


def test_empty_web_search_query_is_filled_from_user_text():
    from dududa.planner.planner import GeneratedPlan, PlannedStep

    registry = CapabilityRegistry()
    register_all_mcp_services(registry)
    plan = GeneratedPlan(
        goal="hotel",
        steps=(PlannedStep(
            step_id="s1", capability_id="mcp.web_search",
            arguments={"action": "search", "q": ""}, purpose="p"),))
    out = _ProdOrchestrator._ensure_step_args(
        plan, "兰州盛达希尔顿酒店怎么样",
        registry.filter_candidates(permissions=(), max_count=24))
    assert out.steps[0].arguments["q"] == "兰州盛达希尔顿酒店怎么样"


@pytest.mark.asyncio
async def test_failed_tool_query_never_falls_back_to_guessing():
    state = RuntimeState(
        tool_plan=SimpleNamespace(steps=(SimpleNamespace(
            capability_id="mcp.web_search"),)),
        tool_observations=(ToolObservation(
            step_id="s1", capability_id="mcp.web_search",
            success=False, error="empty query"),),
    )
    reply = await _orchestrator(
        "你觉得兰州盛达希尔顿酒店怎么样")._compose_prod_text(state)
    assert "没有拿到可靠结果" in reply
    assert "不乱猜" in reply


@pytest.mark.asyncio
async def test_identity_question_gets_concrete_answer_without_llm():
    reply = await _orchestrator("你是谁啊")._compose_prod_text(RuntimeState())
    assert "运行在 QQ 里的 AI 群友" in reply
    assert "不会装作知道" in reply
