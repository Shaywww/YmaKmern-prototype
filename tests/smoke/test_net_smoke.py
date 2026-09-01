#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实网络 smoke 层（文档 2.5.10 Phase 9）：与阻塞 CI 分开。

默认 pytest 收集时会被排除（pyproject addopts 含 -m "not net"）；
仅显式 -m net 时运行：
    cd <repository-root>
    bash ops/smoke_net.sh    # 推荐：自动从 systemd 注入生产密钥
    python3.12 -m pytest tests/smoke -m net -q --tb=short
"""
import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from unittest import mock

import httpx
import pytest
from tests.path_config import AGENT_SRC, PLUGIN_DIR
from dududa.mcp.course_schedule import CourseScheduleService
from dududa.mcp.weather_service import WeatherService

pytestmark = pytest.mark.net

PROTO = AGENT_SRC
PLUGIN = PLUGIN_DIR


def _load_plugin():
    """生产插件装配（与 verify_*.py 相同的加载模式）。"""
    for p in (str(PROTO), str(PLUGIN)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "net_smoke_main", str(PLUGIN / "main.py"))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    try:
        ctx = main.star.Context()
    except TypeError:
        ctx = mock.Mock()
    return main, main.Main(ctx)


def _has_keys() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def test_gateway_reachable_deepseek():
    """主网关 HTTPS 可达（真实网络）。"""
    if not _has_keys():
        pytest.skip("DEEPSEEK_API_KEY 未注入（请用 ops/smoke_net.sh 运行）")
    r = httpx.get("https://api.deepseek.com", timeout=8)
    assert r.status_code < 500, f"主网关异常: HTTP {r.status_code}"


def test_gateway_reachable_fallback():
    """降级网关 HTTPS 可达（真实网络）。"""
    r = httpx.get("https://www.mhcoding.ai", timeout=8,
                  follow_redirects=True)
    assert r.status_code < 500, f"降级网关异常: HTTP {r.status_code}"


def test_production_llm_roundtrip():
    """生产 Router 真实 LLM 往返：deepseek 主路径，非空回复 + 限时。"""
    if not _has_keys():
        pytest.skip("DEEPSEEK_API_KEY 未注入（请用 ops/smoke_net.sh 运行）")
    main_mod, p = _load_plugin()
    assert p._model_router is not None, "生产 Router 未装配"

    async def _go():
        t0 = time.monotonic()
        reply = await p._call_llm(
            "You are a smoke-test assistant. Reply with exactly: PONG",
            "ping", max_tokens=16, temperature=0,
            run_id="net-smoke", trace_id="net-smoke", skip_render=True)
        return reply, time.monotonic() - t0

    reply, latency = asyncio.run(_go())
    assert isinstance(reply, str) and reply.strip(), "LLM 返回为空"
    assert latency < 60, f"LLM 往返过慢: {latency:.1f}s"

    # 证明走的是 Router 主模型而不是 fallback（degraded 才切 fallback）
    assert "PONG" in reply.upper(), f"回复内容不符合 smoke 期望: {reply[:80]!r}"



def test_production_clock_capability():
    """进程内真实能力（Clock）经生产装配调用（离线但走生产路径）。"""
    main_mod, p = _load_plugin()
    cap = p.cap_registry.get("mcp.clock")
    assert cap is not None, "mcp.clock 未注册"
    provider = p.cap_registry.get_provider("mcp.clock")
    assert provider is not None and provider.health(), "mcp.clock provider 不健康"

    async def _go():
        obs = await provider.execute(cap, {"action": "get_date"})
        return obs

    obs = asyncio.run(_go())
    assert obs.success, f"Clock 调用失败: {obs.error}"
    text = str(obs.data)
    assert "星期" in text, f"Clock 结果异常: {text[:80]!r}"


def test_production_course_capability_health():
    """课程类能力注册/健康检查（不发起真实抓取，避免副作用）。"""
    main_mod, p = _load_plugin()
    caps = {c.capability_id: c for c in p.cap_registry.list_enabled()}
    for want in ("mcp.course_schedule", "mcp.exam_schedule",
                 "mcp.academic_calendar", "mcp.academic_affairs"):
        assert want in caps, f"{want} 未注册"
        assert caps[want].is_healthy, f"{want} 不健康"


def test_live_course_snapshot_query(tmp_path):
    """公开课程快照真实联网查询；仅在显式 net smoke 中运行。"""
    svc = CourseScheduleService(cache_dir=tmp_path)
    result = asyncio.run(svc.search("数据结构"))
    assert result.success, f"课程快照查询失败: {result.error}"
    assert result.data, "课程快照返回为空"
    assert any(item.get("course_name") == "数据结构" for item in result.data)


def test_live_weather_query():
    """wttr.in 真实联网查询；仅在显式 net smoke 中运行。"""
    result = asyncio.run(WeatherService().search(city="临泽县"))
    assert result.success, f"天气查询失败: {result.error}"
    assert result.data.get("query_city") == "临泽县"
    assert result.data.get("temp_c") not in (None, "")
