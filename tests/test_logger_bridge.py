# -*- coding: utf-8 -*-
"""P1-3：日志桥接 —— 插件 logger 自带 handler，不依赖 root 冒泡。

AstrBot 的 star_manager / aiocqhttp 会清空 root logger 的 handler，
普通 logging.getLogger + propagate 的日志会静默丢失；get_logger 通过
LogManager.GetLogger 给 logger 挂独立 loguru 桥接 handler。
"""
import logging

import pytest

from packages.application.dududa_log import get_logger


def _astrbot_ok() -> bool:
    try:
        import astrbot.core.log  # noqa: F401
        return True
    except Exception:
        return False


def test_get_logger_is_same_singleton():
    log = get_logger("dududa20")
    assert log is logging.getLogger("dududa20")


def test_get_logger_attaches_bridge_and_stops_propagation():
    if not _astrbot_ok():
        pytest.skip("astrbot 不可用")
    log = get_logger("dududa20")
    assert not log.propagate
    assert any(getattr(h, "_astrbot_loguru_handler", False) for h in log.handlers)


def test_sub_loggers_reach_bridged_parent():
    if not _astrbot_ok():
        pytest.skip("astrbot 不可用")
    get_logger("dududa20")
    parent = logging.getLogger("dududa20")
    assert any(getattr(h, "_astrbot_loguru_handler", False) for h in parent.handlers)
    sub = logging.getLogger("dududa20.memory")
    assert sub.propagate  # 冒泡到已桥接的父级即可出日志