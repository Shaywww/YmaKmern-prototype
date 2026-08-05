"""嘟嘟哒 2.0 Perception —— 回答"这条消息表达了什么"。

输出版本化 PerceptionResult：目标用户、言语行为、话题、实体、
指代、候选意图、工具需求、置信度和歧义。
平台确认的 @、回复链和命令事实优先于模型推断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
