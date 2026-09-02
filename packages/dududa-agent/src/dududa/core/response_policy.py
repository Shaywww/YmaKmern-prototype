"""Versioned response-policy domain types and deterministic resolvers.

The module deliberately contains no model calls and no user text.  Raw data
must be converted into sourced signals by an application-layer extractor
before it reaches these resolvers.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .quality_eval import persona_contract_violations


POLICY_VERSION = "response-policy/1.2"


class Scene(str, Enum):
    UNKNOWN = "unknown"
    CASUAL_CHAT = "casual_chat"
    PLAYFUL_BANTER = "playful_banter"
    EMOTIONAL_SUPPORT = "emotional_support"
    INFORMATION = "information"
    TASK = "task"
    TOOL_RESULT = "tool_result"
    IDENTITY_PROBE = "identity_probe"
    PRIDE_ACKNOWLEDGED = "pride_acknowledged"
    HIGH_RISK = "high_risk"


class Emotion(str, Enum):
    UNKNOWN = "unknown"
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    NEGATIVE = "negative"


class EmotionIntensity(str, Enum):
    UNKNOWN = "unknown"
    MILD = "mild"
    MODERATE = "moderate"
    STRONG = "strong"


class RiskLevel(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Familiarity(str, Enum):
    UNKNOWN = "unknown"
    NEW = "new"
    FAMILIAR = "familiar"
    CLOSE = "close"


class ResponseOrigin(str, Enum):
    UNKNOWN = "unknown"
    TEXT = "text"
    REPLY = "reply"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    TOOL = "tool"
    NATIVE_SCENE = "native_scene"
    LIGHT_REACTION = "light_reaction"
    PROGRESS = "progress"
    COMMAND = "command"
    SUBSCRIPTION = "subscription"
    USER_CANCELLED = "user_cancelled"
    SYSTEM_ERROR = "system_error"


class ConfidenceBand(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ContinuationValue(str, Enum):
    UNKNOWN = "unknown"
    NONE = "none"
    USEFUL = "useful"
    IMPORTANT = "important"


class FollowupMode(str, Enum):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"


class Tone(str, Enum):
    CALM = "calm"
    WARM = "warm"
    NEUTRAL = "neutral"
    LIVELY = "lively"
    PRECISE = "precise"


class SignalName(str, Enum):
    SCENE = "scene"
    EMOTION = "emotion"
    EMOTION_INTENSITY = "emotion_intensity"
    RISK_LEVEL = "risk_level"
    FAMILIARITY = "familiarity"
    RESPONSE_ORIGIN = "response_origin"
    PERCEPTION_CONFIDENCE = "perception_confidence"
    GROUNDING_CONFIDENCE = "grounding_confidence"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    CONTINUATION_VALUE = "continuation_value"
    SAFETY_CLARIFICATION = "safety_clarification_required"
    USER_HUMOR_PREFERENCE = "user_humor_preference"
    USER_KAOMOJI_PREFERENCE = "user_kaomoji_preference"


class SignalSource(str, Enum):
    PLATFORM_FACT = "platform_fact"
    DETERMINISTIC_RULE = "deterministic_rule"
    CALIBRATED_CLASSIFIER = "calibrated_classifier"
    MODEL_AUXILIARY = "model_auxiliary"
    TOOL_METADATA = "tool_metadata"
    PROFILE = "profile"
    USER_PREFERENCE = "user_preference"
    SAFETY_ENGINE = "safety_engine"
    DEFAULT = "default"


@dataclass(frozen=True)
class SignalEvidence:
    signal_name: SignalName
    source: SignalSource
    rule_id: str
    confidence_band: Optional[ConfidenceBand] = None

    def to_dict(self) -> dict:
        return {
            "signal_name": self.signal_name.value,
            "source": self.source.value,
            "rule_id": self.rule_id,
            "confidence_band": (
                self.confidence_band.value if self.confidence_band else None),
        }


@dataclass(frozen=True)
class SafetyDecision:
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    clarification_required: bool = False
    refusal_required: bool = False
    reason_codes: tuple[str, ...] = ()
    evidence: tuple[SignalEvidence, ...] = ()


@dataclass(frozen=True)
class InteractionSignals:
    missing_required_fields: tuple[str, ...] = ()
    continuation_value: ContinuationValue = ContinuationValue.UNKNOWN
    safety_clarification_required: bool = False
    evidence: tuple[SignalEvidence, ...] = ()


@dataclass(frozen=True)
class StyleSignals:
    scene: Scene = Scene.UNKNOWN
    emotion: Emotion = Emotion.UNKNOWN
    emotion_intensity: EmotionIntensity = EmotionIntensity.UNKNOWN
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    familiarity: Familiarity = Familiarity.UNKNOWN
    response_origin: ResponseOrigin = ResponseOrigin.UNKNOWN
    perception_confidence: float = math.nan
    grounding_confidence: Optional[float] = None
    evidence: tuple[SignalEvidence, ...] = ()


@dataclass(frozen=True)
class PersonaStyleDefaults:
    humor_level: int = 1
    allows_kaomoji: bool = True


@dataclass(frozen=True)
class UserStylePreference:
    humor_level: Optional[int] = None
    allow_kaomoji: Optional[bool] = None


@dataclass(frozen=True)
class InteractionPolicy:
    followup_mode: FollowupMode
    reason_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class OutputStylePolicy:
    tone: Tone
    humor_level: int
    max_kaomoji: int
    max_colored_emoji: int = 0
    # Zero means no deterministic length budget.  A tight budget is reserved
    # for verified low-effort banter; ordinary chat and factual answers must
    # not be truncated merely because they are user-visible.
    max_chars: int = 0
    policy_version: str = POLICY_VERSION
    reason_codes: tuple[str, ...] = ()

    @property
    def allow_kaomoji(self) -> bool:
        return self.max_kaomoji > 0


@dataclass(frozen=True)
class ResolvedResponsePolicy:
    interaction: InteractionPolicy
    style: OutputStylePolicy

    def fingerprint(self) -> str:
        payload = {
            "interaction": {
                "followup_mode": self.interaction.followup_mode.value,
                "reason_codes": list(self.interaction.reason_codes),
                "policy_version": self.interaction.policy_version,
            },
            "style": {
                "tone": self.style.tone.value,
                "humor_level": self.style.humor_level,
                "max_kaomoji": self.style.max_kaomoji,
                "max_colored_emoji": self.style.max_colored_emoji,
                "max_chars": self.style.max_chars,
                "reason_codes": list(self.style.reason_codes),
                "policy_version": self.style.policy_version,
            },
        }
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=True,
            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


def confidence_band(value: Optional[float]) -> ConfidenceBand:
    if value is None:
        return ConfidenceBand.UNKNOWN
    try:
        score = float(value)
    except (TypeError, ValueError):
        return ConfidenceBand.UNKNOWN
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return ConfidenceBand.UNKNOWN
    if score >= 0.85:
        return ConfidenceBand.HIGH
    if score >= 0.60:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


class InteractionPolicyResolver:
    """Resolve whether a response may or must continue the interaction."""

    @classmethod
    def resolve(cls, signals: InteractionSignals,
                safety: SafetyDecision) -> InteractionPolicy:
        reasons: list[str] = []
        if safety.clarification_required or signals.safety_clarification_required:
            reasons.append("safety_clarification_required")
            mode = FollowupMode.REQUIRED
        elif safety.refusal_required:
            reasons.append("safety_refusal_forbids_followup")
            mode = FollowupMode.FORBIDDEN
        elif tuple(x for x in signals.missing_required_fields if str(x).strip()):
            reasons.append("required_fields_missing")
            mode = FollowupMode.REQUIRED
        elif signals.continuation_value in (
                ContinuationValue.USEFUL, ContinuationValue.IMPORTANT):
            reasons.append("continuation_has_value")
            mode = FollowupMode.OPTIONAL
        else:
            if signals.continuation_value == ContinuationValue.UNKNOWN:
                reasons.append("unknown_continuation_fails_closed")
            else:
                reasons.append("no_followup_value")
            mode = FollowupMode.FORBIDDEN
        return InteractionPolicy(
            followup_mode=mode, reason_codes=tuple(reasons))


class OutputStylePolicyResolver:
    """Resolve immutable style caps; invalid signals fail to calm-neutral."""

    _SCENE_HUMOR_CAP = {
        Scene.CASUAL_CHAT: 2,
        Scene.PLAYFUL_BANTER: 2,
        Scene.EMOTIONAL_SUPPORT: 0,
        Scene.INFORMATION: 0,
        Scene.TASK: 0,
        Scene.TOOL_RESULT: 0,
        Scene.IDENTITY_PROBE: 1,
        Scene.PRIDE_ACKNOWLEDGED: 1,
        Scene.HIGH_RISK: 0,
    }
    _RISK_HUMOR_CAP = {
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 0,
        RiskLevel.HIGH: 0,
        RiskLevel.CRITICAL: 0,
    }
    _SCENE_MAX_CHARS = {
        # Human banter is usually a low-effort acknowledgement or comeback,
        # not a complete miniature essay.  Other scenes intentionally remain
        # unbounded here and are governed by their own content requirements.
        Scene.PLAYFUL_BANTER: 48,
    }

    @classmethod
    def neutral(cls, reason: str) -> OutputStylePolicy:
        return OutputStylePolicy(
            tone=Tone.CALM,
            humor_level=0,
            max_kaomoji=0,
            max_chars=0,
            reason_codes=(reason,),
        )

    @classmethod
    def resolve(
        cls,
        signals: StyleSignals,
        *,
        persona: PersonaStyleDefaults = PersonaStyleDefaults(),
        user: UserStylePreference = UserStylePreference(),
    ) -> OutputStylePolicy:
        perception = confidence_band(signals.perception_confidence)
        missing = (
            signals.scene == Scene.UNKNOWN
            or signals.emotion == Emotion.UNKNOWN
            or signals.emotion_intensity == EmotionIntensity.UNKNOWN
            or signals.risk_level == RiskLevel.UNKNOWN
            or signals.familiarity == Familiarity.UNKNOWN
            or signals.response_origin == ResponseOrigin.UNKNOWN
            or perception == ConfidenceBand.UNKNOWN
            or (signals.scene == Scene.TOOL_RESULT
                and confidence_band(signals.grounding_confidence)
                == ConfidenceBand.UNKNOWN)
        )
        if missing:
            return cls.neutral("missing_or_invalid_signal")

        persona_humor = min(2, max(0, int(persona.humor_level)))
        user_humor_cap = (
            persona_humor if user.humor_level is None
            else min(2, max(0, int(user.humor_level))))
        # A preference may make the response quieter, never push it beyond
        # the active persona.  Scene and risk caps are applied afterwards.
        desired_humor = min(persona_humor, user_humor_cap)
        scene_cap = cls._SCENE_HUMOR_CAP.get(signals.scene, 0)
        risk_cap = cls._RISK_HUMOR_CAP.get(signals.risk_level, 0)
        emotion_cap = (
            0 if (signals.emotion == Emotion.NEGATIVE
                  and signals.emotion_intensity in (
                      EmotionIntensity.MODERATE, EmotionIntensity.STRONG))
            else 2)
        humor = min(desired_humor, scene_cap, risk_cap, emotion_cap)

        user_allows = (
            True if user.allow_kaomoji is None else user.allow_kaomoji)
        kaomoji_allowed = bool(
            persona.allows_kaomoji
            and user_allows
            and signals.scene in (Scene.CASUAL_CHAT, Scene.PLAYFUL_BANTER)
            and signals.risk_level == RiskLevel.LOW
            and not (signals.emotion == Emotion.NEGATIVE
                     and signals.emotion_intensity
                     == EmotionIntensity.STRONG)
        )
        tone = cls._tone(signals, humor)
        reasons = (
            f"scene_cap:{scene_cap}",
            f"risk_cap:{risk_cap}",
            f"emotion_cap:{emotion_cap}",
        )
        return OutputStylePolicy(
            tone=tone,
            humor_level=humor,
            max_kaomoji=1 if kaomoji_allowed else 0,
            max_chars=cls._SCENE_MAX_CHARS.get(signals.scene, 0),
            reason_codes=reasons,
        )

    @staticmethod
    def _tone(signals: StyleSignals, humor: int) -> Tone:
        if signals.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return Tone.CALM
        if (signals.emotion == Emotion.NEGATIVE
                and signals.emotion_intensity in (
                    EmotionIntensity.MODERATE, EmotionIntensity.STRONG)):
            return Tone.WARM
        if signals.scene in (Scene.INFORMATION, Scene.TASK, Scene.TOOL_RESULT):
            return Tone.PRECISE
        if signals.scene == Scene.EMOTIONAL_SUPPORT:
            return Tone.WARM
        if signals.scene == Scene.IDENTITY_PROBE:
            return Tone.NEUTRAL
        if signals.scene == Scene.PRIDE_ACKNOWLEDGED:
            return Tone.LIVELY
        return Tone.LIVELY if humor else Tone.NEUTRAL


_COLOR_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]"
)
_KAOMOJI_RE = re.compile(
    r"(?:"
    r"[\(（][^\n()（）]{0,18}(?:ω|▽|∀|‿|︿|﹏|д|Д|•|・|｡|。|°|"
    r"≧|≦|シ|つ|╥|T|Q)[^\n()（）]{0,18}[\)）]"
    r"|\^(?:[_~.\-]?\^)+~?"
    r"|(?:T_T|QAQ|QwQ|qwq|OvO|ovo|orz)"
    r")"
)


def kaomoji_spans(text: str) -> tuple[tuple[int, int], ...]:
    return tuple((match.start(), match.end())
                 for match in _KAOMOJI_RE.finditer(str(text or "")))


def count_kaomoji(text: str) -> int:
    return len(kaomoji_spans(text))


def style_contract_violations(
    text: str, policy: OutputStylePolicy
) -> tuple[str, ...]:
    """Evaluate the future deterministic style contract without rewriting."""
    value = str(text or "").strip()
    violations = list(persona_contract_violations(
        value, casual_chat=(policy.tone != Tone.PRECISE)))
    count = count_kaomoji(value)
    if count > policy.max_kaomoji:
        violations.append(
            "kaomoji_disallowed" if policy.max_kaomoji == 0
            else "too_many_kaomoji")
    if count:
        without = _KAOMOJI_RE.sub("", value)
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", without):
            violations.append("pure_kaomoji")
    if _COLOR_EMOJI_RE.search(value):
        violations.append("unicode_emoji")
    if re.search(r"(?:!{4,}|！{4,}|[~～]{4,})", value):
        violations.append("repeated_punctuation")
    visible_chars = len("".join(value.split()))
    if policy.max_chars > 0 and visible_chars > policy.max_chars:
        violations.append("too_long")
    return tuple(dict.fromkeys(violations))


def clean_response_style(text: str, policy: OutputStylePolicy) -> str:
    """Apply only deterministic, meaning-preserving style cleanup.

    This function intentionally cannot add a closing sentence, manufacture a
    joke, or rewrite factual wording.  A pure-kaomoji result is left for the
    response contract to reject and route through its bounded fallback.
    """
    value = str(text or "")
    value = _COLOR_EMOJI_RE.sub("", value)
    seen = 0

    def keep_allowed(match: re.Match) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= policy.max_kaomoji else ""

    value = _KAOMOJI_RE.sub(keep_allowed, value)
    value = re.sub(r"!{4,}", "!!!", value)
    value = re.sub(r"！{4,}", "！！！", value)
    value = re.sub(r"[~～]{4,}", "~~~", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return value.strip()
