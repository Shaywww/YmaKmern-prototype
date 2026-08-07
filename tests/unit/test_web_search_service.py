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

    @pytest.mark.asyncio
    async def test_search_keyword_alias(self, monkeypatch):
        captured = {}

        async def fake_query(cache_key=None, **kwargs):
            captured["cache_key"] = cache_key
            captured["kwargs"] = kwargs
            return ws.ServiceResult.ok([], "live")

        svc = ws.WebSearchService()
        svc.query = fake_query
        r = await svc.search(keyword="USTC")
        assert r.success
        assert captured["kwargs"]["q"] == "USTC"

    def test_mock_placeholder(self):
        svc = ws.WebSearchService()
        data = svc._get_mock(q="ustc")
        assert data[0]["link"].startswith("https://")


class TestLeakCleanup:
    """回复泄漏兜底：工具名/原始 JSON 必须被截断。"""

    @staticmethod
    def _strip(text):
        from dududa.application import dududa_handlers as h
        return h._strip_tool_leak(text)

    def test_mcp_name_with_raw_dict(self):
        out = self._strip(
            "搜到啦！USTC就是中科大～ ^^ mcp.web_search: "
            "[{'title': '中国科学技术大学', 'link': 'https://www.ustc.edu.cn/'}]")
        assert "mcp.web_search" not in out
        assert "搜到啦" in out
        assert not out.rstrip().endswith("^")

    def test_tool_prefix_block(self):
        out = self._strip(
            "答案如下。\n[工具 mcp.course_schedule]:\n[{'course_id': 'CS2001'}]")
        assert "[工具" not in out
        assert "CS2001" not in out
        assert out.strip() == "答案如下"

    def test_bare_raw_dict(self):
        out = self._strip("参考：[{'title': 'x', 'link': 'y'}] 没了")
        assert "{'title'" not in out
        assert out == ""

    def test_dangling_source_intro_removed(self):
        out = self._strip(
            "搜到啦～超实用的～ ^^~ ^^  （来源：[{'title': '中国科学技术大学',"
            " 'link': 'https://www.ustc.edu.cn/'}]")
        assert "{" not in out
        assert "来源" not in out
        assert out.rstrip().endswith("超实用的")

    def test_mcp_name_equals_json(self):
        out = self._strip(
            "搜到啦～ ^^ 来源：mcp.web_search=[{'title': '中国科学技术大学',"
            " 'link': 'https://www.ustc.edu.cn/'}]")
        assert "mcp.web_search" not in out
        assert "{" not in out
        assert "来源" not in out
        assert out == "搜到啦"

    def test_dangling_source_with_tool_name(self):
        out = self._strip(
            "嗯嗯，就是这样～ （来源：mcp.web_search=[{'title': 'x', 'link': 'y'}]")
        assert "mcp.web_search" not in out
        assert "来源" not in out
        assert out == "嗯嗯，就是这样"

    def test_source_intro_with_equals_and_space(self):
        out = self._strip(
            "查到了：\n\n来源：mcp.web_search = [{'title': 'x', 'link': 'y'}]")
        assert "mcp.web_search" not in out
        assert "来源" not in out
        assert "{" not in out
        assert out == "查到了"

    def test_mcp_dict_form(self):
        out = self._strip(
            "结果：mcp.web_search:{'title': 'x', 'link': 'y'}")
        assert "mcp.web_search" not in out
        assert "{" not in out
        assert out == ""

    def test_direct_bracket_after_tool_name(self):
        out = self._strip(
            "给你看：mcp.web_search[{'title': 'x', 'link': 'y'}]")
        assert "mcp.web_search" not in out
        assert "{" not in out
        assert out == "给你看"

    def test_bare_tool_name_line(self):
        out = self._strip(
            "诶呀，短路了呢～\n参考：**mcp.web_search**")
        assert "mcp.web_search" not in out
        assert "**" not in out
        assert out == "诶呀，短路了呢"

    def test_inline_bare_tool_name(self):
        out = self._strip("这是mcp.web_search查到的结果哦")
        assert "mcp.web_search" not in out
        assert out == "这是查到的结果哦"

    def test_bare_tool_name_with_source_intro(self):
        out = self._strip("好哦～ 来源：mcp.web_search")
        assert "mcp.web_search" not in out
        assert "来源" not in out
        assert out == "好哦"

    def test_normal_reply_untouched(self):
        text = "嘿嘿，USTC就是中国科学技术大学哦～在安徽合肥，1958年创办的！"
        assert self._strip(text) == text


class TestFormatToolData:
    def test_list_of_dicts_readable(self):
        from dududa.application.dududa_prod import _ProdOrchestrator
        data = [{"title": "中国科学技术大学", "link": "https://www.ustc.edu.cn/",
                 "snippet": "官方主页"}]
        out = _ProdOrchestrator._format_tool_data(data)
        assert "中国科学技术大学" in out
        assert "https://www.ustc.edu.cn/" in out
        assert "{'title'" not in out

    def test_non_list_fallback(self):
        from dududa.application.dududa_prod import _ProdOrchestrator
        assert _ProdOrchestrator._format_tool_data("plain") == "plain"


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
