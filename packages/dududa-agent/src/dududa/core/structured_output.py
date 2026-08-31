# -*- coding: utf-8 -*-
"""Structured Output 校验、规则-模型 Merger 与安全降级（文档 2.5.4）。

- StructuredOutputValidator：模型输出整体过 Schema 才有效；
  任一字段非法 -> 整包丢弃（fail closed，不挑字段继续执行）。
- PerceptionMerger：规则结果优先（平台事实 @/回复链/命令 永远以规则为准），
  模型信号在置信度达标时补充言语行为/话题/实体/意图；
  模型缺失、非法或置信度不足 -> 只用规则（模型失败时减少主动回复）。
- decision_from_signal：把模型提议的六动作决策转成 SocialDecision，
  未知 action / reason code 一律拒绝。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .decision import DecisionReason, SocialDecision
from .perception import EntityRef, PerceptionResult, SpeechAct
from .state import SocialAction

# 允许的言语行为集合（模型输出不在此集合 -> 整包无效）
KNOWN_SPEECH_ACTS = frozenset({
    "question", "statement", "command", "greeting", "complaint",
    "acknowledgment", "farewell", "noun_query",
})

# 模型 action -> SocialAction 映射（含兼容别名）
_ACTION_MAP = {
    "ignore": SocialAction.IGNORE,
    "react": SocialAction.REACT,
    "direct_reply": SocialAction.DIRECT_REPLY,
    "answer": SocialAction.DIRECT_REPLY,          # 旧名别名
    "ask_clarification": SocialAction.ASK_CLARIFICATION,
    "ask": SocialAction.ASK_CLARIFICATION,        # 旧名别名
    "use_tools": SocialAction.USE_TOOLS,
    "defer": SocialAction.DEFER,
    "block": SocialAction.BLOCK,
}

DEFAULT_MIN_MODEL_CONFIDENCE = 0.5

# These are model/runtime primitives, not user-facing external tools.  A
# perception model occasionally hallucinates ``chat`` as a tool step for an
# ordinary opinion question; accepting that step sends the message through a
# second chat call and can surface the provider's generic fallback text.
INTERNAL_MODEL_CAPABILITIES = frozenset({"chat", "vision", "file_reader"})

# 生产可选：模型感知信号的严格 JSON 指令（DUDUDA_PERCEPTION_MODEL=1 启用）
PERCEPTION_SYSTEM_PROMPT = (
    "你是感知模块，只输出严格 JSON，不要任何其他文字。"
    "字段: confidence(0-1), speech_acts:[{act_type,confidence}], topics:[], "
    "entities:[{name,entity_type,confidence,evidence}], candidate_intents:[], "
    "suggested_capabilities:[], needs_tools:bool, ambiguities:[], "
    "tool_plan:{steps:[{capability_id,arguments}]}。"
    "act_type 只能是 question/statement/command/greeting/complaint/"
    "acknowledgment/farewell/noun_query。"
    "tool_plan 仅在需要查实时数据/执行操作时给出（不需要时给 {\"steps\":[]}）；"
    "开放式生活建议、吃什么、穿什么等普通闲聊不需要联网；"
    "不能仅因为消息里出现地名就调用天气工具，必须明确提到天气、气温或降水；"
    "capability_id 必须从可用工具中选，arguments 键名必须与工具参数一致。"
)


def _as_float01(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0.0 or v > 1.0:
        return None
    return v


def _as_str_list(value: Any) -> Optional[list[str]]:
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        out.append(item.strip())
    return out


def _parse_raw(raw: Any) -> Optional[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    return raw


def _parse_tool_plan(raw: Any) -> Optional[dict]:
    """规范化感知信号附带的工具计划（可选字段，fail closed）。

    结构: {"steps":[{"capability_id": str, "arguments": {标量键值}}]}。
    任何一步结构非法 -> 整体返回 None（不挑字段继续执行）。
    """
    if not isinstance(raw, dict):
        return None
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        return None
    steps = []
    for sr in steps_raw:
        if not isinstance(sr, dict):
            return None
        cid = sr.get("capability_id")
        args = sr.get("arguments") or {}
        if not isinstance(cid, str) or not cid.strip():
            return None
        if not isinstance(args, dict):
            return None
        clean = {}
        for k, v in args.items():
            if not isinstance(k, str) or not k.strip():
                return None
            if isinstance(v, (str, int, float, bool)):
                clean[k.strip()] = v
        steps.append({"capability_id": cid.strip(), "arguments": clean})
    return {"steps": steps}


class StructuredOutputValidator:
    """整体 Schema 校验（fail closed）。"""

    @staticmethod
    def validate_perception_signal(raw: Any) -> Optional[dict]:
        """模型感知信号 -> 规范化 dict；整体不通过返回 None。"""
        raw = _parse_raw(raw)
        if raw is None:
            return None
        confidence = _as_float01(raw.get("confidence", 0.5))
        if confidence is None:
            return None
        acts_raw = raw.get("speech_acts", [])
        if not isinstance(acts_raw, list):
            return None
        speech_acts = []
        for a in acts_raw:
            if not isinstance(a, dict):
                return None
            act_type = a.get("act_type")
            act_conf = _as_float01(a.get("confidence", 1.0))
            if not isinstance(act_type, str) or act_type not in KNOWN_SPEECH_ACTS:
                return None
            if act_conf is None:
                return None
            speech_acts.append({"act_type": act_type, "confidence": act_conf})
        ents_raw = raw.get("entities", [])
        if not isinstance(ents_raw, list):
            return None
        entities = []
        for e in ents_raw:
            if not isinstance(e, dict):
                return None
            name = e.get("name")
            etype = e.get("entity_type")
            econf = _as_float01(e.get("confidence", 1.0))
            evidence = e.get("evidence", "")
            if not isinstance(name, str) or not name.strip():
                return None
            if not isinstance(etype, str) or not etype.strip():
                return None
            if econf is None or not isinstance(evidence, str):
                return None
            entities.append({
                "name": name.strip(), "entity_type": etype.strip(),
                "confidence": econf, "evidence": evidence,
            })
        topics = _as_str_list(raw.get("topics", []))
        intents = _as_str_list(raw.get("candidate_intents", []))
        caps = _as_str_list(raw.get("suggested_capabilities", []))
        ambiguities = _as_str_list(raw.get("ambiguities", []))
        if topics is None or intents is None or caps is None or ambiguities is None:
            return None
        needs_tools = raw.get("needs_tools", False)
        if not isinstance(needs_tools, bool):
            return None
        tool_plan = raw.get("tool_plan")
        if tool_plan is not None:
            tool_plan = _parse_tool_plan(tool_plan)
            if tool_plan is None:
                return None  # 附带的工具计划非法 -> 整包丢弃（fail closed）
        return {
            "confidence": confidence,
            "speech_acts": speech_acts,
            "topics": topics,
            "entities": entities,
            "candidate_intents": intents,
            "suggested_capabilities": caps,
            "ambiguities": ambiguities,
            "needs_tools": needs_tools,
            "tool_plan": tool_plan,
        }

    @staticmethod
    def validate_decision_signal(raw: Any) -> Optional[dict]:
        """模型决策信号 -> 规范化 dict；未知 action/reason 返回 None。"""
        raw = _parse_raw(raw)
        if raw is None:
            return None
        action = _ACTION_MAP.get(str(raw.get("action", "")).strip().lower())
        if action is None:
            return None
        reasons_raw = raw.get("reason_codes", [])
        if not isinstance(reasons_raw, list):
            return None
        reasons = []
        for r in reasons_raw:
            if not isinstance(r, str):
                return None
            try:
                reasons.append(DecisionReason(r.strip()))
            except ValueError:
                return None  # 未知原因码 -> fail closed
        confidence = _as_float01(raw.get("confidence", 0.8))
        if confidence is None:
            return None
        use_tools = raw.get("should_use_tools", False)
        if not isinstance(use_tools, bool):
            return None
        question = raw.get("clarification_question")
        if question is not None and not isinstance(question, str):
            return None
        return {
            "action": action,
            "reason_codes": tuple(reasons),
            "confidence": confidence,
            "should_use_tools": use_tools,
            "clarification_question": question,
        }


class PerceptionMerger:
    """规则感知结果 + 模型信号 -> 合并结果（规则优先，安全降级）。"""

    def __init__(self, min_model_confidence: float = DEFAULT_MIN_MODEL_CONFIDENCE):
        self._min_model_confidence = min_model_confidence

    def merge(self, rule: PerceptionResult,
              signal: Optional[dict]) -> PerceptionResult:
        if signal is None:
            return rule
        # 安全降级：模型置信度不足 -> 只用规则（模型失败时减少主动回复）
        if float(signal.get("confidence", 0.0)) < self._min_model_confidence:
            return rule
        speech_acts = list(rule.speech_acts)
        seen = {a.act_type for a in speech_acts}
        for a in signal.get("speech_acts", ()):
            if a["act_type"] not in seen:
                speech_acts.append(SpeechAct(
                    act_type=a["act_type"], confidence=a["confidence"]))
                seen.add(a["act_type"])
        entities = list(rule.entities)
        seen_entities = {(e.name, e.entity_type) for e in entities}
        for e in signal.get("entities", ()):
            key = (e["name"], e["entity_type"])
            if key not in seen_entities:
                entities.append(EntityRef(
                    name=e["name"], entity_type=e["entity_type"],
                    confidence=e["confidence"], evidence=e["evidence"]))
                seen_entities.add(key)
        model_tool_plan = signal.get("tool_plan")
        if model_tool_plan:
            external_steps = [
                step for step in model_tool_plan.get("steps", ())
                if step.get("capability_id") not in INTERNAL_MODEL_CAPABILITIES
            ]
            model_tool_plan = ({"steps": external_steps}
                               if external_steps else None)
        # A bare model ``needs_tools=true`` is not executable evidence.  The
        # deterministic rules may still request discovery, while a model may
        # promote a message only by proposing at least one external step that
        # has already passed the strict schema validator above.
        model_needs_tools = bool(
            model_tool_plan and model_tool_plan.get("steps"))
        return PerceptionResult(
            schema_version=rule.schema_version,
            target_users=rule.target_users,
            speech_acts=tuple(speech_acts),
            topics=tuple(dict.fromkeys(
                list(rule.topics) + signal.get("topics", []))),
            entities=tuple(entities),
            resolved_references=dict(rule.resolved_references or {}),
            candidate_intents=tuple(dict.fromkeys(
                list(rule.candidate_intents)
                + signal.get("candidate_intents", []))),
            needs_tools=rule.needs_tools or model_needs_tools,
            tool_plan=model_tool_plan or rule.tool_plan,
            suggested_capabilities=tuple(dict.fromkeys(
                list(rule.suggested_capabilities)
                + signal.get("suggested_capabilities", []))),
            confidence=max(rule.confidence,
                           float(signal.get("confidence", 0.0))),
            ambiguities=tuple(dict.fromkeys(
                list(rule.ambiguities) + signal.get("ambiguities", []))),
            has_explicit_mention=rule.has_explicit_mention,
            has_reply_chain=rule.has_reply_chain,
            is_explicit_command=rule.is_explicit_command,
        )


def merge_perception_with_model(
        rule: PerceptionResult, raw_signal: Any,
        min_model_confidence: float = DEFAULT_MIN_MODEL_CONFIDENCE,
) -> tuple[PerceptionResult, bool]:
    """生产入口：校验 + 合并 + 安全降级。

    返回 (merged, used)。used=False 表示模型信号被丢弃（非法/低置信度），
    调用方应继续使用规则结果。
    """
    signal = StructuredOutputValidator.validate_perception_signal(raw_signal)
    merged = PerceptionMerger(
        min_model_confidence=min_model_confidence).merge(rule, signal)
    return merged, merged is not rule


def decision_from_signal(
        raw_signal: Any,
        min_model_confidence: float = DEFAULT_MIN_MODEL_CONFIDENCE,
) -> Optional[SocialDecision]:
    """模型决策信号 -> SocialDecision；非法/低置信度返回 None（安全降级）。"""
    signal = StructuredOutputValidator.validate_decision_signal(raw_signal)
    if signal is None:
        return None
    if float(signal["confidence"]) < min_model_confidence:
        return None
    return SocialDecision(
        action=signal["action"],
        reason_codes=signal["reason_codes"],
        confidence=signal["confidence"],
        should_use_tools=signal["should_use_tools"],
        clarification_question=signal["clarification_question"],
    )
