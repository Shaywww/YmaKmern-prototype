"""Enhanced Persona-aware renderer that applies OC personality to DraftResponse."""
from __future__ import annotations
import re
from typing import Optional
from .templates import PersonaTemplate, FormalityLevel, EmojiStyle, PRESETS
from .emoji_strategy import EmojiStrategy
from .expressions import ExpressionLibrary

class PersonaRenderer:
    def __init__(self, persona: Optional[PersonaTemplate] = None):
        self._persona = persona or PRESETS["dududa_default"]
        self._emoji = EmojiStrategy(self._persona)
        self._expr = ExpressionLibrary(self._persona)

    @property
    def persona(self) -> PersonaTemplate:
        return self._persona

    def set_persona(self, persona: PersonaTemplate):
        self._persona = persona
        self._emoji.set_persona(persona)
        self._expr.set_persona(persona)

    def render(self, draft_text: str, mood: str = "neutral") -> str:
        if not draft_text.strip():
            return draft_text
        persona = self._persona
        text = draft_text
        text = self._apply_formality(text, persona.tone.formality)
        text = self._apply_playfulness(text, persona.tone.playfulness, mood)
        if persona.favorite_phrases:
            text = self._expr.wrap_response(text, mood)
        if persona.tone.emoji_style != EmojiStyle.NONE:
            emoji_mood = self._emoji.mood_from_text(text)
            emoji = self._emoji.pick_emoji(emoji_mood, 1)
            if emoji.strip():
                text = text + emoji
        text = self._normalize_ending(text, persona.tone.sentence_endings)
        return text

    def render_with_fact_protection(self, draft_text: str, facts: dict[str, str], mood: str = "neutral") -> tuple[str, bool]:
        rendered = self.render(draft_text, mood)
        for key, value in facts.items():
            if value and value not in rendered:
                return draft_text, False
        return rendered, True

    def _apply_formality(self, text: str, level: FormalityLevel) -> str:
        if level in (FormalityLevel.VERY_CASUAL, FormalityLevel.CASUAL):
            return text
        import re as _re
        reps = {
            FormalityLevel.NEUTRAL: [
                ("gonna", "going to"), ("wanna", "want to"),
                ("yeah", "yes"), ("nope", "no"), ("ok", "okay"),
            ],
            FormalityLevel.FORMAL: [
                ("gonna", "going to"), ("wanna", "want to"),
                ("yeah", "yes"), ("nope", "no"), ("ok", "understood"),
                ("hey", "hello"), ("thanks", "thank you"),
                ("sorry", "I apologize"),
            ],
            FormalityLevel.VERY_FORMAL: [
                ("gonna", "going to"), ("wanna", "wish to"),
                ("yeah", "yes"), ("nope", "no"), ("ok", "acknowledged"),
                ("hey", "greetings"), ("thanks", "thank you very much"),
                ("sorry", "I sincerely apologize"),
                ("dunno", "I am not certain"), ("cool", "excellent"),
            ],
        }
        for casual, formal in reps.get(level, []):
            pattern = r"(?i)\b" + _re.escape(casual) + r"\b"
            text = _re.sub(pattern, formal, text)
        return text

    def _apply_playfulness(self, text: str, level, mood: str) -> str:
        if level == "serious":
            import re as _re
            repeated = _re.sub(r"([!?]){2,}", r"\1", text)
            return repeated
        return text

    def _normalize_ending(self, text: str, endings: tuple[str, ...]) -> str:
        text = text.rstrip()
        if not text:
            return text
        has_ending = any(text.endswith(e) for e in endings)
        if not has_ending:
            import random
            text += random.choice(endings)
        return text
