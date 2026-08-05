"""嘟嘟哒 2.0 Safeguards —— 身份、权限、隐私、预算与安全检查。

保障机制横穿 Connector、Context、Decision、Tool、Model、
Response、Output 和 Memory，而不是最后再运行一个安全 Prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ..core.envelope import Actor, Platform


class Permission(str, Enum):
    """权限标签。"""
    READ_MESSAGES = "read_messages"
    SEND_MESSAGES = "send_messages"
    USE_TOOLS = "use_tools"
    WRITE_MEMORY = "write_memory"
    MANAGE_CONFIG = "manage_config"
    ADMIN = "admin"


@dataclass(frozen=True)
class IdentityCheck:
    """身份一致性校验结果。"""
    actor: Actor
    verified: bool
    issues: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.actor.is_privileged()


class IdentityValidator:
    """身份校验器。

    在 Connector 进入 Core 前完成版本、身份一致性、
    大小、时间、会话范围和幂等校验。
    半完整身份不能靠模型补齐。
    """

    @staticmethod
    def validate(actor: Actor) -> IdentityCheck:
        issues: list[str] = []

        if not actor.actor_id or actor.actor_id == "unknown":
            issues.append("Missing actor_id")
        if not actor.display_name:
            issues.append("Missing display_name")
        if actor.platform not in Platform:
            issues.append(f"Unknown platform: {actor.platform}")

        return IdentityCheck(
            actor=actor,
            verified=len(issues) == 0,
            issues=tuple(issues),
        )

    @staticmethod
    def verify_consistency(
        envelope_actor: Actor, platform_actor_id: str
    ) -> bool:
        """验证信封中的 Actor 与平台确认的身份一致。"""
        return envelope_actor.actor_id == platform_actor_id


class PrivacyLevel(str, Enum):
    """隐私分级。"""
    PUBLIC = "public"       # 任何群可见
    GROUP = "group"         # 同群可见
    PRIVATE = "private"     # 仅当事人
    RESTRICTED = "restricted"  # 需额外确认


@dataclass(frozen=True)
class PrivacyScope:
    """隐私边界。"""
    level: PrivacyLevel
    allowed_actors: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    requires_confirmation: bool = False
    max_retention_days: Optional[int] = None


class PrivacyGuard:
    """隐私守卫。

    确保个人信息只在被授权的 Scope 内使用。
    """

    def __init__(self):
        self._scopes: dict[str, PrivacyScope] = {}

    def register_scope(self, scope_id: str, scope: PrivacyScope):
        self._scopes[scope_id] = scope

    def check_access(
        self,
        scope_id: str,
        actor_id: str,
        group_id: Optional[str] = None,
    ) -> bool:
        """检查 Actor 是否有权访问指定 Scope。"""
        scope = self._scopes.get(scope_id)
        if scope is None:
            return False

        if scope.level == PrivacyLevel.PUBLIC:
            return True

        if scope.allowed_actors and actor_id in scope.allowed_actors:
            return True

        if (
            scope.level == PrivacyLevel.GROUP
            and group_id
            and scope.allowed_groups
            and group_id in scope.allowed_groups
        ):
            return True

        return False

    def redact(
        self, text: str, sensitive_patterns: tuple[str, ...]
    ) -> str:
        """脱敏：移除敏感信息。"""
        result = text
        for pattern in sensitive_patterns:
            result = result.replace(pattern, "[REDACTED]")
        return result


@dataclass(frozen=True)
class BudgetTracker:
    """预算跟踪器。

    跟踪每次运行的模型调用次数、工具步骤和 token 使用。
    """
    max_model_calls: int = 6
    max_tool_steps: int = 4
    max_tokens: int = 8000
    model_calls_used: int = 0
    tool_steps_used: int = 0
    tokens_used: int = 0
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def can_call_model(self) -> bool:
        return self.model_calls_used < self.max_model_calls

    def can_use_tool(self) -> bool:
        return self.tool_steps_used < self.max_tool_steps

    def can_use_tokens(self, count: int) -> bool:
        return self.tokens_used + count <= self.max_tokens

    def record_model_call(self, tokens: int = 0):
        return BudgetTracker(
            **{
                **self.__dict__,
                "model_calls_used": self.model_calls_used + 1,
                "tokens_used": self.tokens_used + tokens,
            }
        )

    def record_tool_step(self):
        return BudgetTracker(
            **{
                **self.__dict__,
                "tool_steps_used": self.tool_steps_used + 1,
            }
        )

    @property
    def model_call_ratio(self) -> float:
        if self.max_model_calls == 0:
            return 1.0
        return self.model_calls_used / self.max_model_calls

    @property
    def tool_step_ratio(self) -> float:
        if self.max_tool_steps == 0:
            return 1.0
        return self.tool_steps_used / self.max_tool_steps
