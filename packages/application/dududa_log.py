# -*- coding: utf-8 -*-
"""统一日志入口：优先走 AstrBot LogManager 桥接，日志不依赖 root logger。

背景：AstrBot 的 star_manager（插件加载末尾）与 aiocqhttp 适配器启动时会
``logging.root.removeHandler(...)`` 清空 root logger 的 handler；依赖 root
冒泡的普通 logger 会静默丢失日志（P1-3 Flow start/end 在 journal 不可见）。
``LogManager.GetLogger`` 会给目标 logger 挂上独立的 loguru 桥接 handler 并
关闭 propagate，日志直接进 AstrBot 的 loguru 控制台 sink（journalctl 可见）。

子 logger（dududa20.memory / dududa20.mcp.client 等）经父级传播自动被覆盖。
"""
import logging

try:
    from astrbot.core.log import LogManager
except Exception:  # pragma: no cover - 无 astrbot 环境（纯测试）降级为标准 logger
    LogManager = None


def get_logger(name: str = "dududa20") -> logging.Logger:
    """返回带桥接 handler 的 logger；name 与现有代码一致（dududa20 / 子级）。"""
    logger = logging.getLogger(name)
    if LogManager is not None:
        try:
            LogManager.GetLogger(name)
        except Exception:
            pass
    return logger
