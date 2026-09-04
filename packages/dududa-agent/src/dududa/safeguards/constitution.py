# -*- coding: utf-8 -*-
"""Deterministic, versioned constitutional policy above role permissions.

The constitution is code, not a prompt.  HARD rules cannot be bypassed by an
owner or a runtime maintenance override.  OPERATIONAL rules may request an
explicit confirmation; an override is accepted only when an external verifier
validates it.  The default engine intentionally ships without such a verifier.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


CONSTITUTION_VERSION = "ymakmern-constitution/1.0"


class ConstitutionTier(str, Enum):
    HARD = "hard"
    OPERATIONAL = "operational"


class ConstitutionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class ConstitutionReason(str, Enum):
    NO_RULE_MATCHED = "no_rule_matched"
    CROSS_ACTOR_PRIVATE_MEMORY = "cross_actor_private_memory"
    CROSS_SCOPE_SENSITIVE_MEMORY = "cross_scope_sensitive_memory"
    SECRET_EXPORT = "secret_export"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"
    VERIFIED_MAINTENANCE_OVERRIDE = "verified_maintenance_override"


@dataclass(frozen=True)
class ConstitutionRequest:
    actor_id: str
    actor_role: str
    action: str
    resource: str = ""
    resource_owner_id: str = ""
    source_scope: str = ""
    target_scope: str = ""
    sensitivity: str = "internal"
    data_class: str = "public"
    confirmed: bool = False


@dataclass(frozen=True)
class MaintenanceOverride:
    """Opaque, externally signed authorization for operational rules only."""

    override_id: str
    rule_ids: tuple[str, ...]
    approved_by: str
    reason: str
    expires_at: float
    signature: str

    @property
    def active(self) -> bool:
        return bool(
            self.override_id and self.approved_by and self.reason
            and self.signature and time.time() < float(self.expires_at)
        )


Predicate = Callable[[ConstitutionRequest], bool]


@dataclass(frozen=True)
class ConstitutionRule:
    rule_id: str
    tier: ConstitutionTier
    decision: ConstitutionDecision
    reason: ConstitutionReason
    predicate: Predicate
    description: str

    def public_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "tier": self.tier.value,
            "decision": self.decision.value,
            "reason": self.reason.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class ConstitutionResult:
    decision: ConstitutionDecision
    reason: ConstitutionReason
    rule_id: str = ""
    tier: ConstitutionTier | None = None
    constitution_version: str = CONSTITUTION_VERSION
    constitution_digest: str = ""
    override_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == ConstitutionDecision.ALLOW


def _private_cross_actor(request: ConstitutionRequest) -> bool:
    return bool(
        request.action in {"read_memory", "export_memory"}
        and request.sensitivity in {"private", "restricted"}
        and request.resource_owner_id
        and request.resource_owner_id != request.actor_id
    )


def _sensitive_cross_scope(request: ConstitutionRequest) -> bool:
    return bool(
        request.action in {"read_memory", "export_memory", "copy_memory"}
        and request.sensitivity in {"private", "restricted"}
        and request.source_scope and request.target_scope
        and request.source_scope != request.target_scope
    )


def _secret_export(request: ConstitutionRequest) -> bool:
    return bool(
        request.action in {"export_memory", "export_data", "send_data"}
        and request.data_class in {"credential", "secret", "restricted"}
    )


def _human_gate_action(request: ConstitutionRequest) -> bool:
    return bool(
        request.action in {
            "modify_constitution", "expand_experiment_rollout",
            "resume_experiment", "promote_experiment",
        }
        and not request.confirmed
    )


def default_rules() -> tuple[ConstitutionRule, ...]:
    return (
        ConstitutionRule(
            rule_id="memory.cross_actor_private.deny",
            tier=ConstitutionTier.HARD,
            decision=ConstitutionDecision.DENY,
            reason=ConstitutionReason.CROSS_ACTOR_PRIVATE_MEMORY,
            predicate=_private_cross_actor,
            description="Private or restricted memory belongs to its actor; owner is not exempt.",
        ),
        ConstitutionRule(
            rule_id="memory.cross_scope_sensitive.deny",
            tier=ConstitutionTier.HARD,
            decision=ConstitutionDecision.DENY,
            reason=ConstitutionReason.CROSS_SCOPE_SENSITIVE_MEMORY,
            predicate=_sensitive_cross_scope,
            description="Sensitive memory cannot cross its source scope.",
        ),
        ConstitutionRule(
            rule_id="data.secret_export.deny",
            tier=ConstitutionTier.HARD,
            decision=ConstitutionDecision.DENY,
            reason=ConstitutionReason.SECRET_EXPORT,
            predicate=_secret_export,
            description="Credentials and secret-class data cannot be exported.",
        ),
        ConstitutionRule(
            rule_id="governance.human_gate.require",
            tier=ConstitutionTier.OPERATIONAL,
            decision=ConstitutionDecision.REQUIRE_CONFIRMATION,
            reason=ConstitutionReason.HUMAN_CONFIRMATION_REQUIRED,
            predicate=_human_gate_action,
            description="Authority expansion requires an explicit human confirmation.",
        ),
    )


OverrideVerifier = Callable[[MaintenanceOverride, str, str], bool]


class ConstitutionEngine:
    """Evaluate hard rules before operational policy and ordinary RBAC."""

    def __init__(
        self,
        rules: tuple[ConstitutionRule, ...] | None = None,
        *,
        version: str = CONSTITUTION_VERSION,
        override_verifier: Optional[OverrideVerifier] = None,
    ):
        self.rules = tuple(rules or default_rules())
        self.version = str(version)
        self._override_verifier = override_verifier
        self.digest = self._manifest_digest()

    def _manifest_digest(self) -> str:
        manifest = []
        for rule in self.rules:
            try:
                predicate_source = inspect.getsource(rule.predicate)
            except (OSError, TypeError):
                predicate_source = rule.predicate.__qualname__
            manifest.append({
                **rule.public_dict(),
                "predicate_sha256": hashlib.sha256(
                    predicate_source.encode("utf-8", "replace")
                ).hexdigest(),
            })
        payload = json.dumps({
            "version": self.version, "rules": manifest,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def manifest(self) -> dict:
        return {
            "version": self.version,
            "digest": self.digest,
            "rules": [rule.public_dict() for rule in self.rules],
            "hard_rule_count": sum(
                rule.tier == ConstitutionTier.HARD for rule in self.rules),
            "runtime_hard_override": False,
        }

    def evaluate(
        self,
        request: ConstitutionRequest,
        *,
        override: MaintenanceOverride | None = None,
    ) -> ConstitutionResult:
        matched = [rule for rule in self.rules if rule.predicate(request)]
        matched.sort(key=lambda rule: (
            0 if rule.tier == ConstitutionTier.HARD else 1,
            rule.rule_id,
        ))
        for rule in matched:
            # HARD means exactly that: no owner role and no runtime override.
            if rule.tier == ConstitutionTier.HARD:
                return self._result(rule)
            if self._verified_override(override, rule):
                return ConstitutionResult(
                    decision=ConstitutionDecision.ALLOW,
                    reason=ConstitutionReason.VERIFIED_MAINTENANCE_OVERRIDE,
                    rule_id=rule.rule_id,
                    tier=rule.tier,
                    constitution_version=self.version,
                    constitution_digest=self.digest,
                    override_id=str(override.override_id),
                )
            return self._result(rule)
        return ConstitutionResult(
            decision=ConstitutionDecision.ALLOW,
            reason=ConstitutionReason.NO_RULE_MATCHED,
            constitution_version=self.version,
            constitution_digest=self.digest,
        )

    def _verified_override(
        self, override: MaintenanceOverride | None,
        rule: ConstitutionRule,
    ) -> bool:
        if rule.tier == ConstitutionTier.HARD:
            return False
        if override is None or not override.active:
            return False
        if rule.rule_id not in override.rule_ids:
            return False
        if self._override_verifier is None:
            return False
        try:
            return bool(self._override_verifier(
                override, self.version, self.digest))
        except Exception:
            return False

    def _result(self, rule: ConstitutionRule) -> ConstitutionResult:
        return ConstitutionResult(
            decision=rule.decision,
            reason=rule.reason,
            rule_id=rule.rule_id,
            tier=rule.tier,
            constitution_version=self.version,
            constitution_digest=self.digest,
        )


def default_constitution() -> ConstitutionEngine:
    return ConstitutionEngine()
