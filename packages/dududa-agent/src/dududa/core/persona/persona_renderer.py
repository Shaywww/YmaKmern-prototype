"""Deterministic persona fallback for already composed responses.

The primary model owns wording and personality.  This fallback deliberately
does not inject random catchphrases, punctuation or emoji: a validation failure
must never turn into a second, unpredictable style pass.
"""
from __future__ import annotations
from typing import Optional
from .templates import PersonaTemplate, PRESETS

class PersonaRenderer:
    def __init__(self, persona: Optional[PersonaTemplate] = None):
        self._persona = persona or PRESETS["dududa_default"]

    @property
    def persona(self) -> PersonaTemplate:
        return self._persona

    def set_persona(self, persona: PersonaTemplate):
        self._persona = persona

    def render(self, draft_text: str, mood: str = "neutral") -> str:
        return draft_text

    def render_with_fact_protection(self, draft_text: str, facts: dict[str, str], mood: str = "neutral") -> tuple[str, bool]:
        rendered = self.render(draft_text, mood)
        for key, value in facts.items():
            if value and value not in rendered:
                return draft_text, False
        return rendered, True
