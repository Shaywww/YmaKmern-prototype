# -*- coding: utf-8 -*-
"""Weather MCP service —— wttr.in 实时天气（无需 API key，中文城市支持）。"""
from __future__ import annotations

import urllib.parse

import httpx

from .base import BaseMCPService, CachePolicy, MCPServiceConfig, ServiceResult

_WTTR_URL = "https://wttr.in/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class WeatherService(BaseMCPService):
    """实时天气 + 3 天预报（wttr.in，无密钥）。默认城市合肥。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="weather",
            description="Real-time weather and 3-day forecast for a city (Chinese cities supported, default Hefei)",
            cache_policy=CachePolicy.SHORT,
            timeout_seconds=10.0,
            max_retries=1,
            mock_mode=False,
        ))

    async def _fetch_live(self, **kwargs) -> dict:
        city = str(kwargs.get("city") or kwargs.get("q") or "").strip() or "合肥"
        url = _WTTR_URL + urllib.parse.quote(city) + "?format=j1&lang=zh"
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds,
                                     follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
        cur = (data.get("current_condition") or [{}])[0]
        desc = ((cur.get("weatherDesc") or [{}])[0].get("value", ""))
        days = []
        for d in (data.get("weather") or [])[1:4]:
            days.append({
                "date": d.get("date", ""),
                "min_c": d.get("mintempC", ""),
                "max_c": d.get("maxtempC", ""),
                "desc": ((d.get("weatherDesc") or [{}])[0].get("value", "")),
            })
        area = (data.get("nearest_area") or [{}])[0]
        return {
            "city": ((area.get("areaName") or [{}])[0].get("value", city)),
            "observed_at": cur.get("localObsDateTime", ""),
            "temp_c": cur.get("temp_C", ""),
            "feels_like_c": cur.get("FeelsLikeC", ""),
            "desc": desc,
            "humidity": cur.get("humidity", ""),
            "wind_kph": cur.get("windspeedKmph", ""),
            "forecast_3d": days,
        }

    def _get_mock(self, **kwargs) -> dict:
        return {
            "city": "合肥", "observed_at": "", "temp_c": "30", "feels_like_c": "32",
            "desc": "晴", "humidity": "60", "wind_kph": "10",
            "forecast_3d": [{"date": "2026-08-08", "min_c": "26", "max_c": "35", "desc": "晴"}],
        }

    async def search(self, city: str = "", q: str = ""):
        city = (city or q or "").strip()
        if not city:
            return ServiceResult.fail("empty city")
        return await self.query(cache_key=city, city=city)