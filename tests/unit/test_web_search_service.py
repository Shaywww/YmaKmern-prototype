"""Unit tests for web search MCP service (mocked live fetch, no real network)."""
import pytest

from dududa.mcp import web_search_service as ws
from dududa.mcp.registry import create_all_services, register_all_mcp_services
from dududa.core.capability import CapabilityRegistry


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self._seen = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, headers=None):
        self._seen["url"] = url
        self._seen["params"] = params
        self._seen["headers"] = headers
        rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>中国科学技术大学</title><link>https://www.ustc.edu.cn/</link>
<description>中国科学技术大学&lt;b&gt;官方&lt;/b&gt;网站 &amp;amp; 主页</description></item>
<item><title>USTC News</title><link>https://news.ustc.edu.cn/</link>
<description>Latest &lt;i&gt;news&lt;/i&gt; from USTC</description></item>
<item><title>Third</title><link>https://example.com/3</link><description>three</description></item>
</channel></rss>"""
        return _FakeResp(rss)


class TestStripHtml:
    def test_tags_and_entities(self):
        assert ws._strip_html("<b>中国科学技术大学</b> &amp; more") == "中国科学技术大学 & more"

    def test_none_safe(self):
        assert ws._strip_html(None) == ""


class TestWebSearchService:
    @pytest.mark.asyncio
    async def test_empty_query_fails(self):
        svc = ws.WebSearchService()
        r = await svc.search("   ")
        assert not r.success
        assert "empty" in (r.error or "")

    @pytest.mark.asyncio
    async def test_search_clamps_max_results(self):
        svc = ws.WebSearchService()
        assert svc.config.mock_mode is False
        r = await svc.search("ustc", max_results=999)
        assert not r.success or len(r.data) <= ws._MAX_RESULTS

    @pytest.mark.asyncio
    async def test_fetch_live_parses_bing_rss(self, monkeypatch):
        monkeypatch.setattr(ws.httpx, "AsyncClient", _FakeClient)
        svc = ws.WebSearchService()
        results = await svc._fetch_live(q="ustc", max_results=5)
        assert len(results) == 3
        assert results[0]["title"] == "中国科学技术大学"
        assert results[0]["link"] == "https://www.ustc.edu.cn/"
        assert "官方" in results[0]["snippet"]
        assert results[0]["snippet"].endswith("& 主页")
        assert results[1]["title"] == "USTC News"

    @pytest.mark.asyncio
    async def test_fetch_live_max_results_cap(self, monkeypatch):
        monkeypatch.setattr(ws.httpx, "AsyncClient", _FakeClient)
        svc = ws.WebSearchService()
        results = await svc._fetch_live(q="ustc", max_results=2)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_via_query_uses_cache_key(self, monkeypatch):
        captured = {}
        async def fake_query(cache_key=None, **kwargs):
            captured["cache_key"] = cache_key
            captured["kwargs"] = kwargs
            return ws.ServiceResult.ok([], "live")
        svc = ws.WebSearchService()
        svc.query = fake_query
        r = await svc.search("  USTC  ", max_results=3)
        assert r.success
        assert captured["cache_key"] == "ustc"
        assert captured["kwargs"]["q"] == "USTC"
        assert captured["kwargs"]["max_results"] == 3

    def test_mock_placeholder(self):
        svc = ws.WebSearchService()
        data = svc._get_mock(q="ustc")
        assert data[0]["link"].startswith("https://")


class TestRegistryIntegration:
    def test_service_registered_in_registry(self):
        svcs = create_all_services()
        assert "web_search" in svcs
        assert svcs["web_search"].config.mock_mode is False

    def test_capability_registered(self):
        reg = CapabilityRegistry()
        n = register_all_mcp_services(reg)
        caps = {c.capability_id for c in reg.list_enabled()}
        assert "mcp.web_search" in caps
        assert n >= 9
