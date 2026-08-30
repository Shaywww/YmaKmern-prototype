from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""P0: 受信 Attachment Repository（文档 2.4.2 Multimodal Preprocessor）。

覆盖：
- 不透明 ref / 元数据保留 / 惰性 URL 条目
- fail-closed：未知引用、过期、跨会话、跨用户一律 None
- take-once / take_scope 配对语义
- 有界：TTL、条目上限淘汰最旧、单条/总量配额拒绝
- 非法入仓（kind / 双来源 / 非 http(s) URL）拒绝
- 群图 stash 接线：受信仓库路径 + 旧桩兜底 + 跨用户隔离
- 生产插件装配 media_repo
"""
import sys
import time


import pytest
from types import SimpleNamespace

from dududa.core.attachment_repo import (
    AttachmentRepository, AttachmentRef, AttachmentRecord,
)
from dududa.application.dududa_handlers import (
    _stash_group_media, _take_paired_media,
)


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


class TestRepository:
    def test_roundtrip_bytes(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", mime="image/jpeg",
                       kind="image", data=b"IMG-DATA")
        assert isinstance(ref, AttachmentRef)
        rec = repo.get(ref.ref, "qq", "g1", "u1")
        assert isinstance(rec, AttachmentRecord)
        assert rec.data == b"IMG-DATA"
        assert rec.name == "a.jpg"
        assert rec.mime == "image/jpeg"
        assert rec.kind == "image"

    def test_ref_is_opaque(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                       data=b"X" * 64)
        assert len(ref.ref) == 32
        for bad in ("/", "http", "base64", "data:", ".jpg"):
            assert bad not in ref.ref
        assert ref.size == 64

    def test_summary_kept_but_not_in_ref(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                       data=b"X", summary="一张课程表截图")
        assert ref.summary == "一张课程表截图"
        assert "课程表" not in ref.ref

    def test_unknown_ref_fail_closed(self):
        repo = AttachmentRepository()
        assert repo.get("deadbeef", "qq", "g1", "u1") is None
        assert repo.take("deadbeef", "qq", "g1", "u1") is None

    def test_wrong_conversation_denied(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image", data=b"X")
        assert repo.get(ref.ref, "qq", "g2", "u1") is None
        assert repo.get(ref.ref, "qq", "g1", "u2") is None
        assert repo.get(ref.ref, "wx", "g1", "u1") is None

    def test_take_once(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image", data=b"X")
        assert repo.take(ref.ref, "qq", "g1", "u1") is not None
        assert repo.take(ref.ref, "qq", "g1", "u1") is None
        assert len(repo) == 0

    def test_take_scope_latest(self):
        repo = AttachmentRepository()
        repo.put("qq", "g1", "u1", name="1.jpg", kind="image", data=b"1")
        repo.put("qq", "g1", "u1", name="2.jpg", kind="image", data=b"2")
        rec = repo.take_scope("qq", "g1", "u1")
        assert rec is not None and rec.name == "2.jpg"
        rec2 = repo.take_scope("qq", "g1", "u1")
        assert rec2 is not None and rec2.name == "1.jpg"
        assert repo.take_scope("qq", "g1", "u1") is None

    def test_ttl_expiry(self):
        repo = AttachmentRepository(ttl_seconds=0.05)
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image", data=b"X")
        assert repo.get(ref.ref, "qq", "g1", "u1") is not None
        time.sleep(0.06)
        assert repo.get(ref.ref, "qq", "g1", "u1") is None
        assert len(repo) == 0

    def test_max_entries_evicts_oldest(self):
        repo = AttachmentRepository(max_entries=2)
        r1 = repo.put("qq", "g1", "u1", name="1.jpg", kind="image", data=b"1")
        repo.put("qq", "g1", "u1", name="2.jpg", kind="image", data=b"2")
        repo.put("qq", "g1", "u1", name="3.jpg", kind="image", data=b"3")
        assert repo.get(r1.ref, "qq", "g1", "u1") is None  # 最旧被淘汰
        assert len(repo) == 2

    def test_oversized_entry_rejected(self):
        repo = AttachmentRepository(max_bytes_per_entry=4)
        assert repo.put("qq", "g1", "u1", name="big.jpg", kind="image",
                        data=b"12345") is None
        assert len(repo) == 0

    def test_total_quota_rejected(self):
        repo = AttachmentRepository(max_total_bytes=6)
        assert repo.put("qq", "g1", "u1", name="1.jpg", kind="image",
                        data=b"1234") is not None
        assert repo.put("qq", "g1", "u1", name="2.jpg", kind="image",
                        data=b"5678") is None
        assert repo.total_bytes == 4

    def test_invalid_kind_rejected(self):
        repo = AttachmentRepository()
        assert repo.put("qq", "g1", "u1", name="x.bin", kind="exec",
                        data=b"X") is None
        assert repo.put("qq", "g1", "u1", name="", kind="file",
                        data=b"X") is None

    def test_dual_source_rejected(self):
        repo = AttachmentRepository()
        assert repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                        data=b"X", source_url="https://x/a.jpg") is None
        assert repo.put("qq", "g1", "u1", name="a.jpg", kind="image") is None

    def test_non_http_url_rejected(self):
        repo = AttachmentRepository()
        assert repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                        source_url="file:///etc/passwd") is None
        assert repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                        source_url="ftp://x/a.jpg") is None

    def test_lazy_url_entry(self):
        repo = AttachmentRepository()
        ref = repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                       source_url="https://cdn.example/a.jpg")
        assert ref is not None and ref.size == 0
        rec = repo.take(ref.ref, "qq", "g1", "u1")
        assert rec is not None and rec.source_url == "https://cdn.example/a.jpg"
        assert rec.data == b""


class TestStashIntegration:
    """群图 stash 接线：受信仓库路径 + 旧桩兜底。"""

    def _plugin_with_repo(self):
        class _Plugin:
            media_repo = AttachmentRepository()

        return _Plugin()

    def _plugin_legacy(self):
        class _Plugin:
            _recent_media = None

        return _Plugin()

    def test_stash_uses_repo_and_take_returns_bytes(self, tmp_path):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"REAL-IMG-BYTES")
        p = self._plugin_with_repo()
        ev = _ev([_img(url=str(img))])
        assert _stash_group_media(p, ev, [_img(url=str(img))]) is True
        assert len(p.media_repo) == 1
        got = _take_paired_media(p, _ev([], at=True, mstr="帮我看看这张图"))
        assert got[0] == b"REAL-IMG-BYTES"  # 受信仓库持有字节（源文件可被清理）
        assert got[1] == "a.jpg"
        assert got[2] is True

    def test_missing_local_file_falls_back_to_remote_url(self):
        def _remote_ev():
            e = _ev([_img()], mstr="")
            e.raw_message = SimpleNamespace(
                message=[{"type": "image", "url": "https://cdn.example/g.jpg"}])
            return e

        p = self._plugin_with_repo()
        # 本地路径不存在，但 raw_message 里有远程 URL -> 惰性入仓
        assert _stash_group_media(p, _remote_ev(), [_img()]) is True
        rec = p.media_repo.take_scope("qq", "g1", "u1")
        assert rec is not None
        assert rec.source_url == "https://cdn.example/g.jpg"
        assert rec.data == b""

    def test_cross_user_isolation(self, tmp_path):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"IMG")
        p = self._plugin_with_repo()
        _stash_group_media(p, _ev([_img(url=str(img))], sender="u1"),
                           [_img(url=str(img))])
        assert _take_paired_media(p, _ev([], at=True, sender="u2",
                                         mstr="看看图")) == ()
        assert _take_paired_media(p, _ev([], at=True, sender="u1",
                                         mstr="看看图")) != ()

    def test_unrelated_text_does_not_consume(self, tmp_path):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"IMG")
        p = self._plugin_with_repo()
        _stash_group_media(p, _ev([_img(url=str(img))]),
                           [_img(url=str(img))])
        assert _take_paired_media(p, _ev([], at=True, mstr="你好你好你好")) == ()
        assert len(p.media_repo) == 1

    def test_oversized_media_not_stashed(self, tmp_path):
        img = tmp_path / "big.jpg"
        img.write_bytes(b"12345")  # 5 字节 > 2 字节配额

        class _TinyRepoPlugin:
            media_repo = AttachmentRepository(max_bytes_per_entry=2)

        p = _TinyRepoPlugin()
        assert _stash_group_media(p, _ev([_img(url=str(img))]),
                                  [_img(url=str(img))]) is False
        assert len(p.media_repo) == 0

    def test_legacy_fallback_still_works(self):
        p = self._plugin_legacy()
        ev = _ev([_img()])
        assert _stash_group_media(p, ev, [_img()]) is True
        got = _take_paired_media(p, _ev([], at=True, mstr="看看图"))
        assert got[0] == "/root/data/temp/a.jpg"

    def test_private_image_not_stashed(self):
        p = self._plugin_with_repo()
        ev = _ev([_img()], at=True, gid="")
        assert _stash_group_media(p, ev, [_img()]) is False
        assert len(p.media_repo) == 0


class TestProdWiring:
    """生产插件装配 media_repo（真实加载 main.py）。"""

    @staticmethod
    def _make_main():
        sys.path.insert(0, str(PLUGIN_DIR))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dududa_main_mr", str(PLUGIN_MAIN))
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        try:
            ctx = main.star.Context()
        except TypeError:
            from unittest import mock
            ctx = mock.Mock()
        return main, main.Main(ctx)

    def test_prod_plugin_has_media_repo(self):
        main, p = self._make_main()
        assert hasattr(p, "media_repo")
        assert isinstance(p.media_repo, AttachmentRepository)

    def test_prod_repo_roundtrip(self):
        main, p = self._make_main()
        ref = p.media_repo.put("qq", "g1", "u1", name="a.jpg", kind="image",
                               data=b"IMG")
        assert ref is not None
        rec = p.media_repo.take(ref.ref, "qq", "g1", "u1")
        assert rec is not None and rec.data == b"IMG"

