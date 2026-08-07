"""Exam schedule MCP service."""
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class Exam:
    exam_id: str; course_id: str; course_name: str; date: str
    start_time: str; end_time: str; location: str
    seat_number: str|None = None; exam_type: str = "期末考试"; notes: str = ""

class ExamScheduleService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(service_name="exam_schedule", description="Query USTC exam schedules", cache_policy=CachePolicy.MEDIUM))

    async def _fetch_live(self, **kwargs): raise NotImplementedError

    def _get_mock(self, **kwargs):
        course_id = kwargs.get("course_id", "")
        exams = [
            Exam("EX001","CS2001","数据结构","2026-06-15","08:00","10:00","3A101",exam_type="期末考试",notes="闭卷"),
            Exam("EX002","CS2002","操作系统","2026-06-17","14:00","16:00","3A201",exam_type="期末考试",notes="闭卷"),
            Exam("EX003","CS3001","编译原理","2026-06-20","08:00","10:00","3A301",exam_type="期末考试",notes="开卷"),
            Exam("EX004","MATH2001","概率论","2026-06-22","08:00","10:00","5A101",exam_type="期末考试",notes="闭卷"),
            Exam("EX005","CS2001","数据结构","2026-04-15","19:00","21:00","3A101",exam_type="期中考试",notes="闭卷"),
            Exam("EX006","CS3005","机器学习","2026-06-25","14:00","16:00","3A401",exam_type="期末考试",notes="闭卷"),
        ]
        if course_id: return [e for e in exams if e.course_id == course_id]
        return exams

    async def get_exams_by_course(self, course_id: str) -> ServiceResult:
        return await self.query(cache_key=f"exam:course:{course_id}", course_id=course_id)

    async def get_personal_exams(self, student_id: str, semester: str = "2026-spring") -> ServiceResult:
        return await self.query(cache_key=f"exam:student:{student_id}", student_id=student_id, semester=semester)

    async def get_all_exams(self, semester: str = "2026-spring") -> ServiceResult:
        return await self.query(cache_key=f"exam:all:{semester}", semester=semester)
