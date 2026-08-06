"""嘟嘟哒 2.0 Model Router —— 按角色路由模型请求（文档 2.5.7 八类角色）。

不同任务（Perception、Social Decision、Tool Planning、Direct Chat、
Response Composition、Memory Summary、Image Understanding、Image
Generation）需要不同的模型角色、推理预算和失败语义。
Model Router 负责统一模型选择、数据分类过滤、健康检查与降级。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional
from ..core.trace_recorder import trace_recorder


class ModelRole(str, Enum):
    """模型角色 —— 文档 2.5.7 八类角色。"""
    PERCEPTION = "perception"                 # 理解消息：轻量、快速
    SOCIAL_DECISION = "social_decision"       # 社交判断：中等
    TOOL_PLANNING = "tool_planning"           # 工具规划：高推理
    DIRECT_CHAT = "direct_chat"               # 直接对话：通用
    RESPONSE_COMPOSITION = "response_composition"  # 回复组织：中等
    MEMORY_SUMMARY = "memory_summary"         # 摘要/记忆：轻量
    IMAGE_UNDERSTANDING = "image_understanding"    # 图像理解：多模态
    IMAGE_GENERATION = "image_generation"     # 图像生成：多模态

    # 兼容别名（旧名保留可导入，映射到规范角色）
    DECISION = "social_decision"
    PLANNING = "tool_planning"
    COMPOSER = "response_composition"
    RENDERER = "response_composition"
    SUMMARIZER = "memory_summary"


class ModelDataClass(str, Enum):
    """请求数据分类 —— 敏感数据不能路由到未授权 Provider。"""
    PUBLIC = "public"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class ModelErrorKind(str, Enum):
    """稳定失败语义（文档 2.5.7）：429/timeout/认证/安全拒绝/无合法路由。"""
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    SAFETY_REJECTED = "safety_rejected"
    NO_ROUTE = "no_route"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL = "internal"


class ModelError(Exception):
    """带稳定错误码的模型调用异常。"""

    def __init__(
        self,
        kind: ModelErrorKind,
        message: str = "",
        retryable: bool = False,
        role: Optional[ModelRole] = None,
        model_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.role = role
        self.model_id = model_id

    @property
    def stable_code(self) -> str:
        return self.kind.value


def _trace_ids(request) -> dict:
    """Trace 关联 ID：从请求 metadata 提取，用于跨层关联（文档 2.5.10）。"""
    return {
        "run_id": str(request.metadata.get("run_id", "")),
        "trace_id": str(request.metadata.get("trace_id", "")),
    }


def _record_model_response(request, model_id, degraded, latency_ms, error_kind=""):
    trace_recorder.record(
        event="model_response", **_trace_ids(request),
        role=request.role.value, model_id=model_id,
        degraded=degraded, latency_ms=round(latency_ms, 1),
        error_kind=error_kind)


def _record_model_error(request, model_id, error_kind):
    trace_recorder.record(
        event="model_error", **_trace_ids(request),
        role=request.role.value, model_id=model_id or "",
        error_kind=error_kind)


@dataclass(frozen=True)
class ModelRequest:
    """标准模型请求。"""
    role: ModelRole
    messages: list[dict[str, str]]
    data_class: ModelDataClass = ModelDataClass.PUBLIC
    route_hint: Optional[str] = None          # 不可信提示：不能越权或降级数据类别
    structured_output: Optional[dict[str, Any]] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """标准模型响应。"""
    role: ModelRole
    text: str
    model_id: str
    degraded: bool = False
    latency_ms: float = 0.0
    usage_tokens: int = 0
    error: Optional[ModelError] = None


@dataclass(frozen=True)
class ModelConfig:
    """模型配置。"""
    role: ModelRole
    model_id: str
    reasoning_effort: str = "medium"  # low | medium | high | xhigh
    max_tokens: int = 2048
    temperature: float = 0.7
    timeout_seconds: float = 15.0
    retry_count: int = 1
    structured_output: Optional[dict[str, Any]] = None
    allow_sensitive: bool = False     # 是否允许接收 SENSITIVE/RESTRICTED 数据
    route_hint_allowed: bool = False  # 是否允许调用方 route_hint 指定模型（默认防越权）

    # 降级模型
    fallback_model_id: Optional[str] = None


@dataclass(frozen=True)
class RouterConfig:
    """路由配置 —— 每个角色到模型的映射。"""
    roles: dict[ModelRole, ModelConfig] = field(default_factory=dict)

    def get(self, role: ModelRole) -> Optional[ModelConfig]:
        return self.roles.get(role)

    @classmethod
    def default_config(cls) -> "RouterConfig":
        """默认配置：所有角色使用同一模型（简单模式）。"""
        default_model = "gpt-5.6-sol"
        def _cfg(role: ModelRole, effort: str, tokens: int, temp: float = 0.7) -> ModelConfig:
            return ModelConfig(role=role, model_id=default_model,
                               reasoning_effort=effort, max_tokens=tokens,
                               temperature=temp)
        return cls(
            roles={
                ModelRole.PERCEPTION: _cfg(ModelRole.PERCEPTION, "low", 1024),
                ModelRole.SOCIAL_DECISION: _cfg(ModelRole.SOCIAL_DECISION, "medium", 512),
                ModelRole.TOOL_PLANNING: _cfg(ModelRole.TOOL_PLANNING, "high", 2048),
                ModelRole.DIRECT_CHAT: _cfg(ModelRole.DIRECT_CHAT, "medium", 2048),
                ModelRole.RESPONSE_COMPOSITION: _cfg(ModelRole.RESPONSE_COMPOSITION, "medium", 2048),
                ModelRole.MEMORY_SUMMARY: _cfg(ModelRole.MEMORY_SUMMARY, "low", 1024),
                ModelRole.IMAGE_UNDERSTANDING: _cfg(ModelRole.IMAGE_UNDERSTANDING, "medium", 1024),
                ModelRole.IMAGE_GENERATION: _cfg(ModelRole.IMAGE_GENERATION, "medium", 1024),
            }
        )


class CredentialResolver(ABC):
    """凭据解析器 —— 按模型 ID 解析密钥；密钥不进入 Runtime State。"""

    @abstractmethod
    def resolve(self, model_id: str) -> Optional[str]:
        ...


class ModelProvider(ABC):
    """抽象模型提供者。"""

    @abstractmethod
    async def complete(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        config: ModelConfig,
    ) -> str:
        ...

    @abstractmethod
    def health(self, model_id: str) -> bool:
        ...


class ModelRouter:
    """模型路由器。

    负责：
    1. 按角色选择模型（八类角色）
    2. 数据分类过滤（敏感数据不进入未授权 Provider）
    3. route hint 越权防护（提示不能升级数据类别或绕过过滤）
    4. 健康检查、降级与稳定失败语义
    """

    def __init__(
        self,
        config: Optional[RouterConfig] = None,
        provider: Optional[ModelProvider] = None,
        credential_resolver: Optional[CredentialResolver] = None,
    ):
        self._config = config or RouterConfig.default_config()
        self._provider = provider
        self._credential_resolver = credential_resolver

        # 健康状态缓存
        self._health_cache: dict[str, tuple[bool, float]] = {}

    async def route_request(
        self,
        request: ModelRequest,
        provider: Optional[ModelProvider] = None,
    ) -> ModelResponse:
        """严格路由契约：失败抛 ModelError（稳定错误码）。

        provider 可临时覆盖提供者（测试注入 / 会话级切换）；
        缺省使用自身装配的 provider。
        """
        import time
        start = time.time()
        provider = provider or self._provider

        config = self._config.get(request.role)
        if config is None:
            raise ModelError(
                ModelErrorKind.NO_ROUTE,
                f"No model configured for role: {request.role}",
                role=request.role,
            )

        # 请求级预算覆盖：per-call max_tokens/temperature 生效（文档 2.5.7）
        if request.max_tokens is not None or request.temperature is not None:
            config = replace(
                config,
                max_tokens=request.max_tokens or config.max_tokens,
                temperature=(request.temperature
                             if request.temperature is not None
                             else config.temperature),
            )

        # 数据分类过滤：敏感/受限数据不得进入未授权 Provider
        if request.data_class != ModelDataClass.PUBLIC and not config.allow_sensitive:
            raise ModelError(
                ModelErrorKind.SAFETY_REJECTED,
                f"provider not allowed for data_class={request.data_class.value}",
                role=request.role,
                model_id=config.model_id,
            )

        # route hint 越权防护：hint 不能改变模型选择，除非显式放行
        model_id = config.model_id
        if request.route_hint and config.route_hint_allowed:
            model_id = request.route_hint

        trace_recorder.record(event="model_request", **_trace_ids(request),
                              role=request.role.value, model_id=model_id,
                              data_class=request.data_class.value)

        if provider is None:
            _record_model_response(request, model_id, True,
                                   (time.time() - start) * 1000,
                                   error_kind="no_provider")
            return ModelResponse(
                role=request.role,
                text=self._stub_response(request.role, request.messages),
                model_id=model_id,
                degraded=True,
                latency_ms=(time.time() - start) * 1000,
            )

        try:
            text = await provider.complete(model_id, request.messages, config)
            _record_model_response(request, model_id, False,
                                   (time.time() - start) * 1000)
            return ModelResponse(
                role=request.role,
                text=text,
                model_id=model_id,
                latency_ms=(time.time() - start) * 1000,
            )
        except ModelError as e:
            # 可重试错误尝试降级模型
            if e.retryable and config.fallback_model_id:
                try:
                    text = await provider.complete(
                        config.fallback_model_id, request.messages, config
                    )
                    _record_model_response(request, config.fallback_model_id, True,
                                           (time.time() - start) * 1000)
                    return ModelResponse(
                        role=request.role,
                        text=text,
                        model_id=config.fallback_model_id,
                        degraded=True,
                        latency_ms=(time.time() - start) * 1000,
                    )
                except ModelError:
                    pass
                except Exception as ex:
                    raise ModelError(
                        ModelErrorKind.PROVIDER_UNAVAILABLE,
                        f"fallback failed: {ex}",
                        retryable=True,
                        role=request.role,
                    ) from ex
            _record_model_error(request, model_id, e.stable_code)
            raise
        except Exception as e:
            _record_model_error(request, model_id, "provider_unavailable")
            raise ModelError(
                ModelErrorKind.PROVIDER_UNAVAILABLE,
                f"provider call failed: {e}",
                retryable=True,
                role=request.role,
                model_id=model_id,
            ) from e

    async def route(
        self,
        role: ModelRole,
        messages: list[dict[str, str]],
        override_config: Optional[ModelConfig] = None,
    ) -> str:
        """兼容入口：失败时返回确定性桩响应（不抛出）。"""
        try:
            response = await self.route_request(ModelRequest(role=role, messages=messages))
            return response.text
        except ModelError:
            config = override_config or self._config.get(role)
            return self._stub_response(role, messages)

    def _is_healthy(self, model_id: str) -> bool:
        import time
        now = time.time()
        cached = self._health_cache.get(model_id)
        if cached and now - cached[1] < 60:  # 缓存 60s
            return cached[0]
        return True

    def update_health(self, model_id: str, healthy: bool):
        import time
        self._health_cache[model_id] = (healthy, time.time())

    @staticmethod
    def _stub_response(
        role: ModelRole, messages: list[dict[str, str]]
    ) -> str:
        """桩响应 —— 当模型不可用时的确定性降级。"""
        last_msg = messages[-1]["content"] if messages else ""
        stub_map = {
            ModelRole.PERCEPTION: '{"topics":[],"needs_tools":false}',
            ModelRole.SOCIAL_DECISION: '{"action":"ignore","confidence":0.0}',
            ModelRole.TOOL_PLANNING: '{"goal":"","steps":[]}',
            ModelRole.DIRECT_CHAT: last_msg,
            ModelRole.RESPONSE_COMPOSITION: "嗯嗯。",
            ModelRole.MEMORY_SUMMARY: "",
            ModelRole.IMAGE_UNDERSTANDING: '{"description":""}',
            ModelRole.IMAGE_GENERATION: "",
        }
        return stub_map.get(role, "")