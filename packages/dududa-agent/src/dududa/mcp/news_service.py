# -*- coding: utf-8 -*-
"""News MCP service —— 国内科技/时政 RSS 聚合（无需 API key）。"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from .base import BaseMCPService, CachePolicy, MCPServiceConfig, ServiceResult

_FEEDS = (
    ("36氪", "https://36kr.com/feed"),
    ("IT之家", "https://www.ithome.com/rss/"),
    ("澎湃", "https://www.thepaper.cn/rss_newsDetail_1000"),
    ("少数派", "https://sspai.com/feed"),
)
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"


class NewsService(BaseMCPService):
    """最新资讯聚合：36氪 / IT之家 / 澎湃 / 少数派，关键词过滤，按时间倒序。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="news",
            description="Latest news aggregation from Chinese tech/news RSS feeds (36kr, ithome, thepaper, sspai)",
            cache_policy=CachePolicy.SHORT,
            timeout_seconds=12.0,
            max_retries=1,
            mock_mode=False,
        ))

    async def _fetch_live(self, **kwargs) -> list[dict]:
        q = str(kwargs.get("q") or kwargs.get("keyword") or "").strip().lower()
        limit = max(1, min(int(kwargs.get("limit", 10)), 20))
        items: list[dict] = []
        for name, url in _FEEDS:
            try:
                async with httpx.AsyncClient(timeout=8.0,
                                             follow_redirects=True) as client:
                    resp = await client.get(
                        url, headers={"User-Agent": _USER_AGENT})
                    resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    desc = (item.findtext("description") or "")
                    pub = (item.findtext("pubDate") or "").strip()
                    if not title:
                        continue
                    if q and q not in title.lower() and q not in desc.lower():
                        continue
                    items.append({
                        "title": title[:120],
                        "link": link,
                        "source": name,
                        "pub": pub,
                    })
            except Exception:
                continue
        items.sort(key=lambda it: it["pub"], reverse=True)
        return items[:limit]

    def _get_mock(self, **kwargs) -> list[dict]:
        return [{
            "title": "Mock news for dududa",
            "link": "https://example.com/",
            "source": "mock",
            "pub": "",
        }]

    async def search(self, q: str = "", keyword: str = "", limit: int = 10):
        q = (q or keyword or "").strip()
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 10
        return await self.query(cache_key=(q or "_all"), q=q, limit=limit)