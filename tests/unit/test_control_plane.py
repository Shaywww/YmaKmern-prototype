import os

import pytest
from fastapi.testclient import TestClient
from dududa.control_plane.app import create_app


@pytest.fixture
def client(tmp_path):
    """CP-P0（ADR-0001）：所有请求带管理 token；审计落 tmp。"""
    os.environ["DUDUDA_CP_TOKEN"] = "cp-test-token"
    os.environ["DUDUDA_CP_AUDIT"] = str(tmp_path / "cp_audit.jsonl")
    os.environ["DUDUDA_EVOLUTION_DIR"] = str(tmp_path / "evolution")
    # MCP access 隔离：指向不存在的路径 -> legacy allow（生产 default deny 不干扰测试）
    os.environ.setdefault("DUDUDA_MCP_ACCESS", "/tmp/dududa-cp-test-access-absent.json")
    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer cp-test-token"})
    yield c
    os.environ.pop("DUDUDA_MCP_ACCESS", None)
    os.environ.pop("DUDUDA_CP_TOKEN", None)
    os.environ.pop("DUDUDA_CP_AUDIT", None)
    os.environ.pop("DUDUDA_EVOLUTION_DIR", None)

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "services" in data
        expected = ("ok" if all(v == "healthy" for v in data["services"].values())
                    else "degraded")
        assert data["status"] == expected
        assert "active_persona" in data

class TestPersonas:
    def test_list_personas(self, client):
        r = client.get("/personas")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 4
        assert "dududa_default" in data["personas"]

    def test_get_persona(self, client):
        r = client.get("/personas/dududa_default")
        assert r.status_code == 200
        data = r.json()
        assert data["persona_id"] == "dududa_default"

    def test_get_persona_404(self, client):
        r = client.get("/personas/nonexistent")
        assert r.status_code == 404

    def test_activate_persona(self, client):
        r = client.post("/personas/dududa_serious/activate")
        assert r.status_code == 200
        assert r.json()["active"] == "dududa_serious"
        # Switch back
        client.post("/personas/dududa_default/activate")

    def test_activate_404(self, client):
        r = client.post("/personas/nonexistent/activate")
        assert r.status_code == 404

    def test_create_and_delete_persona(self, client):
        r = client.put("/personas", json={
            "persona_id": "test_oc",
            "name": "Test OC",
            "display_name": "Test OC",
            "description": "A test persona"
        })
        assert r.status_code == 200
        assert r.json()["created"] == "test_oc"
        # Delete it
        r = client.delete("/personas/test_oc")
        assert r.status_code == 200

    def test_create_duplicate(self, client):
        r = client.put("/personas", json={"persona_id": "dududa_default", "name": "dup"})
        assert r.status_code == 409

    def test_delete_protected(self, client):
        r = client.delete("/personas/dududa_default")
        assert r.status_code == 400

    def test_overrides(self, client):
        # Set group override
        r = client.put("/personas/overrides/groups/g123", json={"persona_id": "dududa_mentor"})
        assert r.status_code == 200
        # List overrides
        r = client.get("/personas/overrides")
        assert r.status_code == 200
        assert "g123" in r.json()["groups"]
        # Clear
        r = client.delete("/personas/overrides/groups/g123")
        assert r.status_code == 200

class TestMCP:
    def test_list_services(self, client):
        r = client.get("/mcp/services")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 13
        assert "clock" in data["services"]
        assert "course_schedule" in data["services"]

    def test_service_health(self, client):
        r = client.get("/mcp/services/course_schedule/health")
        assert r.status_code == 200
        assert r.json()["health"] in ("healthy", "degraded", "unavailable", "unknown")

    def test_service_404(self, client):
        r = client.get("/mcp/services/nonexistent/health")
        assert r.status_code == 404

    def test_query_service(self, client):
        r = client.post("/mcp/services/clock/query", json={"action": "get_now"})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True

    def test_query_unknown_action(self, client):
        r = client.post("/mcp/services/course_schedule/query", json={"action": "nonexistent"})
        assert r.status_code == 400

class TestTraces:
    def test_list_traces(self, client):
        r = client.get("/traces")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert "count" in data

    def test_trace_404(self, client):
        r = client.get("/traces/no-such-trace")
        assert r.status_code == 404

    def test_run_traces_empty(self, client):
        r = client.get("/traces/runs/no-such-run")
        assert r.status_code == 200
        assert r.json()["count"] == 0

class TestRuntime:
    def test_runtime_state(self, client):
        r = client.get("/runtime/state")
        assert r.status_code == 200
        data = r.json()
        assert "active_persona" in data
        assert data["persona_count"] >= 4
        assert data["mcp_services"] == 13
        assert data["evolution"]["mode"] == "shadow"
        assert data["evolution"]["auto_activate"] is False


class TestShadowEvolution:
    def test_owner_can_collect_analyze_and_review_without_activation(self, client):
        for summary in ("天气地点错误一", "天气地点错误二", "天气地点错误三"):
            r = client.post("/evolution/experiences", json={"summary": summary})
            assert r.status_code == 200
        r = client.post("/evolution/analyze")
        assert r.status_code == 200
        assert r.json()["created_or_updated"] == 1
        candidate = client.get("/evolution/candidates").json()["candidates"][0]
        assert candidate["activation"] == "disabled"
        r = client.post(
            f"/evolution/candidates/{candidate['candidate_id']}/decision",
            json={"decision": "approve", "note": "值得实现"})
        assert r.status_code == 200
        assert r.json()["status"] == "approved_for_implementation"
        assert r.json()["activation"] == "disabled"

    def test_non_owner_cannot_write(self, client):
        r = client.post("/evolution/experiences", json={"summary": "测试"},
                        headers={"X-CP-Role": "normal", "X-CP-Operator": "u1"})
        assert r.status_code == 403
        assert client.get("/evolution/experiences", headers={
            "X-CP-Role": "normal", "X-CP-Operator": "u1"}).status_code == 403

    def test_no_activation_endpoint_exists(self, client):
        assert client.post("/evolution/candidates/anything/activate").status_code == 404

class TestDashboard:
    def test_dashboard_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "YmaKmern" in r.text

print("Test file ready")
