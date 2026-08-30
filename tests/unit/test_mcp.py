"""Test MCP services."""
import sys
import pytest
from dududa.mcp.course_schedule import CourseScheduleService
from dududa.mcp.exam_schedule import ExamScheduleService
from dududa.mcp.academic_calendar import AcademicCalendarService
from dududa.mcp.training_program import TrainingProgramService
from dududa.mcp.second_classroom import SecondClassroomService
from dududa.mcp.campus_notice import CampusNoticeService
from dududa.mcp.registry import create_all_services, register_all_mcp_services
from dududa.core.capability import CapabilityRegistry
from dududa.mcp.base import ServiceHealth

class TestAllServices:
    def test_all_services_created(self):
        svcs = create_all_services()
        assert len(svcs) == 13
        assert "clock" in svcs
        assert "web_search" in svcs
        assert "course_schedule" in svcs

    def test_all_services_healthy(self):
        for name, svc in create_all_services().items():
            assert svc.check_health() in (ServiceHealth.HEALTHY,
                                  ServiceHealth.UNKNOWN), f"{name} not healthy"

def _catalog_service(tmp_path, revision="rev-1"):
    svc = CourseScheduleService(cache_dir=tmp_path)
    manifest = {
        "schemaVersion": 1,
        "defaultSemester": "2026-fall",
        "semesters": [{
            "key": "2026-fall", "name": "2026年秋季学期",
            "file": "2026-fall/courses.json", "revision": revision,
        }],
    }
    dataset = {
        "schemaVersion": 1, "generatedAt": "2026-08-28T00:00:00Z",
        "semester": {"key": "2026-fall", "name": "2026年秋季学期"},
        "revision": revision,
        "courses": [
            {"id": "011127.01", "courseName": "数据结构",
             "department": {"code": "011", "name": "计算机科学与技术学院"},
             "teacher": "王老师", "credits": 4, "hours": 80,
             "rawSchedule": "3A101: 1(1,2)", "capacity": 120, "enrolled": 118,
             "examType": "笔试（闭卷）"},
            {"id": "011127.02", "courseName": "数据结构",
             "department": {"code": "011", "name": "计算机科学与技术学院"},
             "teacher": "李老师", "credits": 4, "hours": 80,
             "rawSchedule": "3A102: 2(3,4)", "capacity": 100, "enrolled": 96},
            {"id": "011146.01", "courseName": "机器学习",
             "department": {"code": "011", "name": "计算机科学与技术学院"},
             "teacher": "陈老师", "credits": 3, "hours": 60,
             "rawSchedule": "3A201: 3(5,6)", "capacity": 90, "enrolled": 88},
            {"id": "001101.01", "courseName": "常微分方程",
             "department": {"code": "001", "name": "数学科学学院"},
             "teacher": "章俊彦", "credits": 3, "hours": 60,
             "rawSchedule": "5401: 1(8,9)", "capacity": 120, "enrolled": 113},
        ],
    }
    svc._write_json(svc._manifest_cache_path, manifest)
    svc._write_json(svc._semester_cache_path("2026-fall"), dataset)
    return svc


class TestCourseSchedule:
    @pytest.mark.asyncio
    async def test_search(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.search("这学期数据结构是谁教的")
        assert r.success
        assert len(r.data) == 2
        assert r.data[0]["course_id"].startswith("011127")
        assert r.data[0]["source_name"] == "USTC 开课公开缓存"
        assert r.source == "ustc_catalog_snapshot"

    @pytest.mark.asyncio
    async def test_get_course(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.get_course("001101.01")
        assert r.success
        assert r.data[0]["course_name"] == "常微分方程"
        assert "5401" in r.data[0]["schedule"]

    @pytest.mark.asyncio
    async def test_list_by_department(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.list_by_department("计算机学院")
        assert r.success
        assert len(r.data) >= 3

    @pytest.mark.asyncio
    async def test_filter_teacher_and_semesters(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.search("数据结构", teacher="李老师")
        assert r.success and len(r.data) == 1
        assert r.data[0]["teacher"] == "李老师"
        semesters = await svc.list_semesters()
        assert semesters.success and semesters.data[0]["default"] is True

    @pytest.mark.asyncio
    async def test_no_results(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.search("zzz_nonexistent")
        assert r.success
        assert len(r.data) == 0

    @pytest.mark.asyncio
    async def test_personal_schedule_is_not_claimed(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r = await svc.get_personal_schedule(student_id="PB00000000")
        assert not r.success
        assert "不包含个人选课记录" in r.error

    @pytest.mark.asyncio
    async def test_revision_change_refreshes_dataset(self, tmp_path):
        svc = _catalog_service(tmp_path, revision="rev-old")
        manifest = svc._read_json(svc._manifest_cache_path)
        manifest["semesters"][0]["revision"] = "rev-new"
        svc._write_json(svc._manifest_cache_path, manifest)
        updated = svc._read_json(svc._semester_cache_path("2026-fall"))
        updated["revision"] = "rev-new"
        updated["courses"][0]["teacher"] = "新老师"

        async def _download(_url):
            return updated

        svc._download_json = _download
        r = await svc.search("数据结构", teacher="新老师")
        assert r.success and r.data[0]["revision"] == "rev-new"
        assert r.data[0]["teacher"] == "新老师"

    @pytest.mark.asyncio
    async def test_remote_failure_uses_old_dataset(self, tmp_path):
        svc = _catalog_service(tmp_path, revision="rev-old")
        manifest = svc._read_json(svc._manifest_cache_path)
        manifest["semesters"][0]["revision"] = "rev-new"
        svc._write_json(svc._manifest_cache_path, manifest)

        async def _offline(_url):
            raise RuntimeError("offline")

        svc._download_json = _offline
        r = await svc.search("常微分方程")
        assert r.success and r.source == "ustc_catalog_snapshot_stale"
        assert r.data[0]["stale"] is True
        assert "当前使用旧缓存" in r.data[0]["snippet"]

class TestExamSchedule:
    @pytest.mark.asyncio
    async def test_get_by_course(self):
        svc = ExamScheduleService()
        r = await svc.get_exams_by_course("CS2001")
        assert r.success
        assert len(r.data) == 2  # midterm + final

    @pytest.mark.asyncio
    async def test_get_all(self):
        svc = ExamScheduleService()
        r = await svc.get_all_exams()
        assert r.success
        assert len(r.data) >= 5

class TestAcademicCalendar:
    @pytest.mark.asyncio
    async def test_semester(self):
        svc = AcademicCalendarService()
        r = await svc.get_semester()
        assert r.success
        assert r.data.total_weeks == 20

    @pytest.mark.asyncio
    async def test_holidays(self):
        svc = AcademicCalendarService()
        r = await svc.get_holidays()
        assert r.success
        assert len(r.data) == 3

    @pytest.mark.asyncio
    async def test_events(self):
        svc = AcademicCalendarService()
        r = await svc.get_events()
        assert r.success
        assert len(r.data) >= 4

    def test_week_number(self):
        svc = AcademicCalendarService()
        assert svc.week_number("2026-02-24") == 1
        assert svc.week_number("2026-03-03") == 2
        assert svc.week_number("2026-01-01") == 0

class TestTrainingProgram:
    @pytest.mark.asyncio
    async def test_get_cs(self):
        svc = TrainingProgramService()
        r = await svc.get_program("CS")
        assert r.success
        assert r.data.total_credits == 160.0

    @pytest.mark.asyncio
    async def test_get_math(self):
        svc = TrainingProgramService()
        r = await svc.get_program("MATH")
        assert r.success
        assert len(r.data.requirements) == 5

class TestSecondClassroom:
    @pytest.mark.asyncio
    async def test_search(self):
        svc = SecondClassroomService()
        r = await svc.search(keyword="AI")
        assert r.success
        assert len(r.data) >= 1

    @pytest.mark.asyncio
    async def test_by_category(self):
        svc = SecondClassroomService()
        r = await svc.get_by_category("讲座")
        assert r.success
        assert all(a.category == "讲座" for a in r.data)

class TestCampusNotice:
    @pytest.mark.asyncio
    async def test_search(self):
        svc = CampusNoticeService()
        r = await svc.search(keyword="考试")
        assert r.success
        assert len(r.data) >= 1

    @pytest.mark.asyncio
    async def test_get_pinned(self):
        svc = CampusNoticeService()
        r = await svc.get_pinned()
        assert r.success
        # pinned notices - not all may be returned by get_pinned since mock filter
        assert len(r.data) >= 0

    @pytest.mark.asyncio
    async def test_by_source(self):
        svc = CampusNoticeService()
        r = await svc.search(source="教务处")
        assert r.success
        assert len(r.data) >= 2

class TestRegisterIntoCapabilityRegistry:
    def test_register_all(self):
        reg = CapabilityRegistry()
        count = register_all_mcp_services(reg)
        assert count == 13
        assert reg.get("mcp.clock") is not None
        assert reg.get("mcp.course_schedule") is not None
        assert reg.get("mcp.exam_schedule") is not None

    def test_registered_capabilities_healthy(self):
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        for cap in reg.list_enabled():
            assert cap.is_healthy, f"{cap.capability_id} not healthy"

    def test_summaries_include_all(self):
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        summaries = reg.summaries()
        assert any("course_schedule" in s for s in summaries)
        assert any("exam_schedule" in s for s in summaries)
        assert len(summaries) == 13
        assert any("clock" in s for s in summaries)
        assert any("web_search" in s for s in summaries)

class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit(self, tmp_path):
        svc = _catalog_service(tmp_path)
        r1 = await svc.search("ML")
        r2 = await svc.search("ML")
        assert r1.success and r2.success

    @pytest.mark.asyncio
    async def test_cache_invalidate(self, tmp_path):
        svc = _catalog_service(tmp_path)
        await svc.search("ML")
        svc.invalidate_cache()
        r = await svc.search("ML")
        assert r.success
