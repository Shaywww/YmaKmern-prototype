# -*- coding: utf-8 -*-
"""CP-P2 高级能力测试（ADR-0001）：Playground 沙箱 / 成本性能 / 告警 / 日志检索。"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from packages.control_plane.app import create_app
from packages.core.capability import CapabilityRisk

TOKEN = "cp-secret"


def _write_traces(trace_dir):
    trace_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() * 1000
    lines = []
    resp = [
        (100.0, False, ""), (200.0, True, "timeout"), (300.0, True, "timeout"),
        (400.0, True, "timeout"), (500.0, False, ""),
    ]
    for i, (lat, deg, err) in enumerate(resp):
        lines.append(json.dumps({
            "ts": "2026-08-07T00:00:00", "ts_ms": now - 1000 * (5 - i),
            "event": "model_response", "run_id": f"r{i}", "trace_id": f"t{i}",
            "role": "response_composition", "model_id": "deepseek-chat",
            "degraded": deg, "latency_ms": lat, "error_kind": err,
        }))
    lines.append(json.dumps({
        "ts": "2026-08-07T00:00:00", "ts_ms": now,
        "event": "flow_start", "run_id": "rf", "trace_id": "tf",
        "msg": "hello key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "session": "s1",
    }))
    for i in range(5):
        lines.append(json.dumps({
            "ts": "2026-08-07T00:00:00", "ts_ms": now - i * 1000,
            "event": "memory_gate", "decision": "reject",
            "sensitivity": "internal",
        }))
    (trace_dir / "2026-08-07.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def cp(tmp_path):
    os.environ["DUDUDA_CP_TOKEN"] = TOKEN
    os.environ["DUDUDA_CP_AUDIT"] = str(tmp_path / "cp_audit.jsonl")
    os.environ.setdefault("DUDUDA_MCP_ACCESS", "/tmp/dududa-cp-test-access-absent.json")
    os.environ["DUDUDA_MEMORY_FILE"] = str(tmp_path / "memory.json")
    os.environ["DUDUDA_EVAL_DIR"] = str(tmp_path / "evals")
    os.environ["DUDUDA_CP_TRACE_DIR"] = str(tmp_path / "traces")
    _write_traces(tmp_path / "traces")
    app = create_app()
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    yield app, client
    for k in ("DUDUDA_CP_TOKEN", "DUDUDA_CP_AUDIT", "DUDUDA_MCP_ACCESS",
              "DUDUDA_MEMORY_FILE", "DUDUDA_EVAL_DIR", "DUDUDA_CP_TRACE_DIR"):
        os.environ.pop(k, None)


def _auth(**extra):
    h = {"Authorization": f"Bearer {TOKEN}"}
    h.update(extra)
    return h


class TestPlayground:
    def test_owner_run_offline(self, cp):
        app, client = cp
        r = client.post("/playground/run", json={"message": "你好"})
        assert r.status_code == 200
        data = r.json()
        assert data["sandboxed"] is True
        assert data["outcome"]
        assert isinstance(data["reply"], str)
        assert data["run_id"]

    def test_non_owner_forbidden(self, cp):
        app, client = cp
        h = _auth(**{"X-CP-Operator": "u1", "X-CP-Role": "trusted"})
        r = client.post("/playground/run", json={"message": "你好"}, headers=h)
        assert r.status_code == 403

    def test_no_token_401(self, cp):
        app, client = cp
        r = client.post("/playground/run", json={"message": "你好"},
                        headers={"Authorization": ""})
        assert r.status_code == 401

    def test_empty_message_400(self, cp):
        app, client = cp
        assert client.post("/playground/run",
                           json={"message": "  "}).status_code == 400

    def test_long_message_400(self, cp):
        app, client = cp
        assert client.post("/playground/run",
                           json={"message": "x" * 4001}).status_code == 400

    def test_dangerous_capabilities_removed(self, cp):
        app, client = cp
        caps = app.state.playground.cap_registry.list_enabled()
        assert all(c.risk != CapabilityRisk.DANGEROUS for c in caps)


class TestMetricsCosts:
    def test_costs_aggregation(self, cp):
        app, client = cp
        r = client.get("/metrics/costs")
        assert r.status_code == 200
        data = r.json()
        assert data["window_events"] == 5
        assert data["calls_by_model"]["deepseek-chat"] == 5
        assert data["degraded"] == 3
        assert data["errors"] == 3
        assert data["estimate"] is True


class TestMetricsPerformance:
    def test_performance_stats(self, cp):
        app, client = cp
        r = client.get("/metrics/performance")
        assert r.status_code == 200
        data = r.json()
        assert data["calls"] == 5
        assert data["latency_ms_avg"] == 300.0
        assert data["latency_ms_p50"] == 300.0
        assert data["latency_ms_p95"] == 400.0
        assert data["error_rate"] == 0.6


class TestAlerts:
    def test_alert_rules(self, cp):
        app, client = cp
        r = client.get("/alerts")
        assert r.status_code == 200
        data = r.json()
        rules = {a["rule"]: a["severity"] for a in data["alerts"]}
        assert rules["model_degraded_ratio"] == "warn"
        assert rules["model_errors"] == "critical"
        assert rules["memory_gate_pressure"] == "info"


class TestLogs:
    def test_logs_traces(self, cp):
        app, client = cp
        r = client.get("/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "traces"
        assert data["count"] == 11  # 5 resp + 1 flow_start + 5 memory_gate

    def test_logs_level_filter(self, cp):
        app, client = cp
        r = client.get("/logs", params={"level": "flow_start"})
        assert r.json()["count"] == 1

    def test_logs_query_filter(self, cp):
        app, client = cp
        r = client.get("/logs", params={"query": "memory_gate"})
        assert r.json()["count"] == 5

    def test_logs_redaction(self, cp):
        app, client = cp
        r = client.get("/logs", params={"query": "sk-ABC"})
        text = json.dumps(r.json())
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in text
        assert "[REDACTED]" in text

    def test_logs_source_audit(self, cp):
        app, client = cp
        client.get("/personas")
        r = client.get("/logs", params={"source": "audit"})
        data = r.json()
        assert data["count"] >= 1
        assert data["logs"][0]["source"] == "audit"

    def test_logs_limit(self, cp):
        app, client = cp
        r = client.get("/logs", params={"limit": 3})
        assert r.json()["count"] == 3
