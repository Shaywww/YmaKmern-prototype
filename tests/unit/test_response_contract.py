"""CHAT/TOOL_ANSWER response contract regression tests."""

import pytest

from dududa.core.renderer import (
    DraftResponse, FinalResponse, ResponseKind,
)
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
