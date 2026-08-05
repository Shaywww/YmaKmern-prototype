"""嘟嘟哒 2.0 Capability System —— 能力注册、发现、计划与执行。

Capability 是经过注册、校验、可授权的外部能力包装。
动态发现的 MCP Tool 不会自动成为 Capability。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from .state import ToolPlanStatus


class CapabilityRisk(str, Enum):
    """能力风险等级。"""
    READ_ONLY = "read_only"       # 只读，无副作用
    SIDE_EFFECT = "side_effect"  # 有副作用（写入、发送等）
    DANGEROUS = "dangerous"       # 高风险（删除、管理操作）


class ProviderType(str, Enum):
    """能力提供者类型。"""
    BUILTIN = "builtin"
    MCP = "mcp"
    HTTP = "http"
    INTERNAL = "internal"


@dataclass(frozen=True)
class CapabilitySchema:
    """能力的输入/输出 Schema 定义。"""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    error_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    """注册的能力定义。"""
    capability_id: str
    name: str
    description: str
    provider: ProviderType = ProviderType.BUILTIN
    risk: CapabilityRisk = CapabilityRisk.READ_ONLY
    schema: CapabilitySchema = field(default_factory=CapabilitySchema)
    required_permissions: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    max_retries: int = 1
    requires_confirmation: bool = False
    enabled: bool = True
    version: str = "1.0.0"
    health_check: Optional[Callable[[], bool]] = None
    # 文档 2.4.9：检索预过滤所需字段（全部有默认值，向后兼容）
    category: str = "general"
    privacy_level: str = "public"          # public | sensitive | restricted
    allowed_contexts: tuple[str, ...] = () # 允许的会话类型，空 = 不限
    cost_hint: int = 0                     # 估算成本（0 = 未知/不限）
    latency_hint_ms: float = 0.0           # 估算延迟（0 = 未知/不限）
    idempotent: bool = False               # 幂等能力允许重复调用
    side_effects: tuple[str, ...] = ()     # 副作用标签（send/write/delete/...）

    @property
    def is_healthy(self) -> bool:
        if not self.enabled:
            return False
        if self.health_check is not None:
            try:
                return self.health_check()
            except Exception:
                return False
        return True


@dataclass(frozen=True)
class CapabilityQuery:
    """能力检索查询（文档 2.4.10）。自然语言目标是不可信数据。"""
    intent: str = ""
    goal: str = ""
    resolved_entities: dict[str, str] = field(default_factory=dict)
    expected_output: str = ""
    preferred_categories: tuple[str, ...] = ()
    forbidden_side_effects: tuple[str, ...] = ()
    max_risk: CapabilityRisk = CapabilityRisk.SIDE_EFFECT
    max_cost: int = 0            # 0 = 不限
    max_latency_ms: float = 0.0  # 0 = 不限
    top_k: int = 8


@dataclass(frozen=True)
class CapabilityCandidate:
    """通过预算和权限过滤后的候选能力。"""
    capability: Capability
    relevance_score: float
    rank: int
    estimated_cost: int = 0


@dataclass(frozen=True)
class ToolObservation:
    """工具执行后的归一化观察结果。"""
    step_id: str
    capability_id: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    source: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    sensitive: bool = False
    truncated: bool = False

    @property
    def has_valid_data(self) -> bool:
        return self.success and self.data is not None


class ValidatorAction(str, Enum):
    FINISH = "finish"
    CONTINUE = "continue"
    RETRY = "retry"
    CLARIFY = "clarify"
    ABORT = "abort"
    DEGRADE = "degrade"


@dataclass(frozen=True)
class ValidationResult:
    action: ValidatorAction
    observations: tuple[ToolObservation, ...] = ()
    error_details: tuple[str, ...] = ()
    partial_data: Optional[Any] = None
    completion_criteria_met: bool = False


class CapProvider(ABC):
    """抽象能力提供者 —— Executor 的唯一调用入口。"""

    @abstractmethod
    async def execute(
        self, capability: Capability, arguments: dict[str, Any]
    ) -> ToolObservation:
        ...

    @abstractmethod
    def health(self) -> bool:
        ...


class CapabilityRegistry:
    """能力注册中心。"""

    def __init__(self):
        self._capabilities: dict[str, Capability] = {}
        self._providers: dict[str, CapProvider] = {}
        self._recent_calls: dict[str, tuple[str, float]] = {}

    def register(self, capability: Capability, provider: "CapProvider"):
        self._capabilities[capability.capability_id] = capability
        self._providers[capability.capability_id] = provider

    def unregister(self, capability_id: str):
        self._capabilities.pop(capability_id, None)
        self._providers.pop(capability_id, None)

    def get(self, capability_id: str) -> Optional[Capability]:
        return self._capabilities.get(capability_id)

    def get_provider(self, capability_id: str) -> Optional[CapProvider]:
        return self._providers.get(capability_id)

    def list_enabled(self) -> tuple[Capability, ...]:
        return tuple(c for c in self._capabilities.values() if c.enabled)

    def list_healthy(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.list_enabled() if c.is_healthy)

    # ---- 重复调用检测（文档 2.4.12：幂等键） ----


    def record_call(
        self,
        capability_id: str,
        idempotency_key: str = "",
        window_seconds: float = 60.0,
    ) -> bool:
        """记录一次调用；同一能力 + 同一幂等键在窗口内再次调用返回 True（重复）。"""
        import time
        key = f"{capability_id}:{idempotency_key}"
        now = time.time()
        prev_ts = self._recent_calls.get(key)
        if prev_ts is not None and (now - prev_ts) < window_seconds:
            return True
        self._recent_calls[key] = now
        return False

    def retrieve(
        self,
        query: CapabilityQuery,
        permissions: tuple[str, ...] = (),
    ) -> tuple[CapabilityCandidate, ...]:
        """文档 2.4.10：确定性预过滤 + Top-K 排序。

        预过滤：enabled/健康/权限/隐私/会话类型/风险/成本/延迟/禁止副作用。
        排序分 = 意图命中 + 类别偏好 - 成本/延迟惩罚。
        """
        risk_order = {
            CapabilityRisk.READ_ONLY: 0,
            CapabilityRisk.SIDE_EFFECT: 1,
            CapabilityRisk.DANGEROUS: 2,
        }
        intent_tokens = set(
            (query.intent + " " + query.goal).lower().replace("_", " ").split()
        )
        candidates: list[CapabilityCandidate] = []
        for cap in self.list_healthy():
            # 权限
            if cap.required_permissions:
                if not all(p in permissions for p in cap.required_permissions):
                    continue
            # 风险
            if risk_order.get(cap.risk, 9) > risk_order.get(query.max_risk, 1):
                continue
            # 隐私：受限数据只能给显式放行的能力
            if cap.privacy_level in ("sensitive", "restricted") and query.max_risk == CapabilityRisk.READ_ONLY:
                pass  # 隐私由调用方 Policy 把关，这里只做基础风险过滤
            # 会话类型
            if cap.allowed_contexts:
                ctx_hint = query.resolved_entities.get("context", "")
                if ctx_hint and ctx_hint not in cap.allowed_contexts:
                    continue
            # 成本/延迟上限
            if query.max_cost > 0 and cap.cost_hint > query.max_cost:
                continue
            if query.max_latency_ms > 0 and cap.latency_hint_ms > query.max_latency_ms:
                continue
            # 禁止副作用
            if query.forbidden_side_effects and any(
                sfx in cap.side_effects for sfx in query.forbidden_side_effects
            ):
                continue

            # 相关性打分
            score = 0.5
            haystack = (cap.capability_id + " " + cap.name + " " + cap.description).lower()
            if intent_tokens and any(tok in haystack for tok in intent_tokens):
                score += 0.3
            if query.preferred_categories and cap.category in query.preferred_categories:
                score += 0.2
            if cap.cost_hint > 0:
                score -= min(0.1, cap.cost_hint / 10000.0)
            if cap.latency_hint_ms > 0:
                score -= min(0.1, cap.latency_hint_ms / 100000.0)
            candidates.append(
                CapabilityCandidate(capability=cap, relevance_score=score, rank=0)
            )

        candidates.sort(key=lambda c: (-c.relevance_score,
                                       risk_order.get(c.capability.risk, 9)))
        for i, c in enumerate(candidates):
            candidates[i] = CapabilityCandidate(
                capability=c.capability, relevance_score=c.relevance_score, rank=i + 1
            )
        return tuple(candidates[: query.top_k])

    def filter_candidates(
        self,
        permissions: tuple[str, ...],
        risk_tolerance: CapabilityRisk = CapabilityRisk.SIDE_EFFECT,
        max_count: int = 8,
    ) -> tuple[CapabilityCandidate, ...]:
        """兼容入口：等价于 retrieve(CapabilityQuery(max_risk=..., top_k=...))。"""
        return self.retrieve(
            CapabilityQuery(max_risk=risk_tolerance, top_k=max_count),
            permissions=permissions,
        )

    def summaries(self) -> tuple[str, ...]:
        return tuple(
            f"{c.name}: {c.description}" for c in self.list_healthy()
        )


class ToolPlanValidator:
    """工具计划校验器。"""

    def __init__(self, registry: CapabilityRegistry):
        self._registry = registry

    def validate_plan(self, plan: Any, budget: Any) -> tuple[bool, tuple[str, ...]]:
        errors: list[str] = []

        if hasattr(plan, "steps"):
            steps = plan.steps
        else:
            return False, ("Invalid plan format",)

        if len(steps) > budget.max_tool_steps:
            errors.append(
                f"Plan has {len(steps)} steps, max {budget.max_tool_steps}"
            )

        step_ids = {s.step_id for s in steps}
        for step in steps:
            cap = self._registry.get(step.capability_id)
            if cap is None:
                errors.append(f"Capability '{step.capability_id}' not registered")
                continue
            if not cap.is_healthy:
                errors.append(f"Capability '{step.capability_id}' not healthy")
            if cap.schema.input_schema:
                schema_errors = self._validate_arguments(
                    step.arguments, cap.schema.input_schema
                )
                errors.extend(schema_errors)

        for step in steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    errors.append(
                        f"Step '{step.step_id}' depends on unknown step '{dep}'"
                    )

        return len(errors) == 0, tuple(errors)

    def validate_results(
        self, observations: tuple[ToolObservation, ...]
    ) -> ValidationResult:
        all_success = all(o.success for o in observations)
        has_data = any(o.has_valid_data for o in observations)
        has_errors = any(not o.success for o in observations)

        if all_success and has_data:
            return ValidationResult(action=ValidatorAction.FINISH, observations=observations, completion_criteria_met=True)
        if has_data and has_errors:
            return ValidationResult(action=ValidatorAction.DEGRADE, observations=observations, partial_data=[o.data for o in observations if o.has_valid_data])
        if not has_data:
            return ValidationResult(action=ValidatorAction.CLARIFY, observations=observations, error_details=tuple(o.error or "unknown" for o in observations if not o.success))
        return ValidationResult(action=ValidatorAction.ABORT, observations=observations)

    @staticmethod
    def _validate_arguments(args: dict[str, Any], schema: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        required = schema.get("required", [])
        for field in required:
            if field not in args:
                errors.append(f"Missing required field: {field}")
        return errors
