# -*- coding: utf-8 -*-
"""Versioned feature experiments with a human promotion gate and kill switch.

The registry is deliberately deterministic and model-free.  It owns only
assignment and lifecycle state; callers still own the product decision.  A
feature can be observed in SHADOW without changing behaviour, while CANARY and
PROMOTED are the only stages that may grant live treatment.

Subject identifiers are never persisted.  Bucketing and per-subject kills use
an HMAC digest derived from ``DUDUDA_EXPERIMENT_BUCKET_SALT``.  Missing salt is
fail-closed: no subject is assigned and a live experiment cannot take effect.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional


AMBIENT_EXPERIMENT_ID = "ambient-participation-v1"
EXPERIMENT_SCHEMA_VERSION = 1


class ExperimentStage(str, Enum):
    DRAFT = "draft"
    SHADOW = "shadow"
    CANARY = "canary"
    PROMOTED = "promoted"
    PAUSED = "paused"


class ExperimentReason(str, Enum):
    UNREGISTERED = "unregistered"
    MISSING_BUCKET_SALT = "missing_bucket_salt"
    DRAFT = "draft"
    SHADOW_CONTROL = "shadow_control"
    SHADOW_TREATMENT = "shadow_treatment"
    LIVE_CONTROL = "live_control"
    LIVE_TREATMENT = "live_treatment"
    GLOBAL_KILL = "global_kill"
    SUBJECT_KILL = "subject_kill"
    PAUSED = "paused"


@dataclass(frozen=True)
class HumanApproval:
    """An explicit human decision used for every state-expanding mutation."""

    actor_id: str
    reason: str
    approved_at: float = 0.0

    def validated(self) -> "HumanApproval":
        actor = str(self.actor_id or "").strip()
        reason = " ".join(str(self.reason or "").split()).strip()
        if not actor:
            raise ValueError("human approval requires actor_id")
        if not reason:
            raise ValueError("human approval requires a reason")
        return replace(
            self, actor_id=actor, reason=reason[:500],
            approved_at=float(self.approved_at or time.time()),
        )


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    flag: str
    strategy_version: str
    rollout: int = 0
    stage: ExperimentStage = ExperimentStage.DRAFT
    owner: str = ""
    review_date: str = ""
    killed: bool = False
    previous_stage: str = ""
    updated_at: float = 0.0
    approved_by: str = ""
    approval_reason: str = ""

    def __post_init__(self):
        if not str(self.experiment_id or "").strip():
            raise ValueError("experiment_id is required")
        if not str(self.flag or "").strip():
            raise ValueError("flag is required")
        if not str(self.strategy_version or "").strip():
            raise ValueError("strategy_version is required")
        if not 0 <= int(self.rollout) <= 100:
            raise ValueError("rollout must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "flag": self.flag,
            "strategy_version": self.strategy_version,
            "rollout": int(self.rollout),
            "stage": self.stage.value,
            "owner": self.owner,
            "review_date": self.review_date,
            "killed": bool(self.killed),
            "previous_stage": self.previous_stage,
            "updated_at": float(self.updated_at),
            "approved_by": self.approved_by,
            "approval_reason": self.approval_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExperimentSpec":
        return cls(
            experiment_id=str(raw.get("experiment_id", "")),
            flag=str(raw.get("flag", "")),
            strategy_version=str(raw.get("strategy_version", "")),
            rollout=int(raw.get("rollout", 0)),
            stage=ExperimentStage(str(raw.get("stage", "draft"))),
            owner=str(raw.get("owner", "")),
            review_date=str(raw.get("review_date", "")),
            killed=bool(raw.get("killed", False)),
            previous_stage=str(raw.get("previous_stage", "")),
            updated_at=float(raw.get("updated_at", 0.0) or 0.0),
            approved_by=str(raw.get("approved_by", "")),
            approval_reason=str(raw.get("approval_reason", "")),
        )


@dataclass(frozen=True)
class ExperimentDecision:
    experiment_id: str
    flag: str = ""
    strategy_version: str = ""
    stage: ExperimentStage = ExperimentStage.DRAFT
    bucket: Optional[int] = None
    assigned: bool = False
    shadow_enabled: bool = False
    live_enabled: bool = False
    killed: bool = False
    reason: ExperimentReason = ExperimentReason.UNREGISTERED
    subject_hash: str = ""

    def to_trace(self) -> dict[str, Any]:
        """Return attribution metadata without the raw subject identifier."""
        return {
            "experiment_id": self.experiment_id,
            "flag": self.flag,
            "strategy_version": self.strategy_version,
            "stage": self.stage.value,
            "bucket": self.bucket,
            "assigned": self.assigned,
            "shadow_enabled": self.shadow_enabled,
            "live_enabled": self.live_enabled,
            "killed": self.killed,
            "experiment_reason": self.reason.value,
            "subject_hash": self.subject_hash,
        }


def default_experiment_specs() -> tuple[ExperimentSpec, ...]:
    """Compiled-safe defaults: observe assignment only, never change output."""
    return (
        ExperimentSpec(
            experiment_id=AMBIENT_EXPERIMENT_ID,
            flag="ambient_participation",
            strategy_version="ambient-policy/1.0",
            # The framework is present in SHADOW, but no cohort is selected
            # until a human explicitly sets rollout through the control plane.
            rollout=0,
            stage=ExperimentStage.SHADOW,
            owner="project-owner",
            review_date="",
        ),
    )


_FORWARD_TRANSITIONS = {
    ExperimentStage.DRAFT: {ExperimentStage.SHADOW},
    ExperimentStage.SHADOW: {ExperimentStage.CANARY},
    ExperimentStage.CANARY: {ExperimentStage.PROMOTED},
    ExperimentStage.PROMOTED: set(),
    ExperimentStage.PAUSED: {
        ExperimentStage.SHADOW, ExperimentStage.CANARY,
        ExperimentStage.PROMOTED,
    },
}


class ExperimentRegistry:
    """Atomic JSON experiment registry with synchronous hot-path refresh."""

    def __init__(
        self,
        path: str,
        *,
        bucket_salt: str = "",
        defaults: Iterable[ExperimentSpec] = (),
    ):
        self._path = Path(path)
        self._salt = str(bucket_salt or "")
        self._defaults = {spec.experiment_id: spec for spec in defaults}
        self._experiments = dict(self._defaults)
        self._subject_kills: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._load(force=True)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def has_bucket_salt(self) -> bool:
        return bool(self._salt)

    def _file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _load(self, *, force: bool = False) -> None:
        signature = self._file_signature()
        if not force and signature == self._signature:
            return
        experiments = dict(self._defaults)
        subject_kills: dict[str, dict[str, dict[str, Any]]] = {}
        if signature is not None:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                for experiment_id, item in dict(
                        raw.get("experiments", {})).items():
                    if not isinstance(item, dict):
                        continue
                    item = dict(item)
                    item.setdefault("experiment_id", str(experiment_id))
                    spec = ExperimentSpec.from_dict(item)
                    experiments[spec.experiment_id] = spec
                for experiment_id, items in dict(
                        raw.get("subject_kills", {})).items():
                    if isinstance(items, dict):
                        subject_kills[str(experiment_id)] = {
                            str(key): dict(value)
                            for key, value in items.items()
                            if isinstance(value, dict)
                        }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Corrupt control state may never grant a live experiment.
                experiments = {
                    key: replace(spec, stage=ExperimentStage.SHADOW,
                                 killed=True)
                    for key, spec in self._defaults.items()
                }
                subject_kills = {}
        self._experiments = experiments
        self._subject_kills = subject_kills
        self._signature = signature

    def _refresh(self) -> None:
        with self._lock:
            self._load()

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiments": {
                key: spec.to_dict()
                for key, spec in sorted(self._experiments.items())
            },
            # Keys are HMAC subject digests; raw group/user ids never persist.
            "subject_kills": self._subject_kills,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)
        self._signature = self._file_signature()

    def _subject_digest(self, experiment_id: str, subject_id: str) -> str:
        if not self._salt:
            return ""
        payload = f"{experiment_id}|{subject_id}".encode("utf-8", "replace")
        return hmac.new(
            self._salt.encode("utf-8", "replace"), payload,
            hashlib.sha256,
        ).hexdigest()[:24]

    def _bucket(self, experiment_id: str, subject_id: str) -> tuple[int, str]:
        digest = self._subject_digest(experiment_id, subject_id)
        if not digest:
            raise ValueError("missing bucket salt")
        # The strategy version is intentionally absent: changing code does not
        # silently move a group between control and treatment.
        return int(digest, 16) % 100, digest

    def list(self) -> tuple[ExperimentSpec, ...]:
        self._refresh()
        with self._lock:
            return tuple(self._experiments[key]
                         for key in sorted(self._experiments))

    def get(self, experiment_id: str) -> Optional[ExperimentSpec]:
        self._refresh()
        with self._lock:
            return self._experiments.get(str(experiment_id))

    def create(self, spec: ExperimentSpec, approval: HumanApproval) -> ExperimentSpec:
        approval = approval.validated()
        if spec.stage != ExperimentStage.DRAFT:
            raise ValueError("new experiments must start in DRAFT")
        with self._lock:
            self._load()
            if spec.experiment_id in self._experiments:
                raise ValueError("experiment already exists")
            stored = replace(
                spec, updated_at=time.time(), approved_by=approval.actor_id,
                approval_reason=approval.reason)
            self._experiments[stored.experiment_id] = stored
            self._save_locked()
            return stored

    def transition(
        self,
        experiment_id: str,
        target: ExperimentStage,
        approval: HumanApproval,
    ) -> ExperimentSpec:
        approval = approval.validated()
        target = ExperimentStage(target)
        with self._lock:
            self._load()
            current = self._required(experiment_id)
            if target == ExperimentStage.PAUSED:
                return self._pause_locked(current, approval, killed=False)
            if target not in _FORWARD_TRANSITIONS[current.stage]:
                raise ValueError(
                    f"invalid transition: {current.stage.value}->{target.value}")
            if current.stage == ExperimentStage.PAUSED:
                try:
                    previous = ExperimentStage(current.previous_stage)
                except ValueError as exc:
                    raise ValueError("paused experiment has no valid resume stage") from exc
                allowed_resume = {
                    ExperimentStage.SHADOW: {ExperimentStage.SHADOW},
                    ExperimentStage.CANARY: {
                        ExperimentStage.SHADOW, ExperimentStage.CANARY},
                    ExperimentStage.PROMOTED: {
                        ExperimentStage.SHADOW, ExperimentStage.CANARY,
                        ExperimentStage.PROMOTED},
                    ExperimentStage.DRAFT: {ExperimentStage.SHADOW},
                    ExperimentStage.PAUSED: set(),
                }[previous]
                if target not in allowed_resume:
                    raise ValueError(
                        f"resume cannot exceed previous stage {previous.value}")
            if target in (ExperimentStage.CANARY, ExperimentStage.PROMOTED):
                if current.rollout <= 0:
                    raise ValueError("live stages require rollout > 0")
                if not self._salt:
                    raise ValueError("live stages require bucket salt")
            updated = replace(
                current, stage=target, killed=False, previous_stage="",
                updated_at=time.time(), approved_by=approval.actor_id,
                approval_reason=approval.reason)
            self._experiments[experiment_id] = updated
            self._save_locked()
            return updated

    def set_rollout(
        self, experiment_id: str, rollout: int, approval: HumanApproval,
    ) -> ExperimentSpec:
        approval = approval.validated()
        rollout = int(rollout)
        if not 0 <= rollout <= 100:
            raise ValueError("rollout must be between 0 and 100")
        with self._lock:
            self._load()
            current = self._required(experiment_id)
            updated = replace(
                current, rollout=rollout, updated_at=time.time(),
                approved_by=approval.actor_id,
                approval_reason=approval.reason)
            self._experiments[experiment_id] = updated
            self._save_locked()
            return updated

    def kill(
        self,
        experiment_id: str,
        approval: HumanApproval,
        *,
        subject_id: str = "",
        ttl_seconds: float | None = None,
    ) -> ExperimentSpec:
        """Synchronously lower authority globally or for one opaque subject."""
        approval = approval.validated()
        with self._lock:
            self._load()
            current = self._required(experiment_id)
            if subject_id:
                digest = self._subject_digest(experiment_id, str(subject_id))
                if not digest:
                    raise ValueError("subject kill requires bucket salt")
                expires_at = 0.0
                if ttl_seconds is not None:
                    ttl = max(1.0, float(ttl_seconds))
                    expires_at = time.time() + ttl
                self._subject_kills.setdefault(experiment_id, {})[digest] = {
                    "killed_at": time.time(),
                    "expires_at": expires_at,
                    "approved_by": approval.actor_id,
                    "reason": approval.reason,
                }
                self._save_locked()
                return current
            return self._pause_locked(current, approval, killed=True)

    def release_subject_kill(
        self, experiment_id: str, subject_id: str,
        approval: HumanApproval,
    ) -> bool:
        """Human-reviewed release; it never advances the experiment stage."""
        approval.validated()
        digest = self._subject_digest(experiment_id, str(subject_id))
        if not digest:
            raise ValueError("subject kill release requires bucket salt")
        with self._lock:
            self._load()
            self._required(experiment_id)
            removed = self._subject_kills.get(experiment_id, {}).pop(
                digest, None) is not None
            if removed:
                self._save_locked()
            return removed

    def _pause_locked(
        self, current: ExperimentSpec, approval: HumanApproval, *, killed: bool,
    ) -> ExperimentSpec:
        previous = (current.previous_stage if current.stage == ExperimentStage.PAUSED
                    else current.stage.value)
        updated = replace(
            current, stage=ExperimentStage.PAUSED,
            killed=bool(killed or current.killed), previous_stage=previous,
            updated_at=time.time(), approved_by=approval.actor_id,
            approval_reason=approval.reason)
        self._experiments[current.experiment_id] = updated
        self._save_locked()
        return updated

    def _required(self, experiment_id: str) -> ExperimentSpec:
        try:
            return self._experiments[str(experiment_id)]
        except KeyError as exc:
            raise KeyError(f"unknown experiment: {experiment_id}") from exc

    def decide(self, experiment_id: str, subject_id: str) -> ExperimentDecision:
        """Resolve one assignment after refreshing kill state from disk."""
        self._refresh()
        with self._lock:
            spec = self._experiments.get(str(experiment_id))
            if spec is None:
                return ExperimentDecision(experiment_id=str(experiment_id))
            base = dict(
                experiment_id=spec.experiment_id,
                flag=spec.flag,
                strategy_version=spec.strategy_version,
                stage=spec.stage,
            )
            if spec.killed:
                return ExperimentDecision(
                    **base, killed=True, reason=ExperimentReason.GLOBAL_KILL)
            if spec.stage == ExperimentStage.PAUSED:
                return ExperimentDecision(
                    **base, reason=ExperimentReason.PAUSED)
            if not self._salt:
                return ExperimentDecision(
                    **base, reason=ExperimentReason.MISSING_BUCKET_SALT)
            bucket, subject_hash = self._bucket(
                spec.experiment_id, str(subject_id or ""))
            kill = self._subject_kills.get(spec.experiment_id, {}).get(
                subject_hash)
            if kill:
                expires_at = float(kill.get("expires_at", 0.0) or 0.0)
                if not expires_at or time.time() < expires_at:
                    return ExperimentDecision(
                        **base, bucket=bucket, subject_hash=subject_hash,
                        killed=True, reason=ExperimentReason.SUBJECT_KILL)
            assigned = bucket < int(spec.rollout)
            if spec.stage == ExperimentStage.DRAFT:
                reason = ExperimentReason.DRAFT
            elif spec.stage == ExperimentStage.SHADOW:
                reason = (ExperimentReason.SHADOW_TREATMENT if assigned
                          else ExperimentReason.SHADOW_CONTROL)
            else:
                reason = (ExperimentReason.LIVE_TREATMENT if assigned
                          else ExperimentReason.LIVE_CONTROL)
            return ExperimentDecision(
                **base, bucket=bucket, subject_hash=subject_hash,
                assigned=assigned,
                shadow_enabled=(spec.stage == ExperimentStage.SHADOW and assigned),
                live_enabled=(spec.stage in (
                    ExperimentStage.CANARY, ExperimentStage.PROMOTED) and assigned),
                reason=reason,
            )
