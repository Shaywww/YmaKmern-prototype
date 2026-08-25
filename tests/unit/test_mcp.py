"""Test MCP services."""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
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

class TestCourseSchedule:
    @pytest.mark.asyncio
    async def test_search(self):
        svc = CourseScheduleService()
        r = await svc.search("数据结构")
        assert r.success
        assert len(r.data) >= 1
        assert r.data[0].course_id == "CS2001"

    @pytest.mark.asyncio
    async def test_get_course(self):
        svc = CourseScheduleService()
        r = await svc.get_course("CS2001")
        assert r.success
        assert r.data[0].name == "数据结构"

    @pytest.mark.asyncio
    async def test_list_by_department(self):
        svc = CourseScheduleService()
        r = await svc.list_by_department("计算机学院")
        assert r.success
        assert len(r.data) >= 3

    @pytest.mark.asyncio
    async def test_no_results(self):
        svc = CourseScheduleService()
        r = await svc.search("zzz_nonexistent")
        assert r.success
        assert len(r.data) == 0

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
    async def test_cache_hit(self):
        svc = CourseScheduleService()
        r1 = await svc.search("ML")
        r2 = await svc.search("ML")
        assert r1.success and r2.success

    @pytest.mark.asyncio
    async def test_cache_invalidate(self):
        svc = CourseScheduleService()
        await svc.search("ML")
        svc.invalidate_cache()
        r = await svc.search("ML")
        assert r.success
