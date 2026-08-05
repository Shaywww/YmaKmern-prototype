"""嘟嘟哒 2.0 Application Ports —— 核心对基础设施的依赖倒置接口。

对应文档 2.4.3：Orchestrator 只依赖 Context、Perception、Decision、
Model、Tool、Memory、Composer 和 Renderer 的 Protocol，不依赖具体
AstrBot、MCP Server 或 Provider SDK。基础设施在组合时以适配器注入。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .envelope import Platform
from .memory import MemoryRecord, MemoryScope
from .delivery import DeliveryReceipt, RuntimeResult


@runtime_checkable
class MemoryPort(Protocol):
    """受控记忆端口。

    Repository 只做精确 Scope 过滤 + TTL + 语义排序，
    不允许先取全局候选再在 Python 侧删减。
    """

    def write(self, record: MemoryRecord) -> str: ...

    def query(
        self, scope: MemoryScope, limit: int = 20
    ) -> tuple[MemoryRecord, ...]: ...

    def delete(self, record_id: str) -> bool: ...

    def count(self, scope: Optional[MemoryScope] = None) -> int: ...


@runtime_checkable
class ModelPort(Protocol):
    """模型端口：按角色 / 数据等级 / 模态路由。"""

    async def complete(
        self, role: Any, messages: list[dict[str, Any]], **kwargs: Any
    ) -> str: ...


@runtime_checkable
class CapabilityPort(Protocol):
    """能力端口：注册与检索分离，Provider 只用稳定 ID 引用。"""

    def list_enabled(self) -> tuple[Any, ...]: ...

    def summaries(self) -> tuple[str, ...]: ...


@runtime_checkable
class DeliveryPort(Protocol):
    """投递端口：发送后回传 DeliveryReceipt，Runtime 确认后才完成。"""

    async def deliver(
        self,
        result: RuntimeResult,
        platform: Platform,
        conversation_id: str,
    ) -> DeliveryReceipt: ...
