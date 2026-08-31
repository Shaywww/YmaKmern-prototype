# -*- coding: utf-8 -*-
"""CP-P2 高级能力测试（ADR-0001）：Playground 沙箱 / 成本性能 / 告警 / 日志检索。"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from dududa.control_plane.app import create_app
from dududa.core.capability import CapabilityRisk

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



def _write_tool_traces(trace_dir):
    """额外写入含 tool_result 的 trace（工具面板聚合用）。"""
    lines = [
        {"ts": "2026-08-08T10:00:00", "ts_ms": 1786000000000,
         "event": "tool_result", "run_id": "tr1", "trace_id": "tt1",
         "step_id": "s1", "capability_id": "mcp.clock",
         "success": True, "latency_ms": 12.3, "retries_used": 0},
        {"ts": "2026-08-08T10:01:00", "ts_ms": 1786000060000,
         "event": "tool_result", "run_id": "tr1", "trace_id": "tt1",
         "step_id": "s2", "capability_id": "mcp.clock",
         "success": True, "latency_ms": 8.1, "retries_used": 0},
        {"ts": "2026-08-08T10:02:00", "ts_ms": 1786000120000,
         "event": "tool_result", "run_id": "tr2", "trace_id": "tt2",
         "step_id": "s1", "capability_id": "mcp.web_search",
         "success": False, "latency_ms": 150.0, "retries_used": 2},
        {"ts": "2026-08-09T10:00:00", "ts_ms": 1786086400000,
         "event": "tool_result", "run_id": "tr3", "trace_id": "tt3",
         "step_id": "s1", "capability_id": "mcp.clock",
         "success": True, "latency_ms": 9.9, "retries_used": 0},
    ]
    (trace_dir / "2026-08-08.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")


def _write_persona_traces(trace_dir):
    """写入不含原始消息的人格影子评分 trace。"""
    lines = [
        {"ts": "2026-08-08T10:00:00", "event": "persona_shadow_score",
         "run_id": "pr1", "trace_id": "pt1", "scope_hash": "opaque1",
         "persona_consistency": 0.9, "conversationality": 0.8,
         "non_customer_tone": 0.7, "overall": 0.8,
         "violations": ["customer_template"]},
        {"ts": "2026-08-08T11:00:00", "event": "persona_shadow_score",
         "run_id": "pr2", "trace_id": "pt2", "scope_hash": "opaque2",
         "persona_consistency": 1.0, "conversationality": 0.9,
         "non_customer_tone": 0.8, "overall": 0.9,
         "violations": []},
        {"ts": "2026-08-09T10:00:00", "event": "persona_shadow_score",
         "run_id": "pr3", "trace_id": "pt3", "scope_hash": "opaque3",
         "persona_consistency": 0.8, "conversationality": 0.7,
         "non_customer_tone": 0.6, "overall": 0.7,
         "violations": ["customer_template", "listicle"]},
        {"ts": "2026-08-09T10:01:00", "event": "persona_shadow_error",
         "run_id": "pr4", "trace_id": "pt4", "error": "TimeoutError"},
    ]
    (trace_dir / "2026-08-09.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8")

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


class TestDashboard:
    def test_dashboard_public_html(self, cp):
        app, client = cp
        r = client.get("/", headers={"Authorization": ""})
        assert r.status_code == 200
        assert "工具使用率" in r.text and "成本周报" in r.text
        assert "人格质量趋势" in r.text
        assert "cp_token" in r.text  # 前端登录框（localStorage）

    def test_dashboard_api_still_protected(self, cp):
        app, client = cp
        for p in ("/metrics/tools", "/metrics/costs",
                  "/metrics/persona", "/personas"):
            assert client.get(p, headers={"Authorization": ""}).status_code == 401


class TestMetricsPersona:
    def test_persona_quality_aggregation(self, cp, tmp_path):
        app, client = cp
        _write_persona_traces(tmp_path / "traces")
        r = client.get("/metrics/persona")
        assert r.status_code == 200
        data = r.json()
        assert data["samples"] == 3
        assert data["errors"] == 1
        assert data["averages"] == {
            "persona_consistency": 0.9,
            "conversationality": 0.8,
            "non_customer_tone": 0.7,
            "overall": 0.8,
        }
        assert data["violations"] == {
            "customer_template": 2, "listicle": 1}
        assert data["by_day"] == [
            {"day": "2026-08-08", "samples": 2, "overall": 0.85},
            {"day": "2026-08-09", "samples": 1, "overall": 0.7},
        ]
        assert data["privacy"] == "scores_only"
        assert "user_message" not in json.dumps(data)

    def test_persona_quality_empty(self, cp):
        app, client = cp
        r = client.get("/metrics/persona")
        assert r.status_code == 200
        data = r.json()
        assert data["samples"] == 0
        assert data["errors"] == 0
        assert data["averages"]["overall"] is None
        assert data["by_day"] == []


class TestMetricsTools:
    def test_tools_aggregation(self, cp, tmp_path):
        app, client = cp
        _write_tool_traces(tmp_path / "traces")
        r = client.get("/metrics/tools")
        assert r.status_code == 200
        data = r.json()
        assert data["window_calls"] == 4
        assert data["window_failures"] == 1
        assert data["window_fail_rate"] == 0.25
        tools = {t["capability_id"]: t for t in data["by_tool"]}
        assert tools["mcp.clock"]["calls"] == 3
        assert tools["mcp.clock"]["failures"] == 0
        assert tools["mcp.clock"]["avg_latency_ms"] == 10.1
        assert tools["mcp.web_search"]["calls"] == 1
        assert tools["mcp.web_search"]["failures"] == 1
        assert tools["mcp.web_search"]["fail_rate"] == 1.0
        assert tools["mcp.web_search"]["retries_used"] == 2
        assert data["by_tool"][0]["capability_id"] == "mcp.clock"  # 按调用量降序
        assert [d["day"] for d in data["by_day"]] == [
            "2026-08-08", "2026-08-09"]
        assert data["by_day"][0]["fail_rate"] == round(1 / 3, 4)

    def test_tools_empty(self, cp):
        app, client = cp
        r = client.get("/metrics/tools")
        assert r.status_code == 200
        data = r.json()
        assert data["window_calls"] == 0
        assert data["window_failures"] == 0
        assert data["window_fail_rate"] == 0.0
        assert data["by_tool"] == []
        assert data["by_day"] == []


class TestCostsWeekly:
    def test_costs_weekly_report(self, cp):
        app, client = cp
        r = client.get("/metrics/costs")
        assert r.status_code == 200
        data = r.json()
        assert data["estimate"] is True
        assert data["est_cost_yuan"] > 0
        assert isinstance(data["weekly"], list) and data["weekly"]
        w = data["weekly"][0]
        assert w["week"] and w["start"]
        assert w["calls"] == 5
        assert w["degraded"] == 3
        assert w["errors"] == 3
        assert w["est_cost_yuan"] > 0
        assert w["by_model"]["deepseek-chat"] == 5


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
