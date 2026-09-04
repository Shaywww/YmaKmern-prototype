from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""TraceRecorder：JSONL 落盘 + run_message_flow 挂载（文档 2.5.10 / Phase 9）。"""
import sys, types
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_tr", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.core.trace_recorder import TraceRecorder


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
        rec.record(event="flow_start", run_id="r1", trace_id="t1",
                   msg="hi", msg_len=2)
        rec.record(event="flow_end", run_id="r1", trace_id="t1",
                   reply="hello", reply_len=5)
        lines = rec.lines_for()
        assert len(lines) == 2
        assert [l["event"] for l in lines] == ["flow_start", "flow_end"]
        assert lines[0]["trace_id"] == "t1"
        assert "msg" not in lines[0] and lines[0]["msg_len"] == 2
        assert "reply" not in lines[1] and lines[1]["reply_len"] == 5

    def test_sink_drops_nested_content_and_hashes_identity(self, tmp_path):
        rec = TraceRecorder(tmp_path)
        rec.record(
            event="x", scope="qq|bot|481757927|2320584044",
            payload={
                "messages": [{"content": "群聊原文"}],
                "group_id": "481757927",
                "safe_count": 2,
            })
        line = rec.lines_for()[0]
        assert "scope" not in line and len(line["scope_hash"]) == 16
        assert "messages" not in line["payload"]
        assert "group_id" not in line["payload"]
        assert len(line["payload"]["group_id_hash"]) == 16
        assert line["payload"]["safe_count"] == 2
        assert "群聊原文" not in str(line)

    def test_record_redacts_credentials_at_sink(self, tmp_path):
        rec = TraceRecorder(tmp_path)
        rec.record(
            event="x", note="api_key=abcdefghijklmnop1234",
            url="https://user:pass@example.com/?token=abc")
        lines = rec.lines_for()
        assert lines[0]["note"] == "[REDACTED]"
        assert "[REDACTED]@" in lines[0]["url"]
        assert "token=[REDACTED]" in lines[0]["url"]
        assert "abcdefghijklmnop1234" not in str(lines)
        assert "pass@" not in str(lines)

    def test_read_missing_day_empty(self, tmp_path):
        rec = TraceRecorder(tmp_path)
        assert rec.lines_for() == []

    @pytest.mark.asyncio
    async def test_run_message_flow_records_flow_trace(self, monkeypatch, tmp_path):
        from dududa.application import dududa_handlers as h
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

    @pytest.mark.asyncio
    async def test_flow_does_not_store_raw_message_or_reply(
            self, monkeypatch, tmp_path):
        from dududa.application import dududa_handlers as h
        rec = TraceRecorder(tmp_path / "traces")
        monkeypatch.setattr(h, "trace_recorder", rec)
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE",
                            str(tmp_path / "confirmations.json"))
        plugin = main.Main(_make_context())
        plugin.enabled = True

        async def fake_inner(plugin_, event, msgs, run_id, trace_id):
            return "这是回复正文 secret_password=XYZ123456789"

        monkeypatch.setattr(h, "_run_flow_inner", fake_inner)
        ev = _FakeEvent("@bot 这是原始群消息 secret_value=abcdefghij")
        reply = await h.run_message_flow(plugin, ev)
        assert reply
        lines = rec.lines_for()
        start = next(l for l in lines if l["event"] == "flow_start")
        end = next(l for l in lines if l["event"] == "flow_end")
        # 原始消息 / 回复 / 会话 ID 不再落盘，只留长度与脱敏哈希
        assert "msg" not in start and "session" not in start
        assert start["msg_len"] == len(ev.message_str)
        assert "session_hash" in start
        assert "reply" not in end and end["reply_len"] == len(reply)
        blob = str(lines)
        assert "这是原始群消息" not in blob
        assert "这是回复正文" not in blob
        assert "secret_value" not in blob and "secret_password" not in blob

    def test_scrub_history_is_recursive_and_drops_broken_lines(self, tmp_path):
        from ops.scrub_traces import scrub_file
        path = tmp_path / "old.jsonl"
        path.write_text(
            '{"event":"x","session":"481757927","payload":'
            '{"messages":[{"content":"群聊原文"}],"safe":1}}\n'
            'broken raw 群聊原文\n', encoding="utf-8")
        changed, deleted = scrub_file(path, delete=False, dry_run=False)
        assert not deleted and changed >= 3
        content = path.read_text(encoding="utf-8")
        assert "481757927" not in content
        assert "群聊原文" not in content
        obj = __import__("json").loads(content)
        assert "session" not in obj and len(obj["session_hash"]) == 16
        assert "messages" not in obj["payload"]
        assert obj["payload"]["safe"] == 1
