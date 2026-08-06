"""嘟嘟哒 2.0 Delivery —— 平台输出适配与投递确认。

run() 最多推进到 READY_TO_EMIT；
Output Adapter 实际发送后回传 DeliveryReceipt，
Runtime 才确认投递、评估 Memory 并完成运行。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from .envelope import Platform
from .state import RunOutcome, RuntimeState
from .renderer import FinalResponse


class DeliveryStatus(str, Enum):
    """投递状态。"""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeResult:
    """运行完成时的结果 —— 在 READY_TO_EMIT 阶段产生。"""
    run_id: str
    outcome: RunOutcome
    trace_id: str = ""
    final_response: Optional[FinalResponse] = None
    reaction: Optional[str] = None      # 轻量反应（表情等）
    reason_codes: tuple[str, ...] = ()
    trace_summary: dict[str, Any] = field(default_factory=dict)
    requires_delivery_ack: bool = True

    @property
    def has_visible_output(self) -> bool:
        return self.final_response is not None or self.reaction is not None


@dataclass(frozen=True)
class DeliveryReceipt:
    """投递回执 —— Output Adapter 发送后回传。

    run_id 是唯一绑定。Runtime 校验回执对应等待确认的运行、
    时间带时区、成功引用属于目标 platform/Bot/conversation，
    并使重复回执幂等。
    """
    run_id: str
    status: DeliveryStatus
    acknowledged_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    platform_message_ref: Optional[str] = None  # 平台返回的消息 ID
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == DeliveryStatus.SUCCEEDED


@dataclass(frozen=True)
class CompletionReceipt:
    """运行完成回执 —— 记录 run ID、final phase、delivery 状态和 Memory write receipts。"""
    run_id: str
    final_phase: str
    delivery_status: Optional[DeliveryStatus] = None
    memory_write_receipts: tuple[str, ...] = ()  # record_id 列表


class OutputAdapter(ABC):
    """平台输出适配器抽象。

    把平台无关的 RuntimeResult 转成平台特定格式（QQ 文本、@、图片、
    引用、表情、分片或合并转发），实际发送后返回 DeliveryReceipt。
    """

    @abstractmethod
    async def send(
        self,
        target_platform: Platform,
        conversation_id: str,
        result: RuntimeResult,
    ) -> DeliveryReceipt:
        ...

    @abstractmethod
    async def send_reaction(
        self,
        target_platform: Platform,
        conversation_id: str,
        reaction: str,
    ) -> DeliveryReceipt:
        ...


class NoOpOutputAdapter(OutputAdapter):
    """空操作输出适配器 —— 用于测试。"""

    async def send(
        self,
        target_platform: Platform,
        conversation_id: str,
        result: RuntimeResult,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            run_id=result.run_id,
            status=DeliveryStatus.SUCCEEDED,
            platform_message_ref=f"noop-{uuid4().hex[:8]}",
        )

    async def send_reaction(
        self,
        target_platform: Platform,
        conversation_id: str,
        reaction: str,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            run_id=uuid4().hex,
            status=DeliveryStatus.SUCCEEDED,
        )


class DeliveryManager:
    """投递管理器 —— 管理 RuntimeResult -> OutputAdapter -> DeliveryReceipt 流程。"""

    def __init__(self, adapter: OutputAdapter):
        self._adapter = adapter
        self._pending: dict[str, RuntimeResult] = {}
        self._receipts: dict[str, DeliveryReceipt] = {}

    async def deliver(
        self,
        result: RuntimeResult,
        platform: Platform,
        conversation_id: str,
    ) -> DeliveryReceipt:
        """发送并记录回执。"""
        if not result.has_visible_output:
            return DeliveryReceipt(
                run_id=result.run_id,
                status=DeliveryStatus.SUCCEEDED,
            )

        self._pending[result.run_id] = result
        receipt = await self._adapter.send(
            platform, conversation_id, result
        )
        self._receipts[result.run_id] = receipt
        self._pending.pop(result.run_id, None)
        return receipt

    def get_receipt(self, run_id: str) -> Optional[DeliveryReceipt]:
        return self._receipts.get(run_id)

    def has_pending(self) -> bool:
        return len(self._pending) > 0
