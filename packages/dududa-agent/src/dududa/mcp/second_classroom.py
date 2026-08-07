"""Second classroom MCP service."""
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class Activity:
    activity_id: str; title: str; category: str; date: str; time: str
    location: str; organizer: str; description: str = ""
    registration_required: bool = False; credit_hours: float = 0.0
    max_participants: int = 0; enrolled: int = 0

class SecondClassroomService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(service_name="second_classroom", description="Query USTC second classroom activities", cache_policy=CachePolicy.MEDIUM))

    async def _fetch_live(self, **kwargs): raise NotImplementedError

    def _get_mock(self, **kwargs):
        category = kwargs.get("category", "")
        keyword = kwargs.get("keyword", "")
        acts = [
            Activity("SC001","AI前沿技术讲座","讲座","2026-06-10","14:00-16:00","东区水上报告厅","计算机学院","MIT教授讲AI最新进展",True,2.0,300,250),
            Activity("SC002","ACM校赛","竞赛","2026-06-15","09:00-17:00","西区电三楼","ACM俱乐部","第20届ACM校赛",True,4.0,200,180),
            Activity("SC003","校园植树","志愿服务","2026-06-12","08:00-12:00","西区绿化带","青协","春季校园绿化",True,4.0,50,45),
            Activity("SC004","量子计算Workshop","讲座","2026-06-18","19:00-21:00","东区理化大楼","物理学院","量子计算基础",False,2.0,100,60),
            Activity("SC005","机器人竞赛宣讲","竞赛","2026-06-20","15:00-17:00","西区力四楼","RoboWalker","RoboMaster校内选拔",False,1.0,150,80),
            Activity("SC006","暑期支教招募","社会实践","2026-06-25","全天","线上","芳草社","2026暑期支教",True,40.0,30,22),
            Activity("SC007","摄影社外拍","社团活动","2026-06-22","14:00-17:00","滨湖湿地公园","摄影协会","风光摄影实践",False,3.0,40,35),
            Activity("SC008","数学建模分享","讲座","2026-06-28","19:00-21:00","东区五教","数学学院","国赛一等奖分享",False,2.0,200,120),
        ]
        results = acts
        if category: results = [a for a in results if a.category == category]
        if keyword:
            kw = keyword.lower()
            results = [a for a in results if kw in a.title.lower() or kw in a.description.lower()]
        return results

    async def search(self, keyword: str = "", category: str = "") -> ServiceResult:
        return await self.query(cache_key=f"sc:search:{keyword}:{category}", keyword=keyword, category=category)

    async def get_upcoming(self, days: int = 14) -> ServiceResult:
        return await self.query(cache_key=f"sc:upcoming:{days}", days=days)

    async def get_by_category(self, category: str) -> ServiceResult:
        return await self.query(cache_key=f"sc:cat:{category}", category=category)
