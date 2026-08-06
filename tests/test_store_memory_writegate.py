# -*- coding: utf-8 -*-
"""P0 Memory v2 收尾：生产写入路径全部经 WriteGate（文档 2.5.3）。

- WriteGate：TTL<=0 REJECT；缺来源/证据 REQUIRE_CONFIRMATION；
  同 Scope 内容冲突 DEFER_FOR_CONFLICT；完全重复 REJECT
- _store_memory 集成：冲突/重复/受限内容不落盘，正常内容落盘
"""
import os, sys, types
sys.path.insert(0, "/opt/dududa20-prototype")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_wg", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from packages.core.memory import (
    MemoryType, MemoryScope, MemoryRecord,
    InMemoryRepository, MemoryCandidate, WriteGate, WriteGateDecision,
)


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


def _scope(bot="bot1", conv="g1", actor="u1",
           mem_type=MemoryType.SHORT_TERM, persona=None):
    return MemoryScope(
        memory_type=mem_type, platform="qq", bot_id=bot,
        conversation_id=conv, actor_id=actor, persona_id=persona,
    )


class _FakeEvent:
    """与 test_p5_security_memory 同构的最小事件替身。"""

    def __init__(self, text, group="g1", user="u1", bot="bot1",
                 session=None, sender_role="member"):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = session if session is not None else (group or f"private_{user}")
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user, role=sender_role),
            self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message" if group else "private_message"
        self._components = []

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def get_sender(self): return self.sender
    def plain_result(self, text): return text
    def stop_event(self): pass


class TestWriteGateRules:
    def test_ttl_non_positive_rejected(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        cand = MemoryCandidate(proposed_record=MemoryRecord(
            scope=_scope(), content="过期即弃", ttl_seconds=0,
            source="message", evidence=("ev",)))
        assert gate.evaluate(cand) == WriteGateDecision.REJECT
        cand2 = MemoryCandidate(proposed_record=MemoryRecord(
            scope=_scope(), content="负 TTL", ttl_seconds=-5,
            source="message", evidence=("ev",)))
        assert gate.evaluate(cand2) == WriteGateDecision.REJECT

    def test_missing_source_evidence_requires_confirmation(self):
        repo = InMemoryRepository()
        gate = WriteGate(repo)
        cand = MemoryCandidate(proposed_record=MemoryRecord(
            scope=_scope(), content="无来源无证据"))
        assert gate.evaluate(cand) == WriteGateDecision.REQUIRE_CONFIRMATION

    def test_exact_duplicate_rejected(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=_scope(), content="今天天气很好"))
        gate = WriteGate(repo)
        cand = MemoryCandidate(proposed_record=MemoryRecord(
            scope=_scope(), content="今天天气很好",
            source="message", evidence=("ev",)))
        assert gate.evaluate(cand) == WriteGateDecision.REJECT

    def test_containment_conflict_defers(self):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=_scope(), content="考试安排在明天上午"))
        gate = WriteGate(repo)
        cand = MemoryCandidate(proposed_record=MemoryRecord(
            scope=_scope(), content="考试安排在明天上午 改到下午了",
            source="message", evidence=("ev2",)))
        assert gate.evaluate(cand) == WriteGateDecision.DEFER_FOR_CONFLICT


class TestStoreMemoryWriteGateIntegration:
    def _plugin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
        return main.Main(_make_context())

    def test_normal_write_still_works(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(ev, "明天考试在上午")
        records = plugin.memory.query(plugin._make_scope(ev), limit=10)
        assert len(records) == 1
        assert records[0].content == "明天考试在上午"
        assert records[0].source == "message"
        assert records[0].evidence

    def test_conflicting_second_write_not_stored(self, monkeypatch, tmp_path):
        """同 Scope 包含冲突：第二条经 WriteGate DEFER，不落盘。"""
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(ev, "考试安排在明天上午")
        plugin._store_memory(ev, "考试安排在明天上午 改到下午了")
        records = plugin.memory.query(plugin._make_scope(ev), limit=10)
        assert len(records) == 1

    def test_exact_duplicate_not_stored(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(ev, "完全一样的内容")
        plugin._store_memory(ev, "完全一样的内容")
        assert plugin.memory.count(plugin._make_scope(ev)) == 1

    def test_restricted_never_stored(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(ev, "密码: hunter2")
        assert plugin.memory.count(plugin._make_scope(ev)) == 0
