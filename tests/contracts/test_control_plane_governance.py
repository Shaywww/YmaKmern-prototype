# -*- coding: utf-8 -*-
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from dududa.control_plane.app import create_app


TOKEN = "cp-governance-test"


@pytest.fixture()
def cp(tmp_path):
    values = {
        "DUDUDA_CP_TOKEN": TOKEN,
        "DUDUDA_CP_AUDIT": str(tmp_path / "audit.jsonl"),
        "DUDUDA_EXPERIMENT_FILE": str(tmp_path / "experiments.json"),
        "DUDUDA_EXPERIMENT_BUCKET_SALT": "stable-test-salt",
        "DUDUDA_MCP_ACCESS": str(tmp_path / "missing-access.json"),
        "DUDUDA_MEMORY_FILE": str(tmp_path / "memory.json"),
    }
    os.environ.update(values)
    app = create_app()
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    yield app, client
    for key in values:
        os.environ.pop(key, None)


def test_default_ambient_experiment_is_shadow_and_cannot_emit(cp):
    app, client = cp
    response = client.get("/experiments")
    assert response.status_code == 200
    body = response.json()
    assert body["bucket_salt_configured"] is True
    ambient = next(item for item in body["experiments"]
                   if item["experiment_id"] == "ambient-participation-v1")
    assert ambient["stage"] == "shadow"
    assert ambient["rollout"] == 0


def test_human_can_create_and_progress_without_skipping_stages(cp):
    app, client = cp
    created = client.post("/experiments", json={
        "experiment_id": "feature-test",
        "flag": "feature_test",
        "strategy_version": "sha256:test-v1",
        "rollout": 10,
        "owner": "cp_owner",
        "review_date": "2026-09-10",
        "reason": "create a reviewable draft",
    })
    assert created.status_code == 200
    assert created.json()["stage"] == "draft"

    skipped = client.post(
        "/experiments/feature-test/transition",
        json={"target_stage": "canary", "reason": "skip"})
    assert skipped.status_code == 400

    shadow = client.post(
        "/experiments/feature-test/transition",
        json={"target_stage": "shadow", "reason": "shadow reviewed"})
    assert shadow.status_code == 200
    assert shadow.json()["stage"] == "shadow"


def test_expansion_without_reason_is_blocked_by_constitution(cp):
    app, client = cp
    response = client.post(
        "/experiments/ambient-participation-v1/rollout",
        json={"rollout": 10, "reason": ""})
    assert response.status_code == 409
    assert "human_confirmation_required" in response.text
    assert any(
        line.get("event") == "constitution_block"
        and line.get("rule_id") == "governance.human_gate.require"
        for line in app.state.audit_logger.lines()
    )


def test_non_owner_cannot_mutate_experiment(cp):
    app, client = cp
    response = client.post(
        "/experiments/ambient-participation-v1/rollout",
        json={"rollout": 10, "reason": "not authorized"},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-CP-Operator": "member-1",
            "X-CP-Role": "normal",
        },
    )
    assert response.status_code == 403


def test_subject_kill_is_immediate_audited_and_never_echoes_raw_id(cp):
    app, client = cp
    response = client.post(
        "/experiments/ambient-participation-v1/kill",
        json={
            "reason": "member asked the bot to stop",
            "subject_id": "481757927",
            "ttl_seconds": 3600,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kill_scope"] == "subject"
    assert body["subject_hash"]
    assert "481757927" not in response.text
    decision = app.state.experiment_registry.decide(
        "ambient-participation-v1", "481757927")
    assert decision.killed is True
    assert any(
        line.get("event") == "experiment_killed"
        and line.get("kill_scope") == "subject"
        for line in app.state.audit_logger.lines()
    )


def test_constitution_manifest_is_read_only_and_versioned(cp):
    app, client = cp
    response = client.get("/governance/constitution")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "ymakmern-constitution/1.0"
    assert len(body["digest"]) == 64
    assert body["runtime_hard_override"] is False
