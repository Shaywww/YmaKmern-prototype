from types import SimpleNamespace

import pytest

from dududa.application.dududa_prod import _ProdOrchestrator
from dududa.core.memory import (
    InMemoryRepository, MemoryRecord, MemoryScope, MemoryType,
)
from dududa.core.persona.prompt_policy import build_user_visible_system_prompt
from dududa.core.response_policy import (
    ContinuationValue,
    Emotion,
    EmotionIntensity,
    Familiarity,
    InteractionPolicyResolver,
    InteractionSignals,
    OutputStylePolicyResolver,
    ResolvedResponsePolicy,
    ResponseOrigin,
    RiskLevel,
    SafetyDecision,
    Scene,
    StyleSignals,
)


def _banter_policy():
    style = OutputStylePolicyResolver.resolve(StyleSignals(
        scene=Scene.PLAYFUL_BANTER,
        emotion=Emotion.NEUTRAL,
        emotion_intensity=EmotionIntensity.MILD,
        risk_level=RiskLevel.LOW,
        familiarity=Familiarity.FAMILIAR,
        response_origin=ResponseOrigin.TEXT,
        perception_confidence=0.95,
    ))
    interaction = InteractionPolicyResolver.resolve(
        InteractionSignals(continuation_value=ContinuationValue.NONE),
        SafetyDecision(risk_level=RiskLevel.LOW),
    )
    return ResolvedResponsePolicy(interaction, style)


def test_recent_repeated_openers_and_ai_labels_create_aggregate_guidance():
    orchestrator = object.__new__(_ProdOrchestrator)
    orchestrator._recent_bot_utterances = lambda state, limit=3: (
        "哎哟，我这嘴欠的AI可不敢。",
        "哈哈，我这个讨饭的AI记住了。",
        "哈哈，没钱包的AI只能讲笑话。",
    )
    lines = orchestrator._rhythm_persona_lines(SimpleNamespace())
    assert any("不再用这类开头" in line for line in lines)
    assert any("不要再提 AI" in line for line in lines)


def test_recent_bot_rhythm_never_crosses_conversations():
    repo = InMemoryRepository()
    for conversation, content in (
        ("group-a", "哈哈，group-a"),
        ("group-b", "哎哟，group-b"),
    ):
        repo.write(MemoryRecord(
            scope=MemoryScope(
                memory_type=MemoryType.BOT_UTTERANCE,
                platform="qq",
                bot_id="bot",
                conversation_id=conversation,
                actor_id="member",
                persona_id="default",
            ),
            content=content,
        ))
    plugin = SimpleNamespace(
        memory=repo,
        _make_scope=lambda event, msg_type="bot": MemoryScope(
            memory_type=MemoryType.BOT_UTTERANCE,
            platform="qq",
            bot_id="bot",
            conversation_id="group-a",
            actor_id="current-member",
            persona_id="default",
        ),
    )
    orchestrator = object.__new__(_ProdOrchestrator)
    orchestrator._plugin = plugin
    orchestrator._pending_event = object()
    assert orchestrator._recent_bot_utterances(
        SimpleNamespace(), limit=3) == ("哈哈，group-a",)


def test_rhythm_guidance_does_not_include_raw_previous_utterances():
    orchestrator = object.__new__(_ProdOrchestrator)
    secret_phrase = "哈哈，private-marker-123 的AI"
    orchestrator._recent_bot_utterances = lambda state, limit=3: (
        secret_phrase, secret_phrase, secret_phrase)
    lines = orchestrator._rhythm_persona_lines(SimpleNamespace())
    prompt = build_user_visible_system_prompt(
        _banter_policy(),
        scene=Scene.PLAYFUL_BANTER,
        dynamic_style_rules=lines,
    )
    assert "private-marker-123" not in prompt
    assert "不要再提 AI" in prompt


@pytest.mark.asyncio
async def test_overlong_banter_gets_one_bounded_compression_attempt():
    class Plugin:
        def __init__(self):
            self.calls = 0

        async def _call_llm(self, system, user_msg, **kwargs):
            self.calls += 1
            assert "不超过 48" in system
            return "这帽子扣得挺顺手。"

    orchestrator = object.__new__(_ProdOrchestrator)
    orchestrator._plugin = Plugin()
    state = SimpleNamespace(run_id="run-1", trace_id="trace-1")
    result = await orchestrator._compress_low_effort_reply(
        state, "这是一段明显超过预算而且铺垫非常多的完整小作文。" * 3, 48)
    assert result == "这帽子扣得挺顺手。"
    assert orchestrator._plugin.calls == 1


def test_deterministic_budget_clip_is_stable():
    source = "第一句已经够用了。后面全是重复铺垫和解释。"
    first = _ProdOrchestrator._clip_to_char_budget(source, 12)
    second = _ProdOrchestrator._clip_to_char_budget(first, 12)
    assert first == "第一句已经够用了。"
    assert second == first
