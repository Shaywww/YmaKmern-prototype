"""Low-frequency memory consolidation and conflict resolution.

The consolidator never receives raw platform events.  It only works on records
that already passed redaction/basic memory gates, and only persists a compact
summary after a successful model result.  Group source records are deleted once
their summary is committed so raw group text does not become long-term memory.
"""
from __future__ import annotations

import inspect
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .memory import (
    MemoryCandidate,
    MemoryRecord,
    MemoryRepository,
    MemoryScope,
    MemoryType,
    SensitivityLevel,
    WriteGate,
)
from .state import WriteGateDecision
from .trace_recorder import trace_recorder


SummaryProvider = Callable[[tuple[MemoryRecord, ...], bool],
                           Awaitable[str] | str]


@dataclass(frozen=True)
class ConsolidationResult:
    due: bool = False
    resolved_conflicts: int = 0
    summary_record_id: str = ""
    source_records_removed: int = 0
    reason: str = ""


class MemoryConsolidator:
    """Run consolidation every N engaged messages or once per day."""

    def __init__(
        self,
        repository: MemoryRepository,
        state_path: Optional[str] = None,
        every_n_messages: int = 12,
        daily_seconds: float = 86400.0,
        max_source_records: int = 15,
    ):
        self._repo = repository
        self._state_path = Path(state_path) if state_path else None
        self._every_n = max(2, int(every_n_messages))
        self._daily_seconds = max(300.0, float(daily_seconds))
        self._max_source = max(3, int(max_source_records))
        self._state: dict[str, dict[str, float | int]] = {}
        self._load()

    @staticmethod
    def _scope_key(scope: MemoryScope) -> str:
        return "|".join((
            scope.platform,
            scope.bot_id,
            scope.conversation_id,
            scope.actor_id,
            scope.persona_id or "*",
        ))

    def observe(self, scope: MemoryScope, now: Optional[float] = None) -> bool:
        """Record one engaged message and report whether consolidation is due."""
        current = float(now if now is not None else time.time())
        key = self._scope_key(scope)
        item = self._state.setdefault(
            key, {"observed": 0, "last_run": current})
        item["observed"] = int(item.get("observed", 0)) + 1
        last_run = float(item.get("last_run", current))
        due = (
            int(item["observed"]) >= self._every_n
            or current - last_run >= self._daily_seconds
        )
        self._save()
        return due

    def _mark_complete(self, scope: MemoryScope, now: Optional[float] = None) -> None:
        current = float(now if now is not None else time.time())
        self._state[self._scope_key(scope)] = {
            "observed": 0,
            "last_run": current,
        }
        self._save()

    def resolve_conflicts(self, scope: MemoryScope) -> int:
        """Prefer the record backed by the newest evidence timestamp.

        ``created_at`` is the auditable evidence timestamp available in the
        current schema.  The winning record retains both evidence lists and a
        ``supersedes:<record_id>`` link when the new value wins.
        """
        resolved = 0
        for conflict in self._repo.list_deferred(scope=scope, limit=100):
            existing = self._repo.get_record(conflict.existing_record_id)
            prefer_new = (
                existing is None
                or conflict.proposed_record.created_at >= existing.created_at
            )
            self._repo.resolve_deferred(
                conflict.conflict_id, prefer_new=prefer_new)
            resolved += 1
        return resolved

    async def consolidate(
        self,
        source_scope: MemoryScope,
        summary_provider: Optional[SummaryProvider],
        *,
        is_group: bool,
        force: bool = False,
        now: Optional[float] = None,
    ) -> ConsolidationResult:
        due = force or self.observe(source_scope, now=now)
        if not due:
            return ConsolidationResult(due=False, reason="not_due")

        resolved = self.resolve_conflicts(source_scope)
        records = self._repo.query(source_scope, limit=self._max_source)
        records = tuple(
            record for record in records
            if record.source in {"message", "explicit", "inference"}
            and record.content.strip()
        )
        if summary_provider is None or not records:
            self._mark_complete(source_scope, now=now)
            return ConsolidationResult(
                due=True, resolved_conflicts=resolved,
                reason="no_summary_source")

        summary = summary_provider(records, is_group)
        if inspect.isawaitable(summary):
            summary = await summary
        summary = " ".join(str(summary or "").split()).strip()[:600]
        if not summary:
            self._mark_complete(source_scope, now=now)
            return ConsolidationResult(
                due=True, resolved_conflicts=resolved,
                reason="empty_summary")

        target_scope = MemoryScope(
            memory_type=(MemoryType.GROUP_MEMORY if is_group
                         else MemoryType.USER_PROFILE),
            platform=source_scope.platform,
            bot_id=source_scope.bot_id,
            conversation_id=source_scope.conversation_id,
            actor_id=("group" if is_group else source_scope.actor_id),
            persona_id=source_scope.persona_id,
        )
        evidence = tuple(f"record:{record.record_id}" for record in records)
        summary_record = MemoryRecord(
            scope=target_scope,
            content=summary,
            source="inference",
            sensitivity=(SensitivityLevel.INTERNAL if is_group
                         else SensitivityLevel.PRIVATE),
            visibility=(SensitivityLevel.INTERNAL if is_group
                        else SensitivityLevel.PRIVATE),
            evidence=evidence,
            ttl_seconds=(7200 if is_group else None),
        )
        decision = WriteGate(self._repo).evaluate(MemoryCandidate(
            proposed_record=summary_record,
            metadata={"source": "memory_consolidation"},
        ))
        record_id = ""
        removed = 0
        if decision == WriteGateDecision.ALLOW:
            record_id = self._repo.write(summary_record)
        elif decision == WriteGateDecision.DEFER_FOR_CONFLICT:
            # A newer summary often overlaps the previous capsule.  Resolve it
            # immediately in the target scope instead of leaving an orphaned
            # conflict that a source-scope job can never see.
            resolved += self.resolve_conflicts(target_scope)
            if self._repo.get_record(summary_record.record_id) is not None:
                record_id = summary_record.record_id
        if record_id and is_group:
            # Summary succeeded: remove only the source records included in
            # this batch.  No raw group transcript is promoted long-term.
            for record in records:
                if self._repo.delete(record.record_id):
                    removed += 1
        self._mark_complete(source_scope, now=now)
        trace_recorder.record(
            event="memory_consolidation",
            scope=source_scope.to_key(),
            is_group=is_group,
            resolved_conflicts=resolved,
            summary_written=bool(record_id),
            source_records_removed=removed,
            decision=decision.value,
        )
        return ConsolidationResult(
            due=True,
            resolved_conflicts=resolved,
            summary_record_id=record_id,
            source_records_removed=removed,
            reason=decision.value,
        )

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._state = {
                str(key): value for key, value in data.items()
                if isinstance(value, dict)
            }
