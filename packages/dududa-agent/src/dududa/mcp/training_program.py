"""Training program MCP service."""
from dataclasses import dataclass
from .base import BaseMCPService, MCPServiceConfig, CachePolicy, ServiceResult

@dataclass
class CourseRequirement:
    category: str; min_credits: float; courses: tuple = ()

@dataclass
class TrainingProgram:
    major_id: str; major_name: str; department: str; total_credits: float
    requirements: tuple; description: str = ""

class TrainingProgramService(BaseMCPService):
    def __init__(self):
        super().__init__(MCPServiceConfig(service_name="training_program", description="Query USTC degree programs", cache_policy=CachePolicy.PERMANENT))

    async def _fetch_live(self, **kwargs): raise NotImplementedError

    def _get_mock(self, **kwargs):
        major_id = kwargs.get("major_id", "CS")
        progs = {
            "CS": TrainingProgram("CS","计算机科学与技术","计算机学院",160.0,(CourseRequirement("通识必修",30),CourseRequirement("学科基础",40),CourseRequirement("专业核心",35),CourseRequirement("专业选修",20),CourseRequirement("实践环节",35)),"培养计算机科学高级专门人才"),
            "MATH": TrainingProgram("MATH","数学与应用数学","数学学院",155.0,(CourseRequirement("通识必修",30),CourseRequirement("学科基础",45),CourseRequirement("专业核心",35),CourseRequirement("专业选修",15),CourseRequirement("实践环节",30))),
            "PHYS": TrainingProgram("PHYS","物理学","物理学院",158.0,(CourseRequirement("通识必修",30),CourseRequirement("学科基础",42),CourseRequirement("专业核心",36),CourseRequirement("专业选修",18),CourseRequirement("实践环节",32))),
        }
        return progs.get(major_id, progs["CS"])

    async def get_program(self, major_id: str) -> ServiceResult:
        return await self.query(cache_key=f"program:{major_id}", major_id=major_id)

    async def list_majors(self) -> ServiceResult:
        return await self.query(cache_key="majors:all", query_type="list")
