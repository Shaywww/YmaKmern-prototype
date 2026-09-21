# -*- coding: utf-8 -*-
"""CP-P0 安全基线测试（ADR-0001）：鉴权 / 权限 / 审计 / 脱敏 / Scope / MCP 入口。

对应 ADR 第 6 节退出门禁：
1. 负向鉴权：无/错 token 401，/health 豁免，未配置凭据 fail closed；
2. 非 owner 写操作 403；
3. 脱敏不变量：Trace metadata 不含明文凭证；
4. Scope 过滤：非 owner 只见自己的事件；
5. 审计完整性：受保护请求必有审计行（含 actor/role/status）；
6. MCP query 不再直连，只允许注册表中的通用只读能力。
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from dududa.control_plane.app import create_app
from dududa.observability.observability import TraceEvent

TOKEN = "cp-secret"


@pytest.fixture()
def cp(tmp_path):
    os.environ["DUDUDA_CP_TOKEN"] = TOKEN
    os.environ["DUDUDA_CP_AUDIT"] = str(tmp_path / "cp_audit.jsonl")
    app = create_app()
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    yield app, client
    os.environ.pop("DUDUDA_CP_TOKEN", None)
    os.environ.pop("DUDUDA_CP_AUDIT", None)


def _auth(**extra):
    h = {"Authorization": f"Bearer {TOKEN}"}
    h.update(extra)
    return h


class TestAuth:
    def test_health_public(self, cp):
        app, client = cp
        assert client.get("/health").status_code == 200

    def test_no_token_401(self, cp):
        app, client = cp
        blank = {"Authorization": ""}
        assert client.get("/personas", headers=blank).status_code == 401
        assert client.get("/traces", headers=blank).status_code == 401
        r = client.post("/mcp/services/clock/query", json={"action": "get_now"},
                        headers=blank)
        assert r.status_code == 401

    def test_wrong_token_401(self, cp):
        app, client = cp
        assert client.get("/personas",
                          headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_x_cp_token_header_ok(self, cp):
        app, client = cp
        assert client.get("/personas",
                          headers={"X-CP-Token": TOKEN}).status_code == 200

    def test_invalid_role_401(self, cp):
        app, client = cp
        assert client.get("/personas", headers=_auth(
            **{"X-CP-Role": "superuser"})).status_code == 401

    def test_token_unset_fail_closed(self, tmp_path):
        os.environ.pop("DUDUDA_CP_TOKEN", None)
        os.environ["DUDUDA_CP_AUDIT"] = str(tmp_path / "a.jsonl")
        client = TestClient(create_app())
        assert client.get("/personas").status_code == 401
        os.environ.pop("DUDUDA_CP_AUDIT", None)


class TestWritePermission:
    def test_normal_role_write_forbidden(self, cp):
        app, client = cp
        h = _auth(**{"X-CP-Operator": "u_normal", "X-CP-Role": "normal"})
        r = client.put("/personas/overrides/groups/g1",
                       json={"persona_id": "dududa_default"}, headers=h)
        assert r.status_code == 403

    def test_admin_role_write_forbidden(self, cp):
        app, client = cp
        h = _auth(**{"X-CP-Operator": "u_admin", "X-CP-Role": "admin"})
        r = client.delete("/personas/overrides/users/u1", headers=h)
        assert r.status_code == 403

    def test_owner_write_allowed_and_cleaned(self, cp):
        app, client = cp
        r = client.put("/personas/overrides/groups/g1",
                       json={"persona_id": "dududa_default"})
        assert r.status_code == 200
        assert client.delete("/personas/overrides/groups/g1").status_code == 200

    def test_normal_role_read_allowed(self, cp):
        app, client = cp
        h = _auth(**{"X-CP-Operator": "u_n", "X-CP-Role": "normal"})
        assert client.get("/personas", headers=h).status_code == 200


class TestAudit:
    def test_protected_requests_audited(self, cp):
        app, client = cp
        client.get("/personas")
        client.put("/personas/overrides/groups/g1",
                   json={"persona_id": "dududa_default"})
        lines = app.state.audit_logger.lines()
        assert len(lines) >= 2
        assert any(l["path"] == "/personas" for l in lines)
        write = [l for l in lines if l["method"] == "PUT"]
        assert write and write[0]["status"] == 200

    def test_audit_has_actor_and_role(self, cp):
        app, client = cp
        client.get("/personas", headers=_auth(
            **{"X-CP-Operator": "u1", "X-CP-Role": "trusted"}))
        line = app.state.audit_logger.lines()[-1]
        assert line["actor"] == "u1" and line["role"] == "trusted"


class TestRedactionAndScope:
    def test_trace_redaction(self, cp):
        app, client = cp
        app.state.trace_sink.write(TraceEvent(
            level="phase", phase="test",
            metadata={"msg": "key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"}))
        r = client.get("/traces")
        assert r.status_code == 200
        text = json.dumps(r.json())
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in text
        assert "[REDACTED]" in text

    def test_scope_filter_non_owner(self, cp):
        app, client = cp
        app.state.trace_sink.write(TraceEvent(
            level="phase", phase="p", trace_id="t1",
            metadata={"actor": "u1"}))
        app.state.trace_sink.write(TraceEvent(
            level="phase", phase="p", trace_id="t2",
            metadata={"actor": "u2"}))
        h = _auth(**{"X-CP-Operator": "u1", "X-CP-Role": "trusted"})
        events = client.get("/traces", headers=h).json()["events"]
        assert len(events) == 1
        assert events[0]["metadata"]["actor"] == "u1"

    def test_owner_sees_all(self, cp):
        app, client = cp
        app.state.trace_sink.write(TraceEvent(
            level="phase", phase="p", metadata={"actor": "u1"}))
        app.state.trace_sink.write(TraceEvent(
            level="phase", phase="p", metadata={"actor": "u2"}))
        assert client.get("/traces").json()["count"] >= 2


class TestMCPEntry:
    def test_query_via_registry_success(self, cp):
        app, client = cp
        r = client.post("/mcp/services/clock/query", json={"action": "get_now"})
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_removed_school_service_query_404(self, cp):
        app, client = cp
        r = client.post("/mcp/services/course_schedule/query",
                        json={"action": "nope"})
        assert r.status_code == 404
