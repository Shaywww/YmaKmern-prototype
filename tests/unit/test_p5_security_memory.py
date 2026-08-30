from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""P5: 安全（Policy / 持久确认 / Redaction / Restricted 过滤）+ Memory v2。

对应文档 2.5.3（Memory）与 2.5.9（权限、安全、隐私与信任边界）：
- PermissionEngine：default deny、角色排序、muted overlay、能力风险
- ConfirmationStore：绑定 requester/Scope/payload、管理员批准、单次消费、
  执行时重新授权、dump/restore 持久化（重启不丢失）
- Redactor：credential value / URL user-info / URL query / 嵌套值，幂等
- Restricted 数据（密码/Token/Cookie/私钥/QQ 登录态）：不进 Memory、不发模型
- JSONMemoryRepository：持久化、原子落盘、损坏文件隔离（fail-closed）
- query_visible：RESTRICTED 永不召回、PRIVATE 仅本人
- 生产接线：角色解析（含群级管理员边界）、管理命令授权、记忆脱敏与可见性
"""
import os, sys, types, glob, json
sys.path.insert(0, str(PLUGIN_DIR))

# 角色配置必须在加载 main.py 之前注入（模块常量在 import 时读取）
os.environ.setdefault("DUDUDA_OWNER_IDS", "u_owner")
os.environ.setdefault("DUDUDA_ADMIN_IDS", "u_admin")
os.environ.setdefault("DUDUDA_TRUSTED_IDS", "u_trusted")
os.environ.setdefault("DUDUDA_MUTED_IDS", "u_muted")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_p5", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from datetime import datetime, timedelta, timezone

from dududa.core.memory import (
    MemoryType, SensitivityLevel, MemoryScope, MemoryRecord,
    InMemoryRepository, JSONMemoryRepository,
)
from dududa.core.envelope import Actor, Platform
from dududa.safeguards.security import (
    PermissionEngine, AuthorizationDecision, AuthReason,
    ConfirmationStore, Redactor,
)




def _make_context():
    """跨版本构造 AstrBot Context：真实构造不可用时降级为 mock。"""
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()

def _actor(actor_id="u1", role="normal", muted=False):
    return Actor(actor_id=actor_id, platform=Platform.QQ,
                 display_name=actor_id, role="muted" if muted else role)


def _scope(bot="bot1", conv="g1", actor="u1",
           mem_type=MemoryType.SHORT_TERM, persona=None):
    return MemoryScope(
        memory_type=mem_type, platform="qq", bot_id=bot,
        conversation_id=conv, actor_id=actor, persona_id=persona,
    )


class _FakeEvent:
    """生产事件替身：满足 input_adapter + _make_scope + _actor_for 所需接口。"""

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


class TestPermissionEngine:
    def test_default_deny_unknown_actor(self):
        res = PermissionEngine().authorize(None, "read")
        assert res.decision == AuthorizationDecision.DENY
        assert AuthReason.UNKNOWN_ACTOR in res.reason_codes

    def test_default_deny_empty_scope(self):
        res = PermissionEngine().authorize(_actor("u1"), "read", scope_key="")
        assert res.decision == AuthorizationDecision.DENY
        assert AuthReason.UNKNOWN_SCOPE in res.reason_codes

    def test_muted_overlay_beats_owner(self):
        res = PermissionEngine().authorize(
            _actor("owner1", role="owner", muted=True), "admin", scope_key="g1")
        assert res.decision == AuthorizationDecision.DENY
        assert AuthReason.MUTED in res.reason_codes

    def test_role_ranking(self):
        eng = PermissionEngine()
        assert eng.authorize(_actor("o", "owner"), "admin", scope_key="g1").allowed
        assert eng.authorize(_actor("a", "admin"), "admin", scope_key="g1").decision             == AuthorizationDecision.DENY  # admin 角色不能执行 owner 级动作
        assert eng.authorize(_actor("a", "admin"), "send", scope_key="g1").allowed
        assert eng.authorize(_actor("t", "trusted"), "use_tool", scope_key="g1").allowed
        assert eng.authorize(_actor("n", "normal"), "use_tool", scope_key="g1").decision             == AuthorizationDecision.DENY

    def test_capability_risk_gate(self):
        eng = PermissionEngine()
        # low(1) 任意角色可执行；critical(4) 需要 owner
        assert eng.authorize(_actor("n", "normal"), "read",
                             scope_key="g1", capability_risk="low").allowed
        assert eng.authorize(_actor("t", "trusted"), "use_tool",
                             scope_key="g1", capability_risk="low").allowed
        assert eng.authorize(_actor("a", "admin"), "use_tool",
                             scope_key="g1", capability_risk="critical").decision == AuthorizationDecision.DENY
        assert eng.authorize(_actor("o", "owner"), "use_tool",
                             scope_key="g1", capability_risk="critical").allowed

    def test_requires_confirmation(self):
        res = PermissionEngine().authorize(
            _actor("o", "owner"), "use_tool", scope_key="g1",
            requires_confirmation=True)
        assert res.decision == AuthorizationDecision.REQUIRE_CONFIRMATION


class TestConfirmationStoreDurable:
    def _store(self, ttl=600):
        return ConfirmationStore(ttl_seconds=ttl)

    def test_approve_and_consume_single_use(self):
        store = self._store()
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        assert store.approve(conf.confirmation_id)
        res = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "x"})
        assert res.allowed
        assert AuthReason.CONFIRMATION_OK in res.reason_codes
        # 单次使用：重放拒绝
        res2 = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                             "bot1|g1|persona",
                             {"resource": "persona", "target": "x"})
        assert res2.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_REPLAYED in res2.reason_codes

    def test_consume_without_approve_denied(self):
        store = self._store()
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        res = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "x"})
        assert res.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_REQUIRED in res.reason_codes

    def test_binding_mismatches(self):
        store = self._store()
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        store.approve(conf.confirmation_id)
        # actor 不匹配
        res = store.consume(conf.confirmation_id, _actor("other", "trusted"),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "x"})
        assert AuthReason.CONFIRMATION_ACTOR_MISMATCH in res.reason_codes
        # scope 不匹配
        res = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                            "bot1|g2|persona",
                            {"resource": "persona", "target": "x"})
        assert AuthReason.CONFIRMATION_SCOPE_MISMATCH in res.reason_codes
        # payload digest 不匹配
        res = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "y"})
        assert AuthReason.CONFIRMATION_DIGEST_MISMATCH in res.reason_codes

    def test_expired_denied(self):
        store = self._store(ttl=-1)
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        store.approve(conf.confirmation_id)
        assert conf.is_expired
        res = store.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "x"})
        assert AuthReason.CONFIRMATION_EXPIRED in res.reason_codes

    def test_execution_mute_overlay(self):
        """执行时重新授权：发起者被 mute 后即使已批准也拒绝。"""
        store = self._store()
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        store.approve(conf.confirmation_id)
        res = store.consume(conf.confirmation_id,
                            _actor("u_trusted", "trusted", muted=True),
                            "bot1|g1|persona",
                            {"resource": "persona", "target": "x"})
        assert AuthReason.MUTED in res.reason_codes

    def test_dump_restore_roundtrip(self):
        store = self._store()
        conf = store.create(_actor("u_trusted", "trusted"), "bot1|g1|persona",
                            "manage_config", {"resource": "persona", "target": "x"})
        store.approve(conf.confirmation_id)
        data = store.dump()
        assert len(data) == 1 and data[0]["approved"] is True

        store2 = ConfirmationStore()
        assert store2.restore(data) == 1
        conf2 = store2.get(conf.confirmation_id)
        assert conf2 is not None and conf2.approved
        assert conf2.actor_id == "u_trusted"
        assert conf2.payload_digest == conf.payload_digest
        # 消费后 dump/restore 保留 consumed 状态
        store2.consume(conf.confirmation_id, _actor("u_trusted", "trusted"),
                       "bot1|g1|persona", {"resource": "persona", "target": "x"})
        store3 = ConfirmationStore()
        store3.restore(store2.dump())
        assert store3.get(conf.confirmation_id).is_consumed

    def test_restore_skips_corrupt_and_expired(self):
        store = ConfirmationStore()
        n = store.restore([
            {"bad": "item"},
            {"confirmation_id": "c2", "actor_id": "a", "scope_key": "s",
             "action": "manage_config", "payload_digest": "d",
             "created_at": "not-a-date"},
        ])
        assert n == 0
        expired = ConfirmationStore(ttl_seconds=-1)
        conf = expired.create(_actor("u"), "s", "manage_config", {"a": 1})
        store2 = ConfirmationStore()
        assert store2.restore(expired.dump()) == 0  # 过期项不恢复

    def test_prune(self):
        store = self._store()
        c1 = store.create(_actor("u1"), "s1", "manage_config", {"a": 1})
        c2 = store.create(_actor("u2"), "s2", "manage_config", {"b": 2})
        store.approve(c1.confirmation_id)
        store.consume(c1.confirmation_id, _actor("u1"), "s1", {"a": 1})
        assert store.prune() == 1
        assert store.get(c1.confirmation_id) is None
        assert store.get(c2.confirmation_id) is not None


class TestRedactor:
    def _redact(self, text):
        return Redactor().redact(text)

    def test_credential_values(self):
        out, reasons = self._redact("key=sk-abcdefghijklmnopqrstuvwxyz123456 end")
        assert "sk-" not in out and "[REDACTED]" in out
        assert "credential" in reasons

    def test_bearer_token(self):
        out, _ = self._redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz.1234567890")
        assert "Bearer abcdefghijklmnopqrstuvwxyz" not in out

    def test_url_userinfo(self):
        out, reasons = self._redact("http://user:pass@example.com/x")
        assert "user:pass@" not in out and "[REDACTED]@" in out
        assert "url_userinfo" in reasons

    def test_url_query(self):
        out, reasons = self._redact("https://x.com/api?token=abc123&page=2")
        assert "token=abc123" not in out and "token=[REDACTED]" in out
        assert "page=2" in out  # 非敏感参数保留
        assert "url_query" in reasons

    def test_nested_structures(self):
        data = {"a": [{"b": "sk-abcdefghijklmnopqrstuvwxyz123456"}], "c": 1}
        out, reasons = Redactor().redact(data)
        assert "sk-" not in json.dumps(out)
        assert out["c"] == 1
        assert "credential" in reasons

    def test_idempotent(self):
        r = Redactor()
        once, _ = r.redact("token=sk-abcdefghijklmnopqrstuvwxyz123456")
        twice, _ = r.redact(once)
        assert once == twice


class TestRestrictedFilter:
    def test_restricted_detection(self):
        cases = [
            "password=hunter2",
            "密码: abc123456",
            "cookie=uin=123;skey=abc",
            "p_skey=ABCDEF123456",
            "-----BEGIN RSA PRIVATE KEY-----",
            "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
        ]
        for c in cases:
            assert main._contains_restricted(c), c
        for c in ("今天天气不错", "帮我查一下数据结构课程", "12345"):
            assert not main._contains_restricted(c), c

    def test_redact_text_helper(self):
        out = main._redact_text("我的 key: sk-abcdefghijklmnopqrstuvwxyz123456")
        assert "sk-" not in out and "[REDACTED]" in out
        assert main._redact_text("普通文本") == "普通文本"


class TestJSONMemoryV2:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "memory.json")
        repo = JSONMemoryRepository(path)
        repo.write(MemoryRecord(scope=_scope(), content="持久化消息"))
        repo2 = JSONMemoryRepository(path)
        assert len(repo2.query(_scope())) == 1
        assert repo2.query(_scope())[0].content == "持久化消息"

    def test_atomic_save_no_tmp_left(self, tmp_path):
        path = str(tmp_path / "memory.json")
        repo = JSONMemoryRepository(path)
        repo.write(MemoryRecord(scope=_scope(), content="x"))
        assert not os.path.exists(path + ".tmp")
        assert os.path.exists(path)

    def test_corrupt_file_quarantined_fail_closed(self, tmp_path):
        path = str(tmp_path / "memory.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        repo = JSONMemoryRepository(path)
        assert repo.count() == 0
        quarantined = glob.glob(path + ".corrupt-*")
        assert quarantined, "损坏文件应被隔离，不静默吞数据"
        assert not os.path.exists(path)

    def test_write_missing_scope_rejected(self, tmp_path):
        repo = JSONMemoryRepository(str(tmp_path / "m.json"))
        bad = MemoryRecord(
            scope=MemoryScope(
                memory_type=MemoryType.SHORT_TERM, platform="qq",
                bot_id="", conversation_id="g1", actor_id="u1"),
            content="缺 bot_id",
        )
        with pytest.raises(ValueError):
            repo.write(bad)

    def test_load_skips_missing_metadata(self, tmp_path):
        path = str(tmp_path / "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{
                "record_id": "bad1",
                "scope": {"memory_type": "short_term", "platform": "qq"},
                "content": "缺字段记录",
            }], f)
        repo = JSONMemoryRepository(path)
        assert repo.count() == 0

    def test_query_visible_sensitivity(self, tmp_path):
        repo = InMemoryRepository()
        repo.write(MemoryRecord(scope=_scope(actor="u1"), content="公开话题",
                                sensitivity=SensitivityLevel.INTERNAL))
        repo.write(MemoryRecord(scope=_scope(actor="u1"), content="本人私密",
                                sensitivity=SensitivityLevel.PRIVATE))
        repo.write(MemoryRecord(scope=_scope(actor="u1"), content="受限数据",
                                sensitivity=SensitivityLevel.RESTRICTED))
        # 本人：INTERNAL + PRIVATE，但 RESTRICTED 永不召回
        got = repo.query_visible(_scope(actor="u1"), viewer_actor_id="u1")
        contents = {r.content for r in got}
        assert "公开话题" in contents and "本人私密" in contents
        assert "受限数据" not in contents
        # 他人：PRIVATE 不可见
        got2 = repo.query_visible(_scope(actor="u1"), viewer_actor_id="u2")
        assert all(r.content != "本人私密" for r in got2)
        # 无 viewer：PRIVATE 也不可见
        got3 = repo.query_visible(_scope(actor="u1"))
        assert all(r.content != "本人私密" for r in got3)


class TestMainSecurityWiring:
    def _plugin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
        return main.Main(_make_context())

    def test_actor_for_roles(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "OWNER_IDS", {"u_owner"})
        monkeypatch.setattr(main, "ADMIN_IDS", {"u_admin"})
        monkeypatch.setattr(main, "TRUSTED_IDS", {"u_trusted"})
        monkeypatch.setattr(main, "MUTED_IDS", {"u_muted"})
        plugin = self._plugin(monkeypatch, tmp_path)
        assert plugin._actor_for(_FakeEvent("x", user="u_owner")).role == "owner"
        assert plugin._actor_for(_FakeEvent("x", user="u_admin")).role == "admin"
        assert plugin._actor_for(_FakeEvent("x", user="u_trusted")).role == "trusted"
        assert plugin._actor_for(_FakeEvent("x", user="u_muted")).role == "muted"
        assert plugin._actor_for(_FakeEvent("x", user="normal_user")).role == "normal"
        # 群级管理员边界：群主/管理员在群内视为 admin
        ev = _FakeEvent("x", user="group_admin", group="g1", sender_role="admin")
        assert plugin._actor_for(ev).role == "admin"
        # 私聊里群管理员身份不生效
        ev2 = _FakeEvent("x", user="group_admin", group=None, sender_role="admin")
        assert plugin._actor_for(ev2).role == "normal"

    def test_authorize_manage_default_deny(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        res, conf = plugin._authorize_manage(
            _FakeEvent("x", user="stranger"), resource="persona",
            payload={"target": "dududa_serious"})
        assert not res.allowed
        assert AuthReason.ROLE_TOO_LOW in res.reason_codes

    def test_authorize_manage_owner_admin_allowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "OWNER_IDS", {"u_owner"})
        monkeypatch.setattr(main, "ADMIN_IDS", {"u_admin"})
        monkeypatch.setattr(main, "MUTED_IDS", {"u_muted_owner"})
        plugin = self._plugin(monkeypatch, tmp_path)
        res, _ = plugin._authorize_manage(
            _FakeEvent("x", user="u_owner"), resource="persona",
            payload={"target": "dududa_serious"})
        assert res.allowed and AuthReason.OWNER_ALLOWED in res.reason_codes
        res, _ = plugin._authorize_manage(
            _FakeEvent("x", user="u_admin"), resource="persona",
            payload={"target": "dududa_serious"})
        assert res.allowed
        # muted 覆盖 owner
        monkeypatch.setattr(main, "MUTED_IDS", {"u_owner"})
        res, _ = plugin._authorize_manage(
            _FakeEvent("x", user="u_owner"), resource="persona",
            payload={"target": "dududa_serious"})
        assert not res.allowed and AuthReason.MUTED in res.reason_codes

    def test_trusted_confirmation_flow_persistent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "TRUSTED_IDS", {"u_trusted"})
        monkeypatch.setattr(main, "ADMIN_IDS", {"u_admin"})
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("x", user="u_trusted")
        res, conf = plugin._authorize_manage(
            ev, resource="persona", payload={"target": "dududa_serious"})
        assert res.decision == main.AuthorizationDecision.REQUIRE_CONFIRMATION
        assert conf is not None and conf.confirmation_id

        # 重启后确认仍存在（持久确认）
        plugin2 = self._plugin(monkeypatch, tmp_path)
        conf2 = plugin2.confirmations.get(conf.confirmation_id)
        assert conf2 is not None and not conf2.is_consumed

        # 未批准时重试仍拒绝
        res_pending, _ = plugin2._authorize_manage(
            ev, resource="persona", payload={"target": "dududa_serious"})
        assert not res_pending.allowed

        # 管理员批准
        assert plugin2.confirmations.approve(conf.confirmation_id)
        plugin2._save_confirmations()

        # 发起者重试 -> 消费成功
        res_ok, _ = plugin2._authorize_manage(
            ev, resource="persona", payload={"target": "dududa_serious"})
        assert res_ok.allowed
        assert AuthReason.CONFIRMATION_OK in res_ok.reason_codes

        # 重放 -> 拒绝（单次使用）
        res_replay, _ = plugin2._authorize_manage(
            ev, resource="persona", payload={"target": "dududa_serious"})
        assert not res_replay.allowed

    def test_store_memory_redacts_and_skips_restricted(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(
            ev, "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz123456")
        scope = plugin._make_scope(ev)
        records = plugin.memory.query(scope, limit=10)
        assert len(records) == 1
        assert "sk-" not in records[0].content
        assert "[REDACTED]" in records[0].content
        # Restricted 数据不落盘
        plugin._store_memory(ev, "密码: hunter2")
        assert plugin.memory.count(scope) == 1
        # 群聊记录默认 INTERNAL
        assert records[0].sensitivity == SensitivityLevel.INTERNAL

    def test_store_memory_private_defaults_private(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group=None)
        plugin._store_memory(ev, "我的私密笔记")
        records = plugin.memory.query(plugin._make_scope(ev), limit=10)
        assert records[0].sensitivity == SensitivityLevel.PRIVATE
        assert records[0].visibility == SensitivityLevel.PRIVATE

    def test_read_memory_visibility(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        group_ev = _FakeEvent("hi", user="u1", group="g1")
        plugin._store_memory(group_ev, "群里的公开话题")
        priv_ev = _FakeEvent("hi", user="u1", group=None, session="p1")
        plugin._store_memory(priv_ev, "我的私密笔记")
        # 群读：私聊记录不可见（跨会话隔离）
        text = plugin._read_memory(group_ev)
        assert "群里的公开话题" in text
        assert "私密笔记" not in text
        # 本人读私聊
        text2 = plugin._read_memory(priv_ev)
        assert "私密笔记" in text2
        # RESTRICTED 记录即使本人也读不到
        restricted_scope = plugin._make_scope(group_ev)
        plugin.memory.write(MemoryRecord(
            scope=restricted_scope, content="受限数据",
            sensitivity=SensitivityLevel.RESTRICTED,
            visibility=SensitivityLevel.RESTRICTED))
        text3 = plugin._read_memory(group_ev)
        assert "受限数据" not in text3

    def test_social_decision_fallback(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        # 实现层抛异常 -> 兜底普通回答
        def boom(self_, event):
            raise RuntimeError("boom")
        monkeypatch.setattr(main.Main, "_social_decision_impl", boom)
        action, reason = plugin._social_decision(_FakeEvent("x"))
        assert action == main.SocialAction.ANSWER
        assert reason == "normal"

    def test_group_safe_observations(self):
        obs_ok = main.ToolObservation(
            step_id="", capability_id="mcp.course_schedule",
            success=True, data="数据结构课程信息", source="mock")
        obs_sensitive = main.ToolObservation(
            step_id="", capability_id="mcp.exam_schedule",
            success=True, data="我的个人课表: 周一高数", source="mock")
        in_group = main._group_safe_observations([obs_ok, obs_sensitive], True)
        assert len(in_group) == 1 and in_group[0].capability_id == "mcp.course_schedule"
        in_private = main._group_safe_observations([obs_ok, obs_sensitive], False)
        assert len(in_private) == 2

    def test_scope_includes_persona(self, monkeypatch, tmp_path):
        plugin = self._plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("hi", user="u1", group="g1")
        assert plugin._make_scope(ev).persona_id == plugin.personas.active_id
        assert plugin.personas.switch("dududa_serious")
        assert plugin._make_scope(ev).persona_id == "dududa_serious"
        plugin.personas.switch("dududa_default")


class TestIsolationProd:
    def test_make_scope_isolation_matrix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "m.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "c.json"))
        plugin = main.Main(_make_context())
        ev1 = _FakeEvent("hi", user="u1", group="g1", bot="bot1")
        ev2 = _FakeEvent("hi", user="u1", group="g2", bot="bot1")
        ev3 = _FakeEvent("hi", user="u2", group="g1", bot="bot1")
        ev4 = _FakeEvent("hi", user="u1", group=None, session="p1", bot="bot1")
        ev5 = _FakeEvent("hi", user="u1", group="g1", bot="bot2")
        scopes = [plugin._make_scope(ev1), plugin._make_scope(ev2),
                  plugin._make_scope(ev3), plugin._make_scope(ev4),
                  plugin._make_scope(ev5)]
        keys = [s.to_key() for s in scopes]
        assert len(set(keys)) == 5, "跨群/跨用户/私聊/跨 Bot 全部隔离"
        # persona 切换产生不同 scope key
        plugin.personas.switch("dududa_serious")
        keys.append(plugin._make_scope(ev1).to_key())
        assert len(set(keys)) == 6
        plugin.personas.switch("dududa_default")

    def test_persistence_across_instances(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "mem.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "conf.json"))
        m1 = main.Main(_make_context())
        ev = _FakeEvent("hi", user="u1", group="g1")
        m1._store_memory(ev, "跨实例持久化测试")
        m2 = main.Main(_make_context())
        text = m2._read_memory(ev)
        assert "跨实例持久化测试" in text
