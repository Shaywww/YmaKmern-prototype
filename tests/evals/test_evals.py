# -*- coding: utf-8 -*-
"""版本化 Eval：五组件 fixture + 阈值（文档 2.5.10 / Phase 9 前半）。"""
import sys
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

import pytest

from tests.evals import evals

_THRESHOLDS = evals.load_thresholds()


def _assert_ok(component, metric):
    ok, failures = evals.check(component, metric, _THRESHOLDS)
    assert ok, f"{component} eval failed: {failures}"


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
