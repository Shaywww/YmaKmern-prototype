# -*- coding: utf-8 -*-
"""Perception 模型化 P0：结构化 PerceptionRecord 生成与入库。

覆盖：to_record 字段映射、valid 标记、PerceptionStore JSONL 往返、
Orchestrator 每次感知自动入库（含 run/trace 与会话绑定）。
"""
import asyncio
import sys


import pytest

from dududa.core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef, Actor,
)
from dududa.core.perception import PerceptionResult, SpeechAct, EntityRef
from dududa.core.perception_store import (
    PerceptionStore, perception_store, record_state_perception,
)
from dududa.core.state import SocialAction
from dududa.core.decision import (
    SocialDecisionEngine, SocialDecision, DecisionReason,
)
from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
from dududa.core.capability import CapabilityRegistry
from dududa.core.memory import InMemoryRepository
from dududa.runtime.orchestrator import RuntimeOrchestrator


def _perception():
    return PerceptionResult(
        target_users=("u1",),
        speech_acts=(SpeechAct(act_type="command", confidence=0.9),),
        topics=("course",),
        entities=(EntityRef(name="数据结构", entity_type="course",
                            confidence=0.8, evidence="msg"),),
        resolved_references={"text": "帮我查一下数据结构"},
        candidate_intents=("course_query",),
        needs_tools=True,
        suggested_capabilities=("mcp.course_schedule",),
        confidence=0.9,
        has_explicit_mention=True,
        is_explicit_command=True,
    )


def _envelope(text="帮我查一下数据结构", conversation="g1"):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conversation, platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="u"),
        text=text, mentions=("u1",),
    )


class _Engine(SocialDecisionEngine):
    def decide(self, perception=None, context=None, now=None):
        return SocialDecision(
            action=SocialAction.ANSWER,
            reason_codes=(DecisionReason.HIGH_RELEVANCE,),
            confidence=1.0,
        )


def _orch():
    return RuntimeOrchestrator(
        decision_engine=_Engine(),
        capability_registry=CapabilityRegistry(),
        memory_repo=InMemoryRepository(),
        delivery_manager=DeliveryManager(NoOpOutputAdapter()),
    )


class TestPerceptionRecord:
    def test_to_record_maps_fields(self):
        rec = _perception().to_record(
            run_id="r1", trace_id="t1", platform="qq",
            conversation_id="g1", actor_id="u1", text="帮我查一下数据结构")
        assert rec.run_id == "r1" and rec.trace_id == "t1"
        assert rec.platform == "qq"
        assert rec.conversation_id == "g1" and rec.actor_id == "u1"
        assert rec.text == "帮我查一下数据结构"
        assert rec.source == "rule"
        assert rec.schema_version == "1.0"
        assert rec.needs_tools is True
        assert rec.candidate_intents == ("course_query",)
        assert rec.topics == ("course",)
        assert rec.speech_acts[0].act_type == "command"
        assert rec.entities[0].name == "数据结构"
        assert rec.valid is True
        assert rec.record_id

    def test_to_record_invalid_flag(self):
        bad = PerceptionResult(confidence=1.5)  # 置信度越界 -> is_valid False
        assert bad.is_valid is False
        assert bad.to_record().valid is False

    def test_store_roundtrip(self, tmp_path):
        store = PerceptionStore(directory=str(tmp_path))
        store.record(**_perception().to_record(run_id="r1", trace_id="t1").to_dict())
        lines = store.lines_for()
        assert len(lines) == 1
        entry = lines[0]
        assert entry["run_id"] == "r1" and entry["trace_id"] == "t1"
        assert entry["needs_tools"] is True
        assert entry["speech_acts"] == [["command", 0.9]]
        assert entry["entities"][0]["name"] == "数据结构"
        assert entry["candidate_intents"] == ["course_query"]
        assert entry["valid"] is True

    def test_record_state_perception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DUDUDA_PERCEPTION_DIR", str(tmp_path))

        class _State:
            envelope = _envelope()
            run_id = "r2"
            trace_id = "t2"

        record_state_perception(_perception(), _State())
        lines = perception_store.lines_for()
        assert len(lines) == 1
        assert lines[0]["conversation_id"] == "g1"
        assert lines[0]["platform"] == "qq"
        assert lines[0]["actor_id"] == "u1"
        assert lines[0]["source"] == "rule"
        assert lines[0]["run_id"] == "r2" and lines[0]["trace_id"] == "t2"


class TestOrchestratorRecords:
    def test_phase_perceive_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DUDUDA_PERCEPTION_DIR", str(tmp_path))
        asyncio.run(_orch().run(
            _envelope("帮我查一下数据结构"), run_id="r3", trace_id="t3"))
        lines = PerceptionStore(directory=str(tmp_path)).lines_for()
        assert len(lines) == 1
        entry = lines[0]
        assert entry["run_id"] == "r3" and entry["trace_id"] == "t3"
        assert entry["text"] == "帮我查一下数据结构"
        assert entry["needs_tools"] is True
        assert entry["candidate_intents"] == ["course_query"]

    def test_each_message_records(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DUDUDA_PERCEPTION_DIR", str(tmp_path))
        orch = _orch()
        for i in range(3):
            asyncio.run(orch.run(
                _envelope(f"消息 {i}"), run_id=f"r-{i}", trace_id=f"t-{i}"))
        lines = PerceptionStore(directory=str(tmp_path)).lines_for()
        assert len(lines) == 3
        assert {l["run_id"] for l in lines} == {"r-0", "r-1", "r-2"}