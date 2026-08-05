"""Phase 3 —— 稳定错误类型。

所有模块统一 raise/捕获 DududaError 子类，携带稳定 reason code
与 recoverable 标记，供 Trace、降级与审计使用。
"""
from __future__ import annotations


class DududaError(Exception):
    """所有 Dududa 错误的基类。"""

    def __init__(self, message: str = "", *, reason: str = "", recoverable: bool = False):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.recoverable = recoverable


class ConfigError(DududaError):
    """配置非法或缺失。"""

    def __init__(self, message: str = "配置错误", *, field: str = ""):
        super().__init__(message, reason="config_error")
        self.field = field


class UnauthorizedError(DududaError):
    """权限不足（default deny）。"""

    def __init__(self, message: str = "权限不足", *, reason: str = "denied"):
        super().__init__(message, reason=reason)


class ConfirmationError(DududaError):
    """确认失败（过期/重放/绑定不匹配）。"""

    def __init__(self, message: str = "确认失败", *, reason: str = "confirmation_error"):
        super().__init__(message, reason=reason)


class ModelError(DududaError):
    """模型调用失败。"""

    def __init__(self, message: str = "模型调用失败", *, reason: str = "model_error", recoverable: bool = True):
        super().__init__(message, reason=reason, recoverable=recoverable)


class CapabilityError(DududaError):
    """能力调用失败。"""

    def __init__(self, message: str = "能力调用失败", *, reason: str = "capability_error", recoverable: bool = True):
        super().__init__(message, reason=reason, recoverable=recoverable)


class MemoryError(DududaError):
    """记忆读写失败。"""

    def __init__(self, message: str = "记忆读写失败", *, reason: str = "memory_error", recoverable: bool = True):
        super().__init__(message, reason=reason, recoverable=recoverable)


class DeliveryError(DududaError):
    """投递失败。"""

    def __init__(self, message: str = "投递失败", *, reason: str = "delivery_error", recoverable: bool = True):
        super().__init__(message, reason=reason, recoverable=recoverable)
