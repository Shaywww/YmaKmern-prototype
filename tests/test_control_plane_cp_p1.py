# -*- coding: utf-8 -*-
"""CP-P1 只读面板测试（ADR-0001）：Memory Explorer / Eval 报告 / Trace 只读面。"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from packages.control_plane.app import create_app
from packages.core.memory import (
    JSONMemoryRepository, MemoryRecord, MemoryScope,
    MemoryType, SensitivityLevel,
)

TOKEN = "cp-secret"


def _write_memory(path):
    """预置 4 条：u1 public / u1 private（含凭据样式的 key=sk-…）/ u2 restricted / u2 public。"""
    repo = JSONMemoryRepository(path=str(path))

    def rec(actor, sensitivity, content, conv="c1"):
        return MemoryRecord(
            scope=MemoryScope(
                memory_type=MemoryType.SHORT_TERM,
                platform="qq", bot_id="b1",
                conversation_id=conv, actor_id=actor),
            content=content, sensitivity=sensitivity, source="message")

    rids = {
        "pub": repo.write(rec("u1", SensitivityLevel.INTERNAL, "u1 public note")),
        "priv": repo.write(rec("u1", SensitivityLevel.PRIVATE,
                               "u1 private secret key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")),
        "rest": repo.write(rec("u2", SensitivityLevel.RESTRICTED, "u2 restricted")),
        "u2": repo.write(rec("u2", SensitivityLevel.INTERNAL, "u2 public note")),
    }
    return rids


@pytest.fixture()
def cp(tmp_path):
    os.environ["DUDUDA_CP_TOKEN"] = TOKEN
    os.environ["DUDUDA_CP_AUDIT"] = str(tmp_path / "cp_audit.jsonl")
    # MCP access 隔离：指向不存在的路径 -> legacy allow
    os.environ.setdefault("DUDUDA_MCP_ACCESS", "/tmp/dududa-cp-test-access-absent.json")
    mem = tmp_path / "memory.json"
    os.environ["DUDUDA_MEMORY_FILE"] = str(mem)
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "thresholds.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (evals / "2026-08-07.jsonl").write_text(
        '{"ts":"2026-08-07T00:00:00","event":"flow_start","msg":"key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"}\n'
        '{"ts":"2026-08-07T00:00:01","event":"run_end","outcome":"succeeded"}\n',
        encoding="utf-8")
    os.environ["DUDUDA_EVAL_DIR"] = str(evals)
    rids = _write_memory(mem)
    app = create_app()
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    yield app, client, rids
    for k in ("DUDUDA_CP_TOKEN", "DUDUDA_CP_AUDIT", "DUDUDA_MCP_ACCESS",
              "DUDUDA_MEMORY_FILE", "DUDUDA_EVAL_DIR"):
        os.environ.pop(k, None)


def _auth(**extra):
    h = {"Authorization": f"Bearer {TOKEN}"}
    h.update(extra)
    return h


class TestMemoryExplorer:
    def test_owner_lists_public_only(self, cp):
        app, client, rids = cp
        r = client.get("/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2  # u1 public + u2 public；private/restricted 永不外泄
        actors = {rec["scope"]["actor_id"] for rec in data["records"]}
        assert actors == {"u1", "u2"}

    def test_owner_scope_by_actor(self, cp):
        app, client, rids = cp
        r = client.get("/memory", params={"actor_id": "u1"})
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert r.json()["records"][0]["scope"]["actor_id"] == "u1"

    def test_owner_cannot_see_private_or_restricted(self, cp):
        app, client, rids = cp
        assert client.get(f"/memory/{rids['priv']}").status_code == 404
        assert client.get(f"/memory/{rids['rest']}").status_code == 404

    def test_actor_sees_own_private_redacted(self, cp):
        app, client, rids = cp
        h = _auth(**{"X-CP-Operator": "u1", "X-CP-Role": "trusted"})
        r = client.get(f"/memory/{rids['priv']}", headers=h)
        assert r.status_code == 200
        text = json.dumps(r.json())
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in text
        assert "[REDACTED]" in text

    def test_actor_cannot_scope_to_others(self, cp):
        app, client, rids = cp
        h = _auth(**{"X-CP-Operator": "u1", "X-CP-Role": "trusted"})
        r = client.get("/memory", params={"actor_id": "u2"}, headers=h)
        assert r.status_code == 403

    def test_non_owner_sees_only_self(self, cp):
        app, client, rids = cp
        h = _auth(**{"X-CP-Operator": "u1", "X-CP-Role": "trusted"})
        r = client.get("/memory", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2  # u1 public + u1 private（本人）
        assert all(rec["scope"]["actor_id"] == "u1" for rec in data["records"])

    def test_invalid_memory_type_400(self, cp):
        app, client, rids = cp
        assert client.get("/memory", params={"memory_type": "nope"}).status_code == 400

    def test_no_write_route(self, cp):
        app, client, rids = cp
        r = client.post("/memory", json={"content": "x"})
        assert r.status_code == 405

    def test_memory_read_is_audited(self, cp):
        app, client, rids = cp
        client.get("/memory")
        lines = app.state.audit_logger.lines()
        assert any(l["path"] == "/memory" for l in lines)


class TestEvalReports:
    def test_list_reports(self, cp):
        app, client, rids = cp
        r = client.get("/eval/reports")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        names = {rep["name"] for rep in data["reports"]}
        assert names == {"thresholds.json", "2026-08-07.jsonl"}

    def test_read_report_redacted(self, cp):
        app, client, rids = cp
        r = client.get("/eval/reports/2026-08-07.jsonl")
        assert r.status_code == 200
        text = json.dumps(r.json())
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in text
        assert "[REDACTED]" in text
        assert r.json()["count"] == 2

    def test_read_thresholds(self, cp):
        app, client, rids = cp
        r = client.get("/eval/reports/thresholds.json")
        assert r.status_code == 200
        assert r.json()["entries"][0] == {"ok": True}

    def test_path_traversal_rejected(self, cp):
        app, client, rids = cp
        assert client.get("/eval/reports/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
        assert client.get("/eval/reports/foo.txt").status_code == 400

    def test_missing_report_404(self, cp):
        app, client, rids = cp
        assert client.get("/eval/reports/nope.jsonl").status_code == 404


class TestTraceReadOnly:
    def test_trace_limit_param(self, cp):
        app, client, rids = cp
        r = client.get("/traces", params={"limit": 3})
        assert r.status_code == 200
        assert r.json()["count"] <= 3
