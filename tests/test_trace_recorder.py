# -*- coding: utf-8 -*-
"""TraceRecorder：JSONL 落盘 + run_message_flow 挂载（文档 2.5.10 / Phase 9）。"""
import sys, types
sys.path.insert(0, "/opt/dududa20-prototype")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_tr", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from packages.core.trace_recorder import TraceRecorder


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", bot="bot1"):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = group or f"private_{user}"
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message" if group else "private_message"
        self._components = [object()]  # 非空消息，保证进入流程
        self.is_at_or_wake_command = True

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


class TestTraceRecorder:
    def test_writes_and_reads_jsonl(self, tmp_path):
        rec = TraceRecorder(tmp_path)
        rec.record(event="flow_start", run_id="r1", trace_id="t1", msg="hi")
        rec.record(event="flow_end", run_id="r1", trace_id="t1", reply="hello")
        lines = rec.lines_for()
        assert len(lines) == 2
        assert [l["event"] for l in lines] == ["flow_start", "flow_end"]
        assert lines[0]["trace_id"] == "t1"
        assert lines[1]["reply"] == "hello"

    def test_read_missing_day_empty(self, tmp_path):
        rec = TraceRecorder(tmp_path)
        assert rec.lines_for() == []

    @pytest.mark.asyncio
    async def test_run_message_flow_records_flow_trace(self, monkeypatch, tmp_path):
        from packages.application import dududa_handlers as h
        rec = TraceRecorder(tmp_path / "traces")
        monkeypatch.setattr(h, "trace_recorder", rec)
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
        plugin = main.Main(_make_context())
        plugin.enabled = True

        async def fake_inner(plugin_, event, msgs, run_id, trace_id):
            return "好的～"

        monkeypatch.setattr(h, "_run_flow_inner", fake_inner)
        ev = _FakeEvent("@bot USTC")
        reply = await h.run_message_flow(plugin, ev)
        assert reply == "好的～"
        lines = rec.lines_for()
        events = [l["event"] for l in lines]
        assert events == ["flow_start", "flow_end"], events
        assert lines[0]["trace_id"] == lines[1]["trace_id"]
        assert lines[1]["duration_ms"] >= 0
