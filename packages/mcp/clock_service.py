"""Clock MCP service —— 系统时钟（当前北京时间/日期），不依赖 LLM 猜测。"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

_CST = timezone(timedelta(hours=8))
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class ClockService(BaseMCPService):
    """当前北京时间（UTC+8）与日期。实时数据，禁用缓存。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="clock",
            description="Current Beijing time (UTC+8) and date",
            cache_policy=CachePolicy.NONE))

    async def _fetch_live(self, **kwargs):
        return self._now_text(**kwargs)

    def _get_mock(self, **kwargs):
        return self._now_text(**kwargs)

    @staticmethod
    def _now_text(fmt: str = "full") -> str:
        now = datetime.now(_CST)
        if fmt == "date":
            return f"{now:%Y-%m-%d} {_WEEKDAYS[now.weekday()]}（北京时间）"
        if fmt == "time":
            return f"{now:%H:%M:%S}（北京时间 UTC+8）"
        return (f"{now:%Y-%m-%d %H:%M:%S} {_WEEKDAYS[now.weekday()]}"
                "（北京时间，UTC+8）")

    async def get_now(self, fmt: str = "full") -> ServiceResult:
        return await self.query(cache_key=None, fmt=fmt)

    async def get_date(self) -> ServiceResult:
        return await self.query(cache_key=None, fmt="date")

    async def get_time(self) -> ServiceResult:
        return await self.query(cache_key=None, fmt="time")
