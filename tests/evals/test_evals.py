# -*- coding: utf-8 -*-
"""版本化 Eval：五组件 fixture + 阈值（文档 2.5.10 / Phase 9 前半）。"""
import sys

import pytest

from tests.evals import evals

_THRESHOLDS = evals.load_thresholds()


def _assert_ok(component, metric):
    result = evals.check(component, metric, _THRESHOLDS)
    if result.status == "skip":
        pytest.skip("; ".join(result.skipped))
    assert result.ok, f"{component} eval failed: {result.failures}"


def test_statistical_threshold_skips_insufficient_samples():
    result = evals.check("demo", {"cases": 2, "accuracy": 0.0}, {
        "demo": {"accuracy": {
            "class": "statistical", "gte": 0.95, "min_samples": 8,
        }},
    })
    assert result.status == "skip"
    assert result.failures == ()


def test_deterministic_threshold_never_relaxes():
    result = evals.check("demo", {"safety": 0.99}, {
        "demo": {"safety": {"class": "deterministic", "gte": 1.0}},
    })
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_perception_eval():
    _assert_ok("perception", evals.run_perception())


@pytest.mark.asyncio
async def test_social_decision_eval():
    _assert_ok("social_decision", evals.run_social_decision())


@pytest.mark.asyncio
async def test_social_decision_policy_eval():
    _assert_ok("social_decision_policy", evals.run_social_decision_policy())


@pytest.mark.asyncio
async def test_tool_runtime_eval():
    _assert_ok("tool_runtime", await evals.run_tool_runtime())


@pytest.mark.asyncio
async def test_capability_retrieval_eval():
    _assert_ok("capability_retrieval", evals.run_capability_retrieval())


@pytest.mark.asyncio
async def test_memory_writegate_eval():
    _assert_ok("memory_writegate", evals.run_memory_writegate())


@pytest.mark.asyncio
async def test_oc_render_eval():
    _assert_ok("oc_render", evals.run_oc_render())
