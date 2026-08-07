"""Academic affairs MCP service —— 教务（学籍 / 成绩 / 学分汇总 / 毕业要求）。

文档 2.5.6 候选新服务；涉及账号和个人数据，先定义边界：
- 授权：个人动作（学籍/成绩/学分汇总）必须携带 token，与
  DUDUDA_ACADEMIC_AFFAIRS_TOKEN 精确匹配（constant-time 比较）；
  凭据未配置时 fail closed（unauthorized: credentials not configured）。
- 凭据：仅从环境变量读取，不落盘、不入库；吊销 = 删除/更换环境变量。
- 数据时效：个人数据 CachePolicy.NONE（零缓存），凭据/数据变更立即生效。
- 写操作边界：本服务全部为只读动作，不提供任何写/副作用入口。
- 私聊/群边界：服务注册进 access.py ICOURSE_SERVICE_IDS，
  受按群/按人策略约束（生产 default deny + owner 放行）。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

_TOKEN_ENV = "DUDUDA_ACADEMIC_AFFAIRS_TOKEN"
PERSONAL_ACTIONS = frozenset(
    {"get_student_info", "get_grade", "get_credits_summary"})


def _token_ok(token: str) -> bool:
    """凭据校验：未配置 -> False（fail closed）；配置 -> constant-time 精确匹配。"""
    expected = os.environ.get(_TOKEN_ENV, "").strip()
    if not expected:
        return False
    return secrets.compare_digest((token or "").strip(), expected)


@dataclass
class StudentInfo:
    student_id: str
    name_masked: str
    major: str
    department: str
    grade_year: int
    enrollment_status: str


@dataclass
class Grade:
    semester: str
    course_id: str
    course_name: str
    credits: float
    score: int
    grade_point: float


@dataclass
class Requirement:
    category: str
    required_credits: float
    completed_credits: float
    courses: tuple[str, ...]


class AcademicAffairsService(BaseMCPService):
    """教务服务：个人动作带 token 授权且零缓存；毕业要求公开可查。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="academic_affairs",
            description="Query USTC academic affairs: student info, grades, "
                        "credit summary, graduation requirements",
            cache_policy=CachePolicy.NONE,   # 个人数据不缓存
            timeout_seconds=10.0))

    async def _fetch_live(self, **kwargs):
        raise NotImplementedError("USTC 教务 API not connected")

    def _get_mock(self, **kwargs):
        kind = kwargs.get("kind", "info")
        if kind == "grad":
            return [
                Requirement("公共必修", 36.0, 36.0,
                            ("PHYS1002", "HIST1001", "ENG2001")),
                Requirement("专业必修", 52.0, 48.5,
                            ("CS2001", "CS2002", "CS3001")),
                Requirement("专业选修", 24.0, 18.0, ("CS3005",)),
                Requirement("总学分", 160.0, 132.5, ()),
            ]
        sid = kwargs.get("student_id", "PB21000001")
        if kind == "info":
            return StudentInfo(sid, "张**", "计算机科学与技术",
                               "计算机学院", 2021, "在读")
        if kind == "grades":
            semester = kwargs.get("semester", "2026-spring")
            return [
                Grade(semester, "CS2001", "数据结构", 4.0, 92, 4.3),
                Grade(semester, "CS2002", "操作系统", 4.0, 88, 3.7),
                Grade(semester, "MATH2001", "概率论与数理统计", 3.0, 90, 4.0),
            ]
        # kind == "credits"
        return {
            "student_id": sid,
            "total_credits": 132.5,
            "passed_credits": 132.5,
            "gpa": 3.78,
            "rank_percent": 12.0,
        }

    # ---- 个人动作（需 token，零缓存） ----

    async def get_student_info(self, student_id: str, token: str = "") -> ServiceResult:
        if not _token_ok(token):
            return ServiceResult.fail("unauthorized: invalid or missing token")
        if not student_id:
            return ServiceResult.fail("student_id required")
        return await self.query(cache_key=None, kind="info", student_id=student_id)

    async def get_grade(self, student_id: str, semester: str = "2026-spring",
                        token: str = "") -> ServiceResult:
        if not _token_ok(token):
            return ServiceResult.fail("unauthorized: invalid or missing token")
        if not student_id:
            return ServiceResult.fail("student_id required")
        return await self.query(cache_key=None, kind="grades",
                                student_id=student_id, semester=semester)

    async def get_credits_summary(self, student_id: str,
                                  token: str = "") -> ServiceResult:
        if not _token_ok(token):
            return ServiceResult.fail("unauthorized: invalid or missing token")
        if not student_id:
            return ServiceResult.fail("student_id required")
        return await self.query(cache_key=None, kind="credits",
                                student_id=student_id)

    # ---- 公开动作（毕业要求，可缓存） ----

    async def get_graduation_requirements(
            self, major_id: str = "计算机科学与技术") -> ServiceResult:
        return await self.query(
            cache_key=f"grad:{major_id}", kind="grad", major_id=major_id)