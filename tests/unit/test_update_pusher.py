# -*- coding: utf-8 -*-
"""更新公告推送：存储/推送器/命令（QQ 好友逐人推送，只推一次）。"""
import sys, types
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_announce", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest

from dududa.application.update_pusher import (
    UpdateNoticeStore, UpdatePusher, build_notice_text,
)
from dududa.application import dududa_commands


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", bot="bot1", session=None):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = session if session is not None else (group or f"private_{user}")
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message" if group else "private_message"
        self._components = []
        self.is_at_or_wake_command = True

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


class _FakeBot:
    def __init__(self, friends, fail_ids=()):
        self.friends = list(friends)
        self.fail = set(str(x) for x in fail_ids)
        self.calls = []
        self.sent = []

    async def call_action(self, action, **params):
        self.calls.append((action, dict(params)))
        if action == "get_friend_list":
            return [{"user_id": u} for u in self.friends]
        if action == "send_private_msg":
            uid = str(params.get("user_id"))
            if uid in self.fail:
                raise RuntimeError("send failed")
            self.sent.append(uid)
        return None


class _FakeAdapter:
    def __init__(self, bot):
        self.bot = bot
        self._meta = types.SimpleNamespace(id="aiocqhttp")

    def meta(self):
        return self._meta


class _FakePM:
    def __init__(self, bot):
        self.bot = bot

    def get_insts(self):
        return [_FakeAdapter(self.bot)]


def _store(tmp_path):
    return UpdateNoticeStore(str(tmp_path / "update_notice.json"))


def _notice(version="v4.4.0", content="新增天气查询", pushed_at=None, ids=()):
    return {
        "version": version,
        "content": content,
        "created_at": "2026-08-11 10:00:00",
        "pushed_at": pushed_at,
        "pushed_ids": list(ids),
    }


# ---- 1. 存储 ----

class TestStore:
    def test_write_pending_roundtrip(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        assert st.pending() is not None
        assert st.load()["version"] == "v4.4.0"

    def test_pushed_not_pending(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice(pushed_at="2026-08-11 10:01:00"))
        assert st.pending() is None
        assert st.load() is not None

    def test_missing_or_empty_not_pending(self, tmp_path):
        st = _store(tmp_path)
        assert st.pending() is None
        assert st.load() is None
        (tmp_path / "update_notice.json").write_text("not json", encoding="utf-8")
        assert st.load() is None

    def test_record_pushed_ids_keeps_pending(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        st.record_pushed_ids(st.load(), ["1", "3"])
        assert st.pending() is not None
        assert st.load()["pushed_ids"] == ["1", "3"]

    def test_save_pushed_marks_done(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        st.save_pushed(st.load(), ["1", "2"])
        n = st.load()
        assert n["pushed_at"] is not None
        assert n["pushed_ids"] == ["1", "2"]
        assert st.pending() is None

    def test_build_text(self):
        assert "【嘟嘟哒更新公告】v4.4.0" in build_notice_text(_notice())
        assert "新增天气查询" in build_notice_text(_notice())
        assert "嘟嘟哒 2.0" in build_notice_text(_notice())
        assert "【嘟嘟哒更新公告】" in build_notice_text(_notice(version=""))


# ---- 2. 推送器 ----

class TestPusher:
    @pytest.mark.asyncio
    async def test_no_pending_skips(self, tmp_path):
        bot = _FakeBot(["1", "2"])
        pusher = UpdatePusher(_store(tmp_path), _FakePM(bot))
        report = await pusher.push_pending()
        assert report["skipped"] == "no_pending"
        assert bot.calls == []

    @pytest.mark.asyncio
    async def test_push_all_friends(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        bot = _FakeBot(["1", "2", "3"])
        pusher = UpdatePusher(st, _FakePM(bot))
        report = await pusher.push_pending()
        assert report["ok"] and report["pushed_new"] == 3
        assert sorted(bot.sent) == ["1", "2", "3"]
        assert st.pending() is None
        assert len(st.load()["pushed_ids"]) == 3
        # 再次推送：无待推送
        report2 = await pusher.push_pending()
        assert report2["skipped"] == "no_pending"

    @pytest.mark.asyncio
    async def test_partial_failure_retries_only_failed(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        bot = _FakeBot(["1", "2", "3"], fail_ids=["2"])
        pusher = UpdatePusher(st, _FakePM(bot))
        report = await pusher.push_pending()
        assert not report["ok"] and len(report["failed"]) == 1
        assert st.pending() is not None
        assert sorted(st.load()["pushed_ids"]) == ["1", "3"]
        # 修复后重试：只发给失败的 2
        bot.fail = set()
        report2 = await pusher.push_pending()
        assert report2["ok"] and report2["pushed_new"] == 1
        assert bot.sent == ["1", "3", "2"]
        assert st.pending() is None

    @pytest.mark.asyncio
    async def test_friend_list_failure_keeps_pending(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())
        bot = _FakeBot(["1"])

        async def boom(action, **params):
            raise RuntimeError("ws down")
        bot.call_action = boom
        pusher = UpdatePusher(st, _FakePM(bot))
        report = await pusher.push_pending()
        assert not report["ok"] and report["friends"] is None
        assert "get_friend_list" in report["error"]
        assert st.pending() is not None

    @pytest.mark.asyncio
    async def test_adapter_unavailable(self, tmp_path):
        st = _store(tmp_path)
        st.write(_notice())

        class _EmptyPM:
            def get_insts(self):
                return []
        report = await UpdatePusher(st, _EmptyPM()).push_pending()
        assert report["skipped"] == "adapter_unavailable"
        assert st.pending() is not None


# ---- 3. 命令 ----

@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(main, "GROUP_POLICY_FILE", str(tmp_path / "group_policy.json"))
    monkeypatch.setattr(main, "STYLE_FILE", str(tmp_path / "styles.json"))
    monkeypatch.setattr(main, "NOTICE_FILE", str(tmp_path / "update_notice.json"))
    monkeypatch.setenv("DUDUDA_PROFILE_FILE", str(tmp_path / "profiles.json"))
    p = main.Main(_make_context())
    p._core._react_cooldown.clear()
    return p


class TestAnnounceCommand:
    @pytest.mark.asyncio
    async def test_deny_non_owner(self, plugin):
        out = await dududa_commands.cmd_announce_impl(
            plugin, _FakeEvent("x", user="stranger"), "v4.4.0 更新")
        assert "权限不足" in out
        assert plugin.notice_store.load() is None

    @pytest.mark.asyncio
    async def test_owner_announce_pushes(self, plugin, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "OWNER_IDS", {"u1"})
        bot = _FakeBot(["10001", "10002"])
        plugin.update_pusher = UpdatePusher(
            plugin.notice_store, _FakePM(bot))
        out = await dududa_commands.cmd_announce_impl(
            plugin, _FakeEvent("x", user="u1"),
            "v4.5.0 新增更新推送功能")
        assert "推送完成" in out
        assert sorted(bot.sent) == ["10001", "10002"]
        n = plugin.notice_store.load()
        assert n["version"] == "v4.5.0"
        assert n["pushed_at"] is not None

    @pytest.mark.asyncio
    async def test_announce_no_version(self, plugin, monkeypatch):
        monkeypatch.setattr(main, "OWNER_IDS", {"u1"})
        plugin.update_pusher = UpdatePusher(
            plugin.notice_store, _FakePM(_FakeBot([])))
        out = await dududa_commands.cmd_announce_impl(
            plugin, _FakeEvent("x", user="u1"), "更新内容")
        assert "推送完成" in out
        assert plugin.notice_store.load()["version"] == ""

    @pytest.mark.asyncio
    async def test_empty_content_usage(self, plugin, monkeypatch):
        monkeypatch.setattr(main, "OWNER_IDS", {"u1"})
        out = await dududa_commands.cmd_announce_impl(
            plugin, _FakeEvent("x", user="u1"), None)
        assert "用法" in out

    @pytest.mark.asyncio
    async def test_status(self, plugin, monkeypatch):
        monkeypatch.setattr(main, "OWNER_IDS", {"u1"})
        out = await dududa_commands.cmd_announce_status_impl(plugin)
        assert "暂无更新公告" in out
        plugin.notice_store.write({
            "version": "v4.5.0", "content": "新功能",
            "created_at": "2026-08-11 10:00:00",
            "pushed_at": None, "pushed_ids": [],
        })
        out2 = await dududa_commands.cmd_announce_status_impl(plugin)
        assert "v4.5.0" in out2 and "未推送" in out2