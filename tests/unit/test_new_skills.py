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

class TestProdEnrichArgs:
    """生产 _enrich_plan_args：天气城市 / 新闻话题 / 翻译文本提取。"""

    def _enrich(self, text, cap_id):
        from dududa.planner.planner import GeneratedPlan, PlannedStep
        from dududa.application.dududa_prod import _ProdOrchestrator
        plan = GeneratedPlan(
            goal=text,
            steps=(PlannedStep(step_id="s1", capability_id=cap_id,
                               arguments={"action": "search", "q": "{query}"},
                               purpose="x"),),
            rationale="Pattern: test")
        return _ProdOrchestrator._enrich_plan_args(plan, text)

    def test_weather_city_extraction(self):
        plan = self._enrich("临泽县今天天气怎么样", "mcp.weather")
        assert plan.steps[0].arguments["q"] == "临泽县"

    def test_weather_default_city(self):
        plan = self._enrich("今天天气怎么样", "mcp.weather")
        assert plan.steps[0].arguments["q"] == "合肥"

    def test_news_topic_extraction(self):
        plan = self._enrich("科技方面有什么新闻", "mcp.news")
        assert plan.steps[0].arguments["q"] == "科技"

    def test_news_no_keyword_means_all(self):
        plan = self._enrich("有什么新闻", "mcp.news")
        assert plan.steps[0].arguments["q"] == ""

    def test_translate_text_extraction(self):
        plan = self._enrich("翻译一下 hello world", "mcp.translate")
        assert plan.steps[0].arguments["text"] == "hello world"

    def test_translate_ba_construction(self):
        plan = self._enrich("把你好翻译成英文", "mcp.translate")
        assert plan.steps[0].arguments["text"] == "你好"
        assert plan.steps[0].arguments["target"] == "en"

    def test_translate_ba_to_chinese(self):
        plan = self._enrich("把hello world翻译成中文", "mcp.translate")
        assert plan.steps[0].arguments["text"] == "hello world"
        assert plan.steps[0].arguments["target"] == "zh"

    def test_web_search_keyword_injected(self):
        plan = self._enrich("帮我搜一下USTC", "mcp.web_search")
        assert plan.steps[0].arguments["keyword"] == "USTC"


class TestSkillPerception:
    """_perceive：天气/新闻/翻译/百科话题 -> needs_tools=True。"""

    def _perceive(self, text):
        from dududa.application.dududa_core import DududaCore
        from unittest import mock
        core = DududaCore.__new__(DududaCore)
        core._input_adapter = mock.Mock()
        pre = mock.Mock()
        pre.combined_text = text
        core._input_adapter.to_preprocessed.return_value = pre
        return core._perceive(mock.Mock())

    def test_weather_needs_tools(self):
        assert self._perceive("今天天气怎么样").needs_tools is True

    def test_news_needs_tools(self):
        assert self._perceive("有什么新闻").needs_tools is True

    def test_translate_needs_tools(self):
        assert self._perceive("翻译一下 hello world").needs_tools is True

    def test_admission_needs_tools(self):
        assert self._perceive("USTC今年招生怎么样").needs_tools is True

    def test_greeting_no_tools(self):
        assert self._perceive("你好").needs_tools is False
