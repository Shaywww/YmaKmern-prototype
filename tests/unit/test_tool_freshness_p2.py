import time

import pytest

from dududa.application.dududa_prod import _ProdOrchestrator
from dududa.core.capability import (
    Capability,
    CapabilityRegistry,
    CapProvider,
    ToolObservation,
    infer_data_timestamp,
)
from dududa.mcp.base import BaseMCPService, CachePolicy, MCPServiceConfig
from dududa.planner.executor import ExecutionContext, ToolExecutor
from dududa.planner.planner import GeneratedPlan, PlannedStep


def test_observation_freshness_and_calibrated_disclosure():
    now = 2_000_000.0
    stale = ToolObservation(
        step_id="s", capability_id="weather", success=True, data={},
        cached=True, data_timestamp=now - 7200, confidence=0.62)
    assert stale.freshness_seconds(now) == 7200
    status, disclose = _ProdOrchestrator._observation_status(stale, now=now)
    assert disclose
    assert "缓存" in status
    assert "2 小时前" in status
    assert "可靠性偏低" in status

    fresh = ToolObservation(
        step_id="s", capability_id="weather", success=True, data={},
        cached=False, data_timestamp=now - 60, confidence=0.95)
    status, disclose = _ProdOrchestrator._observation_status(fresh, now=now)
    assert not disclose
    assert "刚更新" in status


def test_timestamp_inference_understands_snapshot_metadata():
    ts = infer_data_timestamp({"items": [{"generated_at": "2026-08-30T12:00:00Z"}]})
    assert ts == 1788091200.0


class _CachedService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="cached", cache_policy=CachePolicy.MEDIUM,
            mock_mode=True))

    async def _fetch_live(self, **kwargs):
        return {"value": 1}

    def _get_mock(self, **kwargs):
        return {"value": 1}


@pytest.mark.asyncio
async def test_base_service_marks_cache_timestamp_and_confidence():
    service = _CachedService()
    first = await service.query(cache_key="one")
    second = await service.query(cache_key="one")
    assert not first.cached
    assert second.cached
    assert second.data_timestamp is not None
    assert second.confidence == 0.75


class _MetadataProvider(CapProvider):
    async def execute(self, capability, arguments):
        return ToolObservation(
            step_id="", capability_id=capability.capability_id,
            success=True, data={"answer": 42}, source="cache",
            cached=True, data_timestamp=time.time() - 3600,
            confidence=0.8, truncated=True)

    def health(self):
        return True


@pytest.mark.asyncio
async def test_executor_preserves_observation_metadata():
    registry = CapabilityRegistry()
    registry.register(Capability(capability_id="test.meta", name="meta",
                                 description="metadata"), _MetadataProvider())
    plan = GeneratedPlan(
        goal="test",
        steps=(PlannedStep(step_id="s1", capability_id="test.meta",
                           arguments={}, purpose="metadata"),),
    )
    results = await ToolExecutor(registry).execute_plan(
        plan, ExecutionContext(max_steps=1))
    result = results[0]
    assert result.success and result.cached and result.truncated
    assert result.source == "cache"
    assert result.data_timestamp is not None
    assert result.confidence == 0.8
