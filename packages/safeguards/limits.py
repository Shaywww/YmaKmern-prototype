# -*- coding: utf-8 -*-
"""嘟嘟哒 2.0 Runtime 限流与预算（文档 2.5.9）。

- RateLimiter：按 key（Actor/Scope）滑动窗口限流，返回稳定结果；
- TokenBudget：按 key 的日 token 预算，JSON 持久化（原子写、损坏容忍）；
- RuntimeLimits：生产组合门禁 check_message / spend_tokens，写 trace 事件；
- make_runtime_limits_from_env：按环境变量装配（DUDUDA_LIMITS_ENABLED=0 关闭）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from packages.core.trace_recorder import trace_recorder


@dataclass(frozen=True)
class RateLimitResult:
    """一次限流检查的结果。"""
    allowed: bool
    limit: int
    remaining: int = 0
    retry_after_seconds: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class BudgetResult:
    """一次预算检查/消费的结果。"""
    allowed: bool
    daily_limit: int
    remaining_tokens: int = 0
    reason: str = ""


class RateLimiter:
    """按 key 的滑动窗口限流（monotonic 时钟，线程安全）。"""

    def __init__(self, max_events: int = 60, window_seconds: float = 60.0,
                 now_fn: Optional[Callable[[], float]] = None):
        self._max = max(1, int(max_events))
        self._window = max(1.0, float(window_seconds))
        self._now = now_fn or time.monotonic
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, cost: int = 1) -> RateLimitResult:
        now = self._now()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > self._window:
                dq.popleft()
            if len(dq) + cost > self._max:
                retry = (self._window - (now - dq[0]) if dq else self._window)
                return RateLimitResult(
                    allowed=False, limit=self._max,
                    remaining=max(0, self._max - len(dq)),
                    retry_after_seconds=max(0.0, retry),
                    reason="rate_limited")
            for _ in range(max(1, int(cost))):
                dq.append(now)
            return RateLimitResult(
                allowed=True, limit=self._max,
                remaining=max(0, self._max - len(dq)), reason="ok")

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


class TokenBudget:
    """按 key 的日 token 预算；state_file 提供跨进程持久化。"""

    def __init__(self, daily_limit: int = 200_000,
                 state_file: Optional[str] = None,
                 today_fn: Optional[Callable[[], str]] = None):
        self._limit = max(1, int(daily_limit))
        self._file = state_file
        self._today_fn = today_fn or (lambda: date.today().isoformat())
        self._usage: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()
        if self._file:
            self._load()

    def check(self, key: str, tokens: int = 1) -> BudgetResult:
        today = self._today_fn()
        with self._lock:
            used = self._usage.get(str(key), {}).get(today, 0)
            remaining = max(0, self._limit - used)
            if int(tokens) > remaining:
                return BudgetResult(
                    allowed=False, daily_limit=self._limit,
                    remaining_tokens=remaining, reason="budget_exhausted")
            return BudgetResult(
                allowed=True, daily_limit=self._limit,
                remaining_tokens=remaining, reason="ok")

    def spend(self, key: str, tokens: int) -> BudgetResult:
        res = self.check(key, tokens)
        if not res.allowed:
            return res
        with self._lock:
            day = self._usage.setdefault(str(key), {})
            today = self._today_fn()
            day[today] = day.get(today, 0) + max(1, int(tokens))
        self._save()
        return BudgetResult(
            allowed=True, daily_limit=self._limit,
            remaining_tokens=max(0, res.remaining_tokens - max(1, int(tokens))),
            reason="ok")

    def _save(self) -> None:
        if not self._file:
            return
        try:
            with self._lock:
                payload = json.dumps(self._usage, ensure_ascii=False, sort_keys=True)
            directory = os.path.dirname(self._file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self._file)
        except OSError:
            pass

    def _load(self) -> None:
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._usage = {
                    str(k): {str(d): int(v) for d, v in day.items()}
                    for k, day in data.items() if isinstance(day, dict)
                }
        except (OSError, ValueError, TypeError):
            self._usage = {}


def _key_digest(key: str) -> str:
    """trace 中的 actor 指纹：不落原始用户 ID。"""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


class RuntimeLimits:
    """生产组合门禁：消息限流 + token 预算（文档 2.5.9）。"""

    RATE_LIMIT_HINT = "诶呀，你发得有点快，我先喘口气～过一小会儿再问我好吗？"
    BUDGET_HINT = "今天的对话额度用完啦，明天再来找我玩吧～"

    def __init__(self, rate_limiter: RateLimiter, token_budget: TokenBudget):
        self._rate = rate_limiter
        self._budget = token_budget

    def check_message(self, key: str, run_id: str = "",
                      trace_id: str = "") -> RateLimitResult:
        res = self._rate.check(key)
        trace_recorder.record(
            event="rate_limit", run_id=run_id, trace_id=trace_id,
            actor=_key_digest(key), allowed=res.allowed,
            limit=res.limit, remaining=res.remaining,
            retry_after=round(res.retry_after_seconds, 1))
        return res

    def spend_tokens(self, key: str, tokens: int,
                     run_id: str = "", trace_id: str = "") -> BudgetResult:
        res = self._budget.spend(key, max(1, int(tokens)))
        trace_recorder.record(
            event="budget", run_id=run_id, trace_id=trace_id,
            actor=_key_digest(key), allowed=res.allowed,
            daily_limit=res.daily_limit,
            remaining_tokens=res.remaining_tokens)
        return res


def make_runtime_limits_from_env(
        budget_file_default: Optional[str] = None) -> Optional[RuntimeLimits]:
    """按环境变量装配生产限流/预算；DUDUDA_LIMITS_ENABLED=0 返回 None。"""
    if os.environ.get("DUDUDA_LIMITS_ENABLED", "1") != "1":
        return None

    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default

    per_min = _int("DUDUDA_RATE_LIMIT_PER_MIN", 60)
    daily = _int("DUDUDA_TOKEN_BUDGET_DAILY", 200_000)
    budget_file = os.environ.get("DUDUDA_BUDGET_FILE") or budget_file_default
    return RuntimeLimits(
        RateLimiter(max_events=per_min, window_seconds=60.0),
        TokenBudget(daily_limit=daily, state_file=budget_file),
    )
