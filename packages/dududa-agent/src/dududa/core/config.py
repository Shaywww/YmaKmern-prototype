"""Phase 3 —— 类型化配置（typed config）。

对应文档 2.4：配置在进入运行时前完成类型与范围校验，拒绝非法值。
仅依赖 pydantic 与标准库；不导入任何基础设施或兄弟包。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ModelRole(str, Enum):
    """文档 2.5.7 的八类模型角色。"""
    PERCEPTION = "perception"
    SOCIAL_DECISION = "social_decision"
    TOOL_PLANNING = "tool_planning"
    DIRECT_CHAT = "direct_chat"
    RESPONSE_COMPOSITION = "response_composition"
    MEMORY_SUMMARY = "memory_summary"
    IMAGE_UNDERSTANDING = "image_understanding"
    IMAGE_GENERATION = "image_generation"


class ModelConfig(BaseModel):
    """单个模型角色的配置。"""
    model_id: str = Field(default="deepseek-chat", min_length=1, description="模型 ID，不能为空")
    base_url: Optional[str] = None
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.5, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=60.0, gt=0)
    fallback_model_id: Optional[str] = None


class BotConfig(BaseModel):
    """机器人整体类型化配置。"""
    owner_ids: tuple[str, ...] = ()
    default_role: Literal["owner", "admin", "trusted", "normal", "muted"] = "normal"
    memory_ttl_seconds: int = Field(default=7 * 86400, ge=0)
    tool_max_steps: int = Field(default=4, ge=1, le=8)
    model_roles: dict[ModelRole, ModelConfig] = Field(default_factory=dict)
    mcp_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
