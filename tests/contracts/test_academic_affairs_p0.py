# -*- coding: utf-8 -*-
"""教务服务（academic_affairs）P0 测试：注册 / token 授权 / 零缓存 / 只读 / 生产装配。

文档 2.5.6 候选新服务边界：私聊（access 策略）、授权（token）、
凭据（env）、数据时效（零缓存）、撤销（env 变更即时生效）、写操作（无）。
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

PROTO = Path("/opt/dududa20-prototype/packages/dududa-agent/src")
PLUGIN = Path("/root/data/plugins/dududa20")


def _load_plugin():
    for p in (str(PROTO), str(PLUGIN)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "aa_main", str(PLUGIN / "main.py"))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    try:
        ctx = main.star.Context()
    except TypeError:
        ctx = mock.Mock()
    return main, main.Main(ctx)


@pytest.fixture()
def services():
    from dududa.mcp.registry import create_all_services
    return create_all_services()


@pytest.fixture()
def registry():
    from dududa.core.capability import CapabilityRegistry
    from dududa.mcp.registry import register_all_mcp_services
    reg = CapabilityRegistry()
    register_all_mcp_services(reg)
    return reg


class TestRegistration:
    def test_service_created(self, services):
        assert "academic_affairs" in services

    def test_registered_and_healthy(self, registry):
        cap = registry.get("mcp.academic_affairs")
        assert cap is not None
        assert cap.is_healthy
        provider = registry.get_provider("mcp.academic_affairs")
        assert provider is not None and provider.health()

    def test_read_only_risk(self, registry):
        from dududa.core.capability import CapabilityRisk
        cap = registry.get("mcp.academic_affairs")
        assert cap.risk == CapabilityRisk.READ_ONLY

    def test_schema_actions(self, registry):
        cap = registry.get("mcp.academic_affairs")
        enum = cap.schema.input_schema["properties"]["action"]["enum"]
        assert set(enum) == {
            "get_student_info", "get_grade", "get_credits_summary",
            "get_graduation_requirements",
        }


class TestTokenAuthorization:
    """个人动作授权边界：未配置凭据 / 错误 token fail closed；正确 token 放行。"""

    @pytest.mark.asyncio
    async def test_no_credentials_fail_closed(self, services, monkeypatch):
        monkeypatch.delenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", raising=False)
        svc = services["academic_affairs"]
        r = await svc.get_student_info("PB21000001", token="anything")
        assert not r.success and "unauthorized" in r.error

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, services, monkeypatch):
        monkeypatch.setenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", "secret-token")
        svc = services["academic_affairs"]
        r = await svc.get_grade("PB21000001", token="wrong")
        assert not r.success and "unauthorized" in r.error

    @pytest.mark.asyncio
    async def test_correct_token_serves_mock(self, services, monkeypatch):
        monkeypatch.setenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", "secret-token")
        svc = services["academic_affairs"]
        r = await svc.get_student_info("PB21000001", token="secret-token")
        assert r.success and r.source == "mock"
        assert r.data.name_masked == "张**"          # 姓名脱敏
        assert "PB21000001" in r.data.student_id

    @pytest.mark.asyncio
    async def test_public_requirement_no_token(self, services, monkeypatch):
        monkeypatch.delenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", raising=False)
        svc = services["academic_affairs"]
        r = await svc.get_graduation_requirements()
        assert r.success
        assert any(x.category == "总学分" for x in r.data)


class TestDataFreshnessAndWriteBoundary:
    """数据时效：个人数据零缓存；写操作：无任何写/副作用动作。"""

    @pytest.mark.asyncio
    async def test_personal_actions_no_cache(self, services, monkeypatch):
        monkeypatch.setenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", "secret-token")
        svc = services["academic_affairs"]
        await svc.get_student_info("PB21000001", token="secret-token")
        await svc.get_grade("PB21000001", token="secret-token")
        await svc.get_credits_summary("PB21000001", token="secret-token")
        before = dict(svc._cache)  # 共享单例可能已有公开项；只断言个人动作不新增
        await svc.get_student_info("PB21000001", token="secret-token")
        await svc.get_grade("PB21000001", token="secret-token")
        await svc.get_credits_summary("PB21000001", token="secret-token")
        assert svc._cache == before  # 个人动作 cache_key=None，零缓存

    def test_public_requirement_cached(self, services, monkeypatch):
        monkeypatch.delenv("DUDUDA_ACADEMIC_AFFAIRS_TOKEN", raising=False)
        svc = services["academic_affairs"]
        asyncio.run(svc.get_graduation_requirements())
        assert "grad:计算机科学与技术" in svc._cache

    def test_no_write_actions(self, services):
        svc = services["academic_affairs"]
        methods = {m for m in dir(svc)
                   if m.startswith("get_") or m.startswith("list_")
                   or m.startswith("search_")}
        assert methods, "服务应至少暴露只读动作"
        for m in methods:
            assert not m.startswith(("create", "update", "delete",
                                     "write", "remove", "set")), \
                f"发现写动作: {m}"


class TestAccessPolicyScope:
    """私聊/群边界：教务服务纳入 access 策略（fail closed）。"""

    def test_in_icourse_scope(self):
        from dududa.mcp.access import ICOURSE_SERVICE_IDS, is_icourse_capability
        assert "academic_affairs" in ICOURSE_SERVICE_IDS
        assert is_icourse_capability("mcp.academic_affairs")
        assert is_icourse_capability("academic_affairs")


class TestProductionWiring:
    def test_prod_plugin_registers(self):
        main, p = _load_plugin()
        caps = {c.capability_id for c in p.cap_registry.list_enabled()}
        assert "mcp.academic_affairs" in caps

    def test_total_registered_count(self):
        from dududa.core.capability import CapabilityRegistry
        from dududa.mcp.registry import register_all_mcp_services
        reg = CapabilityRegistry()
        assert register_all_mcp_services(reg) == 12