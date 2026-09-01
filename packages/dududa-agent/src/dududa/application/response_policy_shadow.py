"""Application-layer signal extraction for response-policy shadow traces.

The extractor may inspect raw event text in memory, but the emitted trace
contains only enums, versions, rule identifiers and aggregate violations.
It never stores message, memory, attachment or tool bodies.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from dududa.core.renderer import extract_atomic_facts, referenced_facts
from dududa.core.message_catalog import (
    SELECTOR_VERSION, MessageCatalog, MessageKey, MessageSelection,
)
from dududa.core.persona.prompt_policy import PERSONA_KERNEL_VERSION
from dududa.core.response_policy import (
    ConfidenceBand, ContinuationValue, Emotion, EmotionIntensity,
    Familiarity, InteractionPolicyResolver, InteractionSignals,
    OutputStylePolicyResolver, POLICY_VERSION, PersonaStyleDefaults,
    ResolvedResponsePolicy,
    ResponseOrigin, RiskLevel, SafetyDecision, Scene, SignalEvidence,
    SignalName, SignalSource, StyleSignals, UserStylePreference,
    confidence_band, style_contract_violations,
)
from dududa.core.state import SocialAction
from dududa.core.trace_recorder import trace_recorder

from .dududa_utils import _contains_restricted, _detect_media_kind


_CRITICAL_RISK_RE = re.compile(
    r"(?:想自杀|想死|不想活|要跳楼|准备轻生|马上伤害自己|"
    r"现在很危险|有人要杀我|正在流血不止)"
)
_HIGH_RISK_RE = re.compile(
    r"(?:诊断|处方|用药|药量|急救|胸痛|呼吸困难|法律责任|律师|"
    r"起诉|投资|借贷|转账|诈骗|银行卡|人身安全|自残|轻生)"
)
_STRONG_NEGATIVE_RE = re.compile(
    r"(?:崩溃|绝望|受不了了|特别难受|非常痛苦|想死|不想活|很危险)"
)
_NEGATIVE_RE = re.compile(
    r"(?:难受|伤心|烦死|好烦|焦虑|害怕|生气|委屈|失败|翻车|糟糕)"
)
_POSITIVE_RE = re.compile(
    r"(?:成功了|过了|太好了|开心|高兴|爽|赢了|好耶|哈哈|厉害|真会)"
)
_QUESTION_RE = re.compile(r"(?:[？?]$|吗[？?]?$|呢[？?]?$|为什么|怎么|多少|哪[里个])")
_IDENTITY_PROBE_RE = re.compile(
    r"(?:你(?:到底)?是(?:不是)?\s*(?:AI|人工智能|机器人)|"
    r"你有(?:没有)?(?:意识|感情|灵魂)|你会(?:害怕|死|难过)|"
    r"你用的什么模型|你的底层模型|你真的懂吗)", re.I)
_PRAISE_RE = re.compile(
    r"(?:你真(?:厉害|会|聪明)|有点东西|太强了|牛啊|卧槽.{0,6}(?:会|强)|"
    r"居然真让你说中了|这都能答)"
)

_EXTRA_ORIGIN = "dududa_response_origin"
_EXTRA_FALLBACK = "dududa_fallback_reason"
_EXTRA_MESSAGE_KEY = "dududa_message_key"
_EXTRA_VARIANT_ID = "dududa_variant_id"
_EXTRA_VARIANT_APPLIED = "dududa_variant_applied"
_EXTRA_VARIANT_SEED_SOURCE = "dududa_variant_seed_source"

_MESSAGE_CATALOG = MessageCatalog()


@dataclass(frozen=True)
class ShadowResolution:
    policy: ResolvedResponsePolicy
    safety: SafetyDecision
    style_signals: StyleSignals
    interaction_signals: InteractionSignals
    violations: tuple[str, ...]
    evidence: tuple[SignalEvidence, ...]


def _event_extra(event, key: str) -> str:
    try:
        value = event.get_extra(key)
        if value is not None:
            return str(value)
    except Exception:
        pass
    return str(getattr(event, f"_{key}", "") or "")


def _set_event_extra(event, key: str, value: str) -> None:
    try:
        event.set_extra(key, value)
        return
    except Exception:
        pass
    try:
        setattr(event, f"_{key}", value)
    except Exception:
        pass


def mark_response_origin(
    event,
    origin: ResponseOrigin,
    *,
    fallback_reason: str = "",
    message_key: str = "",
    variant_id: str = "",
) -> None:
    _set_event_extra(event, _EXTRA_ORIGIN, origin.value)
    if fallback_reason:
        _set_event_extra(event, _EXTRA_FALLBACK, fallback_reason)
    if message_key:
        _set_event_extra(event, _EXTRA_MESSAGE_KEY, message_key)
    if variant_id:
        _set_event_extra(event, _EXTRA_VARIANT_ID, variant_id)


def mark_catalog_message_shadow(
    event,
    key: MessageKey,
    *,
    run_id: str = "",
) -> MessageSelection:
    """Select and trace a future catalogue variant without applying it."""
    message_obj = getattr(event, "message_obj", None)
    platform_message_id = str(
        getattr(message_obj, "message_id", "") or "")
    idempotency_key = (
        _event_extra(event, "dududa_idempotency_key")
        or _event_extra(event, "idempotency_key"))
    conversation_event_id = (
        _event_extra(event, "dududa_conversation_event_id")
        or _event_extra(event, "conversation_event_id"))
    selection = _MESSAGE_CATALOG.select(
        key,
        policy_version=POLICY_VERSION,
        platform_message_id=platform_message_id,
        idempotency_key=idempotency_key,
        conversation_event_id=conversation_event_id,
        run_id=run_id,
    )
    _set_event_extra(event, _EXTRA_MESSAGE_KEY, key.value)
    _set_event_extra(
        event, _EXTRA_VARIANT_ID, selection.variant.variant_id)
    _set_event_extra(event, _EXTRA_VARIANT_APPLIED, "0")
    _set_event_extra(
        event, _EXTRA_VARIANT_SEED_SOURCE, selection.seed_source)
    return selection


def _runtime_state(plugin, run_id: str):
    state = getattr(getattr(plugin, "runtime", None), "_last_state", None)
    return state if str(getattr(state, "run_id", "")) == str(run_id) else None


def _origin(event, state, override: Optional[ResponseOrigin]) -> ResponseOrigin:
    if override is not None:
        return override
    stored = _event_extra(event, _EXTRA_ORIGIN)
    try:
        if stored:
            return ResponseOrigin(stored)
    except ValueError:
        return ResponseOrigin.UNKNOWN
    if state is not None and any(
            getattr(obs, "success", False)
            for obs in getattr(state, "tool_observations", ()) or ()):
        return ResponseOrigin.TOOL
    try:
        if bool(event.get_extra("dududa_reply_context")):
            return ResponseOrigin.REPLY
    except Exception:
        if getattr(event, "_dududa_reply_context", ""):
            return ResponseOrigin.REPLY
    kind = str(_detect_media_kind(event) or "").lower()
    if kind in ("photo", "screenshot", "meme", "sticker", "gif", "image"):
        return ResponseOrigin.IMAGE
    if kind == "video":
        return ResponseOrigin.VIDEO
    if kind in ("file", "document", "audio"):
        return ResponseOrigin.FILE
    return ResponseOrigin.TEXT


def _risk(state, text: str) -> tuple[
        RiskLevel, SafetyDecision, tuple[SignalEvidence, ...]]:
    action = getattr(state, "social_decision", None)
    reason = str(getattr(state, "decision_reason", "") or "").lower()
    if action == SocialAction.BLOCK:
        safety_block = "safety" in reason
        level = RiskLevel.HIGH if safety_block else RiskLevel.MEDIUM
        source = (
            SignalSource.SAFETY_ENGINE if safety_block
            else SignalSource.PLATFORM_FACT)
        evidence = (SignalEvidence(
            SignalName.RISK_LEVEL, source,
            "risk.runtime_block.v1", ConfidenceBand.HIGH),)
        return level, SafetyDecision(
            risk_level=level,
            refusal_required=True,
            reason_codes=("runtime_safety_block" if safety_block
                          else "runtime_permission_block",),
            evidence=evidence,
        ), evidence
    if _CRITICAL_RISK_RE.search(text):
        evidence = (SignalEvidence(
            SignalName.RISK_LEVEL, SignalSource.SAFETY_ENGINE,
            "risk.crisis_explicit.v1", ConfidenceBand.HIGH),)
        return RiskLevel.CRITICAL, SafetyDecision(
            risk_level=RiskLevel.CRITICAL,
            clarification_required=True,
            reason_codes=("explicit_immediate_danger",),
            evidence=evidence,
        ), evidence
    if _HIGH_RISK_RE.search(text) or _contains_restricted(text):
        evidence = (SignalEvidence(
            SignalName.RISK_LEVEL, SignalSource.DETERMINISTIC_RULE,
            "risk.high_stakes_terms.v1", ConfidenceBand.MEDIUM),)
        return RiskLevel.HIGH, SafetyDecision(
            risk_level=RiskLevel.HIGH,
            reason_codes=("high_stakes_topic",),
            evidence=evidence,
        ), evidence
    evidence = (SignalEvidence(
        SignalName.RISK_LEVEL, SignalSource.DETERMINISTIC_RULE,
        "risk.no_high_stakes_signal.v1", ConfidenceBand.MEDIUM),)
    return RiskLevel.LOW, SafetyDecision(
        risk_level=RiskLevel.LOW,
        reason_codes=("no_high_stakes_signal",),
        evidence=evidence,
    ), evidence


def _emotion(text: str) -> tuple[Emotion, EmotionIntensity,
                                 tuple[SignalEvidence, ...]]:
    if _IDENTITY_PROBE_RE.search(text):
        emotion, intensity, rule = (
            Emotion.NEUTRAL, EmotionIntensity.MILD,
            "emotion.identity_probe_neutral.v1")
    elif _STRONG_NEGATIVE_RE.search(text):
        emotion, intensity, rule = (
            Emotion.NEGATIVE, EmotionIntensity.STRONG,
            "emotion.strong_negative_terms.v1")
    elif _NEGATIVE_RE.search(text):
        emotion, intensity, rule = (
            Emotion.NEGATIVE, EmotionIntensity.MODERATE,
            "emotion.negative_terms.v1")
    elif _POSITIVE_RE.search(text):
        emotion, intensity, rule = (
            Emotion.POSITIVE, EmotionIntensity.MODERATE,
            "emotion.positive_terms.v1")
    else:
        emotion, intensity, rule = (
            Emotion.NEUTRAL, EmotionIntensity.MILD,
            "emotion.neutral_default.v1")
    evidence = (
        SignalEvidence(
            SignalName.EMOTION, SignalSource.DETERMINISTIC_RULE,
            rule, ConfidenceBand.MEDIUM),
        SignalEvidence(
            SignalName.EMOTION_INTENSITY, SignalSource.DETERMINISTIC_RULE,
            rule, ConfidenceBand.MEDIUM),
    )
    return emotion, intensity, evidence


def _perception(state, text: str) -> tuple[float, tuple[SignalEvidence, ...]]:
    perception = getattr(state, "perception", None)
    if perception is not None and (
            getattr(perception, "has_explicit_mention", False)
            or getattr(perception, "has_reply_chain", False)
            or getattr(perception, "is_explicit_command", False)):
        return 1.0, (SignalEvidence(
            SignalName.PERCEPTION_CONFIDENCE,
            SignalSource.PLATFORM_FACT,
            "perception.platform_engagement.v1",
            ConfidenceBand.HIGH),)
    if perception is not None:
        try:
            score = min(0.75, max(0.0, float(perception.confidence)))
        except (TypeError, ValueError):
            score = math.nan
        return score, (SignalEvidence(
            SignalName.PERCEPTION_CONFIDENCE,
            SignalSource.MODEL_AUXILIARY,
            "perception.model_score_capped.v1",
            confidence_band(score)),)
    score = 0.70 if text.strip() else math.nan
    return score, (SignalEvidence(
        SignalName.PERCEPTION_CONFIDENCE,
        SignalSource.DETERMINISTIC_RULE,
        "perception.visible_reply_fallback.v1",
        confidence_band(score)),)


def _grounding(state, response: str) -> tuple[Optional[float],
                                               tuple[SignalEvidence, ...]]:
    if state is None:
        return None, ()
    used_scores: list[float] = []
    for obs in getattr(state, "tool_observations", ()) or ():
        if not getattr(obs, "success", False) or getattr(obs, "data", None) is None:
            continue
        facts = extract_atomic_facts(
            obs.data,
            source=str(getattr(obs, "source", "") or ""),
            field=str(getattr(obs, "capability_id", "") or "tool"),
        )
        # Confidence belongs to claims, not the whole tool observation.  If
        # no atomic fact can be extracted or none is present in the answer,
        # the observation must not lower (or raise) this response's band.
        if not facts or not referenced_facts(response, facts):
            continue
        raw = getattr(obs, "confidence", None)
        if raw is None:
            raw = 0.65 if getattr(obs, "cached", False) else 0.85
        try:
            score = min(1.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            continue
        used_scores.append(score)
    if not used_scores:
        return None, ()
    score = min(used_scores)
    return score, (SignalEvidence(
        SignalName.GROUNDING_CONFIDENCE,
        SignalSource.TOOL_METADATA,
        "grounding.referenced_observation_min.v1",
        confidence_band(score)),)


def _familiarity(plugin, state) -> tuple[Familiarity,
                                          tuple[SignalEvidence, ...]]:
    count = 0
    try:
        envelope = state.envelope
        profile = plugin.profile_store.get_user(
            getattr(envelope.platform, "value", str(envelope.platform)),
            "dududa", envelope.sender.actor_id)
        count = int(getattr(profile, "interaction_count", 0) or 0)
    except Exception:
        count = 0
    value = (Familiarity.NEW if count < 3 else
             Familiarity.FAMILIAR if count < 20 else Familiarity.CLOSE)
    return value, (SignalEvidence(
        SignalName.FAMILIARITY,
        SignalSource.PROFILE if count else SignalSource.DEFAULT,
        "familiarity.interaction_count.v1",
        ConfidenceBand.HIGH if count else ConfidenceBand.MEDIUM),)


def _scene(state, text: str, origin: ResponseOrigin,
           risk: RiskLevel, emotion: Emotion) -> tuple[Scene,
                                                       tuple[SignalEvidence, ...]]:
    if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        scene, rule = Scene.HIGH_RISK, "scene.risk_override.v1"
    elif origin == ResponseOrigin.TOOL:
        scene, rule = Scene.TOOL_RESULT, "scene.tool_origin.v1"
    elif origin in (
            ResponseOrigin.COMMAND, ResponseOrigin.PROGRESS,
            ResponseOrigin.SUBSCRIPTION, ResponseOrigin.USER_CANCELLED,
            ResponseOrigin.SYSTEM_ERROR):
        scene, rule = Scene.TASK, "scene.control_plane_origin.v1"
    elif _IDENTITY_PROBE_RE.search(text):
        scene, rule = Scene.IDENTITY_PROBE, "scene.identity_probe.v1"
    elif emotion == Emotion.NEGATIVE:
        scene, rule = Scene.EMOTIONAL_SUPPORT, "scene.negative_emotion.v1"
    else:
        perception = getattr(state, "perception", None)
        action = getattr(state, "social_decision", None)
        if (action == SocialAction.USE_TOOLS
                or getattr(perception, "needs_tools", False)
                or _QUESTION_RE.search(text.strip())):
            scene, rule = Scene.INFORMATION, "scene.question_or_lookup.v1"
        elif _PRAISE_RE.search(text):
            scene, rule = (
                Scene.PRIDE_ACKNOWLEDGED,
                "scene.user_praise_acknowledged.v1")
        elif getattr(perception, "is_explicit_command", False):
            scene, rule = Scene.TASK, "scene.explicit_command.v1"
        else:
            scene, rule = Scene.CASUAL_CHAT, "scene.casual_default.v1"
    return scene, (SignalEvidence(
        SignalName.SCENE, SignalSource.DETERMINISTIC_RULE,
        rule, ConfidenceBand.MEDIUM),)


def _user_preference(plugin, state) -> tuple[UserStylePreference,
                                             tuple[SignalEvidence, ...]]:
    try:
        envelope = state.envelope
        style = plugin.style_store.get(
            getattr(envelope.platform, "value", str(envelope.platform)),
            "dududa", envelope.sender.actor_id,
            getattr(plugin.personas, "active_id", "dududa_default"))
        kind = getattr(envelope, "kind", None)
        kind_value = getattr(kind, "value", str(kind or ""))
        if (style is not None and not style.visible_in_context(
                envelope.conversation.conversation_id,
                is_group=kind_value == "group")):
            style = None
    except Exception:
        style = None
    if style is None:
        return UserStylePreference(), ()
    humor = 2 if style.tone == "teasing" else 0 if style.tone in (
        "formal", "gentle") else 1 if style.tone == "casual" else None
    allow = False if style.emoji == "off" else True if style.emoji == "on" else None
    evidence: list[SignalEvidence] = []
    if humor is not None:
        evidence.append(SignalEvidence(
            SignalName.USER_HUMOR_PREFERENCE,
            SignalSource.USER_PREFERENCE,
            "style_store.tone.v1", ConfidenceBand.HIGH))
    if allow is not None:
        evidence.append(SignalEvidence(
            SignalName.USER_KAOMOJI_PREFERENCE,
            SignalSource.USER_PREFERENCE,
            "style_store.emoji.v1", ConfidenceBand.HIGH))
    return UserStylePreference(humor, allow), tuple(evidence)


def _continuation_value(
    scene: Scene,
    emotion: Emotion,
) -> tuple[ContinuationValue, tuple[SignalEvidence, ...]]:
    """Evaluate conversational continuation centrally, never per adapter."""
    if scene == Scene.EMOTIONAL_SUPPORT and emotion == Emotion.NEGATIVE:
        value, rule = (
            ContinuationValue.USEFUL,
            "continuation.emotional_support.v1",
        )
    else:
        value, rule = (
            ContinuationValue.NONE,
            "continuation.no_verified_value.v1",
        )
    return value, (SignalEvidence(
        SignalName.CONTINUATION_VALUE,
        SignalSource.DETERMINISTIC_RULE,
        rule,
        ConfidenceBand.MEDIUM,
    ),)


def resolve_response_policy_shadow(
    plugin,
    event,
    response: str,
    *,
    run_id: str,
    origin_override: Optional[ResponseOrigin] = None,
    state_override=None,
) -> ShadowResolution:
    state = (state_override if state_override is not None
             else _runtime_state(plugin, run_id))
    text = str(getattr(event, "message_str", "") or "")
    origin = _origin(event, state, origin_override)
    risk, safety, risk_evidence = _risk(state, text)
    emotion, intensity, emotion_evidence = _emotion(text)
    scene, scene_evidence = _scene(state, text, origin, risk, emotion)
    familiarity, familiarity_evidence = _familiarity(plugin, state)
    perception_score, perception_evidence = _perception(state, text)
    grounding_score, grounding_evidence = _grounding(state, response)
    origin_evidence = (SignalEvidence(
        SignalName.RESPONSE_ORIGIN,
        SignalSource.PLATFORM_FACT,
        "origin.visible_path.v1",
        ConfidenceBand.HIGH),)
    all_evidence = (
        risk_evidence + emotion_evidence + scene_evidence
        + familiarity_evidence + perception_evidence
        + grounding_evidence + origin_evidence
    )
    style_signals = StyleSignals(
        scene=scene,
        emotion=emotion,
        emotion_intensity=intensity,
        risk_level=risk,
        familiarity=familiarity,
        response_origin=origin,
        perception_confidence=perception_score,
        grounding_confidence=grounding_score,
        evidence=all_evidence,
    )
    missing: tuple[str, ...] = ()
    pending = _event_extra(event, "dududa_pending_followup_kind")
    if pending:
        missing = (pending,)
    elif getattr(state, "social_decision", None) == SocialAction.ASK:
        missing = ("runtime_clarification",)
    interaction_evidence: tuple[SignalEvidence, ...] = ()
    if missing:
        interaction_evidence = (SignalEvidence(
            SignalName.MISSING_REQUIRED_FIELDS,
            SignalSource.DETERMINISTIC_RULE,
            "interaction.pending_required_field.v1",
            ConfidenceBand.HIGH),)
    continuation, continuation_evidence = _continuation_value(
        scene, emotion)
    interaction_signals = InteractionSignals(
        missing_required_fields=missing,
        continuation_value=continuation,
        safety_clarification_required=safety.clarification_required,
        evidence=(interaction_evidence + continuation_evidence
                  + safety.evidence),
    )
    interaction = InteractionPolicyResolver.resolve(
        interaction_signals, safety)
    persona_obj = getattr(getattr(plugin, "personas", None), "active", None)
    traits = getattr(persona_obj, "traits", None)
    sassiness = float(getattr(traits, "sassiness", 0.0) or 0.0)
    persona = PersonaStyleDefaults(
        humor_level=2 if sassiness >= 0.65 else 1,
        allows_kaomoji=bool(
            getattr(getattr(persona_obj, "tone", None),
                    "use_kaomoji", True)),
    )
    user, user_evidence = _user_preference(plugin, state)
    style = OutputStylePolicyResolver.resolve(
        style_signals, persona=persona, user=user)
    resolved = ResolvedResponsePolicy(
        interaction=interaction, style=style)
    violations = list(style_contract_violations(response, style))
    has_question = bool(re.search(r"[？?]", str(response or "")))
    if (interaction.followup_mode.value == "forbidden" and has_question):
        violations.append("unexpected_followup")
    if (interaction.followup_mode.value == "required" and not has_question):
        violations.append("missing_required_followup")
    return ShadowResolution(
        policy=resolved,
        safety=safety,
        style_signals=style_signals,
        interaction_signals=interaction_signals,
        violations=tuple(dict.fromkeys(violations)),
        evidence=(all_evidence + interaction_evidence
                  + continuation_evidence + user_evidence),
    )


def trace_response_policy_shadow(
    plugin,
    event,
    response: str,
    *,
    run_id: str,
    trace_id: str,
    origin_override: Optional[ResponseOrigin] = None,
) -> Optional[ShadowResolution]:
    if os.environ.get("DUDUDA_RESPONSE_POLICY_SHADOW", "1") != "1":
        return None
    if not str(response or "").strip():
        return None
    try:
        result = resolve_response_policy_shadow(
            plugin, event, response, run_id=run_id,
            origin_override=origin_override)
        style = result.policy.style
        interaction = result.policy.interaction
        trace_recorder.record(
            event="response_policy_shadow",
            shadow_only=True,
            run_id=run_id,
            trace_id=trace_id,
            response_origin=result.style_signals.response_origin.value,
            scene=result.style_signals.scene.value,
            risk_level=result.style_signals.risk_level.value,
            emotion=result.style_signals.emotion.value,
            emotion_intensity=result.style_signals.emotion_intensity.value,
            perception_confidence_band=confidence_band(
                result.style_signals.perception_confidence).value,
            grounding_confidence_band=confidence_band(
                result.style_signals.grounding_confidence).value,
            policy_version=style.policy_version,
            policy_fingerprint=result.policy.fingerprint(),
            persona_version=PERSONA_KERNEL_VERSION,
            persona_preset_version=str(getattr(
                getattr(getattr(plugin, "personas", None), "active", None),
                "version", "")),
            tone=style.tone.value,
            humor_level=style.humor_level,
            max_kaomoji=style.max_kaomoji,
            followup_mode=interaction.followup_mode.value,
            contract_violations=list(result.violations),
            fallback_reason=(
                _event_extra(event, _EXTRA_FALLBACK) or "none"),
            message_key=(
                _event_extra(event, _EXTRA_MESSAGE_KEY) or "not_applicable"),
            variant_id=(
                _event_extra(event, _EXTRA_VARIANT_ID) or "not_applicable"),
            variant_applied=(
                _event_extra(event, _EXTRA_VARIANT_APPLIED) == "1"),
            variant_seed_source=(
                _event_extra(event, _EXTRA_VARIANT_SEED_SOURCE)
                or "not_applicable"),
            variant_selector_version=SELECTOR_VERSION,
            signal_evidence=[item.to_dict() for item in result.evidence],
        )
        return result
    except Exception as exc:
        trace_recorder.record(
            event="response_policy_shadow_error",
            shadow_only=True,
            run_id=run_id,
            trace_id=trace_id,
            error=type(exc).__name__,
        )
        return None


class _ProactiveEvent:
    """Minimal event for visible sends that have no incoming AstrBot event."""

    message_str = ""

    def __init__(self):
        self._extras = {}

    def get_extra(self, key):
        return self._extras.get(key)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_messages(self):
        return ()


def trace_proactive_response_shadow(
    plugin,
    response: str,
    *,
    origin: ResponseOrigin,
    run_id: str = "",
    trace_id: str = "",
) -> Optional[ShadowResolution]:
    """Trace subscription/broadcast sends without retaining destination data."""
    event = _ProactiveEvent()
    return trace_response_policy_shadow(
        plugin,
        event,
        response,
        run_id=run_id or uuid4().hex,
        trace_id=trace_id or uuid4().hex,
        origin_override=origin,
    )


def trace_adapter_response_shadow(
    plugin,
    event,
    response: str,
    *,
    origin: ResponseOrigin,
) -> Optional[ShadowResolution]:
    """Trace a visible adapter/command response with its event identity."""
    run_id = _event_extra(event, "dududa_policy_run_id") or uuid4().hex
    trace_id = _event_extra(event, "dududa_policy_trace_id") or uuid4().hex
    _set_event_extra(event, "dududa_policy_run_id", run_id)
    _set_event_extra(event, "dududa_policy_trace_id", trace_id)
    mark_response_origin(event, origin)
    return trace_response_policy_shadow(
        plugin,
        event,
        response,
        run_id=run_id,
        trace_id=trace_id,
        origin_override=origin,
    )


def command_result_shadow(plugin, event, response: str):
    """Trace and adapt a deterministic command result in one thin-shell call."""
    trace_adapter_response_shadow(
        plugin, event, response, origin=ResponseOrigin.COMMAND)
    return event.plain_result(response)


def trace_subscription_response_shadow(
    plugin, response: str
) -> Optional[ShadowResolution]:
    return trace_proactive_response_shadow(
        plugin, response, origin=ResponseOrigin.SUBSCRIPTION)
