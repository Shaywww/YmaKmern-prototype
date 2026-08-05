"""Campus MCP Services - USTC data capabilities."""
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceHealth
from .course_schedule import CourseScheduleService
from .exam_schedule import ExamScheduleService
from .academic_calendar import AcademicCalendarService
from .training_program import TrainingProgramService
from .second_classroom import SecondClassroomService
from .campus_notice import CampusNoticeService
from .registry import register_all_mcp_services
