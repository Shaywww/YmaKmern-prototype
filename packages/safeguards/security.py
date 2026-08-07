"""嘟嘟哒 2.0 Security —— 授权、持久确认与共享脱敏（Phase 3 契约）。

对应文档 2.4.23 / 2.4.24：
- 授权返回 ALLOW / DENY / REQUIRE_CONFIRMATION + 稳定 reason code；
- 角色顺序 owner > admin > trusted > normal，muted 是 deny overlay；
- 缺身份、Scope、资源或策略时默认拒绝（default deny）；
- 确认绑定 confirmation_id + Actor + Scope + payload digest + 有效期，
  单次使用，执行时重新授权；
- Redaction 幂等，覆盖 credential value pattern、URL user-info/query、
  嵌套 mapping/sequence。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class AuthorizationDecision(str, Enum):
    """授权结果。"""
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class AuthReason(str, Enum):
    """稳定 reason code —— 可审计、可追踪。"""
    UNKNOWN_ACTOR = "unknown_actor"
    UNKNOWN_SCOPE = "unknown_scope"
    UNKNOWN_RESOURCE = "unknown_resource"
    ROLE_TOO_LOW = "role_too_low"
    MUTED = "muted"
    CAPABILITY_RISK = "capability_risk"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_REPLAYED = "confirmation_replayed"
    CONFIRMATION_DIGEST_MISMATCH = "confirmation_digest_mismatch"
    CONFIRMATION_ACTOR_MISMATCH = "confirmation_actor_mismatch"
    CONFIRMATION_SCOPE_MISMATCH = "confirmation_scope_mismatch"
    CONFIRMATION_ACTION_MISMATCH = "confirmation_action_mismatch"
    ROLE_ALLOWED = "role_allowed"
    OWNER_ALLOWED = "owner_allowed"
    CONFIRMATION_OK = "confirmation_ok"


@dataclass(frozen=True)
class AuthorizationResult:
    """一次授权的结果。"""
    decision: AuthorizationDecision
    reason_codes: tuple[AuthReason, ...] = ()
    required_confirmation_id: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.decision == AuthorizationDecision.ALLOW


ROLE_RANK = {"owner": 4, "admin": 3, "trusted": 2, "normal": 1}
ACTION_RANK = {
    "read": 1,
    "send": 1,
    "remember": 1,
    "use_tool": 2,
    "manage_config": 4,
    "admin": 4,
}
RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


class PermissionEngine:
    """权限引擎：Actor + Scope + action/resource -> 授权结果。

    检查顺序：身份 -> muted overlay -> Scope -> 角色等级 ->
    能力风险 -> 确认要求。任一步失败即 DENY（default deny）。
    """

    def authorize(
        self,
        actor: Any,
        action: str,
        scope_key: str = "",
        resource: str = "",
        capability_risk: Optional[str] = None,
        requires_confirmation: bool = False,
    ) -> AuthorizationResult:
        if actor is None or not getattr(actor, "actor_id", "") or actor.actor_id == "unknown":
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.UNKNOWN_ACTOR,)
            )
        if getattr(actor, "is_muted", lambda: False)():
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.MUTED,)
            )
        if not scope_key:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.UNKNOWN_SCOPE,)
            )
        rank = ROLE_RANK.get(getattr(actor, "role", "normal"), 1)
        needed = ACTION_RANK.get(action, 1)
        if rank < needed:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.ROLE_TOO_LOW,)
            )
        if capability_risk and rank < RISK_RANK.get(capability_risk, 2):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CAPABILITY_RISK,)
            )
        if requires_confirmation:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                (AuthReason.CONFIRMATION_REQUIRED,),
            )
        if getattr(actor, "role", "") == "owner":
            return AuthorizationResult(
                AuthorizationDecision.ALLOW, (AuthReason.OWNER_ALLOWED,)
            )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, (AuthReason.ROLE_ALLOWED,)
        )


@dataclass(frozen=True)
class Confirmation:
    """持久确认：绑定 Actor / Scope / action / payload digest。

    单次使用；换会话、换参数、角色变化、过期或重复使用都会使确认无效。
    """
    confirmation_id: str
    actor_id: str
    scope_key: str
    action: str
    payload_digest: str
    required_permission: str = "manage_config"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: Optional[datetime] = None
    consumed_at: Optional[datetime] = None
    approved: bool = False          # 管理员已批准（高风险操作）

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None


class ConfirmationStore:
    """确认仓库：创建、批准与单次消费，执行时重新授权（文档 2.5.9）。

    - 确认绑定 requester(actor_id) + Scope + action + payload digest；
    - 高风险管理操作由管理员 approve 后，发起者重试时消费（单次使用）；
    - 消费时重新授权：绑定一致、未过期、未重放、发起者未被 mute；
    - dump/restore 支持进程重启后恢复（持久确认，不随重启丢失）。
    """

    def __init__(self, ttl_seconds: int = 600):
        self._confirmations: dict[str, Confirmation] = {}
        self._ttl = ttl_seconds

    @staticmethod
    def digest(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def create(
        self,
        actor: Any,
        scope_key: str,
        action: str,
        payload: dict[str, Any],
        required_permission: str = "manage_config",
    ) -> Confirmation:
        conf = Confirmation(
            confirmation_id=uuid4().hex,
            actor_id=actor.actor_id,
            scope_key=scope_key,
            action=action,
            payload_digest=self.digest(payload),
            required_permission=required_permission,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self._ttl),
        )
        self._confirmations[conf.confirmation_id] = conf
        return conf

    def get(self, confirmation_id: str) -> Optional[Confirmation]:
        return self._confirmations.get(confirmation_id)

    def approve(self, confirmation_id: str) -> bool:
        """管理员批准（幂等）。过期或已消费的确认不可批准。"""
        conf = self._confirmations.get(confirmation_id)
        if conf is None or conf.is_expired or conf.is_consumed:
            return False
        self._confirmations[confirmation_id] = Confirmation(
            **{**conf.__dict__, "approved": True}
        )
        return True

    def consume(
        self,
        confirmation_id: str,
        actor: Any,
        scope_key: str,
        payload: dict[str, Any],
    ) -> AuthorizationResult:
        """发起者重试时消费已批准确认（管理员批准 + 单次使用路径）。

        执行时重新授权 = 绑定校验 + mute 覆盖 + 有效期 + 单次使用。
        """
        conf = self._confirmations.get(confirmation_id)
        if conf is None:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.UNKNOWN_RESOURCE,)
            )
        if conf.is_consumed:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_REPLAYED,)
            )
        if conf.is_expired:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_EXPIRED,)
            )
        if not conf.approved:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_REQUIRED,)
            )
        if conf.actor_id != getattr(actor, "actor_id", ""):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_ACTOR_MISMATCH,)
            )
        if conf.scope_key != scope_key:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_SCOPE_MISMATCH,)
            )
        if conf.payload_digest != self.digest(payload):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_DIGEST_MISMATCH,)
            )
        if getattr(actor, "is_muted", lambda: False)():
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.MUTED,)
            )
        self._confirmations[confirmation_id] = Confirmation(
            **{**conf.__dict__, "consumed_at": datetime.now(timezone.utc)}
        )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, (AuthReason.CONFIRMATION_OK,)
        )

    def confirm(
        self,
        confirmation_id: str,
        actor: Any,
        scope_key: str,
        payload: dict[str, Any],
    ) -> AuthorizationResult:
        """角色已达标路径的消费确认：绑定校验 + 执行时重新授权 + 单次使用。"""
        conf = self._confirmations.get(confirmation_id)
        if conf is None:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.UNKNOWN_RESOURCE,)
            )
        if conf.is_consumed:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_REPLAYED,)
            )
        if conf.is_expired:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_EXPIRED,)
            )
        if conf.actor_id != getattr(actor, "actor_id", ""):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_ACTOR_MISMATCH,)
            )
        if conf.scope_key != scope_key:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_SCOPE_MISMATCH,)
            )
        if conf.payload_digest != self.digest(payload):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_DIGEST_MISMATCH,)
            )
        auth = PermissionEngine().authorize(
            actor, conf.action, scope_key=scope_key,
            requires_confirmation=False,
        )
        if not auth.allowed:
            return auth
        self._confirmations[confirmation_id] = Confirmation(
            **{**conf.__dict__, "consumed_at": datetime.now(timezone.utc)}
        )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, (AuthReason.CONFIRMATION_OK,)
        )

    def create_for_actor(
        self,
        actor_id: str,
        scope_key: str,
        action: str,
        payload: dict[str, Any],
        required_permission: str = "use_tool",
    ) -> Confirmation:
        """为工具路径创建持久确认（executor 无 Actor 对象，只传 actor_id）。"""
        conf = Confirmation(
            confirmation_id=uuid4().hex,
            actor_id=actor_id,
            scope_key=scope_key,
            action=action,
            payload_digest=self.digest(payload),
            required_permission=required_permission,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self._ttl),
        )
        self._confirmations[conf.confirmation_id] = conf
        return conf

    def find_pending(
        self,
        actor_id: str,
        scope_key: str,
        action: str,
        payload: dict[str, Any],
    ) -> Optional[Confirmation]:
        """按绑定找未消费未过期的确认（重试同参数自动命中，同命令路径）。"""
        digest = self.digest(payload)
        for c in self._confirmations.values():
            if (c.actor_id == actor_id
                    and c.scope_key == scope_key
                    and c.action == action
                    and c.payload_digest == digest
                    and not c.is_consumed
                    and not c.is_expired):
                return c
        return None

    def authorize_tool(
        self,
        confirmation_id: str,
        actor_id: str,
        scope_key: str,
        action: str,
        payload: dict[str, Any],
        permissions: tuple[str, ...] = (),
    ) -> AuthorizationResult:
        """工具执行时的确认校验 + 单次消费（文档 2.4.12/2.4.23）。

        执行时重新授权：管理员批准、Actor/Scope/action/payload digest 绑定、
        过期与重放拒绝、所需权限复核（角色变化即失效）。消费后 single-use。
        """
        conf = self._confirmations.get(confirmation_id)
        if conf is None:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.UNKNOWN_RESOURCE,))
        if conf.is_consumed:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_REPLAYED,))
        if conf.is_expired:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_EXPIRED,))
        if not conf.approved:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_REQUIRED,))
        if conf.actor_id != actor_id:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_ACTOR_MISMATCH,))
        if conf.scope_key != scope_key:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_SCOPE_MISMATCH,))
        if conf.action != action:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_ACTION_MISMATCH,))
        if conf.payload_digest != self.digest(payload):
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.CONFIRMATION_DIGEST_MISMATCH,))
        if conf.required_permission and conf.required_permission not in permissions:
            return AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.ROLE_TOO_LOW,))
        self._confirmations[confirmation_id] = Confirmation(
            **{**conf.__dict__, "consumed_at": datetime.now(timezone.utc)})
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, (AuthReason.CONFIRMATION_OK,))

    def prune(self) -> int:
        """清理已消费或过期的确认，返回清理数量。"""
        dead = [
            cid for cid, c in self._confirmations.items()
            if c.is_consumed or c.is_expired
        ]
        for cid in dead:
            del self._confirmations[cid]
        return len(dead)

    def dump(self) -> list[dict[str, Any]]:
        """导出为可持久化字典列表（时间字段 ISO 化）。"""
        out: list[dict[str, Any]] = []
        for c in self._confirmations.values():
            item = {
                "confirmation_id": c.confirmation_id,
                "actor_id": c.actor_id,
                "scope_key": c.scope_key,
                "action": c.action,
                "payload_digest": c.payload_digest,
                "required_permission": c.required_permission,
                "approved": c.approved,
                "created_at": c.created_at.isoformat(),
            }
            if c.expires_at is not None:
                item["expires_at"] = c.expires_at.isoformat()
            if c.consumed_at is not None:
                item["consumed_at"] = c.consumed_at.isoformat()
            out.append(item)
        return out

    def restore(self, items: list[dict[str, Any]]) -> int:
        """从 dump() 结果恢复；跳过损坏项与过期项，返回恢复数量。"""
        restored = 0
        for item in items or []:
            try:
                c = Confirmation(
                    confirmation_id=item["confirmation_id"],
                    actor_id=item["actor_id"],
                    scope_key=item["scope_key"],
                    action=item["action"],
                    payload_digest=item["payload_digest"],
                    required_permission=item.get("required_permission", "manage_config"),
                    approved=bool(item.get("approved", False)),
                    created_at=datetime.fromisoformat(item["created_at"]),
                    expires_at=(datetime.fromisoformat(item["expires_at"])
                                if item.get("expires_at") else None),
                    consumed_at=(datetime.fromisoformat(item["consumed_at"])
                                 if item.get("consumed_at") else None),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if c.is_expired:
                continue
            self._confirmations[c.confirmation_id] = c
            restored += 1
        return restored


class Redactor:
    """共享脱敏服务（幂等）。

    处理 credential value pattern、URL user-info/query、嵌套结构。
    返回 (清洗后数据, reason codes)。
    """

    CREDENTIAL_PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
        re.compile(r"api[_-]?key[\"'=:\s]+[A-Za-z0-9_-]{12,}", re.IGNORECASE),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    )
    URL_AUTH_RE = re.compile(r"(https?://)([^/@\s]+)@")
    URL_QUERY_RE = re.compile(
        r"([?&])(token|key|secret|password|code|access_token|refresh_token|api_key|sign|sig)=[^&\s]+",
        re.IGNORECASE,
    )

    def __init__(self, extra_patterns: tuple[str, ...] = ()):
        self._extra = tuple(re.compile(p) for p in extra_patterns)

    def redact(self, value: Any) -> tuple[Any, tuple[str, ...]]:
        reasons: list[str] = []
        out = self._redact_value(value, reasons)
        return out, tuple(sorted(set(reasons)))

    def _redact_value(self, value: Any, reasons: list[str]) -> Any:
        if isinstance(value, str):
            return self._redact_text(value, reasons)
        if isinstance(value, dict):
            return {k: self._redact_value(v, reasons) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            items = [self._redact_value(v, reasons) for v in value]
            return tuple(items) if isinstance(value, tuple) else items
        return value

    def _redact_text(self, text: str, reasons: list[str]) -> str:
        out = text
        for pat in (*self.CREDENTIAL_PATTERNS, *self._extra):
            if pat.search(out):
                out = pat.sub("[REDACTED]", out)
                reasons.append("credential")
        if self.URL_AUTH_RE.search(out):
            out = self.URL_AUTH_RE.sub(r"\1[REDACTED]@", out)
            reasons.append("url_userinfo")
        if self.URL_QUERY_RE.search(out):
            out = self.URL_QUERY_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}=[REDACTED]", out
            )
            reasons.append("url_query")
        return out
