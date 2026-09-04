# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from dududa.core.experiments import (
    AMBIENT_EXPERIMENT_ID,
    ExperimentReason,
    ExperimentRegistry,
    ExperimentSpec,
    ExperimentStage,
    HumanApproval,
    default_experiment_specs,
)


def approval(reason="reviewed"):
    return HumanApproval("human-owner", reason, approved_at=100.0)


def registry(tmp_path, *, salt="test-stable-salt", defaults=()):
    return ExperimentRegistry(
        str(tmp_path / "experiments.json"),
        bucket_salt=salt,
        defaults=defaults,
    )


def create_live_candidate(reg, experiment_id="exp-1", rollout=100):
    reg.create(ExperimentSpec(
        experiment_id=experiment_id,
        flag="feature_x",
        strategy_version="strategy/1",
        rollout=rollout,
    ), approval("create draft"))
    reg.transition(
        experiment_id, ExperimentStage.SHADOW,
        approval("start shadow"))
    return experiment_id


def test_compiled_ambient_experiment_is_shadow_with_zero_rollout(tmp_path):
    reg = registry(tmp_path, defaults=default_experiment_specs())
    spec = reg.get(AMBIENT_EXPERIMENT_ID)
    assert spec.stage == ExperimentStage.SHADOW
    assert spec.rollout == 0
    decision = reg.decide(AMBIENT_EXPERIMENT_ID, "group-1")
    assert decision.live_enabled is False
    assert decision.shadow_enabled is False
    assert decision.reason == ExperimentReason.SHADOW_CONTROL


def test_bucket_is_stable_across_instances_and_strategy_version_changes(tmp_path):
    path = tmp_path / "experiments.json"
    reg = ExperimentRegistry(str(path), bucket_salt="stable")
    create_live_candidate(reg, rollout=53)
    first = reg.decide("exp-1", "group-123")

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["experiments"]["exp-1"]["strategy_version"] = "strategy/2"
    path.write_text(json.dumps(payload), encoding="utf-8")
    second = ExperimentRegistry(
        str(path), bucket_salt="stable").decide("exp-1", "group-123")

    assert first.bucket == second.bucket
    assert first.subject_hash == second.subject_hash
    assert second.strategy_version == "strategy/2"


def test_missing_salt_fails_closed_before_live_stage(tmp_path):
    reg = registry(tmp_path, salt="")
    create_live_candidate(reg)
    decision = reg.decide("exp-1", "group-1")
    assert decision.reason == ExperimentReason.MISSING_BUCKET_SALT
    assert decision.live_enabled is False
    with pytest.raises(ValueError, match="bucket salt"):
        reg.transition(
            "exp-1", ExperimentStage.CANARY,
            approval("start canary"))


def test_human_gate_requires_identity_reason_and_valid_progression(tmp_path):
    reg = registry(tmp_path)
    reg.create(ExperimentSpec(
        experiment_id="exp-1", flag="f", strategy_version="v", rollout=10,
    ), approval("create"))
    with pytest.raises(ValueError, match="reason"):
        reg.transition(
            "exp-1", ExperimentStage.SHADOW,
            HumanApproval("owner", ""))
    with pytest.raises(ValueError, match="invalid transition"):
        reg.transition(
            "exp-1", ExperimentStage.CANARY,
            approval("cannot skip shadow"))


def test_canary_is_the_first_live_stage(tmp_path):
    reg = registry(tmp_path)
    create_live_candidate(reg)
    shadow = reg.decide("exp-1", "group-1")
    assert shadow.assigned is True
    assert shadow.shadow_enabled is True
    assert shadow.live_enabled is False

    reg.transition(
        "exp-1", ExperimentStage.CANARY,
        approval("human go decision"))
    canary = reg.decide("exp-1", "group-1")
    assert canary.live_enabled is True
    assert canary.reason == ExperimentReason.LIVE_TREATMENT


def test_subject_kill_refreshes_in_an_existing_process(tmp_path):
    first = registry(tmp_path)
    create_live_candidate(first)
    first.transition(
        "exp-1", ExperimentStage.CANARY, approval("go"))
    assert first.decide("exp-1", "group-1").live_enabled is True

    second = registry(tmp_path)
    second.kill(
        "exp-1", approval("member asked to stop"),
        subject_id="group-1")
    stopped = first.decide("exp-1", "group-1")
    assert stopped.killed is True
    assert stopped.live_enabled is False
    assert stopped.reason == ExperimentReason.SUBJECT_KILL


def test_global_kill_only_lowers_authority_and_resume_cannot_jump(tmp_path):
    reg = registry(tmp_path)
    create_live_candidate(reg)
    reg.kill("exp-1", approval("kill now"))
    stopped = reg.decide("exp-1", "group-1")
    assert stopped.killed is True
    assert stopped.stage == ExperimentStage.PAUSED

    with pytest.raises(ValueError, match="cannot exceed"):
        reg.transition(
            "exp-1", ExperimentStage.PROMOTED,
            approval("invalid jump"))
    resumed = reg.transition(
        "exp-1", ExperimentStage.SHADOW,
        approval("reviewed recovery"))
    assert resumed.stage == ExperimentStage.SHADOW
    assert resumed.killed is False


def test_persisted_subject_state_contains_only_hmac_identifier(tmp_path):
    reg = registry(tmp_path)
    create_live_candidate(reg)
    reg.kill(
        "exp-1", approval("stop this group"),
        subject_id="481757927")
    raw = (tmp_path / "experiments.json").read_text(encoding="utf-8")
    assert "481757927" not in raw
    assert "subject_kills" in raw
