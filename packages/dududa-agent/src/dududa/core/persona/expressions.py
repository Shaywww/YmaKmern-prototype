"""Expression templates for common conversational scenarios."""
from __future__ import annotations
import random
from typing import Optional
from .templates import PersonaTemplate

class ExpressionLibrary:
    def __init__(self, persona: Optional[PersonaTemplate] = None):
        self._persona = persona

    def set_persona(self, persona: PersonaTemplate):
        self._persona = persona

    def _pick(self, templates: tuple[str, ...], default: str) -> str:
        if templates:
            return random.choice(templates)
        return default

    def greeting(self) -> str:
        if self._persona is None:
            return "Hello!"
        return self._pick(self._persona.greeting_templates, "Hello!")

    def farewell(self) -> str:
        if self._persona is None:
            return "Goodbye!"
        return self._pick(self._persona.farewell_templates, "Goodbye!")

    def refusal(self, reason: str = "") -> str:
        if self._persona is None:
            return f"Sorry, I cannot help with that. {reason}"
        base = self._pick(self._persona.refusal_templates, "Sorry, I cannot help with that.")
        return f"{base} {reason}".strip()

    def confusion(self) -> str:
        if self._persona is None:
            return "I did not quite understand that."
        return self._pick(self._persona.confusion_templates, "I did not quite understand that.")

    def acknowledgment(self) -> str:
        templates = ("Got it!", "Understood.", "OK!", "Noted~", "Alright!", "On it!")
        return random.choice(templates)

    def thinking(self) -> str:
        templates = ("Let me think...", "Hmm, let me check...", "One moment...", "Let me look into that...")
        return random.choice(templates)

    def error_apology(self) -> str:
        templates = ("Oops, something went wrong!", "Sorry, encountered an error.", "That did not work as expected...")
        return random.choice(templates)

    def compliment(self) -> str:
        templates = ("Great question!", "Good thinking!", "Nice catch!", "Well spotted!")
        return random.choice(templates)

    def wrap_response(self, draft_text: str, mood: str = "neutral") -> str:
        """Keep fallback output stable; personality belongs to model compose."""
        return draft_text
