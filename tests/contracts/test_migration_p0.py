"""Phase 5 —— Memory v2 数据迁移与回滚测试（文档 2.5.3）。"""
from datetime import datetime, timezone

import pytest

from dududa.core.errors import MemoryError
from dududa.core.memory import (
    InMemoryRepository, JSONMemoryRepository, MemoryRecord,
    MemoryScope, MemoryType, ScopeSelector,
)
from dududa.core.migration import MemoryMigration, MigrationSnapshot


def rec(bot="bot_a", conv="g1", actor="u1", content="测试记忆", rid=None):
    kwargs = {}
    if rid is not None:
        kwargs["record_id"] = rid
    return MemoryRecord(
        scope=MemoryScope(
            memory_type=MemoryType.SHORT_TERM, platform="qq",
            bot_id=bot, conversation_id=conv, actor_id=actor,
        ),
        content=content,
        source="message",
        evidence=("ev1",),
        **kwargs,
    )


def bad_snapshot():
    """含一条缺 bot_id 记录的快照（fail-closed 场景）。"""
    return MigrationSnapshot(
        snapshot_id="snap-bad",
        created_at=datetime.now(timezone.utc),
        checksum="x",
        records=(
            {
                "record_id": "bad1",
                "scope": {
                    "memory_type": "short_term", "platform": "qq",
                    "bot_id": "unknown", "conversation_id": "g1",
                    "actor_id": "u1",
                },
                "content": "坏记录",
                "source": "message",
            },
        ),
    )


class TestBackup:
    def test_snapshot_counts_and_checksum(self):
        src = InMemoryRepository()
        src.write(rec(content="A"))
        src.write(rec(conv="g2", content="B"))
        snap = MemoryMigration(src, InMemoryRepository()).backup()
        assert snap.total == 2
        assert snap.checksum

    def test_checksum_stable_for_same_data(self):
        src = InMemoryRepository()
        src.write(rec(content="A", rid="r1"))
        mig = MemoryMigration(src, InMemoryRepository())
        assert mig.backup().checksum == mig.backup().checksum

    def test_snapshot_roundtrip(self):
        src = InMemoryRepository()
        src.write(rec(content="A", rid="r1"))
        snap = MemoryMigration(src, InMemoryRepository()).backup()
        data = snap.to_dict()
        back = MigrationSnapshot.from_dict(data)
        assert back.checksum == snap.checksum
        assert back.total == snap.total


class TestDryRun:
    def test_all_valid(self):
        src = InMemoryRepository()
        src.write(rec(content="A"))
        src.write(rec(conv="g2", content="B"))
        snap = MemoryMigration(src, InMemoryRepository()).backup()
        stats = MemoryMigration(src, InMemoryRepository()).dry_run(snap)
        assert stats.ok
        assert stats.valid == 2

    def test_fail_closed_missing_metadata(self):
        src = InMemoryRepository()
        stats = MemoryMigration(src, InMemoryRepository()).dry_run(bad_snapshot())
        assert not stats.ok
        assert stats.failed == 1
        assert "bot_id" in stats.failures[0]


class TestMigrate:
    def test_migrate_writes_all(self, tmp_path):
        src = InMemoryRepository()
        src.write(rec(content="A", rid="r1"))
        src.write(rec(conv="g2", content="B", rid="r2"))
        tgt = JSONMemoryRepository(path=str(tmp_path / "mem.json"))
        mig = MemoryMigration(src, tgt)
        snap = mig.backup()
        stats = mig.migrate(snap)
        assert stats.ok
        assert tgt.count() == snap.total == 2
        contents = {r.content for r in tgt.query_selector(ScopeSelector(), limit=100)}
        assert contents == {"A", "B"}

    def test_migrate_rejects_when_dry_run_fails(self, tmp_path):
        src = InMemoryRepository()
        tgt = JSONMemoryRepository(path=str(tmp_path / "mem.json"))
        with pytest.raises(MemoryError):
            MemoryMigration(src, tgt).migrate(bad_snapshot())
        assert tgt.count() == 0


class TestRollback:
    def test_rollback_restores(self, tmp_path):
        src = InMemoryRepository()
        src.write(rec(content="A", rid="r1"))
        src.write(rec(conv="g2", content="B", rid="r2"))
        tgt = JSONMemoryRepository(path=str(tmp_path / "mem.json"))
        mig = MemoryMigration(src, tgt)
        snap = mig.backup()
        mig.migrate(snap)
        assert tgt.count() == 2
        tgt.write(rec(content="垃圾", rid="junk"))
        assert tgt.count() == 3
        restored = mig.rollback(snap)
        assert restored == 2
        assert tgt.count() == 2
        contents = {r.content for r in tgt.query_selector(ScopeSelector(), limit=100)}
        assert contents == {"A", "B"}

    def test_rollback_idempotent(self, tmp_path):
        src = InMemoryRepository()
        src.write(rec(content="A", rid="r1"))
        tgt = JSONMemoryRepository(path=str(tmp_path / "mem.json"))
        mig = MemoryMigration(src, tgt)
        snap = mig.backup()
        mig.migrate(snap)
        assert mig.rollback(snap) == 1
        assert mig.rollback(snap) == 1
        assert tgt.count() == 1
