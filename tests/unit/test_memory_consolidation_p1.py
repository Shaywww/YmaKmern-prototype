from datetime import datetime, timedelta, timezone

import pytest

from dududa.core.memory import (
    InMemoryRepository,
    JSONMemoryRepository,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    WriteGate,
)
from dududa.core.memory_consolidation import MemoryConsolidator
from dududa.core.state import WriteGateDecision


def _scope(memory_type=MemoryType.SHORT_TERM, actor="u1"):
    return MemoryScope(
        memory_type=memory_type,
        platform="qq",
        bot_id="bot1",
        conversation_id="g1",
        actor_id=actor,
        persona_id="dududa_default",
    )


def _record(content, *, created_at=None, memory_type=MemoryType.SHORT_TERM,
            actor="u1", record_id=None):
    kwargs = {}
    if record_id:
        kwargs["record_id"] = record_id
    return MemoryRecord(
        scope=_scope(memory_type=memory_type, actor=actor),
        content=content,
        source="message",
        evidence=(f"message:{content[-2:]}",),
        created_at=created_at or datetime.now(timezone.utc),
        **kwargs,
    )


def test_conflict_is_queued_and_newer_evidence_supersedes_old():
    repo = InMemoryRepository()
    old = _record(
        "用户在兰州交通大学就读",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        record_id="old",
    )
    repo.write(old)
    new = _record("用户目前在兰州交通大学就读", record_id="new")

    decision = WriteGate(repo).evaluate(MemoryCandidate(proposed_record=new))
    assert decision == WriteGateDecision.DEFER_FOR_CONFLICT
    assert len(repo.list_deferred(_scope())) == 1

    consolidator = MemoryConsolidator(repo)
    assert consolidator.resolve_conflicts(_scope()) == 1
    assert repo.get_record("old") is None
    winner = repo.get_record("new")
    assert winner is not None
    assert "supersedes:old" in winner.evidence
    assert not repo.list_deferred(_scope())


def test_json_repository_persists_conflicts_and_timestamps(tmp_path):
    path = str(tmp_path / "memory.json")
    old_time = datetime.now(timezone.utc) - timedelta(hours=1)
    repo = JSONMemoryRepository(path)
    repo.write(_record("用户喜欢三分糖奶茶", created_at=old_time,
                       record_id="old"))
    proposed = _record("用户很喜欢三分糖奶茶", record_id="new")
    assert WriteGate(repo).evaluate(
        MemoryCandidate(proposed_record=proposed)
    ) == WriteGateDecision.DEFER_FOR_CONFLICT

    loaded = JSONMemoryRepository(path)
    assert loaded.get_record("old").created_at == old_time
    pending = loaded.list_deferred(_scope())
    assert len(pending) == 1
    assert pending[0].proposed_record.record_id == "new"


@pytest.mark.asyncio
async def test_group_consolidation_keeps_summary_and_removes_raw_records(tmp_path):
    repo = JSONMemoryRepository(str(tmp_path / "memory.json"))
    source_scope = _scope(memory_type=MemoryType.GROUP_MEMORY)
    first = _record("[用户]: 大家在讨论明天实验课是否换教室",
                    memory_type=MemoryType.GROUP_MEMORY, record_id="m1")
    second = _record("[用户]: 目前还没有正式通知",
                     memory_type=MemoryType.GROUP_MEMORY, record_id="m2")
    repo.write(first)
    repo.write(second)
    consolidator = MemoryConsolidator(
        repo, state_path=str(tmp_path / "state.json"),
        every_n_messages=2)

    async def summarize(records, is_group):
        assert is_group
        assert {item.record_id for item in records} == {"m1", "m2"}
        return "群里在讨论明天实验课是否换教室，目前没有正式通知"

    assert not (await consolidator.consolidate(
        source_scope, summarize, is_group=True)).due
    result = await consolidator.consolidate(
        source_scope, summarize, is_group=True)
    assert result.due
    assert result.summary_record_id
    assert result.source_records_removed == 2
    assert repo.query(source_scope) == ()

    summary_scope = _scope(memory_type=MemoryType.GROUP_MEMORY, actor="group")
    summaries = repo.query(summary_scope)
    assert len(summaries) == 1
    assert summaries[0].source == "inference"
    assert summaries[0].ttl_seconds == 7200

    repo.write(_record(
        "[用户]: 仍然没有换教室的正式通知",
        memory_type=MemoryType.GROUP_MEMORY, record_id="m3"))

    async def update_summary(records, is_group):
        return "群里继续讨论明天实验课是否换教室，目前仍没有正式通知"

    updated = await consolidator.consolidate(
        source_scope, update_summary, is_group=True, force=True)
    assert updated.summary_record_id
    assert repo.get_record("m3") is None
    current = repo.query(summary_scope)
    assert len(current) == 1
    assert any(item.startswith("supersedes:") for item in current[0].evidence)


def test_bot_utterance_is_a_first_class_memory_type():
    repo = InMemoryRepository()
    bot = MemoryRecord(
        scope=_scope(memory_type=MemoryType.BOT_UTTERANCE),
        content="这次我说过的话",
        source="bot",
        evidence=("run:r1",),
    )
    repo.write(bot)
    assert repo.query(_scope()) == ()
    recalled = repo.query(_scope(memory_type=MemoryType.BOT_UTTERANCE))
    assert len(recalled) == 1 and recalled[0].content == bot.content
    assert "[YmaKmern]" not in bot.content


def test_similar_bot_utterances_are_history_not_profile_conflicts():
    repo = InMemoryRepository()
    scope = _scope(memory_type=MemoryType.BOT_UTTERANCE)
    first = MemoryRecord(
        scope=scope, content="这个我刚才已经说过啦",
        source="bot", evidence=("run:1",))
    second = MemoryRecord(
        scope=scope, content="这个我刚刚已经说过啦",
        source="bot", evidence=("run:2",))
    repo.write(first)
    decision = WriteGate(repo).evaluate(
        MemoryCandidate(proposed_record=second))
    assert decision == WriteGateDecision.ALLOW
    assert not repo.list_deferred(scope)
