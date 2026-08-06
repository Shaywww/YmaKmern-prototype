# -*- coding: utf-8 -*-
"""P1-3 Trace：RuntimeState run_id/trace_id 落日志（给 P2 Eval 铺路）。

- RuntimeState 生成并保留 run_id + trace_id（transition 不丢失）
- RuntimeResult 携带 trace_id；运行结束/失败落日志（Run end / Run error）
- Orchestrator.run 支持注入 run_id/trace_id（生产 flow 传入）
- run_message_flow 全分支 Flow start / Flow end 同 id 落日志
"""
import sys
sys.path.insert(0, "/opt/dududa20-prototype")

import logging
import re
import time

import pytest

from packages.core.state import RuntimeState, RuntimePhase, RunOutcome
from packages.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from packages.core.delivery import DeliveryManager, NoOpOutputAdapter
from packages.runtime.orchestrator import RuntimeOrchestrator
from packages.application import dududa_handlers as h


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestRuntimeStateTrace:
    def test_run_and_trace_id_present_and_unique(self):
        s1 = RuntimeState()
        s2 = RuntimeState()
        assert s1.run_id and s1.trace_id
        assert s1.run_id != s2.run_id
        assert s1.trace_id != s2.trace_id

    def test_transition_preserves_ids(self):
        s = RuntimeState(run_id="r1", trace_id="t1")
        s2 = s.transition(RuntimePhase.VALIDATED)
        assert s2.run_id == "r1"
        assert s2.trace_id == "t1"


class TestRunEndLogs:
    def _capture(self):
        logger = logging.getLogger("dududa20")
        logger.setLevel(logging.DEBUG)
        cap = _Capture()
        logger.addHandler(cap)
        return logger, cap

    def test_result_carries_trace_id_and_logs_run_end(self):
        logger, cap = self._capture()
        try:
            state = RuntimeState(run_id="r-abc", trace_id="t-xyz")
            result = RuntimeOrchestrator._result_from_state(
                state, RunOutcome.SUCCEEDED)
        finally:
            logger.removeHandler(cap)
        assert result.run_id == "r-abc"
        assert result.trace_id == "t-xyz"
        msgs = [r.getMessage() for r in cap.records]
        assert any("Run end" in m and "r-abc" in m and "t-xyz" in m for m in msgs)

    def test_failed_outcome_logs_run_error(self):
        logger, cap = self._capture()
        try:
            state = RuntimeState(run_id="r-err", trace_id="t-err").with_error("boom")
            RuntimeOrchestrator._result_from_state(state, RunOutcome.FAILED)
        finally:
            logger.removeHandler(cap)
        msgs = [r.getMessage() for r in cap.records]
        assert any("Run error" in m and "r-err" in m and "boom" in m for m in msgs)


def _make_envelope(text="你好"):
    return MessageEnvelope(
        platform=Platform.QQ,
        kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="group_123", platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="user_1", platform=Platform.QQ, display_name="小明"),
        text=text,
    )


class TestOrchestratorSeedsIds:
    @pytest.mark.asyncio
    async def test_injected_ids_flow_to_result(self):
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()))
        result = await orch.run(_make_envelope(), run_id="r-inj", trace_id="t-inj")
        assert result.run_id == "r-inj"
        assert result.trace_id == "t-inj"


class TestFlowStartEndLogs:
    @pytest.mark.asyncio
    async def test_run_message_flow_logs_start_and_end_with_same_ids(self, monkeypatch):
        logger = logging.getLogger("dududa20")
        logger.setLevel(logging.DEBUG)
        cap = _Capture()
        logger.addHandler(cap)
        try:
            class _MsgObj:
                message_id = "t-trace-1"
                group_id = "g1"

            class _AtComp:
                type = "At"
                text = ""

            class _Ev:
                message_str = ""
                is_at_or_wake_command = True

                def __init__(self):
                    self.message_obj = _MsgObj()
                    self._msgs = [_AtComp()]

                def get_messages(self):
                    return self._msgs

                def get_sender_id(self):
                    return "u1"

                def stop_event(self):
                    pass

            class _Plugin:
                enabled = True
                _last_file_ts = time.time()
                _processed = set()

                def _is_self_message(self, ev):
                    return False

                def _should_ignore(self, ev):
                    return False

            monkeypatch.setattr(h, "_take_paired_media", lambda plugin, event: ())
            reply = await h.run_message_flow(_Plugin(), _Ev())
            assert reply in h._AT_ONLY_REPLIES
        finally:
            logger.removeHandler(cap)
        msgs = [r.getMessage() for r in cap.records]
        starts = [m for m in msgs if m.startswith("Flow start")]
        ends = [m for m in msgs if m.startswith("Flow end")]
        assert len(starts) == 1 and len(ends) == 1, msgs
        rid = re.search(r"run_id=(\w+)", starts[0]).group(1)
        tid = re.search(r"trace_id=(\w+)", starts[0]).group(1)
        assert rid and tid
        assert ("run_id=" + rid) in ends[0]
        assert ("trace_id=" + tid) in ends[0]
