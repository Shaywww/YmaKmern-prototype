# -*- coding: utf-8 -*-
from __future__ import annotations

import time

from dududa.safeguards.constitution import (
    ConstitutionDecision,
    ConstitutionEngine,
    ConstitutionReason,
    ConstitutionRequest,
    ConstitutionTier,
    MaintenanceOverride,
)


def request(**overrides):
    values = {
        "actor_id": "owner-account",
        "actor_role": "owner",
        "action": "read",
    }
    values.update(overrides)
    return ConstitutionRequest(**values)


def override(rule_id):
    return MaintenanceOverride(
        override_id="offline-signed-1",
        rule_ids=(rule_id,),
        approved_by="human-owner",
        reason="maintenance window",
        expires_at=time.time() + 60,
        signature="opaque-signature",
    )


def test_owner_cannot_export_another_users_private_memory():
    engine = ConstitutionEngine()
    result = engine.evaluate(request(
        action="export_memory",
        resource_owner_id="another-user",
        sensitivity="private",
    ))
    assert result.decision == ConstitutionDecision.DENY
    assert result.reason == ConstitutionReason.CROSS_ACTOR_PRIVATE_MEMORY
    assert result.tier == ConstitutionTier.HARD


def test_hard_deny_cannot_be_bypassed_even_by_verified_override():
    engine = ConstitutionEngine(override_verifier=lambda *_: True)
    rule_id = "memory.cross_actor_private.deny"
    result = engine.evaluate(
        request(
            action="export_memory",
            resource_owner_id="another-user",
            sensitivity="restricted",
        ),
        override=override(rule_id),
    )
    assert result.decision == ConstitutionDecision.DENY
    assert result.rule_id == rule_id
    assert result.override_id == ""


def test_authority_expansion_requires_explicit_human_confirmation():
    engine = ConstitutionEngine()
    blocked = engine.evaluate(request(action="promote_experiment"))
    allowed = engine.evaluate(request(
        action="promote_experiment", confirmed=True))
    assert blocked.decision == ConstitutionDecision.REQUIRE_CONFIRMATION
    assert allowed.decision == ConstitutionDecision.ALLOW


def test_operational_override_requires_external_verifier():
    rule_id = "governance.human_gate.require"
    unsigned_engine = ConstitutionEngine()
    assert unsigned_engine.evaluate(
        request(action="resume_experiment"),
        override=override(rule_id),
    ).decision == ConstitutionDecision.REQUIRE_CONFIRMATION

    verified_engine = ConstitutionEngine(override_verifier=lambda *_: True)
    result = verified_engine.evaluate(
        request(action="resume_experiment"),
        override=override(rule_id),
    )
    assert result.decision == ConstitutionDecision.ALLOW
    assert result.reason == ConstitutionReason.VERIFIED_MAINTENANCE_OVERRIDE


def test_manifest_is_versioned_digested_and_declares_no_hard_override():
    manifest = ConstitutionEngine().manifest()
    assert manifest["version"]
    assert len(manifest["digest"]) == 64
    assert manifest["hard_rule_count"] == 3
    assert manifest["runtime_hard_override"] is False
