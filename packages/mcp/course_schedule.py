"""Course schedule MCP service."""
from __future__ import annotations
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class Course:
    course_id: str; name: str; teacher: str; credits: float
    department: str; semester: str; schedule: str = ""
    capacity: int = 0; enrolled: int = 0; category: str = ""
    description: str = ""; prerequisites: tuple[str, ...] = ()

class CourseScheduleService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="course_schedule",
            description="Query USTC course offerings and personal schedules",
            cache_policy=CachePolicy.LONG))

    async def _fetch_live(self, **kwargs): raise NotImplementedError("USTC教务 API not connected")

    def _get_mock(self, **kwargs):
        semester = kwargs.get("semester", "2026-spring")
        department = kwargs.get("department", "")
        course_id = kwargs.get("course_id", "")
        keyword = kwargs.get("keyword", "")
        all_courses = [
            Course("CS2001","数据结构","张明",4.0,"计算机学院","2026-spring","周一1-2节3A101/周三3-4节3A102",120,118,"必修","基本数据结构与算法",("CS1001",)),
            Course("CS2002","操作系统","李华",4.0,"计算机学院","2026-spring","周二5-6节3A201/周四1-2节3A202",100,95,"必修","操作系统原理",("CS1002","CS2001")),
            Course("CS3001","编译原理","王芳",3.5,"计算机学院","2026-spring","周一7-8节3A301",80,72,"必修","编译器设计",("CS2001","CS2002")),
            Course("CS3005","机器学习","陈强",3.0,"计算机学院","2026-spring","周三1-2节3A401/周五3-4节3A401",90,88,"选修","机器学习基础",("CS2001","MATH2001")),
            Course("MATH2001","概率论与数理统计","刘伟",3.0,"数学学院","2026-spring","周二1-2节5A101/周四7-8节5A102",150,145,"必修","概率论基础",("MATH1001",)),
            Course("PHYS1002","大学物理实验","赵静",1.5,"物理学院","2026-spring","周五5-7节理实楼201",60,55,"必修","基础物理实验",()),
            Course("ENG2001","学术英语写作","周老师",2.0,"外语学院","2026-spring","周一5-6节5B201",40,38,"公选","英文学术写作",()),
            Course("HIST1001","中国近现代史纲要","吴教授",2.0,"人文学院","2026-spring","周三7-8节5C101",200,180,"公选","近现代史概述",()),
        ]
        results = all_courses
        if course_id: results = [c for c in results if c.course_id == course_id]
        if department: results = [c for c in results if c.department == department]
        if keyword:
            kw = keyword.lower()
            results = [c for c in results if kw in c.name.lower() or kw in c.description.lower()]
        return results

    async def search(self, keyword: str) -> ServiceResult:
        return await self.query(cache_key=f"search:{keyword}", keyword=keyword)

    async def get_course(self, course_id: str) -> ServiceResult:
        return await self.query(cache_key=f"course:{course_id}", course_id=course_id)

    async def list_by_department(self, department: str, semester: str = "2026-spring") -> ServiceResult:
        return await self.query(cache_key=f"dept:{department}:{semester}", department=department, semester=semester)

    async def get_personal_schedule(self, student_id: str, semester: str = "2026-spring") -> ServiceResult:
        return await self.query(cache_key=f"schedule:{student_id}:{semester}", student_id=student_id, semester=semester)
