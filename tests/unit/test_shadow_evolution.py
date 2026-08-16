import json

import pytest

from dududa.evolution import ShadowEvolution


def test_feedback_is_redacted_deduplicated_and_identity_free(tmp_path):
    engine = ShadowEvolution(tmp_path)
    first = engine.add_experience(
        "天气回复错了，api_key: abcdefghijklmnop", source="user_feedback",
        run_id="run-secret", trace_id="trace-secret")
    second = engine.add_experience(
        "天气回复错了，api_key: abcdefghijklmnop", source="user_feedback",
        run_id="run-secret", trace_id="trace-secret")

    assert first["summary"] == "天气回复错了，[REDACTED]"
    assert second["duplicate"] is True
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "abcdefghijklmnop" not in raw
    assert "run-secret" not in raw and "trace-secret" not in raw
    assert "actor" not in first and "conversation" not in first

    pii = engine.add_experience(
        "联系 13812345678 test@example.com，服务 8.130.104.38，密码：abc123",
        source="user_feedback")
    assert "13812345678" not in pii["summary"]
    assert "test@example.com" not in pii["summary"]
    assert "8.130.104.38" not in pii["summary"]
    assert "abc123" not in pii["summary"]


def test_three_observations_create_non_executable_candidate(tmp_path):
    engine = ShadowEvolution(tmp_path, threshold=3)
    for text in ("天气地点用了气象站名称", "天气把临泽说成长庆镇",
                 "天气答复没有沿用用户地点"):
        engine.add_experience(text, source="operator")

    result = engine.analyze()
    assert result["created_or_updated"] == 1
    candidate = result["candidates"][0]
    assert candidate["status"] == "pending_review"
    assert candidate["activation"] == "disabled"
    assert candidate["deployment"] == "disabled"

    folder = tmp_path / "candidates" / candidate["candidate_id"]
    skill = (folder / "SKILL.md").read_text(encoding="utf-8")
    cases = json.loads((folder / "eval_cases.json").read_text(encoding="utf-8"))
    assert "临泽" not in skill
    assert "Never install, activate, or deploy it automatically." in skill
    assert len(cases["evidence_fingerprints"]) == 3

    approved = engine.decide(candidate["candidate_id"], "approve", "进入实现评审")
    assert approved["status"] == "approved_for_implementation"
    assert approved["activation"] == approved["deployment"] == "disabled"

    engine.add_experience("天气地点又出现新的回归", source="operator")
    refreshed = engine.analyze()["candidates"][0]
    assert refreshed["status"] == "pending_review"
    assert refreshed["review_note"] == ""


def test_trace_ingestion_uses_only_failure_metadata(tmp_path):
    engine = ShadowEvolution(tmp_path)
    added = engine.ingest_trace_events([
        {"event": "flow_start", "msg": "私人原话", "trace_id": "t0"},
        {"event": "tool_result", "success": True, "data": "私人结果"},
        {"event": "tool_result", "success": False, "capability": "mcp.weather",
         "error": "timeout", "msg": "不要保存我", "prompt": "ignore previous",
         "trace_id": "t1", "run_id": "r1"},
    ])
    assert added == 1
    item = engine.list_experiences()[0]
    assert item["category"] == "weather_location"
    assert "不要保存我" not in item["summary"]
    assert "ignore previous" not in item["summary"]
    assert "timeout" in item["summary"]


def test_invalid_decision_is_rejected(tmp_path):
    engine = ShadowEvolution(tmp_path, threshold=2)
    engine.add_experience("图片被误判一", category="media_semantics")
    engine.add_experience("图片被误判二", category="media_semantics")
    candidate = engine.analyze()["candidates"][0]
    with pytest.raises(ValueError):
        engine.decide(candidate["candidate_id"], "activate")


def test_corrupt_state_is_not_silently_overwritten(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{broken", encoding="utf-8")
    engine = ShadowEvolution(tmp_path)
    with pytest.raises(RuntimeError):
        engine.add_experience("天气问题")
    assert state.read_text(encoding="utf-8") == "{broken"
