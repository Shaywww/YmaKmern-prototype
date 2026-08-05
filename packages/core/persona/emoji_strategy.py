"""Context-aware emoji selection strategy."""
from __future__ import annotations
from typing import Optional
import random
from .templates import PersonaTemplate, EmojiStyle, FormalityLevel

EMOJI_MAP = {
    "happy": ["^^","(*^_^*)","(^^)","(^o^)","(*^^*)","(^-^)"],
    "excited": ["(*>v<*)","(>_<)","(*>_<*)","(o^^o)","(^o^)/"],
    "sad": ["(;_;)","(T_T)","(._.)","(;;)","(-_-)"],
    "confused": ["(^.^?)","(O.O)","(o_o)","(?_?)","(-_-;)"],
    "love": ["(^^)","(*^.^*)","(^_-)","(^^;)"],
    "sorry": ["m(_ _)m","(._.)","(-_-;)"],
    "wink": ["(^_-)","(^.~)","(^-^)b"],
    "shy": ["(// //)","(^^;;)","(*/ *)","(._.)"],
}

EMOJI_UNICODE = {
    "happy": ["^^","^^","^^"],
    "excited": ["^^","^^","^^"],
    "sad": ["TT","^^","^^"],
    "confused": ["^^","^^"],
    "thinking": ["^^","^^"],
    "ok": ["^^","^^"],
    "cool": ["^^","^^","^^"],
    "love": ["^^","^^"],
    "clap": ["^^","^^"],
    "fire": ["^^"],
    "sparkles": ["^^"],
    "check": ["^^"],
    "cross": ["^^"],
    "wave": ["^^","^^"],
}

class EmojiStrategy:
    def __init__(self, persona: Optional[PersonaTemplate] = None):
        self._persona = persona

    def set_persona(self, persona: PersonaTemplate):
        self._persona = persona

    def pick_emoji(self, mood: str, count: int = 1) -> str:
        if self._persona is None:
            return ""
        style = self._persona.tone.emoji_style
        if style == EmojiStyle.NONE:
            return ""
        if style == EmojiStyle.TEXT_ONLY:
            options = EMOJI_MAP.get(mood, EMOJI_MAP["happy"])
            return " " + random.choice(options) if options else ""
        max_n = min(count, self._persona.tone.max_emojis_per_message)
        if max_n <= 0:
            return ""
        options = EMOJI_UNICODE.get(mood, EMOJI_UNICODE["happy"])
        picked = random.sample(options, min(max_n, len(options)))
        return " " + "".join(picked)

    def mood_from_text(self, text: str) -> str:
        t = text.lower()
        if any(w in t for w in ["thank","thanks","thx","^^","love"]):
            return "love"
        if any(w in t for w in ["sorry","apologize","my bad"]):
            return "sorry"
        if any(w in t for w in ["haha","lol","^^","funny","joke"]):
            return "happy"
        if any(w in t for w in ["wow","amazing","great","awesome","congrats"]):
            return "excited"
        if any(w in t for w in ["sad","unfortunately","bad","fail"]):
            return "sad"
        if any(w in t for w in ["what","how","why","confused","huh"]):
            return "confused"
        if any(w in t for w in ["ok","done","got it","sure","fine"]):
            return "ok"
        return "happy"

    def wrap_with_kaomoji(self, text: str, mood: str = "happy") -> str:
        if self._persona is None or not self._persona.tone.use_kaomoji:
            return text
        kaomoji = self.pick_emoji(mood, 1)
        return f"{text}{kaomoji}" if kaomoji.strip() else text
