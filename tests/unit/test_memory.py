"""测试 Memory System。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from dududa.core.memory import (
    MemoryType, SensitivityLevel, MemoryScope, MemoryRecord,
    MemoryCandidate, WriteGate, InMemoryRepository, WriteGateDecision,
)


class TestMemoryScope:
    def test_to_key(self):
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        key = scope.to_key()
        assert "qq" in key
        assert "g1" in key
        assert "u1" in key

    def test_is_subset(self):
        a = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        b = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        assert a.is_subset_of(b)

    def test_not_subset_different_actor(self):
        a = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        b = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u2",
        )
        assert not a.is_subset_of(b)


class TestMemoryRecord:
    def test_is_expired(self):
        record = MemoryRecord(ttl_seconds=0)
        assert record.is_expired

    def test_not_expired(self):
        record = MemoryRecord(ttl_seconds=86400)
        assert not record.is_expired

    def test_no_ttl(self):
        record = MemoryRecord(ttl_seconds=None)
        assert not record.is_expired

    def test_accessed(self):
        record = MemoryRecord()
        accessed = record.accessed()
        assert accessed.access_count == 1
        assert accessed.last_accessed is not None


class TestInMemoryRepository:
    def test_write_and_query(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        record = MemoryRecord(scope=scope, content="测试记忆")
        rid = repo.write(record)
        assert rid

        results = repo.query(scope)
        assert len(results) == 1
        assert results[0].content == "测试记忆"

    def test_delete(self):
        repo = InMemoryRepository()
        record = MemoryRecord(content="to delete")
        rid = repo.write(record)
        assert repo.delete(rid)
        assert not repo.delete("nonexistent")

    def test_find_similar(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        r1 = MemoryRecord(scope=scope, content="今天天气很好")
        repo.write(r1)
        similar = MemoryRecord(scope=scope, content="今天天气很好 不错")
        found = repo.find_similar(similar, threshold=0.1)
        assert found is not None

    def test_find_similar_chinese_near_duplicate_at_writegate_threshold(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(
            scope=scope, content="用户喜欢喝三分糖奶茶"))
        candidate = MemoryRecord(
            scope=scope, content="用户喜欢喝三分糖的奶茶")
        assert repo.find_similar(candidate, threshold=0.8) is not None

    def test_chinese_unrelated_sentences_do_not_collide(self):
        score = InMemoryRepository._text_similarity(
            "用户喜欢喝三分糖奶茶", "明天兰州天气有雨")
        assert score < 0.3

    def test_count(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(scope=scope, content="m1"))
        repo.write(MemoryRecord(scope=scope, content="m2"))
        assert repo.count(scope) == 2

    def test_purge_expired(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(scope=scope, content="expired", ttl_seconds=0))
        repo.write(MemoryRecord(scope=scope, content="fresh", ttl_seconds=99999))
        purged = repo.purge_expired()
        assert purged == 1
        assert repo.count(scope) == 1


class TestWriteGate:
    def test_empty_content_rejected(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        candidate = MemoryCandidate(
            proposed_record=MemoryRecord(content=""),
        )
        assert gate.evaluate(candidate) == WriteGateDecision.REJECT

    def test_restricted_sensitivity_requires_confirmation(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        record = MemoryRecord(
            content="敏感信息",
            sensitivity=SensitivityLevel.RESTRICTED,
        )
        candidate = MemoryCandidate(proposed_record=record)
        assert gate.evaluate(candidate) == WriteGateDecision.REQUIRE_CONFIRMATION

    def test_normal_content_allowed(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        record = MemoryRecord(
            content="普通消息",
            source="message",
            evidence=("msg_1",),
        )
        candidate = MemoryCandidate(proposed_record=record)
        assert gate.evaluate(candidate) == WriteGateDecision.ALLOW

    def test_delivery_dependent_deferred(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        record = MemoryRecord(content="需要确认送达", source="message", evidence=("ev",))
        candidate = MemoryCandidate(
            proposed_record=record,
            requires_delivery_ack=True,
            # No delivery_run_id set
        )
        assert gate.evaluate(candidate) == WriteGateDecision.DEFER_FOR_CONFLICT

    def test_duplicate_rejected(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(scope=scope, content="今天天气很好"))
        gate = WriteGate(repo)
        candidate = MemoryCandidate(
            proposed_record=MemoryRecord(
                scope=scope,
                content="今天天气很好",
                source="message",
                evidence=("ev",),
            )
        )
        assert gate.evaluate(candidate) == WriteGateDecision.REJECT

    def test_chinese_near_duplicate_reaches_conflict_gate(self):
        repo = InMemoryRepository()
        scope = MemoryScope(
            memory_type=MemoryType.SHORT_TERM,
            platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(
            scope=scope, content="用户喜欢喝三分糖奶茶"))
        gate = WriteGate(repo)
        candidate = MemoryCandidate(proposed_record=MemoryRecord(
            scope=scope,
            content="用户喜欢喝三分糖的奶茶",
            source="message", evidence=("ev",),
        ))
        assert gate.evaluate(candidate) == WriteGateDecision.DEFER_FOR_CONFLICT
