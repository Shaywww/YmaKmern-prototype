# -*- coding: utf-8 -*-
"""Unit tests for new skills: weather / news / translate (mocked, no network)."""
import pytest

from dududa.mcp.registry import create_all_services, register_all_mcp_services
from dududa.mcp.weather_service import WeatherService
from dududa.mcp.news_service import NewsService
from dududa.mcp.translate_service import TranslateService
from dududa.core.capability import CapabilityRegistry


class TestRegistered:
    def test_services_in_registry(self):
        svcs = create_all_services()
        for sid in ("weather", "news", "translate"):
            assert sid in svcs, f"{sid} missing from create_all_services"

    def test_capabilities_registered(self):
        from dududa.core.capability import CapabilityRisk
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        ids = {cc.capability.capability_id for cc in reg.filter_candidates(
            permissions=(), max_count=64,
            risk_tolerance=CapabilityRisk.DANGEROUS)}
        for cid in ("mcp.weather", "mcp.news", "mcp.translate"):
            assert cid in ids, f"{cid} not registered"

    def test_services_read_only_risk(self):
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        caps = {cc.capability.capability_id: cc.capability for cc in reg.filter_candidates(permissions=(), max_count=64)}
        assert caps["mcp.weather"].risk.value == "read_only"
        assert caps["mcp.news"].risk.value == "read_only"
        assert caps["mcp.translate"].risk.value == "read_only"


class TestWeatherService:
    @pytest.mark.asyncio
    async def test_empty_city_fails(self):
        r = await WeatherService().search()
        assert r.success is False

    @pytest.mark.asyncio
    async def test_mock_mode(self, monkeypatch):
        svc = WeatherService()
        monkeypatch.setattr(svc.config, "mock_mode", True)
        r = await svc.search(city="合肥")
        assert r.success
        assert r.data["city"] == "合肥"
        assert "temp_c" in r.data


class TestNewsService:
    @pytest.mark.asyncio
    async def test_mock_mode(self, monkeypatch):
        svc = NewsService()
        monkeypatch.setattr(svc.config, "mock_mode", True)
        r = await svc.search(limit=5)
        assert r.success
        assert r.data and "title" in r.data[0]

    @pytest.mark.asyncio
    async def test_limit_clamped(self, monkeypatch):
        svc = NewsService()
        monkeypatch.setattr(svc.config, "mock_mode", True)
        r = await svc.search(limit=999)
        assert r.success


class TestTranslateService:
    @pytest.mark.asyncio
    async def test_empty_text_fails(self):
        r = await TranslateService().search()
        assert r.success is False

    @pytest.mark.asyncio
    async def test_mock_mode(self, monkeypatch):
        svc = TranslateService()
        monkeypatch.setattr(svc.config, "mock_mode", True)
        r = await svc.search(text="hello")
        assert r.success
        assert r.data["translation"]

    def test_detect_target(self):
        assert TranslateService._detect_target("你好世界") == "en"
        assert TranslateService._detect_target("hello world") == "zh"