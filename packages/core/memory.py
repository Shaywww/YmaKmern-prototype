"""嘟嘟哒 2.0 Memory System —— 受控记忆的记录、检索与写入。

核心原则：
- 消息、工具事件和最终回复只能产生 MemoryCandidate
- Write Gate 检查后才能落盘
- 依赖 Delivery Receipt 的写入语义
- 跨群、用户、私聊、Bot、Persona 隔离
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from .state import WriteGateDecision


_logger = logging.getLogger("dududa20.memory")


class MemoryType(str, Enum):
    """记忆类型。"""
    SESSION_STATE = "session_state"    # 本次会话内状态
    SHORT_TERM = "short_term"          # 近期对话
    USER_PROFILE = "user_profile"      # 用户画像（跨会话）
    GROUP_MEMORY = "group_memory"      # 群级记忆
    EPISODIC = "episodic"              # 事件记忆
    EXPLICIT = "explicit"              # 用户显式声明（/remember）


class SensitivityLevel(str, Enum):
    """敏感度分级。"""
    PUBLIC = "public"                  # 可公开
    INTERNAL = "internal"              # 群内可见
    PRIVATE = "private"                # 仅当事人可见
    RESTRICTED = "restricted"          # 受控（需确认）


@dataclass(frozen=True)
class MemoryScope:
    """记忆隔离范围。

    精确指定记忆所属的维度。所有维度必须精确匹配才能检索。
    """
    memory_type: MemoryType
    platform: str
    bot_id: str
    conversation_id: str
    actor_id: str
    persona_id: Optional[str] = None

    def to_key(self) -> str:
        parts = [
            self.memory_type.value,
            self.platform,
            self.bot_id,
            self.conversation_id,
            self.actor_id,
            self.persona_id or "*",
        ]
        return "|".join(parts)

    def is_subset_of(self, other: "MemoryScope") -> bool:
        """检查是否被另一个 Scope 包含。"""
        return (
            self.memory_type == other.memory_type
            and self.platform == other.platform
            and self.bot_id == other.bot_id
            and self.conversation_id == other.conversation_id
            and self.actor_id == other.actor_id
            and (self.persona_id or "*") == (other.persona_id or "*")
        )


@dataclass(frozen=True)
class ScopeSelector:
    """具名检索选择器（文档 2.4.22）。

    字段精确匹配；None 表示不限定该维度。跨类型检索（如同时召回
    episodic 与 short_term）必须显式使用 Selector，不走全局宽松匹配。
    """

    memory_type: Optional[MemoryType] = None
    platform: Optional[str] = None
    bot_id: Optional[str] = None
    conversation_id: Optional[str] = None
    actor_id: Optional[str] = None
    persona_id: Optional[str] = None

    @classmethod
    def from_scope(cls, scope: "MemoryScope") -> "ScopeSelector":
        return cls(
            memory_type=scope.memory_type,
            platform=scope.platform,
            bot_id=scope.bot_id,
            conversation_id=scope.conversation_id,
            actor_id=scope.actor_id,
            persona_id=scope.persona_id,
        )

    def matches(self, record: "MemoryRecord") -> bool:
        s = record.scope
        if self.memory_type is not None and s.memory_type != self.memory_type:
            return False
        if self.platform is not None and s.platform != self.platform:
            return False
        if self.bot_id is not None and s.bot_id != self.bot_id:
            return False
        if self.conversation_id is not None and s.conversation_id != self.conversation_id:
            return False
        if self.actor_id is not None and s.actor_id != self.actor_id:
            return False
        if self.persona_id is not None and (s.persona_id or "*") != self.persona_id:
            return False
        return True


@dataclass(frozen=True)
class MemoryRecord:
    """一条结构化记忆。"""
    record_id: str = field(default_factory=lambda: uuid4().hex)
    scope: MemoryScope = field(
        default_factory=lambda: MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq",
            bot_id="dududa",
            conversation_id="unknown",
            actor_id="unknown",
        )
    )
    content: str = ""
    source: str = ""          # message | tool | explicit | inference
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    visibility: SensitivityLevel = SensitivityLevel.INTERNAL  # 展示可见性
    evidence: tuple[str, ...] = ()  # 支撑证据
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    ttl_seconds: Optional[int] = 86400  # 默认 24h
    access_count: int = 0
    last_accessed: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return (
            datetime.now(timezone.utc)
            > self.created_at + __import__("datetime").timedelta(
                seconds=self.ttl_seconds
            )
        )

    def accessed(self) -> "MemoryRecord":
        """返回访问后的新记录（access_count +1）。"""
        return MemoryRecord(
            **{
                **self.__dict__,
                "access_count": self.access_count + 1,
                "last_accessed": datetime.now(timezone.utc),
            }
        )


@dataclass(frozen=True)
class MemoryCandidate:
    """待审核的记忆候选。

    消息、工具事件和最终回复只能产生 MemoryCandidate。
    """
    candidate_id: str = field(default_factory=lambda: uuid4().hex)
    proposed_record: MemoryRecord = field(default_factory=MemoryRecord)
    requires_delivery_ack: bool = False  # 是否需要等待 DeliveryReceipt
    delivery_run_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WriteGate:
    """记忆写入门禁。

    检查来源、证据、敏感度、未来价值、重复、冲突、
    Scope、TTL、确认和投递依赖。
    """

    def __init__(self, repository: "MemoryRepository"):
        self._repo = repository

    def evaluate(self, candidate: MemoryCandidate) -> WriteGateDecision:
        """评估候选记忆，返回写入决策。"""
        record = candidate.proposed_record

        # 1. 内容检查
        if not record.content.strip():
            return WriteGateDecision.REJECT

        # 2. 敏感度检查
        if record.sensitivity == SensitivityLevel.RESTRICTED:
            return WriteGateDecision.REQUIRE_CONFIRMATION

        # 3. 投递依赖检查
        if candidate.requires_delivery_ack and not candidate.delivery_run_id:
            return WriteGateDecision.DEFER_FOR_CONFLICT

        # 4. 重复 / 冲突检查（保留双方证据，不静默覆盖）
        existing = self._repo.find_similar(record, threshold=0.8)
        if existing is not None:
            if existing.content.strip() == record.content.strip():
                return WriteGateDecision.REJECT  # 完全重复
            return WriteGateDecision.DEFER_FOR_CONFLICT  # 同 Scope 内容冲突
        # 同 Scope 长内容包含短内容视为冲突（新信息覆盖旧信息，保留双方证据）
        for r in self._repo.query(record.scope, limit=20):
            a = r.content.strip()
            b = record.content.strip()
            if not a or not b or len(a) < 8 or len(b) < 8:
                continue
            if a in b or b in a:
                return WriteGateDecision.DEFER_FOR_CONFLICT

        # 5. 来源与证据检查
        if not record.source or not record.evidence:
            return WriteGateDecision.REQUIRE_CONFIRMATION

        return WriteGateDecision.ALLOW

    def evaluate_explicit(
        self, candidate: MemoryCandidate
    ) -> WriteGateDecision:
        """评估用户显式声明的记忆（/remember）。"""
        # 显式声明的记忆信任度更高
        if not candidate.proposed_record.content.strip():
            return WriteGateDecision.REJECT
        if candidate.proposed_record.sensitivity == SensitivityLevel.RESTRICTED:
            return WriteGateDecision.REQUIRE_CONFIRMATION
        return WriteGateDecision.ALLOW


class MemoryRepository(ABC):
    """抽象记忆仓库接口。

    实现可以是：InMemoryRepository、JSONRepository、SQLiteRepository、
    或外部 Adapter（如 Iris）。
    """

    @abstractmethod
    def write(self, record: MemoryRecord) -> str:
        """写入一条记忆，返回 record_id。"""
        ...

    @abstractmethod
    def query(
        self, scope: MemoryScope, limit: int = 20
    ) -> tuple[MemoryRecord, ...]:
        """按 Scope 检索记忆。"""
        ...

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """删除指定记录。"""
        ...

    @abstractmethod
    def find_similar(
        self, record: MemoryRecord, threshold: float = 0.8
    ) -> Optional[MemoryRecord]:
        """查找相似记录。"""
        ...

    @abstractmethod
    def count(self, scope: Optional[MemoryScope] = None) -> int:
        """计数。"""
        ...

    @abstractmethod
    def purge_expired(self) -> int:
        """清理过期记录，返回清理数量。"""
        ...


class InMemoryRepository(MemoryRepository):
    """内存实现 —— 用于测试和原型。"""

    def __init__(self):
        self._records: dict[str, MemoryRecord] = {}

    def write(self, record: MemoryRecord) -> str:
        self._records[record.record_id] = record
        return record.record_id

    def query(
        self, scope: MemoryScope, limit: int = 20
    ) -> tuple[MemoryRecord, ...]:
        results: list[MemoryRecord] = []
        for record in self._records.values():
            if record.is_expired:
                continue
            if record.scope.is_subset_of(scope):
                results.append(record.accessed())
        results.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(results[:limit])

    def query_selector(
        self, selector: ScopeSelector, limit: int = 20
    ) -> tuple[MemoryRecord, ...]:
        """按具名 Selector 检索：字段精确匹配，None 表示不限定。

        跨类型检索（如 episodic + short_term）必须显式使用 Selector，
        不允许走宽松全局匹配。
        """
        results: list[MemoryRecord] = []
        for record in self._records.values():
            if record.is_expired:
                continue
            if selector.matches(record):
                results.append(record.accessed())
        results.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(results[:limit])

    def query_visible(
        self, scope: MemoryScope, viewer_actor_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[MemoryRecord, ...]:
        """可见性过滤检索（文档 2.5.3：Scope -> 权限/隐私 -> 语义排序）。

        - RESTRICTED 永不召回（即使本人）；
        - PRIVATE 仅当 viewer 与记录所属 actor 一致时召回；
        - 其余按 Scope 精确匹配（fail-closed，不做宽松 fallback）。
        """
        results: list[MemoryRecord] = []
        for record in self._records.values():
            if record.is_expired:
                continue
            if not record.scope.is_subset_of(scope):
                continue
            if record.sensitivity == SensitivityLevel.RESTRICTED:
                continue
            if record.sensitivity == SensitivityLevel.PRIVATE:
                if not viewer_actor_id or record.scope.actor_id != viewer_actor_id:
                    continue
            results.append(record.accessed())
        results.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(results[:limit])

    def delete(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None

    def find_similar(
        self, record: MemoryRecord, threshold: float = 0.8
    ) -> Optional[MemoryRecord]:
        for existing in self._records.values():
            if existing.scope.is_subset_of(record.scope):
                similarity = self._text_similarity(
                    existing.content, record.content
                )
                if similarity >= threshold:
                    return existing
        return None

    def count(self, scope: Optional[MemoryScope] = None) -> int:
        if scope is None:
            return len(self._records)
        return sum(
            1
            for r in self._records.values()
            if r.scope.is_subset_of(scope)
        )

    def purge_expired(self) -> int:
        expired = [
            rid for rid, r in self._records.items() if r.is_expired
        ]
        for rid in expired:
            del self._records[rid]
        return len(expired)


    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """简单的文本相似度（Jaccard）。"""
        if not a or not b:
            return 0.0
        set_a = set(a.lower().split())
        set_b = set(b.lower().split())
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)



class JSONMemoryRepository(InMemoryRepository):
    """JSON 文件持久化仓库（fail-closed，文档 2.4.22）。

    - 缺少 Scope 必需字段的记录：写入时拒绝、加载时跳过（不参与召回）；
    - 不提供全局 fallback：检索必须给定精确 Scope 或 ScopeSelector；
    - 每次写入原子落盘（临时文件 + os.replace）。
    """

    REQUIRED_SCOPE_FIELDS = (
        "memory_type", "platform", "bot_id", "conversation_id", "actor_id",
    )

    def __init__(self, path: Optional[str] = None):
        super().__init__()
        self._path = path or os.path.join(
            tempfile.gettempdir(), "dududa_memory.json"
        )
        self._load()

    # -- fail-closed 校验 --

    @staticmethod
    def _validate_scope(scope: MemoryScope) -> None:
        missing = [
            f for f in JSONMemoryRepository.REQUIRED_SCOPE_FIELDS
            if getattr(scope, f, None) in (None, "", "unknown")
        ]
        if missing:
            raise ValueError(f"scope missing required fields: {missing}")

    def write(self, record: MemoryRecord) -> str:
        self._validate_scope(record.scope)
        rid = super().write(record)
        self._save()
        return rid

    # -- 持久化 --

    def _save(self) -> None:
        data = [self._to_dict(r) for r in self._records.values()]
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        os.replace(tmp, self._path)

    def _load(self) -> None:
        # fail-closed：先清空内存，绝不与陈旧/损坏数据混合
        self._records = {}
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # 损坏文件隔离为 .corrupt-<ts>，保留空库（fail-closed，不静默吞数据）
            quarantine = f"{self._path}.corrupt-{int(time.time())}"
            try:
                os.replace(self._path, quarantine)
            except OSError:
                quarantine = self._path
            _logger.warning("Memory file corrupted, quarantined to: %s", quarantine)
            return
        for item in data:
            try:
                record = self._from_dict(item)
            except (ValueError, KeyError, TypeError):
                continue  # fail-closed：缺字段记录不参与召回
            self._records[record.record_id] = record

    @staticmethod
    def _to_dict(record: MemoryRecord) -> dict:
        s = record.scope
        return {
            "record_id": record.record_id,
            "scope": {
                "memory_type": s.memory_type.value,
                "platform": s.platform,
                "bot_id": s.bot_id,
                "conversation_id": s.conversation_id,
                "actor_id": s.actor_id,
                "persona_id": s.persona_id,
            },
            "content": record.content,
            "source": record.source,
            "sensitivity": record.sensitivity.value,
            "visibility": record.visibility.value,
            "evidence": list(record.evidence),
            "ttl_seconds": record.ttl_seconds,
        }

    @classmethod
    def _from_dict(cls, item: dict) -> MemoryRecord:
        scope_data = item.get("scope") or {}
        missing = [
            f for f in cls.REQUIRED_SCOPE_FIELDS if f not in scope_data
        ]
        if missing:
            raise ValueError(f"scope missing fields: {missing}")
        scope = MemoryScope(
            memory_type=MemoryType(scope_data["memory_type"]),
            platform=scope_data["platform"],
            bot_id=scope_data["bot_id"],
            conversation_id=scope_data["conversation_id"],
            actor_id=scope_data["actor_id"],
            persona_id=scope_data.get("persona_id"),
        )
        return MemoryRecord(
            record_id=item.get("record_id") or uuid4().hex,
            scope=scope,
            content=item.get("content", ""),
            source=item.get("source", ""),
            sensitivity=SensitivityLevel(item.get("sensitivity", "internal")),
            visibility=SensitivityLevel(item.get("visibility", "internal")),
            evidence=tuple(item.get("evidence") or ()),
            ttl_seconds=item.get("ttl_seconds"),
        )
