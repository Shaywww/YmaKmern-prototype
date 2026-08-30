"""测试 Observability (Trace + Event Bus)。"""
import sys
import pytest
from dududa.observability.observability import (
    Tracer, TraceLevel, TraceEvent, InMemoryTraceSink,
    EventBus, DomainEvent,
    MemoryWriteRequested, DeliveryCompleted, RunCompleted,
)


class TestInMemoryTraceSink:
    def test_write_and_retrieve(self):
        sink = InMemoryTraceSink()
        event = TraceEvent(run_id="r1", trace_id="t1")
        sink.write(event)
        assert len(sink.events) == 1
        assert sink.by_run("r1") == [event]
        assert sink.by_trace("t1") == [event]

    def test_filtering(self):
        sink = InMemoryTraceSink()
        sink.write(TraceEvent(run_id="r1", trace_id="t1"))
        sink.write(TraceEvent(run_id="r2", trace_id="t2"))
        assert len(sink.by_run("r1")) == 1
        assert len(sink.by_run("nonexistent")) == 0


class TestTracer:
    def test_start_and_record(self):
        sink = InMemoryTraceSink()
        tracer = Tracer(sink)
        tracer.start_run("r1")
        tracer.record_phase("r1", "received", "validated")
        events = tracer.get_events("r1")
        assert len(events) == 1
        assert events[0].level == TraceLevel.PHASE

    def test_record_error(self):
        sink = InMemoryTraceSink()
        tracer = Tracer(sink)
        tracer.start_run("r1")
        tracer.record_error("r1", "TIMEOUT")
        events = tracer.get_events("r1")
        assert len(events) == 1
        assert events[0].error_code == "TIMEOUT"

    def test_end_run(self):
        sink = InMemoryTraceSink()
        tracer = Tracer(sink)
        tracer.start_run("r1")
        tracer.end_run("r1")
        events = tracer.get_events("r1")
        assert len(events) == 0  # 事件保留在 sink 中


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("RunCompleted", handler)

        event = RunCompleted(run_id="r1", outcome="succeeded", phases_visited=5, errors=0)
        await bus.publish(event)
        assert len(received) == 1
        assert received[0].run_id == "r1"

    @pytest.mark.asyncio
    async def test_no_subscribers(self):
        bus = EventBus()
        event = RunCompleted(run_id="r1", outcome="succeeded", phases_visited=1, errors=0)
        # Should not raise
        await bus.publish(event)

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        bus = EventBus()
        results = []

        async def h1(e): results.append("h1")
        async def h2(e): results.append("h2")

        bus.subscribe("RunCompleted", h1)
        bus.subscribe("RunCompleted", h2)

        await bus.publish(RunCompleted(run_id="r1", outcome="ok", phases_visited=1, errors=0))
        assert len(results) == 2


class TestDomainEvents:
    def test_memory_write_requested(self):
        e = MemoryWriteRequested(run_id="r1", record_count=3)
        assert e.record_count == 3

    def test_delivery_completed(self):
        e = DeliveryCompleted(run_id="r1", delivery_status="succeeded")
        assert e.delivery_status == "succeeded"

    def test_run_completed(self):
        e = RunCompleted(run_id="r1", outcome="succeeded", phases_visited=10, errors=0)
        assert e.phases_visited == 10
