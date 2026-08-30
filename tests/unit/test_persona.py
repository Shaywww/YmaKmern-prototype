"""Test OC Persona System."""
import sys
import pytest
from dududa.core.persona.templates import (
    FormalityLevel, PlayfulnessLevel, EmojiStyle,
    PersonaTraits, ToneConfig, PersonaTemplate, PRESETS,
)
from dududa.core.persona.registry import PersonaRegistry
from dududa.core.persona.emoji_strategy import EmojiStrategy
from dududa.core.persona.expressions import ExpressionLibrary
from dududa.core.persona.persona_renderer import PersonaRenderer


class TestPersonaTraits:
    def test_defaults(self):
        t = PersonaTraits()
        assert t.warmth == 0.7
        assert t.humor == 0.5

    def test_to_dict(self):
        t = PersonaTraits(warmth=0.9, seriousness=0.8)
        d = t.to_dict()
        assert d["warmth"] == 0.9
        assert d["seriousness"] == 0.8


class TestToneConfig:
    def test_defaults(self):
        tc = ToneConfig()
        assert tc.formality == FormalityLevel.CASUAL
        assert tc.max_emojis_per_message == 3

    def test_to_dict(self):
        tc = ToneConfig(formality=FormalityLevel.FORMAL)
        d = tc.to_dict()
        assert d["formality"] == "formal"


class TestPersonaTemplate:
    def test_default_persona(self):
        p = PRESETS["dududa_default"]
        assert p.persona_id == "dududa_default"
        assert p.name == "YmaKmern"
        assert 0.3 <= p.traits.sassiness <= 0.6

    def test_persona_render_prompt(self):
        p = PRESETS["dududa_default"]
        prompt = p.render_system_prompt()
        assert "YmaKmern" in prompt
        assert "傲娇" in prompt and "嘴欠" in prompt
        assert "CRITICAL" in prompt or "MUST NOT" in prompt
        assert "emoji" in prompt.lower()

    def test_serious_persona_no_emoji(self):
        p = PRESETS["dududa_serious"]
        assert p.tone.emoji_style == EmojiStyle.NONE
        assert p.tone.max_emojis_per_message == 0

    def test_tsundere_traits(self):
        p = PRESETS["dududa_tsundere"]
        assert p.traits.sassiness > 0.5
        assert "哼" in " ".join(p.favorite_phrases)

    def test_mentor_traits(self):
        p = PRESETS["dududa_mentor"]
        assert p.traits.warmth > 0.8
        assert p.traits.curiosity > 0.8

    def test_forbidden_topics_in_prompt(self):
        p = PRESETS["dududa_serious"]
        prompt = p.render_system_prompt()
        assert "political" in prompt.lower() or "NSFW" in prompt

    def test_to_dict(self):
        p = PRESETS["dududa_default"]
        d = p.to_dict()
        assert d["persona_id"] == "dududa_default"
        assert isinstance(d["traits"], dict)
        assert isinstance(d["tone"], dict)

    def test_all_presets_have_prompts(self):
        for pid, p in PRESETS.items():
            prompt = p.render_system_prompt()
            assert len(prompt) > 50, f"{pid} prompt too short"


class TestPersonaRegistry:
    def test_default_active(self):
        reg = PersonaRegistry()
        assert reg.active_id == "dududa_default"
        assert reg.active.persona_id == "dududa_default"

    def test_switch_persona(self):
        reg = PersonaRegistry()
        assert reg.switch("dududa_serious")
        assert reg.active_id == "dududa_serious"

    def test_switch_invalid(self):
        reg = PersonaRegistry()
        assert not reg.switch("nonexistent")
        assert reg.active_id == "dududa_default"

    def test_list_all(self):
        reg = PersonaRegistry()
        all_ids = reg.list_all()
        assert "dududa_default" in all_ids
        assert "dududa_tsundere" in all_ids
        assert len(all_ids) >= 4

    def test_register_custom(self):
        reg = PersonaRegistry()
        custom = PersonaTemplate(persona_id="custom_test", name="Test")
        reg.register(custom)
        assert reg.get("custom_test") is custom

    def test_unregister_preset_fails(self):
        reg = PersonaRegistry()
        assert not reg.unregister("dududa_default")

    def test_unregister_custom(self):
        reg = PersonaRegistry()
        custom = PersonaTemplate(persona_id="my_custom", name="My")
        reg.register(custom)
        assert reg.unregister("my_custom")

    def test_group_override(self):
        reg = PersonaRegistry()
        reg.set_group_override("group_123", "dududa_tsundere")
        resolved = reg.resolve(group_id="group_123")
        assert resolved.persona_id == "dududa_tsundere"

    def test_user_override_takes_priority(self):
        reg = PersonaRegistry()
        reg.set_group_override("group_123", "dududa_tsundere")
        reg.set_user_override("user_456", "dududa_mentor")
        resolved = reg.resolve(group_id="group_123", user_id="user_456")
        assert resolved.persona_id == "dududa_mentor"

    def test_get_system_prompt(self):
        reg = PersonaRegistry()
        prompt = reg.get_system_prompt()
        assert "YmaKmern" in prompt


class TestEmojiStrategy:
    def test_default_persona_uses_text_faces_only(self):
        p = PRESETS["dududa_default"]
        assert p.tone.emoji_style == EmojiStyle.TEXT_ONLY
        prompt = p.render_system_prompt()
        assert "only kaomoji" in prompt
        assert "no emoji" in prompt

    def test_no_emoji_when_none_style(self):
        p = PRESETS["dududa_serious"]
        es = EmojiStrategy(p)
        result = es.pick_emoji("happy")
        assert result == ""

    def test_kaomoji_when_text_only(self):
        from dududa.core.persona.templates import PersonaTemplate
        p = PersonaTemplate(
            persona_id="test", name="test",
            tone=ToneConfig(emoji_style=EmojiStyle.TEXT_ONLY, use_kaomoji=True),
        )
        es = EmojiStrategy(p)
        result = es.pick_emoji("happy")
        # Should have kaomoji
        assert len(result) > 0

    def test_mood_detection_thanks(self):
        es = EmojiStrategy(PRESETS["dududa_default"])
        mood = es.mood_from_text("thank you so much!")
        assert mood == "love"

    def test_mood_detection_sorry(self):
        es = EmojiStrategy(PRESETS["dududa_default"])
        mood = es.mood_from_text("sorry about that")
        assert mood == "sorry"

    def test_mood_detection_happy(self):
        es = EmojiStrategy(PRESETS["dududa_default"])
        mood = es.mood_from_text("haha that is funny")
        assert mood == "happy"

    def test_wrap_with_kaomoji(self):
        p = PRESETS["dududa_default"]
        es = EmojiStrategy(p)
        result = es.wrap_with_kaomoji("Hello", "happy")
        assert result.startswith("Hello")

    def test_wrap_no_kaomoji_when_disabled(self):
        es = EmojiStrategy(PRESETS["dududa_serious"])
        result = es.wrap_with_kaomoji("Hello")
        assert result == "Hello"


class TestExpressionLibrary:
    def test_greeting(self):
        lib = ExpressionLibrary(PRESETS["dududa_default"])
        g = lib.greeting()
        assert len(g) > 0
        assert any(mark in g for mark in ("?", "~", "!", "？", "～", "！"))

    def test_refusal_with_reason(self):
        lib = ExpressionLibrary(PRESETS["dududa_tsundere"])
        r = lib.refusal("out of scope")
        assert len(r) > 0

    def test_confusion(self):
        lib = ExpressionLibrary(PRESETS["dududa_mentor"])
        c = lib.confusion()
        assert len(c) > 0

    def test_acknowledgment(self):
        lib = ExpressionLibrary(PRESETS["dududa_default"])
        a = lib.acknowledgment()
        assert len(a) > 0

    def test_wrap_response_with_catchphrase(self):
        lib = ExpressionLibrary(PRESETS["dududa_tsundere"])
        results = [lib.wrap_response("test message", "neutral") for _ in range(20)]
        assert set(results) == {"test message"}


class TestPersonaRenderer:
    def test_basic_render(self):
        renderer = PersonaRenderer(PRESETS["dududa_default"])
        result = renderer.render("The course rating is 4.5")
        assert "4.5" in result  # Facts preserved

    def test_fact_protection(self):
        renderer = PersonaRenderer(PRESETS["dududa_default"])
        facts = {"score": "4.5", "source": "icourse"}
        rendered, ok = renderer.render_with_fact_protection(
            "The course rating is 4.5 from icourse", facts
        )
        assert ok

    def test_fact_protection_detects_loss(self):
        renderer = PersonaRenderer(PRESETS["dududa_default"])
        facts = {"score": "XYZ_NOT_IN_TEXT"}
        rendered, ok = renderer.render_with_fact_protection(
            "The course rating is 4.5", facts
        )
        assert not ok

    def test_serious_no_emoji(self):
        renderer = PersonaRenderer(PRESETS["dududa_serious"])
        result = renderer.render("The answer is 42")
        # No emoji (unicode emoji range check)
        emoji_count = sum(1 for c in result if ord(c) > 0x1F000)
        assert emoji_count == 0

    def test_fallback_does_not_rewrite_wording(self):
        from dududa.core.persona.templates import PersonaTemplate, ToneConfig
        p = PersonaTemplate(
            persona_id="test", name="test",
            tone=ToneConfig(formality=FormalityLevel.FORMAL),
        )
        renderer = PersonaRenderer(p)
        result = renderer.render("I am gonna check that")
        assert result == "I am gonna check that"

    def test_fallback_is_deterministic(self):
        renderer = PersonaRenderer(PRESETS["dududa_tsundere"])
        assert {renderer.render("原始草稿") for _ in range(30)} == {"原始草稿"}

    def test_persona_switch(self):
        renderer = PersonaRenderer(PRESETS["dududa_default"])
        result_default = renderer.render("Hello")
        renderer.set_persona(PRESETS["dududa_serious"])
        result_serious = renderer.render("Hello")
        # Both should be valid strings
        assert isinstance(result_default, str)
        assert isinstance(result_serious, str)

    def test_empty_text(self):
        renderer = PersonaRenderer(PRESETS["dududa_default"])
        result = renderer.render("")
        assert result == ""

    def test_all_presets_render(self):
        for pid, preset in PRESETS.items():
            renderer = PersonaRenderer(preset)
            result = renderer.render("This is a test message with data: 42")
            assert "42" in result, f"{pid} lost fact data"
            assert len(result) > 0, f"{pid} produced empty result"
