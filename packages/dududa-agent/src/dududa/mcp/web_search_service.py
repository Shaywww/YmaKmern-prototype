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


# 视频站：无视频意图时降权（避免「搜USTC」返回腾讯视频）
_VIDEO_HOST_MARKERS = (
    "v.qq.com", "bilibili.com", "youku.com", "iqiyi.com", "douyin.com",
    "kuaishou.com", "youtube.com", "m1905.com", "mgtv.com", "sohu.com/v",
    "163.com/video", "v.163.com",
)
_VIDEO_QUERY_HINTS = ("视频", "电影", "番剧", "电视剧", "看", "直播", "预告", "片源", "怎么演")
# 权威/官方来源加权
_BOOST_HOST_MARKERS = (
    "wikipedia.org", "baike.baidu.com", ".edu.cn", ".gov.cn", "zhihu.com",
    "ustc.edu.cn", "docs.mmdustc.top", "qq.com/qqcom", "people.com.cn",
    "xinhuanet.com", "chinanews.com.cn",
)
# 内容子域（news./m./en./bbs./...）：主站是权威答案，查询未点名子域时降权
_CONTENT_SUBDOMAIN_MARKERS = (
    "news.", "m.", "en.", "bbs.", "blog.", "forum.", "wap.", "tv.",
    "mobile.", "video.",
)


def _rank_results(results: list[dict], q: str) -> list[dict]:
    """相关性重排：视频站降权（非视频意图）、权威站加权、同域去重。"""
    ql = (q or "").lower()
    want_video = any(h in ql for h in _VIDEO_QUERY_HINTS)
    toks = _re.findall(r"[A-Za-z]{2,}", q)
    cjk = [c for c in _re.findall(r"[\u4e00-\u9fff]{2,}", q)][:4]
    scored: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for r in results:
        link = (r.get("link") or "").lower()
        domain = _re.sub(r"^https?://", "", link).split("/")[0]
        if domain in seen:
            continue
        seen.add(domain)
        score = 0
        if not want_video and any(m in link for m in _VIDEO_HOST_MARKERS):
            score -= 50
        if any(m in link for m in _BOOST_HOST_MARKERS):
            score += 10
        if any(domain.startswith(m) for m in _CONTENT_SUBDOMAIN_MARKERS):
            marker = next(m for m in _CONTENT_SUBDOMAIN_MARKERS
                          if domain.startswith(m))
            if marker.strip(".") not in ql:
                score -= 8
        hay = ((r.get("title") or "") + " " + (r.get("snippet") or "")).lower()
        for t in toks:
            if t.lower() in hay:
                score += 3
        for c in cjk:
            if c in hay:
                score += 5
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


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
                    if len(results) >= _MAX_RESULTS:
                        break
                results = _rank_results(results, q)[:max_results]
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
