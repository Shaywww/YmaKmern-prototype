# -*- coding: utf-8 -*-
"""群聊 At-only 拦截 + stash 文件保活测试。"""
import os
from types import SimpleNamespace

from packages.application import dududa_handlers as h


class _FakeComp:
    def __init__(self, ctype, text=""):
        self.type = ctype
        self.text = text


class _FakeEvent(SimpleNamespace):
    is_at_or_wake_command = True


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
