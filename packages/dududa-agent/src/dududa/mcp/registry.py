"""Register all MCP services into CapabilityRegistry."""
import logging

logger = logging.getLogger("dududa20.mcp.registry")
from .course_schedule import CourseScheduleService
from .exam_schedule import ExamScheduleService
from .academic_calendar import AcademicCalendarService
from .training_program import TrainingProgramService
from .second_classroom import SecondClassroomService
from .campus_notice import CampusNoticeService
from .clock_service import ClockService
from .academic_affairs import AcademicAffairsService
from .web_search_service import WebSearchService
import time
import os
from typing import Any, Optional

from ..core.capability import ToolObservation

_SERVICES = {}

class ServerCircuitBreaker:
    """服务级熔断（文档 2.5.6 Server Registry）。

    连续失败 threshold 次 -> open（快速失败，不触碰 service）；
    冷却 reset_seconds 后放行一个 half-open 探针；探针成功 -> closed，
    失败 -> 立即重新 open。
    """

    def __init__(self, threshold: Optional[int] = None,
                 reset_seconds: Optional[float] = None):
        env = os.environ
        self._threshold = (
            threshold if threshold is not None
            else max(1, int(env.get("DUDUDA_MCP_BREAKER_THRESHOLD", "3"))))
        self._reset = (
            reset_seconds if reset_seconds is not None
            else max(0.0, float(env.get("DUDUDA_MCP_BREAKER_RESET", "30.0"))))
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._probing: set[str] = set()

    def allow(self, server_id: str) -> bool:
        now = time.time()
        until = self._open_until.get(server_id, 0.0)
        if until <= 0.0:
            return True
        if now < until:
            return False
        if server_id not in self._probing:
            self._probing.add(server_id)
            return True
        return False

    def record_success(self, server_id: str) -> None:
        self._failures.pop(server_id, None)
        self._open_until.pop(server_id, None)
        self._probing.discard(server_id)

    def record_failure(self, server_id: str) -> None:
        if server_id in self._probing:
            # half-open 探针失败：立即重新打开
            self._probing.discard(server_id)
            self._failures[server_id] = 0
            self._open_until[server_id] = time.time() + self._reset
            return
        n = self._failures.get(server_id, 0) + 1
        if n >= self._threshold:
            self._failures[server_id] = 0
            self._open_until[server_id] = time.time() + self._reset
            logger.warning("MCP server circuit OPEN: %s (reset %.0fs)",
                           server_id, self._reset)
        else:
            self._failures[server_id] = n

    def state(self, server_id: str) -> str:
        if server_id in self._probing:
            return "half_open"
        if self._open_until.get(server_id, 0.0) > time.time():
            return "open"
        return "closed"

    def status(self) -> dict[str, str]:
        sids = set(self._failures) | set(self._open_until) | set(self._probing)
        return {sid: self.state(sid) for sid in sorted(sids)}


breaker = ServerCircuitBreaker()


def breaker_status() -> dict[str, str]:
    """全部已注册 MCP 服务的熔断状态（ops/命令用）。"""
    return {svc_id: breaker.state(svc_id) for svc_id in _SERVICES}


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
        "academic_affairs": AcademicAffairsService(),
        "clock": ClockService(),
        "web_search": WebSearchService(),
    }
    return _SERVICES

class MCPProvider:
    """默认 MCP Provider：直接调用 service 方法（mock/live 由 service 决定）。

    经 ServerCircuitBreaker 熔断：open 时快速失败，不触碰 service；
    服务调用失败（异常或 ServiceResult.fail）计入连续失败。
    """

    def __init__(self, svc, server_id: str = ""):
        self._svc = svc
        self._server_id = server_id or getattr(svc, "name", "") or "mcp"

    async def execute(self, cap, args):
        start = time.time()
        if not breaker.allow(self._server_id):
            return ToolObservation(
                step_id="", capability_id=cap.capability_id, success=False,
                error=f"circuit breaker open: {cap.capability_id} (try later)",
                latency_ms=(time.time() - start) * 1000)
        try:
            action = args.get("action", "search")
            method = getattr(self._svc, action, None)
            if method is None:
                return ToolObservation(
                    step_id="", capability_id=cap.capability_id, success=False,
                    error=f"Unknown action: {action}")
            svc_args = {k: v for k, v in args.items() if k != "action"}
            result = await method(**svc_args)
            if result.success:
                breaker.record_success(self._server_id)
            elif result.error:
                breaker.record_failure(self._server_id)
            return ToolObservation(
                step_id="", capability_id=cap.capability_id,
                success=result.success,
                data=result.data if result.success else None,
                error=result.error, source=result.source,
                latency_ms=(time.time() - start) * 1000,
                cached=result.cached)
        except Exception as e:
            breaker.record_failure(self._server_id)
            return ToolObservation(
                step_id="", capability_id=cap.capability_id, success=False,
                error=str(e), latency_ms=(time.time() - start) * 1000)

    def health(self):
        if breaker.state(self._server_id) == "open":
            return False
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
        "academic_affairs": {"type":"object","properties":{"action":{"type":"string","enum":["get_student_info","get_grade","get_credits_summary","get_graduation_requirements"]},"student_id":{"type":"string"},"semester":{"type":"string"},"major_id":{"type":"string"},"token":{"type":"string"}}},
    }

    count = 0
    for svc_id, svc in services.items():
        provider = provider_factory(svc) if provider_factory else MCPProvider(svc, server_id=svc_id)

        def _health_check(sid: str = svc_id, prov: Any = provider) -> bool:
            if breaker.state(sid) == "open":
                return False
            health = getattr(prov, "health", None)
            if health is None:
                return True
            try:
                value = health()
                if isinstance(value, bool):
                    return value
                return value != "unavailable"
            except Exception:
                return True

        cap = Capability(
            capability_id=f"mcp.{svc_id}", name=svc.config.service_name,
            description=svc.config.description, provider=ProviderType.MCP,
            risk=CapabilityRisk.READ_ONLY,
            schema=CapabilitySchema(input_schema=schemas.get(svc_id, {"type":"object"}),
                                     output_schema={"type":"object","description":"ServiceResult"}),
            timeout_seconds=svc.config.timeout_seconds, max_retries=svc.config.max_retries,
            health_check=_health_check,
        )
        registry.register(cap, provider)
        count += 1
    return count
