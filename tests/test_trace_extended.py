# -*- coding: utf-8 -*-
"""Trace 贯穿度扩展测试（文档 2.5.10）：Model / Tool / Memory / Delivery 事件。"""
import sys
sys.path.insert(0, "/opt/dududa20-prototype")

import pytest

from packages.core.trace_recorder import TraceRecorder
from packages.core.memory import (
    InMemoryRepository, WriteGate, MemoryCandidate, MemoryRecord,
    MemoryScope, MemoryType, SensitivityLevel,
)
from packages.core.delivery import (
    DeliveryManager, DeliveryStatus, DeliveryReceipt, RuntimeResult,
)
from packages.core.renderer import FinalResponse
from packages.core.state import RunOutcome
from packages.router.router import (
    ModelRouter, RouterConfig, ModelRequest,
    ModelRole, ModelConfig, ModelError, ModelErrorKind,
)
from packages.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, ProviderType,
    CapProvider, ToolObservation,
)
from packages.planner.planner import PlannedStep, GeneratedPlan
from packages.planner.executor import ToolExecutor, ExecutionContext


# ---------------- Model Router ----------------

class StubProvider:
    def __init__(self, text="ok", fail_kind=None, fail_on="m-a"):
        self.text = text
        self.fail_kind = fail_kind
        self.fail_on = fail_on
        self.calls = []

    async def complete(self, model_id, messages, config):
        self.calls.append(model_id)
        if self.fail_kind is not None and model_id == self.fail_on:
            raise ModelError(self.fail_kind, "boom", retryable=True)
        return self.text

    def health(self, model_id):
        return True


def _router_config(model="m-a", fallback="m-b"):
    return RouterConfig(roles={
        ModelRole.DIRECT_CHAT: ModelConfig(
            role=ModelRole.DIRECT_CHAT, model_id=model,
            fallback_model_id=fallback),
    })


@pytest.mark.asyncio
async def test_router_records_model_request_response(monkeypatch, tmp_path):
    from packages.router import router as router_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(router_mod, "trace_recorder", rec)
    r = ModelRouter(config=_router_config(), provider=StubProvider())
    await r.route_request(ModelRequest(
        role=ModelRole.DIRECT_CHAT,
        messages=[{"role": "user", "content": "hi"}],
        metadata={"run_id": "r1", "trace_id": "t1"}))
    lines = rec.lines_for()
    assert [l["event"] for l in lines] == ["model_request", "model_response"]
    assert lines[0]["run_id"] == "r1" and lines[0]["model_id"] == "m-a"
    assert lines[1]["degraded"] is False
    assert lines[1]["model_id"] == "m-a"


@pytest.mark.asyncio
async def test_router_records_degraded_fallback(monkeypatch, tmp_path):
    from packages.router import router as router_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(router_mod, "trace_recorder", rec)
    provider = StubProvider(fail_kind=ModelErrorKind.RATE_LIMITED)
    r = ModelRouter(config=_router_config(), provider=provider)
    resp = await r.route_request(ModelRequest(
        role=ModelRole.DIRECT_CHAT,
        messages=[{"role": "user", "content": "hi"}],
        metadata={"run_id": "r2", "trace_id": "t2"}))
    assert resp.degraded and resp.model_id == "m-b"
    lines = rec.lines_for()
    assert [l["event"] for l in lines] == ["model_request", "model_response"]
    assert lines[1]["degraded"] is True
    assert lines[1]["model_id"] == "m-b"


@pytest.mark.asyncio
async def test_router_records_model_error(monkeypatch, tmp_path):
    from packages.router import router as router_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(router_mod, "trace_recorder", rec)
    provider = StubProvider(fail_kind=ModelErrorKind.PROVIDER_UNAVAILABLE)
    r = ModelRouter(config=_router_config(fallback=None), provider=provider)
    with pytest.raises(ModelError):
        await r.route_request(ModelRequest(
            role=ModelRole.DIRECT_CHAT,
            messages=[{"role": "user", "content": "hi"}],
            metadata={"run_id": "r3", "trace_id": "t3"}))
    lines = rec.lines_for()
    assert [l["event"] for l in lines] == ["model_request", "model_error"]
    assert lines[1]["error_kind"] == "provider_unavailable"


# ---------------- Tool Executor ----------------

class StubCapProvider(CapProvider):
    def __init__(self, fail=False):
        self.fail = fail

    async def execute(self, cap, args):
        if self.fail:
            raise RuntimeError("stub failure")
        return ToolObservation(step_id="s", capability_id=cap.capability_id,
                               success=True, data="stub")

    def health(self):
        return True


def _cap(cid):
    return Capability(
        capability_id=cid, name=cid, description=f"Mock {cid}",
        provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY,
    )


def _plan(cid):
    return GeneratedPlan(
        goal="g", rationale="r",
        steps=(PlannedStep(step_id="s1", capability_id=cid,
                           arguments={}, purpose="p"),),
    )


@pytest.mark.asyncio
async def test_executor_records_tool_call_result(monkeypatch, tmp_path):
    from packages.planner import executor as ex
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(ex, "trace_recorder", rec)
    reg = CapabilityRegistry()
    reg.register(_cap("demo.tool"), StubCapProvider())
    executor = ToolExecutor(reg)
    ctx = ExecutionContext(max_steps=2, run_id="r4", trace_id="t4")
    results = await executor.execute_plan(_plan("demo.tool"), ctx)
    assert results[0].success
    lines = rec.lines_for()
    assert [l["event"] for l in lines] == ["tool_call", "tool_result"]
    assert lines[0]["run_id"] == "r4" and lines[0]["capability_id"] == "demo.tool"
    assert lines[1]["success"] is True


@pytest.mark.asyncio
async def test_executor_records_failed_step(monkeypatch, tmp_path):
    from packages.planner import executor as ex
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(ex, "trace_recorder", rec)
    reg = CapabilityRegistry()
    reg.register(_cap("fail.tool"), StubCapProvider(fail=True))
    executor = ToolExecutor(reg)
    ctx = ExecutionContext(max_steps=2, max_retries_per_step=1,
                           run_id="r4b", trace_id="t4b")
    results = await executor.execute_plan(_plan("fail.tool"), ctx)
    assert not results[0].success
    lines = rec.lines_for()
    kinds = [l["event"] for l in lines]
    assert kinds.count("tool_call") >= 1
    assert kinds[-1] == "tool_result" and lines[-1]["success"] is False


# ---------------- Memory WriteGate ----------------

def _record(content="hello world memory", sensitivity=SensitivityLevel.INTERNAL):
    return MemoryRecord(
        scope=MemoryScope(memory_type=MemoryType.SHORT_TERM,
                          platform="qq", bot_id="b1",
                          conversation_id="c1", actor_id="u1"),
        source="message", content=content, sensitivity=sensitivity,
        visibility=sensitivity, evidence=("src:test",),
    )


def test_writegate_records_allow(monkeypatch, tmp_path):
    from packages.core import memory as mem
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(mem, "trace_recorder", rec)
    gate = WriteGate(InMemoryRepository())
    gate.evaluate(MemoryCandidate(
        proposed_record=_record(), metadata={"run_id": "r5", "trace_id": "t5"}))
    lines = rec.lines_for()
    assert len(lines) == 1 and lines[0]["event"] == "memory_gate"
    assert lines[0]["decision"] == "allow"
    assert lines[0]["run_id"] == "r5" and lines[0]["trace_id"] == "t5"


def test_writegate_restricted_not_leaked(monkeypatch, tmp_path):
    from packages.core import memory as mem
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(mem, "trace_recorder", rec)
    gate = WriteGate(InMemoryRepository())
    gate.evaluate(MemoryCandidate(proposed_record=_record(
        content="password=hunter2", sensitivity=SensitivityLevel.RESTRICTED)))
    lines = rec.lines_for()
    assert lines[0]["event"] == "memory_gate"
    assert lines[0]["decision"] == "require_confirmation"
    assert "hunter2" not in str(lines)


def test_writegate_explicit_records(monkeypatch, tmp_path):
    from packages.core import memory as mem
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(mem, "trace_recorder", rec)
    gate = WriteGate(InMemoryRepository())
    gate.evaluate_explicit(MemoryCandidate(proposed_record=_record()))
    lines = rec.lines_for()
    assert lines[0]["event"] == "memory_gate"
    assert lines[0]["decision"] == "allow"


# ---------------- Delivery ----------------

class FakeAdapter:
    def __init__(self, ok=True):
        self.ok = ok

    async def send(self, platform, conversation_id, result):
        return DeliveryReceipt(
            run_id=result.run_id,
            status=DeliveryStatus.SUCCEEDED if self.ok else DeliveryStatus.FAILED,
        )


@pytest.mark.asyncio
async def test_delivery_records_skipped(monkeypatch, tmp_path):
    from packages.core import delivery as dv
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(dv, "trace_recorder", rec)
    mgr = dv.DeliveryManager(FakeAdapter())
    result = RuntimeResult(run_id="r6", outcome=RunOutcome.SUCCEEDED, trace_id="t6")
    receipt = await mgr.deliver(result, "qq", "c1")
    assert receipt.status == DeliveryStatus.SUCCEEDED
    lines = rec.lines_for()
    assert lines[0]["event"] == "delivery"
    assert lines[0]["run_id"] == "r6" and lines[0]["skipped"] is True


@pytest.mark.asyncio
async def test_delivery_records_sent(monkeypatch, tmp_path):
    from packages.core import delivery as dv
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(dv, "trace_recorder", rec)
    mgr = dv.DeliveryManager(FakeAdapter())
    result = RuntimeResult(run_id="r7", outcome=RunOutcome.SUCCEEDED,
                           trace_id="t7",
                           final_response=FinalResponse(text="你好"))
    receipt = await mgr.deliver(result, "qq", "c1")
    assert receipt.status == DeliveryStatus.SUCCEEDED
    lines = rec.lines_for()
    assert lines[0]["event"] == "delivery"
    assert lines[0]["skipped"] is False and lines[0]["platform"] == "qq"


# ---------------- run_id/trace_id 贯穿（生产路径） ----------------

import types


def _load_plugin(tmp_path, monkeypatch):
    import importlib.util
    import types as _types
    from unittest import mock as _mock
    sys.path.insert(0, "/root/data/plugins/dududa20")
    spec = importlib.util.spec_from_file_location(
        "dududa_main_rid", "/root/data/plugins/dududa20/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    try:
        ctx = main.star.Context()
    except TypeError:
        ctx = _mock.Mock()
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    plugin = main.Main(ctx)
    plugin.enabled = True
    return plugin, main


class _FakeEvent:
    def __init__(self):
        self.message_obj = types.SimpleNamespace(group="g9", self_id="bot9")
        self._platform = "aiocqhttp"
        self.session_id = "s9"
        self.sender = types.SimpleNamespace(user_id="u9")
        self.message_str = "hello"
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return "u9"
    def get_platform_name(self): return self._platform
    def get_message_type(self): return "group_message"


@pytest.mark.asyncio
async def test_call_llm_threads_run_id(monkeypatch, tmp_path):
    from packages.application import dududa_core as core_mod
    from packages.router import router as router_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(core_mod, "trace_recorder", rec)
    monkeypatch.setattr(router_mod, "trace_recorder", rec)
    plugin, _ = _load_plugin(tmp_path, monkeypatch)
    provider = StubProvider()
    cfg = RouterConfig(roles={ModelRole.RESPONSE_COMPOSITION: ModelConfig(
        role=ModelRole.RESPONSE_COMPOSITION, model_id="m-a",
        fallback_model_id="m-b")})
    plugin._core._model_router = ModelRouter(config=cfg, provider=provider)
    plugin._core._llm_provider = provider
    reply = await plugin._call_llm("你是嘟嘟哒", "你好", run_id="r8", trace_id="t8")
    assert reply
    lines = rec.lines_for()
    assert [l["event"] for l in lines] == ["model_request", "model_response"]
    assert lines[0]["run_id"] == "r8" and lines[0]["trace_id"] == "t8"
    assert lines[1]["run_id"] == "r8"


@pytest.mark.asyncio
async def test_store_memory_threads_run_id(monkeypatch, tmp_path):
    from packages.application import dududa_core as core_mod
    from packages.core import memory as mem_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(core_mod, "trace_recorder", rec)
    monkeypatch.setattr(mem_mod, "trace_recorder", rec)
    plugin, _ = _load_plugin(tmp_path, monkeypatch)
    plugin._store_memory(_FakeEvent(), "hello trace memory",
                         run_id="r9", trace_id="t9")
    lines = rec.lines_for()
    assert lines and lines[0]["event"] == "memory_gate"
    assert lines[0]["decision"] == "allow"
    assert lines[0]["run_id"] == "r9" and lines[0]["trace_id"] == "t9"


@pytest.mark.asyncio
async def test_call_vision_threads_run_id(monkeypatch, tmp_path):
    from packages.application import dududa_core as core_mod
    rec = TraceRecorder(tmp_path / "traces")
    monkeypatch.setattr(core_mod, "trace_recorder", rec)

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "看到一张图"}}]}

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _FakeResp()

    monkeypatch.setattr(core_mod, "httpx",
                        types.SimpleNamespace(AsyncClient=_FakeClient))
    plugin, _ = _load_plugin(tmp_path, monkeypatch)
    reply = await plugin._call_vision("描述图片", "这是什么", "b64", "image/png",
                                      run_id="r10", trace_id="t10")
    assert reply
    lines = rec.lines_for()
    kinds = [l["event"] for l in lines]
    assert "model_request" in kinds and "model_response" in kinds
    assert lines[0]["run_id"] == "r10"
    assert lines[1]["run_id"] == "r10"


def test_orchestrator_memory_candidates_carry_run_id():
    from packages.runtime.orchestrator import RuntimeOrchestrator
    from packages.core.state import RuntimeState, RuntimePhase, RunOutcome
    from packages.core.renderer import FinalResponse
    orch = RuntimeOrchestrator()
    state = RuntimeState(run_id="r11", trace_id="t11")
    state = state.transition(RuntimePhase.READY_TO_EMIT,
                             outcome=RunOutcome.SUCCEEDED,
                             final_response=FinalResponse(text="你好"))
    cands = orch._build_memory_candidates(state)
    assert cands
    assert all(c.metadata.get("run_id") == "r11" for c in cands)
    assert all(c.metadata.get("trace_id") == "t11" for c in cands)
