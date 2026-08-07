"""Phase 5 —— Memory v2 隔离矩阵（文档 2.4.22 / 2.5.3）。

覆盖：跨群、跨用户、群聊/私聊、不同 Bot、不同 Persona、
缺 metadata fail-closed、过期记录、语义相似不越权、具名 Selector 跨类型。
"""
import sys, os, json, tempfile
sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest

from dududa.core.memory import (
    MemoryType, SensitivityLevel, MemoryScope, MemoryRecord,
    ScopeSelector, InMemoryRepository, JSONMemoryRepository,
    WriteGate, WriteGateDecision, MemoryCandidate,
)


def scope(bot="bot_a", conv="g1", actor="u1",
          mem_type=MemoryType.SHORT_TERM, persona=None):
    return MemoryScope(
        memory_type=mem_type, platform="qq", bot_id=bot,
        conversation_id=conv, actor_id=actor, persona_id=persona,
    )


class TestIsolationMatrix:
    def test_bot_isolation(self):
        """不同 Bot 互不可见（多机器人隔离）。"""
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(bot="bot_a"), content="A 的记忆"))
        repo.write(MemoryRecord(scope=scope(bot="bot_b"), content="B 的记忆"))
        results = repo.query(scope(bot="bot_a"))
        assert len(results) == 1
        assert results[0].content == "A 的记忆"

    def test_group_isolation_same_user(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(conv="g1"), content="g1 消息"))
        repo.write(MemoryRecord(scope=scope(conv="g2"), content="g2 消息"))
        results = repo.query(scope(conv="g1"))
        assert len(results) == 1
        assert results[0].content == "g1 消息"

    def test_user_isolation_same_group(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(actor="u1"), content="u1 的"))
        repo.write(MemoryRecord(scope=scope(actor="u2"), content="u2 的"))
        results = repo.query(scope(actor="u1"))
        assert len(results) == 1
        assert results[0].content == "u1 的"

    def test_memory_type_isolation(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(mem_type=MemoryType.SHORT_TERM), content="short"))
        repo.write(MemoryRecord(scope=scope(mem_type=MemoryType.EPISODIC), content="episodic"))
        results = repo.query(scope(mem_type=MemoryType.SHORT_TERM))
        assert len(results) == 1
        assert results[0].content == "short"

    def test_private_vs_group(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(conv="private_u1"), content="私聊"))
        repo.write(MemoryRecord(scope=scope(conv="g1"), content="群聊"))
        results = repo.query(scope(conv="private_u1"))
        assert len(results) == 1
        assert results[0].content == "私聊"

    def test_persona_isolation(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(persona="dududa_a"), content="A 人设"))
        repo.write(MemoryRecord(scope=scope(persona="dududa_b"), content="B 人设"))
        results = repo.query(scope(persona="dududa_a"))
        assert len(results) == 1
        assert results[0].content == "A 人设"

    def test_expired_excluded(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(), content="过期", ttl_seconds=0))
        repo.write(MemoryRecord(scope=scope(), content="新鲜", ttl_seconds=99999))
        assert len(repo.query(scope())) == 1

    def test_semantic_similarity_does_not_cross_scope(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(conv="g1"), content="今天天气很好"))
        similar_other_group = MemoryRecord(
            scope=scope(conv="g2"), content="今天天气很好 非常不错"
        )
        assert repo.find_similar(similar_other_group, threshold=0.1) is None


class TestScopeSelector:
    def test_selector_cross_type_within_boundary(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(mem_type=MemoryType.SHORT_TERM), content="short"))
        repo.write(MemoryRecord(scope=scope(mem_type=MemoryType.EPISODIC), content="episodic"))
        sel = ScopeSelector(platform="qq", bot_id="bot_a", conversation_id="g1", actor_id="u1")
        assert len(repo.query_selector(sel)) == 2
        # 换 Bot 后即使放宽类型也查不到
        sel_other_bot = ScopeSelector(platform="qq", bot_id="bot_b", conversation_id="g1", actor_id="u1")
        assert len(repo.query_selector(sel_other_bot)) == 0

    def test_selector_persona_filter(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(persona="dududa_a"), content="A"))
        repo.write(MemoryRecord(scope=scope(persona="dududa_b"), content="B"))
        sel = ScopeSelector(platform="qq", bot_id="bot_a", conversation_id="g1", actor_id="u1", persona_id="dududa_a")
        results = repo.query_selector(sel)
        assert len(results) == 1 and results[0].content == "A"

    def test_from_scope_roundtrip(self):
        sel = ScopeSelector.from_scope(scope(bot="b1", persona="p1"))
        assert sel.bot_id == "b1" and sel.persona_id == "p1"
        assert sel.matches(MemoryRecord(scope=scope(bot="b1", persona="p1")))


class TestWriteGateConflict:
    def test_exact_duplicate_rejected(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(), content="今天天气很好"))
        gate = WriteGate(repo)
        candidate = MemoryCandidate(
            proposed_record=MemoryRecord(
                scope=scope(), content="今天天气很好",
                source="message", evidence=("ev",),
            )
        )
        assert gate.evaluate(candidate) == WriteGateDecision.REJECT

    def test_conflicting_content_defers(self):
        """同 Scope 内容冲突：保留双方证据，不静默覆盖。"""
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=scope(), content="考试安排在明天上午"))
        gate = WriteGate(repo)
        candidate = MemoryCandidate(
            proposed_record=MemoryRecord(
                scope=scope(), content="考试安排在明天上午 改到下午了",
                source="message", evidence=("ev2",),
            )
        )
        assert gate.evaluate(candidate) == WriteGateDecision.DEFER_FOR_CONFLICT


class TestJSONMemoryRepository:
    def _tmp_path(self, tmp_path):
        return str(tmp_path / "memory.json")

    def test_roundtrip(self, tmp_path):
        path = self._tmp_path(tmp_path)
        repo = JSONMemoryRepository(path)
        repo.write(MemoryRecord(scope=scope(), content="持久化消息"))
        repo2 = JSONMemoryRepository(path)
        results = repo2.query(scope())
        assert len(results) == 1
        assert results[0].content == "持久化消息"

    def test_write_missing_scope_rejected(self, tmp_path):
        repo = JSONMemoryRepository(self._tmp_path(tmp_path))
        bad = MemoryRecord(
            scope=MemoryScope(
                memory_type=MemoryType.SHORT_TERM, platform="qq",
                bot_id="", conversation_id="g1", actor_id="u1",
            ),
            content="缺 bot_id",
        )
        with pytest.raises(ValueError):
            repo.write(bad)

    def test_load_missing_metadata_fail_closed(self, tmp_path):
        """缺 metadata 的记录加载时跳过，不参与召回。"""
        path = self._tmp_path(tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{
                "record_id": "bad1",
                "scope": {"memory_type": "short_term", "platform": "qq"},  # 缺 bot/conversation/actor
                "content": "缺字段记录",
            }], f, ensure_ascii=False)
        repo = JSONMemoryRepository(path)
        assert repo.count() == 0

    def test_corrupt_file_no_crash(self, tmp_path):
        path = self._tmp_path(tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        repo = JSONMemoryRepository(path)
        assert repo.count() == 0
