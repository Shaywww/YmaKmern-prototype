# -*- coding: utf-8 -*-
"""群聊图片暂存 + @ 配对（QQ 无法同一条消息同时 @ + 图）"""

from types import SimpleNamespace
import time

from dududa.application.dududa_handlers import _stash_group_media, _take_paired_media


def _img(url="/root/data/temp/a.jpg"):
    return SimpleNamespace(type="ComponentType.Image", name="a.jpg",
                           url=url, file=url, path=url)


def _ev(msgs, at=False, gid="g1", sender="u1", mstr=""):
    return SimpleNamespace(
        get_messages=lambda: msgs,
        is_at_or_wake_command=at,
        message_obj=SimpleNamespace(group_id=gid),
        get_sender_id=lambda: sender,
        message_str=mstr,
    )


class _Plugin:
    def __init__(self):
        self._recent_media = None


def test_group_image_without_at_stashed_silently():
    p = _Plugin()
    ev = _ev([_img()])
    assert _stash_group_media(p, ev, [_img()]) is True
    assert p._recent_media[("g1", "u1")][1] == "/root/data/temp/a.jpg"


def test_group_image_with_at_not_stashed():
    p = _Plugin()
    ev = _ev([_img()], at=True)
    assert _stash_group_media(p, ev, [_img()]) is False
    assert not p._recent_media


def test_private_image_not_stashed():
    p = _Plugin()
    ev = _ev([_img()], at=True, gid="")
    assert _stash_group_media(p, ev, [_img()]) is False
    assert not p._recent_media


def test_pair_when_ask_about_image():
    p = _Plugin()
    _stash_group_media(p, _ev([_img()]), [_img()])
    got = _take_paired_media(p, _ev([], at=True, mstr="帮我看看这张图"))
    assert got == ("/root/data/temp/a.jpg", "a.jpg", True)


def test_pair_with_empty_text():
    p = _Plugin()
    _stash_group_media(p, _ev([_img()]), [_img()])
    got = _take_paired_media(p, _ev([], at=True, mstr=""))
    assert got[0] == "/root/data/temp/a.jpg"


def test_no_pair_for_unrelated_text():
    p = _Plugin()
    _stash_group_media(p, _ev([_img()]), [_img()])
    assert _take_paired_media(p, _ev([], at=True, mstr="你好你好你好")) == ()


def test_no_pair_after_expiry():
    p = _Plugin()
    _stash_group_media(p, _ev([_img()]), [_img()])
    key = ("g1", "u1")
    st = p._recent_media[key]
    p._recent_media[key] = (time.time() - 120, st[1], st[2], st[3])
    assert _take_paired_media(p, _ev([], at=True, mstr="看看图")) == ()


def test_pair_consumed_once():
    p = _Plugin()
    _stash_group_media(p, _ev([_img()]), [_img()])
    assert _take_paired_media(p, _ev([], at=True, mstr="看看图"))
    assert _take_paired_media(p, _ev([], at=True, mstr="再看看")) == ()
