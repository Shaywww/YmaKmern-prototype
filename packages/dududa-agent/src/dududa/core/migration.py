"""Phase 5 —— Memory v2 数据迁移与回滚。

对应文档 2.5.3 / Phase 5：旧数据 backup、dry run、人工复核、
迁移/隔离、数量核对和 rollback。
流程：backup() 导出快照（含校验和）-> dry_run() 只校验不写入
（缺 metadata fail-closed）-> migrate() 通过后才写入 ->
rollback() 清空目标并按快照恢复（幂等）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .errors import MemoryError
from .memory import (
    InMemoryRepository,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    ScopeSelector,
    SensitivityLevel,
)

ALL_SELECTOR = ScopeSelector()
BIG_LIMIT = 10**6


@dataclass(frozen=True)
class MigrationStats:
    """迁移统计与数量核对。"""
    total: int = 0
    valid: int = 0
    skipped: int = 0
    failed: int = 0
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.skipped == 0


@dataclass(frozen=True)
class MigrationSnapshot:
    """一次迁移的完整快照（回滚依据）。"""
    snapshot_id: str
    created_at: datetime
    checksum: str
    records: tuple[dict[str, Any], ...]

    @property
    def total(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at.isoformat(),
            "checksum": self.checksum,
            "records": list(self.records),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MigrationSnapshot":
        return cls(
            snapshot_id=data["snapshot_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            checksum=data["checksum"],
            records=tuple(data["records"]),
        )


def _record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    s = record.scope
    return {
        "record_id": record.record_id or uuid4().hex,
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
        "ttl_seconds": record.ttl_seconds,
    }


def _dict_to_record(data: dict[str, Any]) -> MemoryRecord:
    s = data["scope"]
    scope = MemoryScope(
        memory_type=MemoryType(s["memory_type"]),
        platform=s["platform"],
        bot_id=s["bot_id"],
        conversation_id=s["conversation_id"],
        actor_id=s["actor_id"],
        persona_id=s.get("persona_id"),
    )
    return MemoryRecord(
        record_id=data.get("record_id") or uuid4().hex,
        scope=scope,
        content=data.get("content", ""),
        source=data.get("source", "migration"),
        sensitivity=SensitivityLevel(data.get("sensitivity", "internal")),
        ttl_seconds=data.get("ttl_seconds"),
    )


def _checksum(records: tuple[dict[str, Any], ...]) -> str:
    canonical = json.dumps(records, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MemoryMigration:
    """记忆迁移器：backup -> dry_run -> migrate -> rollback。"""

    def __init__(self, source: InMemoryRepository, target: InMemoryRepository):
        self._source = source
        self._target = target

    def backup(self) -> MigrationSnapshot:
        records = tuple(
            _record_to_dict(r)
            for r in self._source.query_selector(ALL_SELECTOR, limit=BIG_LIMIT)
        )
        return MigrationSnapshot(
            snapshot_id=uuid4().hex,
            created_at=datetime.now(timezone.utc),
            checksum=_checksum(records),
            records=records,
        )

    def dry_run(self, snapshot: MigrationSnapshot) -> MigrationStats:
        failures: list[str] = []
        valid = skipped = 0
        for i, data in enumerate(snapshot.records):
            try:
                record = _dict_to_record(data)
                # fail-closed：缺必需 metadata 的记录拒绝迁移
                s = record.scope
                missing = [
                    f for f in ("bot_id", "conversation_id", "actor_id")
                    if getattr(s, f, None) in (None, "", "unknown")
                ]
                if missing:
                    raise ValueError(f"缺必需字段: {missing}")
                if not record.content.strip():
                    skipped += 1
                    continue
                valid += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"#{i}: {exc}")
        return MigrationStats(
            total=len(snapshot.records),
            valid=valid,
            skipped=skipped,
            failed=len(failures),
            failures=tuple(failures),
        )

    def migrate(self, snapshot: MigrationSnapshot) -> MigrationStats:
        stats = self.dry_run(snapshot)
        if not stats.ok:
            raise MemoryError(
                f"dry run 未通过，拒绝迁移（failed={stats.failed}, skipped={stats.skipped}）",
                reason="migration_dry_run_failed",
            )
        written = 0
        for data in snapshot.records:
            self._target.write(_dict_to_record(data))
            written += 1
        return MigrationStats(total=stats.total, valid=written)

    def rollback(self, snapshot: MigrationSnapshot) -> int:
        """清空目标仓库并恢复快照内容（幂等）。"""
        for record in self._target.query_selector(ALL_SELECTOR, limit=BIG_LIMIT):
            self._target.delete(record.record_id)
        restored = 0
        for data in snapshot.records:
            self._target.write(_dict_to_record(data))
            restored += 1
        return restored
