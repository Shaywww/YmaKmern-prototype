import pytest

from dududa.core.quality_eval import LLMPersonaJudge, persona_contract_violations


def test_persona_floor_detects_customer_tone_and_colored_emoji():
    violations = persona_contract_violations(
        "你好！有什么我可以帮你的吗？😊")
    assert "customer_template" in violations
    assert "unicode_emoji" in violations
    assert not persona_contract_violations("行吧，勉强帮你看看 (≧▽≦)")


def test_persona_floor_detects_echoed_self_abuse():
    violations = persona_contract_violations(
        "行行行，我是二逼，你说了算。")
    assert "self_degrading_abuse" in violations
    assert "self_degrading_abuse" not in persona_contract_violations(
        "这次是我没接住，你说的是兰州。")


@pytest.mark.asyncio
async def test_llm_persona_judge_uses_strict_structured_output():
    calls = []

    async def complete(system, user):
        calls.append((system, user))
        return (
            '{"persona_consistency":0.9,"conversationality":0.8,'
            '"non_customer_tone":1.0,"rationale":"自然"}'
        )

    result = await LLMPersonaJudge(complete).evaluate("你好", "来啦，什么事？")
    assert result.overall == 0.9
    assert calls and "不可信数据" not in calls[0][1]


@pytest.mark.asyncio
async def test_llm_persona_judge_fails_closed_on_bad_schema():
    async def complete(system, user):
        return '{"score":1}'

    with pytest.raises(ValueError, match="schema mismatch"):
        await LLMPersonaJudge(complete).evaluate("x", "y")
