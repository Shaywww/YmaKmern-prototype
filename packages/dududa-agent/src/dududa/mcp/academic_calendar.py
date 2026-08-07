"""Academic calendar MCP service."""
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class SemesterInfo:
    name: str; start_date: str; end_date: str; total_weeks: int = 20; exam_weeks: tuple = (18, 19)

@dataclass
class HolidayPeriod:
    name: str; start_date: str; end_date: str; days: int; make_up_days: tuple = ()

@dataclass
class CalendarEvent:
    event_id: str; title: str; date: str; description: str = ""; event_type: str = "holiday"

class AcademicCalendarService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(service_name="academic_calendar", description="Query USTC academic calendar", cache_policy=CachePolicy.LONG))

    async def _fetch_live(self, **kwargs): raise NotImplementedError

    def _get_mock(self, **kwargs):
        qt = kwargs.get("query_type", "semester")
        if qt == "holidays":
            return [HolidayPeriod("清明节","2026-04-04","2026-04-06",3,("2026-04-01",)), HolidayPeriod("劳动节","2026-05-01","2026-05-05",5,("2026-04-27","2026-05-10")), HolidayPeriod("端午节","2026-06-09","2026-06-11",3,("2026-06-15",))]
        if qt == "events":
            return [CalendarEvent("EV001","选课开始","2026-02-10","春季选课开放","registration"), CalendarEvent("EV002","退课截止","2026-03-10","最后退课日","deadline"), CalendarEvent("EV003","期中考试","2026-04-20","第8-9周","event"), CalendarEvent("EV004","期末考试","2026-06-22","第18-19周","event"), CalendarEvent("EV005","成绩公布","2026-07-15","期末成绩查询","deadline")]
        return SemesterInfo("2026春季学期","2026-02-24","2026-07-05",20,(18,19))

    async def get_semester(self) -> ServiceResult:
        return await self.query(cache_key="semester:current", query_type="semester")

    async def get_holidays(self) -> ServiceResult:
        return await self.query(cache_key="holidays:2026", query_type="holidays")

    async def get_events(self) -> ServiceResult:
        return await self.query(cache_key="events:2026", query_type="events")

    def week_number(self, date_str: str) -> int:
        from datetime import date
        d = date.fromisoformat(date_str)
        delta = (d - date(2026, 2, 24)).days
        return delta // 7 + 1 if delta >= 0 else 0
