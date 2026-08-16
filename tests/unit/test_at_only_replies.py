# -*- coding: utf-8 -*-
"""群聊 At-only 拦截 + stash 文件保活测试。"""
import os
from types import SimpleNamespace

from dududa.application import dududa_handlers as h


class _FakeComp:
    def __init__(self, ctype, text=""):
        self.type = ctype
        self.text = text


class _FakeEvent(SimpleNamespace):
    is_at_or_wake_command = True


def test_is_framework_command_true():
    ev = SimpleNamespace(message_obj=SimpleNamespace(message_str="/dududa_health"))
    assert h._is_framework_command(ev) is True


def test_is_framework_command_false_for_chat():
    ev = SimpleNamespace(message_obj=SimpleNamespace(message_str="帮我查一下课程"))
    assert h._is_framework_command(ev) is False


def test_is_framework_command_false_for_at_message():
    ev = SimpleNamespace(message_obj=SimpleNamespace(message_str="[At:3823883634] 查一下"))
    assert h._is_framework_command(ev) is False


def test_is_framework_command_tolerates_missing_obj():
    assert h._is_framework_command(SimpleNamespace(message_obj=None)) is False


def test_strip_tool_leak_dangling_source_is():
    assert h._strip_tool_leak(
        "对了，来源是 mcp.web_search: [{'title': 'x'}]") == "对了"


def test_strip_tool_leak_dangling_data_source_paren():
    assert h._strip_tool_leak(
        "（数据来源：mcp.campus_notice=[{'title': 'x'}]") == ""


def test_strip_tool_leak_dangling_paren_data():
    assert h._strip_tool_leak(
        "查看详情～（数据 mcp.exam_schedule=...") == "查看详情"


def test_strip_tool_leak_dangling_xia_mian():
    assert h._strip_tool_leak(
        "好嘞～（以下为工具返回 mcp.clock=...") == "好嘞"


def test_strip_tool_leak_no_false_positive():
    assert h._strip_tool_leak("今天天气不错哦～") == "今天天气不错哦～"


def test_strip_internal_tool_status_placeholder():
    assert h._strip_tool_leak(
        "正常回复\n（工具状态：: None）") == "正常回复"


def test_is_at_only_true_for_bare_at():
    assert h._is_at_only(_FakeEvent(), [_FakeComp("At")]) is True


def test_is_at_only_false_when_text_present():
    ev = _FakeEvent()
    msgs = [_FakeComp("At"), _FakeComp("Plain", "帮我看看这张图")]
    assert h._is_at_only(ev, msgs) is False


def test_is_at_only_false_when_image_present():
    assert h._is_at_only(_FakeEvent(), [_FakeComp("At"), _FakeComp("Image")]) is False


def test_is_at_only_false_when_not_at():
    ev = SimpleNamespace(is_at_or_wake_command=False)
    assert h._is_at_only(ev, [_FakeComp("Plain", "你好")]) is False


def test_preserve_media_copies_local_file(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(h, "_stash_dir", lambda: str(tmp_path / "stash"))
    src = tmp_path / "pic.jpg"
    src.write_bytes(b"\xff\xd8fake")
    dst = h._preserve_media(str(src))
    assert dst != str(src)
    assert os.path.exists(dst)
    assert open(dst, "rb").read() == b"\xff\xd8fake"
    shutil.rmtree(tmp_path / "stash", ignore_errors=True)


def test_preserve_media_passthrough_missing_and_http(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_stash_dir", lambda: str(tmp_path / "stash"))
    missing = str(tmp_path / "nope.jpg")
    assert h._preserve_media(missing) == missing
    assert h._preserve_media("http://example.com/a.jpg") == "http://example.com/a.jpg"


def test_drop_stash_file_only_inside_stash_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_stash_dir", lambda: str(tmp_path / "stash"))
    stash = tmp_path / "stash"
    stash.mkdir()
    inside = stash / "1_a.jpg"
    inside.write_bytes(b"x")
    outside = tmp_path / "b.jpg"
    outside.write_bytes(b"y")
    h._drop_stash_file(str(inside))
    h._drop_stash_file(str(outside))
    assert not inside.exists()
    assert outside.exists()


def test_is_at_only_empty_msgs_wake():
    assert h._is_at_only(_FakeEvent(), []) is True


def test_is_at_only_empty_msgs_not_wake():
    ev = SimpleNamespace(is_at_or_wake_command=False)
    assert h._is_at_only(ev, []) is False


def test_is_at_only_message_str_at():
    ev = _FakeEvent()
    ev.message_str = "[At:3823883634]"
    assert h._is_at_only(ev, []) is True


def test_is_at_only_message_str_at_with_text():
    ev = _FakeEvent()
    ev.message_str = "[At:3823883634] 帮我看看这张图"
    assert h._is_at_only(ev, []) is False


class _FakeRaw:
    def __init__(self, message):
        self.message = message


def test_remote_media_url_from_raw():
    ev = SimpleNamespace(raw_message=_FakeRaw([
        {"type": "at", "qq": "123"},
        {"type": "image", "url": "http://example.com/a.jpg", "file": "abc.jpg"},
    ]))
    assert h._remote_media_url(ev) == "http://example.com/a.jpg"


def test_remote_media_url_empty():
    ev = SimpleNamespace(raw_message=_FakeRaw([{"type": "text", "data": {"text": "hi"}}]))
    assert h._remote_media_url(ev) == ""


def test_at_only_with_stashed_media_reads_image_first(monkeypatch):
    """纯 @ 且已有配对 stash 图：必须优先读图，而不是回通用短句。"""
    import asyncio
    import time
    from dududa.application import dududa_handlers as h

    class _AtComp:
        type = "ComponentType.At"
        text = ""

    class _MsgObj:
        message_id = "t-stash-pair-1"
        group_id = "1093655251"

    class _Ev:
        message_str = ""
        is_at_or_wake_command = True

        def __init__(self):
            self.message_obj = _MsgObj()
            self._msgs = [_AtComp()]
            self._stopped = False

        def get_messages(self):
            return self._msgs

        def get_message_outline(self):
            return "[At:3823883634]"

        def stop_event(self):
            self._stopped = True

    class _Plugin:
        enabled = True
        _last_file_ts = time.time()
        _processed = set()

        def _is_self_message(self, ev):
            return False

        def _should_ignore(self, ev):
            return False

    calls = {}

    async def fake_handle_media(plugin, event, url, name, is_image, **kwargs):
        calls["media"] = (url, name, is_image)
        return "（读图）这是一张课程表截图 (｡･ω･｡)"

    def fake_take_paired(plugin, event):
        calls["take"] = True
        return ("/tmp/fake_stash.jpg", "stash.jpg", True)

    def fake_drop(path):
        calls["drop"] = path

    monkeypatch.setattr(h, "handle_media", fake_handle_media)
    monkeypatch.setattr(h, "_take_paired_media", fake_take_paired)
    monkeypatch.setattr(h, "_drop_stash_file", fake_drop)

    ev = _Ev()
    plugin = _Plugin()
    reply = asyncio.run(h.run_message_flow(plugin, ev))

    assert reply and "课程表" in reply, reply
    assert calls.get("take") is True
    assert calls.get("drop") == "/tmp/fake_stash.jpg"
    assert ev._stopped is True
    assert reply not in h._AT_ONLY_REPLIES
