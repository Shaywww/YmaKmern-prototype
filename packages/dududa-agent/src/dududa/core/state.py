"""嘟嘟哒 2.0 Runtime State —— 状态机、阶段定义与运行时快照。

Runtime Orchestrator 通过不可变快照或受控 reducer 推进状态，
不能用模块级全局变量存放单次请求。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Optional
from uuid import uuid4


class RuntimePhase(str, Enum):
    """运行阶段 —— 同步主链中的状态节点。"""
    # 输入
    RECEIVED = "received"
    VALIDATED = "validated"
    PREPROCESSED = "preprocessed"

    # 上下文与感知
    SCOPED = "scoped"
    CONTEXT_BUILT = "context_built"
    PERCEIVED = "perceived"

    # 决策
    DECIDED = "decided"

    # 可选工具链
    CAPABILITIES_LISTED = "capabilities_listed"
    TOOLS_PLANNED = "tools_planned"
    TOOLS_EXECUTED = "tools_executed"
    VALIDATED_TOOLS = "validated_tools"

    # 生成
    COMPOSED = "composed"
    RENDERED = "rendered"

    # 输出
    READY_TO_EMIT = "ready_to_emit"
    DELIVERY_ACKNOWLEDGED = "delivery_acknowledged"
    MEMORY_EVALUATED = "memory_evaluated"

    # 终止
    COMPLETED = "completed"
    ABORTED = "aborted"
    CANCELLED = "cancelled"


class RunOutcome(str, Enum):
    """运行结果。"""
    SUCCEEDED = "succeeded"
    IGNORED = "ignored"               # Social Decision 决定不回复
    DEGRADED = "degraded"             # 部分失败，降级回复
    ABORTED = "aborted"               # 安全/权限终止
    FAILED = "failed"                 # 内部错误
    CANCELLED = "cancelled"           # 外部取消


class SocialAction(str, Enum):
    """六种明确动作（文档 2.4.8）：IGNORE/REACT/DIRECT_REPLY/ASK_CLARIFICATION/USE_TOOLS/DEFER。"""
    IGNORE = "ignore"                 # 不参与
    REACT = "react"                   # 轻量反应（表情、简短回应）
    DIRECT_REPLY = "direct_reply"     # 直接回答
    ASK_CLARIFICATION = "ask_clarification"  # 追问澄清（只问一个解除阻塞的问题）
    USE_TOOLS = "use_tools"           # 需要工具链
    DEFER = "defer"                   # 暂缓（当前无足够信息）

    # 兼容别名（旧名保留可导入，映射到规范动作）
    ANSWER = "direct_reply"
    ASK = "ask_clarification"

    # 兼容扩展：安全/权限硬阻断（观察行为 = 不回复；原因码见 DecisionReason）
    BLOCK = "block"


class ToolPlanStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class WriteGateDecision(str, Enum):
    """Memory Write Gate 决策。"""
    ALLOW = "allow"
    REJECT = "reject"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DEFER_FOR_CONFLICT = "defer_for_conflict_resolution"


@dataclass(frozen=True)
class RuntimeBudget:
    """单次运行的资源预算。"""
    max_model_calls: int = 6
    max_tool_steps: int = 4        # 默认 4，全局硬上限 8
    max_tool_retries: int = 2
    max_context_tokens: int = 8000
    deadline_seconds: float = 30.0
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def deadline(self) -> datetime:
        from datetime import timedelta
        return self.created_at + timedelta(seconds=self.deadline_seconds)

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.deadline


@dataclass(frozen=True)
class ToolStep:
    """单个工具执行步骤。"""
    step_id: str
    capability_id: str
    arguments: dict[str, Any]
    purpose: str
    depends_on: tuple[str, ...] = ()
    expected_output: str = ""
    completion_criteria: tuple[str, ...] = ()
    status: ToolPlanStatus = ToolPlanStatus.PENDING
    observation: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class ToolPlan:
    """工具调用计划。"""
    schema_version: str = "1.0"
    goal: str = ""
    steps: tuple[ToolStep, ...] = ()
    max_steps: int = 4

    @property
    def pending_steps(self) -> tuple[ToolStep, ...]:
        return tuple(s for s in self.steps if s.status == ToolPlanStatus.PENDING)

    @property
    def all_completed(self) -> bool:
        return all(
            s.status in (ToolPlanStatus.SUCCEEDED, ToolPlanStatus.FAILED,
                         ToolPlanStatus.DENIED, ToolPlanStatus.UNAVAILABLE)
            for s in self.steps
        )


@dataclass(frozen=True)
class RuntimeState:
    """单次消息处理的完整结构化快照。

    所有字段通过不可变快照或受控 reducer 更新。
    """
    run_id: str = field(default_factory=lambda: uuid4().hex)
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    phase: RuntimePhase = RuntimePhase.RECEIVED
    outcome: Optional[RunOutcome] = None

    # 输入
    envelope: Optional[Any] = None         # MessageEnvelope
    preprocessed: Optional[Any] = None     # PreprocessedEnvelope

    # 上下文
    scope: Optional[Any] = None            # ConversationScope
    context_snapshot: Optional[Any] = None # ContextSnapshot

    # 感知
    perception: Optional[Any] = None       # PerceptionResult

    # 决策
    social_decision: Optional[SocialAction] = None
    decision_reason: str = ""

    # 工具
    capability_candidates: tuple[Any, ...] = ()  # CapabilityCandidate
    tool_plan: Optional[ToolPlan] = None
    tool_observations: tuple[Any, ...] = ()      # ToolObservation
    confirmation_ids: tuple[str, ...] = ()       # 本次运行的持久确认 token（文档 2.4.23）

    # 生成
    draft_response: Optional[Any] = None   # DraftResponse
    final_response: Optional[Any] = None   # FinalResponse

    # 输出
    delivery_receipt: Optional[Any] = None # DeliveryReceipt

    # 记忆
    memory_candidates: tuple[Any, ...] = ()
    memory_write_receipts: tuple[Any, ...] = ()

    # 溯源
    trace: list[dict[str, Any]] = field(default_factory=list)
    errors: tuple[str, ...] = ()
    budget: RuntimeBudget = field(default_factory=RuntimeBudget)

    # --- Transition helpers ---

    def transition(self, phase: RuntimePhase, **updates: Any) -> "RuntimeState":
        """创建不可变状态转换。"""
        new_fields = {
            "phase": phase,
            "trace": self.trace + [{
                "from_phase": self.phase.value,
                "to_phase": phase.value,
            }],
        }
        new_fields.update(updates)
        return type(self)(**{**self.__dict__, **new_fields})

    def with_error(self, error: str) -> "RuntimeState":
        """附加错误信息。"""
        return type(self)(**{
            **self.__dict__,
            "errors": self.errors + (error,),
        })

    @property
    def is_terminal(self) -> bool:
        return self.phase in (
            RuntimePhase.COMPLETED,
            RuntimePhase.ABORTED,
            RuntimePhase.CANCELLED,
        )
