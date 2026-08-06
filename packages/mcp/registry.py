"""Register all MCP services into CapabilityRegistry."""
from .course_schedule import CourseScheduleService
from .exam_schedule import ExamScheduleService
from .academic_calendar import AcademicCalendarService
from .training_program import TrainingProgramService
from .second_classroom import SecondClassroomService
from .campus_notice import CampusNoticeService
from .clock_service import ClockService
import time
from ..core.capability import ToolObservation

_SERVICES = {}

def create_all_services() -> dict:
    global _SERVICES
    if _SERVICES: return _SERVICES
    _SERVICES = {
        "course_schedule": CourseScheduleService(),
        "exam_schedule": ExamScheduleService(),
        "academic_calendar": AcademicCalendarService(),
        "training_program": TrainingProgramService(),
        "second_classroom": SecondClassroomService(),
        "campus_notice": CampusNoticeService(),
        "clock": ClockService(),
    }
    return _SERVICES

class MCPProvider:
    """默认 MCP Provider：直接调用 service 方法（mock/live 由 service 决定）。"""

    def __init__(self, svc):
        self._svc = svc

    async def execute(self, cap, args):
        start = time.time()
        try:
            action = args.get("action", "search")
            method = getattr(self._svc, action, None)
            if method is None:
                return ToolObservation(step_id="", capability_id=cap.capability_id, success=False, error=f"Unknown action: {action}")
            svc_args = {k: v for k, v in args.items() if k != "action"}
            result = await method(**svc_args)
            return ToolObservation(step_id="", capability_id=cap.capability_id, success=result.success, data=result.data if result.success else None, error=result.error, source=result.source, latency_ms=(time.time()-start)*1000, cached=result.cached)
        except Exception as e:
            return ToolObservation(step_id="", capability_id=cap.capability_id, success=False, error=str(e), latency_ms=(time.time()-start)*1000)

    def health(self):
        return self._svc.check_health() != "unavailable"

def register_all_mcp_services(registry, provider_factory=None) -> int:
    from ..core.capability import Capability, CapabilitySchema, ProviderType, CapabilityRisk

    services = create_all_services()
    schemas = {
        "course_schedule": {"type":"object","properties":{"action":{"type":"string","enum":["search","get_course","list_by_department","get_personal_schedule"]},"keyword":{"type":"string"},"course_id":{"type":"string"},"department":{"type":"string"},"semester":{"type":"string"}}},
        "exam_schedule": {"type":"object","properties":{"action":{"type":"string","enum":["get_exams_by_course","get_personal_exams","get_all_exams"]},"course_id":{"type":"string"},"student_id":{"type":"string"},"semester":{"type":"string"}}},
        "academic_calendar": {"type":"object","properties":{"action":{"type":"string","enum":["get_semester","get_holidays","get_events"]}}},
        "training_program": {"type":"object","properties":{"action":{"type":"string","enum":["get_program","list_majors"]},"major_id":{"type":"string"}}},
        "second_classroom": {"type":"object","properties":{"action":{"type":"string","enum":["search","get_upcoming","get_by_category"]},"keyword":{"type":"string"},"category":{"type":"string"},"days":{"type":"integer"}}},
        "campus_notice": {"type":"object","properties":{"action":{"type":"string","enum":["search","get_pinned","get_recent"]},"keyword":{"type":"string"},"source":{"type":"string"},"category":{"type":"string"},"days":{"type":"integer"}}},
        "clock": {"type":"object","properties":{"action":{"type":"string","enum":["get_now","get_date","get_time"]},"fmt":{"type":"string"}}},
    }

    count = 0
    for svc_id, svc in services.items():
        cap = Capability(
            capability_id=f"mcp.{svc_id}", name=svc.config.service_name,
            description=svc.config.description, provider=ProviderType.MCP,
            risk=CapabilityRisk.READ_ONLY,
            schema=CapabilitySchema(input_schema=schemas.get(svc_id, {"type":"object"}),
                                     output_schema={"type":"object","description":"ServiceResult"}),
            timeout_seconds=svc.config.timeout_seconds, max_retries=svc.config.max_retries,
        )
        provider = provider_factory(svc) if provider_factory else MCPProvider(svc)
        registry.register(cap, provider)
        count += 1
    return count
