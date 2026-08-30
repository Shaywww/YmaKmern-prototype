"""CHAT/TOOL_ANSWER response contract regression tests."""

import pytest

from dududa.core.renderer import (
    DraftResponse, FinalResponse, ResponseKind, extract_atomic_facts,
)
from dududa.core.response_contract import validate_response_contract
from dududa.core.state import RuntimePhase, RuntimeState
from dududa.runtime.orchestrator import RuntimeOrchestrator


class _TrackingRenderer:
    def __init__(self):
        self.render_calls = 0
        self.hybrid_calls = 0

    def render(self, draft):
        self.render_calls += 1
        return FinalResponse(text=draft.text, fact_check_passed=True)

    async def render_hybrid(self, draft, **kwargs):
        self.hybrid_calls += 1
        return FinalResponse(text=draft.text, fact_check_passed=True)


@pytest.mark.asyncio
async def test_chat_skips_second_llm_render():
    renderer = _TrackingRenderer()
    orchestrator = RuntimeOrchestrator(renderer=renderer)
    state = RuntimeState(
        phase=RuntimePhase.COMPOSED,
        draft_response=DraftResponse(
            text="已经是人格化闲聊回复",
            kind=ResponseKind.CHAT,
        ),
    )
    rendered = await orchestrator._phase_render(state)
    assert rendered.final_response.text == "已经是人格化闲聊回复"
    assert renderer.render_calls == 1
    assert renderer.hybrid_calls == 0


@pytest.mark.asyncio
async def test_tool_answer_uses_guarded_hybrid_render():
    renderer = _TrackingRenderer()
    orchestrator = RuntimeOrchestrator(renderer=renderer)
    state = RuntimeState(
        phase=RuntimePhase.COMPOSED,
        draft_response=DraftResponse(
            text="评分 8.6 分",
            kind=ResponseKind.TOOL_ANSWER,
        ),
    )
    rendered = await orchestrator._phase_render(state)
    assert rendered.final_response.text == "评分 8.6 分"
    assert renderer.render_calls == 0
    assert renderer.hybrid_calls == 1


def test_unified_contract_blocks_progress_customer_tone_and_internal_leaks():
    progress = validate_response_contract(
        "正在查询，请稍等", kind=ResponseKind.TOOL_ANSWER,
        has_tool_data=True)
    assert "progress_placeholder" in progress.violations

    customer = validate_response_contract("你好！有什么我可以帮你的吗？")
    assert "customer_template" in customer.violations

    leaked = validate_response_contract(
        "工具状态: None，来自 mcp.weather",
        kind=ResponseKind.TOOL_ANSWER, has_tool_data=True)
    assert "internal_tool_leak" in leaked.violations


def test_unified_contract_enforces_semantic_numeric_grounding():
    facts = extract_atomic_facts({"temp_c": 24, "humidity": 60})
    good = validate_response_contract(
        "现在24℃，湿度60%。", kind=ResponseKind.TOOL_ANSWER,
        facts=facts, has_tool_data=True)
    assert good.passed
    bad = validate_response_contract(
        "现在31℃，湿度85%。", kind=ResponseKind.TOOL_ANSWER,
        facts=facts, has_tool_data=True)
    assert "unsupported_numeric_claim" in bad.violations
    assert set(bad.unsupported_claims) == {"31℃", "85%"}
