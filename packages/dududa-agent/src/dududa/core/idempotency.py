# -*- coding: utf-8 -*-
"""Connector 幂等键注册表（文档 2.4.1 / 2.5.10 Connector 契约）。

幂等键 = (platform, bot_id, message_id)：同键在 TTL 窗口内视为重复消息，
平台重推/插件双入口不会导致双回复。线程安全、按时间淘汰、有界容量。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

DEFAULT_TTL_SECONDS = 600.0
DEFAULT_MAX_KEYS = 5000


def make_idempotency_key(platform: str, bot_id: str, message_id: str) -> str:
    """Connector 幂等键：platform + bot_id + message_id。"""
    return f"{platform or ''}|{bot_id or ''}|{message_id or ''}"


class MessageIdempotencyRegistry:
    """有界 TTL 判重注册表。

    check_and_register 返回 True 表示首次（应处理）；False 表示 TTL 窗口内
    重复（应忽略）。空 message_id 不判重（走其他去重策略）。
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_keys: int = DEFAULT_MAX_KEYS):
        self._ttl = float(ttl_seconds)
        self._max_keys = int(max_keys)
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_register(self, platform: str, bot_id: str,
                           message_id: str) -> bool:
        if not message_id:
            return True
        key = make_idempotency_key(platform, bot_id, message_id)
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            if len(self._seen) > self._max_keys:
                oldest = min(self._seen, key=self._seen.get)
                del self._seen[oldest]
            return True

    def _evict_locked(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts >= self._ttl]
        for k in expired:
            del self._seen[k]

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)