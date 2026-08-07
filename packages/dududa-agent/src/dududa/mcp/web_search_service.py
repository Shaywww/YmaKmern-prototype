"""Web search MCP service —— Bing RSS 实时联网搜索，无需 API key（只读）。"""
from __future__ import annotations

import html as _html
import re as _re
import xml.etree.ElementTree as ET

import httpx

from .base import BaseMCPService, CachePolicy, MCPServiceConfig, ServiceResult

_BING_HOSTS = ("cn.bing.com", "www.bing.com")  # 主备源：cn 间歇降级时换国际版
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_RESULTS = 8
_TITLE_LIMIT = 160
_SNIPPET_LIMIT = 300


def _strip_html(text: str) -> str:
    """去掉 RSS description 里的 HTML 标签并反转义实体。"""
    text = _re.sub(r"<[^>]+>", "", text or "")
    return _html.unescape(text).strip()


class WebSearchService(BaseMCPService):
    """实时联网搜索（Bing RSS）。无密钥、短缓存、熔断保护。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="web_search",
            description="Web search returning top ranked results with titles, links and snippets",
            cache_policy=CachePolicy.SHORT,
            timeout_seconds=10.0,
            max_retries=2,
            mock_mode=False,
        ))

    def _looks_relevant(self, results: list[dict], q: str) -> bool:
        """结果与 query 是否相关（启发式）：query 核心词出现在任一结果里。"""
        toks = _re.findall(r"[A-Za-z]{2,}", q)
        cjk = [c[:4] for c in _re.findall(r"[\u4e00-\u9fff]{2,}", q)]
        if not toks and not cjk:
            return True
        hay = " ".join(
            (r.get("title") or "") + " " + (r.get("snippet") or "")
            for r in results).upper()
        return any(t.upper() in hay for t in toks) or any(c in hay for c in cjk)

    async def _fetch_live(self, **kwargs) -> list[dict]:
        q = str(kwargs.get("q", "")).strip()
        max_results = max(1, min(int(kwargs.get("max_results", 5)), _MAX_RESULTS))
        if not q:
            return []
        last_results: list[dict] = []
        last_err: Exception | None = None
        for host in _BING_HOSTS:
            try:
                async with httpx.AsyncClient(
                        timeout=self.config.timeout_seconds,
                        follow_redirects=True) as client:
                    resp = await client.get(
                        f"https://{host}/search",
                        params={"q": q, "format": "rss"},
                        headers={"User-Agent": _USER_AGENT,
                                 "Accept": "application/rss+xml, application/xml, */*"},
                    )
                    resp.raise_for_status()
                    payload = resp.text
                root = ET.fromstring(payload)
                results = []
                for item in root.iter("item"):
                    title = _strip_html(item.findtext("title"))
                    link = (item.findtext("link") or "").strip()
                    snippet = _strip_html(item.findtext("description"))
                    if not title and not link:
                        continue
                    results.append({
                        "title": title[:_TITLE_LIMIT],
                        "link": link,
                        "snippet": snippet[:_SNIPPET_LIMIT],
                    })
                    if len(results) >= max_results:
                        break
                if not results:
                    continue
                if self._looks_relevant(results, q) or host == _BING_HOSTS[-1]:
                    return results
                last_results = results
            except Exception as exc:  # 单源失败/超时换备用源
                last_err = exc
                continue
        if last_results:
            return last_results
        if last_err is not None:
            raise last_err
        return []

    def _get_mock(self, **kwargs) -> list[dict]:
        q = str(kwargs.get("q", "")).strip() or "dududa"
        return [{
            "title": f"Mock result for {q}",
            "link": "https://example.com/",
            "snippet": "mock placeholder",
        }]

    async def search(self, q: str = "", keyword: str = "",
                   max_results: int = 5) -> ServiceResult:
        q = (q or keyword or "").strip()
        if not q:
            return ServiceResult.fail("empty query")
        try:
            max_results = max(1, min(int(max_results), _MAX_RESULTS))
        except (TypeError, ValueError):
            max_results = 5
        return await self.query(cache_key=q.lower(), q=q, max_results=max_results)
