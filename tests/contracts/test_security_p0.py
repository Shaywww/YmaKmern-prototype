"""Phase 3 —— 权限、持久确认与脱敏的负向测试。

覆盖文档 2.4.23 / 2.4.24：default deny、muted overlay、角色等级、
确认绑定（单次使用/过期/digest/换人/换会话）、Redaction 幂等与嵌套。
"""
import sys
import pytest

from dududa.core.envelope import Actor, Platform
from dududa.safeguards.security import (
    AuthorizationDecision, AuthReason, PermissionEngine,
    ConfirmationStore, Redactor,
)


def actor(role="normal", aid="u1", deny=()):
    return Actor(
        actor_id=aid, platform=Platform.QQ, display_name=aid,
        role=role, deny_flags=deny,
    )


class TestPermissionEngine:
    def test_default_deny_unknown_actor(self):
        result = PermissionEngine().authorize(None, "send", scope_key="g1")
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.UNKNOWN_ACTOR in result.reason_codes

    def test_default_deny_missing_scope(self):
        result = PermissionEngine().authorize(actor("admin"), "send", scope_key="")
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.UNKNOWN_SCOPE in result.reason_codes

    def test_muted_overlay_denies_even_admin(self):
        result = PermissionEngine().authorize(
            actor("admin", deny=("muted",)), "admin", scope_key="g1"
        )
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.MUTED in result.reason_codes

    def test_muted_role_denied(self):
        result = PermissionEngine().authorize(actor("muted"), "send", scope_key="g1")
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.MUTED in result.reason_codes

    def test_role_hierarchy(self):
        engine = PermissionEngine()
        # trusted 不能 manage_config
        assert engine.authorize(
            actor("trusted"), "manage_config", scope_key="g1"
        ).decision == AuthorizationDecision.DENY
        # admin 也不能 manage_config（rank 3 < 4）
        assert engine.authorize(
            actor("admin"), "manage_config", scope_key="g1"
        ).decision == AuthorizationDecision.DENY
        # owner 可以
        assert engine.authorize(
            actor("owner"), "manage_config", scope_key="g1"
        ).decision == AuthorizationDecision.ALLOW
        # admin 可以使用工具（rank 3 >= 2）
        assert engine.authorize(
            actor("admin"), "use_tool", scope_key="g1"
        ).decision == AuthorizationDecision.ALLOW

    def test_normal_can_send(self):
        result = PermissionEngine().authorize(actor("normal"), "send", scope_key="g1")
        assert result.decision == AuthorizationDecision.ALLOW
        assert AuthReason.ROLE_ALLOWED in result.reason_codes

    def test_capability_risk_gate(self):
        # trusted(2) 通过 use_tool(2)，但 high 风险(3) 被拦截
        result = PermissionEngine().authorize(
            actor("trusted"), "use_tool", scope_key="g1", capability_risk="high"
        )
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.CAPABILITY_RISK in result.reason_codes
        # admin(3) 允许 high(3) 风险能力
        assert PermissionEngine().authorize(
            actor("admin"), "use_tool", scope_key="g1", capability_risk="high"
        ).decision == AuthorizationDecision.ALLOW

    def test_confirmation_required(self):
        result = PermissionEngine().authorize(
            actor("admin"), "send", scope_key="g1", requires_confirmation=True
        )
        assert result.decision == AuthorizationDecision.REQUIRE_CONFIRMATION
        assert AuthReason.CONFIRMATION_REQUIRED in result.reason_codes


class TestConfirmationStore:
    def test_create_and_confirm(self):
        store = ConfirmationStore()
        conf = store.create(actor("admin"), "g1", "send", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("admin"), "g1", {"x": 1})
        assert result.decision == AuthorizationDecision.ALLOW
        assert AuthReason.CONFIRMATION_OK in result.reason_codes

    def test_single_use_replay_denied(self):
        store = ConfirmationStore()
        conf = store.create(actor("admin"), "g1", "send", {"x": 1})
        store.confirm(conf.confirmation_id, actor("admin"), "g1", {"x": 1})
        replay = store.confirm(conf.confirmation_id, actor("admin"), "g1", {"x": 1})
        assert replay.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_REPLAYED in replay.reason_codes

    def test_expired_denied(self):
        store = ConfirmationStore(ttl_seconds=-1)
        conf = store.create(actor("admin"), "g1", "send", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("admin"), "g1", {"x": 1})
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_EXPIRED in result.reason_codes

    def test_payload_digest_mismatch(self):
        store = ConfirmationStore()
        conf = store.create(actor("admin"), "g1", "send", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("admin"), "g1", {"x": 2})
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_DIGEST_MISMATCH in result.reason_codes

    def test_actor_change_invalidates(self):
        store = ConfirmationStore()
        conf = store.create(actor("admin", aid="u1"), "g1", "send", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("admin", aid="u2"), "g1", {"x": 1})
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_ACTOR_MISMATCH in result.reason_codes

    def test_scope_change_invalidates(self):
        store = ConfirmationStore()
        conf = store.create(actor("admin"), "g1", "send", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("admin"), "g2", {"x": 1})
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.CONFIRMATION_SCOPE_MISMATCH in result.reason_codes

    def test_execute_time_reauthorization(self):
        """角色降级后，即使确认未消费也拒绝。"""
        store = ConfirmationStore()
        conf = store.create(actor("admin"), "g1", "manage_config", {"x": 1})
        result = store.confirm(conf.confirmation_id, actor("normal"), "g1", {"x": 1})
        assert result.decision == AuthorizationDecision.DENY
        assert AuthReason.ROLE_TOO_LOW in result.reason_codes


class TestRedactor:
    def test_sk_key(self):
        out, reasons = Redactor().redact("key=sk-abcdefghijklmnopqrstuvwxyz123456")
        assert "[REDACTED]" in out
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "credential" in reasons

    def test_bearer_token(self):
        out, reasons = Redactor().redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
        assert "[REDACTED]" in out
        assert "credential" in reasons

    def test_url_userinfo(self):
        out, reasons = Redactor().redact("https://admin:secret123@example.com/x")
        assert "admin:secret123@" not in out
        assert "example.com" in out
        assert "url_userinfo" in reasons

    def test_url_query(self):
        out, reasons = Redactor().redact("https://example.com/api?token=abc123&page=2")
        assert "token=abc123" not in out
        assert "token=[REDACTED]" in out
        assert "page=2" in out
        assert "url_query" in reasons

    def test_nested_structure(self):
        data = {
            "ok": True,
            "meta": {"api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"},
            "items": [{"url": "https://u:p@h/x?secret=zzz"}],
        }
        out, reasons = Redactor().redact(data)
        assert out["meta"]["api_key"] == "[REDACTED]"
        assert "secret=zzz" not in out["items"][0]["url"]
        assert "u:p@" not in out["items"][0]["url"]
        assert {"credential", "url_userinfo", "url_query"} <= set(reasons)

    def test_idempotent(self):
        r = Redactor()
        text = "https://u:p@h/x?token=abc sk-abcdefghijklmnopqrstuvwxyz123456"
        first, _ = r.redact(text)
        second, _ = r.redact(first)
        assert first == second

    def test_safe_text_unchanged(self):
        out, reasons = Redactor().redact("今天天气不错，明天考试加油")
        assert out == "今天天气不错，明天考试加油"
        assert reasons == ()
