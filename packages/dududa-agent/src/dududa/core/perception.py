"""嘟嘟哒 2.0 Perception —— 回答"这条消息表达了什么"。

输出版本化 PerceptionResult：目标用户、言语行为、话题、实体、
指代、候选意图、工具需求、置信度和歧义。
平台确认的 @、回复链和命令事实优先于模型推断。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4


@dataclass(frozen=True)
class EntityRef:
    """识别的实体引用。"""
    name: str
    entity_type: str   # person | course | time | location | topic | ...
    confidence: float  # 0.0 - 1.0
    evidence: str = "" # 从上下文中提取的证据片段


@dataclass(frozen=True)
class SpeechAct:
    """言语行为分类。"""
    act_type: str       # question | statement | command | greeting | complaint | ...
    confidence: float


@dataclass(frozen=True)
class PerceptionResult:
    """感知结果 —— 版本化、结构化。

    Structured Output 整体不通过 Schema 时结果无效，
    不能挑出其中几个看似合理的字段继续执行。
    """
    schema_version: str = "1.0"

    # 目标用户（消息指向谁）
    target_users: tuple[str, ...] = ()  # actor_id 列表；空 = 全员

    # 言语行为
    speech_acts: tuple[SpeechAct, ...] = ()

    # 话题
    topics: tuple[str, ...] = ()

    # 实体
    entities: tuple[EntityRef, ...] = ()

    # 指代消解
    resolved_references: dict[str, str] = field(default_factory=dict)

    # 候选意图
    candidate_intents: tuple[str, ...] = ()  # course_query | chitchat | help | ...

    # 模型感知附带的工具计划（可选）：{"steps":[{"capability_id","arguments"}]}
    # 由 _perceive_with_model 从模型信号透传，供规划阶段直接采用（省一次调用）
    tool_plan: Optional[dict] = None

    # 工具需求（语义信号，不是授权）
    needs_tools: bool = False
    suggested_capabilities: tuple[str, ...] = ()

    # 整体置信度与歧义
    confidence: float = 0.5
    ambiguities: tuple[str, ...] = ()

    # 平台确认的事实（@、回复链、命令），优先于模型推断
    has_explicit_mention: bool = False
    has_reply_chain: bool = False
    is_explicit_command: bool = False

    def is_addressed_to(self, actor_id: str) -> bool:
        """检查是否指向特定参与者。"""
        if not self.target_users:
            return True  # 未指定目标 = 全员
        return actor_id in self.target_users

    def is_question(self) -> bool:
        return any(a.act_type == "question" for a in self.speech_acts)

    def is_command(self) -> bool:
        return self.is_explicit_command or any(
            a.act_type == "command" for a in self.speech_acts
        )

    def to_record(self, *, record_id: str = "", run_id: str = "",
                  trace_id: str = "", ts_ms: Optional[int] = None,
                  platform: str = "", bot_id: str = "",
                  conversation_id: str = "", actor_id: str = "",
                  text: str = "", source: str = "rule") -> "PerceptionRecord":
        """把感知结果落成可入库的结构化 PerceptionRecord（文档 2.5.4）。"""
        return PerceptionRecord(
            record_id=record_id or uuid4().hex,
            run_id=run_id,
            trace_id=trace_id,
            ts_ms=ts_ms if ts_ms is not None else int(time.time() * 1000),
            platform=platform,
            bot_id=bot_id,
            conversation_id=conversation_id,
            actor_id=actor_id,
            text=text,
            source=source,
            schema_version=self.schema_version,
            speech_acts=self.speech_acts,
            topics=self.topics,
            entities=self.entities,
            resolved_references=dict(self.resolved_references or {}),
            candidate_intents=self.candidate_intents,
            needs_tools=self.needs_tools,
            suggested_capabilities=self.suggested_capabilities,
            confidence=self.confidence,
            ambiguities=self.ambiguities,
            has_explicit_mention=self.has_explicit_mention,
            has_reply_chain=self.has_reply_chain,
            is_explicit_command=self.is_explicit_command,
            valid=self.is_valid,
        )

    @property
    def is_valid(self) -> bool:
        """检查结果是否通过基本 Schema 校验。"""
        if self.schema_version != "1.0":
            return False
        if self.confidence < 0.0 or self.confidence > 1.0:
            return False
        for e in self.entities:
            if e.confidence < 0.0 or e.confidence > 1.0:
                return False
        return True


@dataclass(frozen=True)
class PerceptionRecord:
    """感知结果的结构化快照（入库版本）。

    绑定 run_id/trace_id 与所在会话（platform/bot/conversation/actor），
    保留完整证据（文本、言语行为、实体、指代、意图、工具需求、置信度、
    歧义与平台事实），供 Eval、用户画像与后续 WebUI 使用。
    """
    record_id: str
    run_id: str = ""
    trace_id: str = ""
    ts_ms: int = 0
    platform: str = ""
    bot_id: str = ""
    conversation_id: str = ""
    actor_id: str = ""
    text: str = ""
    source: str = "rule"          # rule | model | injected
    schema_version: str = "1.0"
    speech_acts: tuple[SpeechAct, ...] = ()
    topics: tuple[str, ...] = ()
    entities: tuple[EntityRef, ...] = ()
    resolved_references: dict[str, str] = field(default_factory=dict)
    candidate_intents: tuple[str, ...] = ()
    needs_tools: bool = False
    suggested_capabilities: tuple[str, ...] = ()
    confidence: float = 0.0
    ambiguities: tuple[str, ...] = ()
    has_explicit_mention: bool = False
    has_reply_chain: bool = False
    is_explicit_command: bool = False
    valid: bool = True

    def to_dict(self) -> dict:
        """JSON 可序列化视图（ts/ts_ms 由 Store 统一写入）。"""
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
            "text": self.text,
            "source": self.source,
            "schema_version": self.schema_version,
            "speech_acts": [[a.act_type, a.confidence]
                            for a in self.speech_acts],
            "topics": list(self.topics),
            "entities": [{"name": e.name, "entity_type": e.entity_type,
                          "confidence": e.confidence, "evidence": e.evidence}
                         for e in self.entities],
            "resolved_references": dict(self.resolved_references or {}),
            "candidate_intents": list(self.candidate_intents),
            "needs_tools": self.needs_tools,
            "suggested_capabilities": list(self.suggested_capabilities),
            "confidence": self.confidence,
            "ambiguities": list(self.ambiguities),
            "has_explicit_mention": self.has_explicit_mention,
            "has_reply_chain": self.has_reply_chain,
            "is_explicit_command": self.is_explicit_command,
            "valid": self.valid,
        }
