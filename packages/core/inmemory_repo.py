"""InMemoryRepository - dict-based MemoryRepository for prototyping."""

from __future__ import annotations
from typing import Optional
from .memory import MemoryRepository, MemoryRecord, MemoryScope


class InMemoryRepository(MemoryRepository):
    """Dict-based in-memory memory store. Lost on restart."""

    def __init__(self):
        self._records: dict[str, MemoryRecord] = {}

    def write(self, record: MemoryRecord) -> str:
        """Write record. record_id must be pre-set by caller."""
        self._records[record.record_id] = record
        return record.record_id

    def query(self, scope: MemoryScope, limit: int = 20) -> tuple[MemoryRecord, ...]:
        """Query by scope. is_expired is a field, not a method."""
        results: list[MemoryRecord] = []
        for r in self._records.values():
            if not r.is_expired and r.scope.is_subset_of(scope):
                results.append(r)
        results.sort(key=lambda x: x.created_at, reverse=True)
        return tuple(results[:limit])

    def find_similar(self, record: MemoryRecord, threshold: float = 0.8) -> Optional[MemoryRecord]:
        """Find similar record by content overlap (case-insensitive)."""
        content = record.content.strip().lower()
        for existing in self._records.values():
            ex = existing.content.strip().lower()
            if not ex or not content:
                continue
            # Check if one contains the other, or they share significant prefix
            if ex in content or content in ex:
                return existing
            # Check common prefix ratio
            min_len = min(len(ex), len(content))
            if min_len > 0:
                match = 0
                for i in range(min_len):
                    if ex[i] == content[i]:
                        match += 1
                    else:
                        break
                if match / min_len >= threshold:
                    return existing
        return None

    def delete(self, record_id: str) -> bool:
        """Delete single record by ID."""
        return self._records.pop(record_id, None) is not None

    def count(self, scope: Optional[MemoryScope] = None) -> int:
        """Count records. scope=None returns total including expired."""
        if scope is None:
            return len(self._records)
        return sum(1 for r in self._records.values()
                   if not r.is_expired and r.scope.is_subset_of(scope))

    def purge_expired(self) -> int:
        """Remove all expired records."""
        before = len(self._records)
        self._records = {k: v for k, v in self._records.items() if not v.is_expired}
        return before - len(self._records)
