"""Campus notice MCP service."""
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class CampusNotice:
    notice_id: str; title: str; source: str; date: str
    url: str = ""; summary: str = ""; category: str = ""; pinned: bool = False

class CampusNoticeService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(service_name="campus_notice", description="Query USTC campus notices", cache_policy=CachePolicy.SHORT))

    async def _fetch_live(self, **kwargs): raise NotImplementedError

    def _get_mock(self, **kwargs):
        source = kwargs.get("source", "")
        category = kwargs.get("category", "")
        keyword = kwargs.get("keyword", "")
        notices = [
            CampusNotice("N001","2026春季期末考试安排","教务处","2026-06-01","","第18-19周期末考试","教学",True),
            CampusNotice("N002","图书馆暑假开放时间调整","图书馆","2026-06-15","","暑假9:00-17:00","后勤"),
            CampusNotice("N003","大学生创新创业项目申报","教务处","2026-06-05","","截止7月15日","科研"),
            CampusNotice("N004","校园网升级维护通知","网络信息中心","2026-06-10","","6月15日23:00起维护","后勤"),
            CampusNotice("N005","2026届毕业生离校手续","学生处","2026-06-20","","7月1日前完成","学工"),
            CampusNotice("N006","挑战杯校内选拔赛通知","校团委","2026-06-08","","校内选拔报名开始","科研"),
            CampusNotice("N007","夏季学期选课通知","教务处","2026-06-25","","6月28日开放选课","教学",True),
            CampusNotice("N008","校园卡系统升级通知","一卡通中心","2026-06-12","","6月18日起升级","后勤"),
        ]
        results = notices
        if source: results = [n for n in results if n.source == source]
        if category: results = [n for n in results if n.category == category]
        if keyword:
            kw = keyword.lower()
            results = [n for n in results if kw in n.title.lower() or kw in n.summary.lower()]
        return results

    async def search(self, keyword: str = "", source: str = "", category: str = "") -> ServiceResult:
        return await self.query(cache_key=f"notice:{keyword}:{source}:{category}", keyword=keyword, source=source, category=category)

    async def get_pinned(self) -> ServiceResult:
        return await self.query(cache_key="notice:pinned", query_type="pinned")

    async def get_recent(self, days: int = 7) -> ServiceResult:
        return await self.query(cache_key=f"notice:recent:{days}", days=days)
