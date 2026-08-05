"""嘟嘟哒 2.0 Observability —— Trace、日志与 Event Bus 桩。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class TraceLevel(str, Enum):
    PHASE = "phase"
    MODEL = "model"
    TOOL = "tool"
    ERROR = "error"
    METRIC = "metric"


@dataclass(frozen=True)
class TraceEvent:
    event_id: str = field(default_factory=lambda: uuid4().hex)
    trace_id: str = ""
    run_id: str = ""
    level: TraceLevel = TraceLevel.PHASE
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    phase: Optional[str] = None
    duration_ms: Optional[float] = None
    model_role: Optional[str] = None
    capability_id: Optional[str] = None
    tool_status: Optional[str] = None
    decision_reason: Optional[str] = None
    memory_count: Optional[int] = None
    delivery_status: Optional[str] = None
    error_code: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TraceSink(ABC):
    @abstractmethod
    def write(self, event: TraceEvent): ...
    @abstractmethod
    def flush(self): ...


class InMemoryTraceSink(TraceSink):
    def __init__(self):
        self.events: list[TraceEvent] = []
    def write(self, event: TraceEvent):
        self.events.append(event)
    def flush(self):
        pass
    def by_run(self, run_id: str) -> list[TraceEvent]:
        return [e for e in self.events if e.run_id == run_id]
    def by_trace(self, trace_id: str) -> list[TraceEvent]:
        return [e for e in self.events if e.trace_id == trace_id]


class Tracer:
    def __init__(self, sink: Optional[TraceSink] = None):
        self._sink = sink or InMemoryTraceSink()
        self._active_traces: dict[str, str] = {}
    def start_run(self, run_id: str) -> str:
        trace_id = uuid4().hex
        self._active_traces[run_id] = trace_id
        return trace_id
    def end_run(self, run_id: str):
        self._active_traces.pop(run_id, None)
    def record(self, run_id: str, level: TraceLevel, **kwargs: Any):
        trace_id = self._active_traces.get(run_id, "")
        event = TraceEvent(trace_id=trace_id, run_id=run_id, level=level, **kwargs)
        self._sink.write(event)
    def record_phase(self, run_id: str, from_phase: str, to_phase: str, duration_ms: Optional[float] = None):
        self.record(run_id, TraceLevel.PHASE, phase=f"{from_phase}->{to_phase}", duration_ms=duration_ms)
    def record_error(self, run_id: str, error_code: str, **kwargs: Any):
        self.record(run_id, TraceLevel.ERROR, error_code=error_code, **kwargs)
    def get_events(self, run_id: str) -> list[TraceEvent]:
        if isinstance(self._sink, InMemoryTraceSink):
            return self._sink.by_run(run_id)
        return []


# === Event Bus (规划中) ===

@dataclass
class DomainEvent:
    """领域事件基类。"""
    schema_version: str = "1.0"
    event_id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str = ""
    trace_id: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MemoryWriteRequested(DomainEvent):
    record_count: int = 0


@dataclass
class DeliveryCompleted(DomainEvent):
    delivery_status: str = ""


@dataclass
class RunCompleted(DomainEvent):
    outcome: str = ""
    phases_visited: int = 0
    errors: int = 0


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list] = {}
    def subscribe(self, event_type: str, handler):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
    async def publish(self, event: DomainEvent):
        event_type = type(event).__name__
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                pass
