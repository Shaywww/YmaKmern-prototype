# -*- coding: utf-8 -*-
"""User-facing interaction state for Dududa.

This module deliberately contains no AstrBot imports.  The plugin adapter owns
message delivery while this layer owns durable preferences, task bookkeeping,
quiet hours and stable support IDs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Optional


MEMORY_MODES = ("active", "paused", "temporary")
_QUIET_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")


def make_support_id(kind: str, detail: object = "", trace_id: str = "") -> str:
    """Return a short, non-secret identifier suitable for user-facing errors."""
    seed = f"{kind}|{type(detail).__name__}|{detail}|{trace_id}|{time.time_ns()}"
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:8].upper()
    prefix = re.sub(r"[^A-Z0-9]", "", (kind or "ERR").upper())[:4] or "ERR"
    return f"{prefix}-{digest}"


def _atomic_json_write(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ux-", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class UserExperienceStore:
    """Privacy-conscious durable user preferences keyed by a SHA-256 digest."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "users": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                self._data = loaded
        except (OSError, ValueError, TypeError):
            # Preferences are convenience state.  Corrupt input is ignored rather
            # than trusted or merged with a clean store.
            self._data = {"version": 1, "users": {}}

    @staticmethod
    def user_key(platform: str, actor_id: str) -> str:
        raw = f"{platform or 'unknown'}:{actor_id or 'unknown'}"
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def event_identity(event: object) -> tuple[str, str]:
        try:
            platform = str(event.get_platform_name())
        except Exception:
            platform = "unknown"
        try:
            actor = str(event.get_sender_id())
        except Exception:
            actor = "unknown"
        return platform, actor

    def key_for_event(self, event: object) -> str:
        return self.user_key(*self.event_identity(event))

    @staticmethod
    def session_key(event: object) -> str:
        try:
            platform = str(event.get_platform_name())
        except Exception:
            platform = "unknown"
        try:
            session = str(event.get_session_id())
        except Exception:
            session = "unknown"
        try:
            actor = str(event.get_sender_id())
        except Exception:
            actor = "unknown"
        digest = hashlib.sha256(
            f"{platform}:{session}:{actor}".encode("utf-8", "replace")
        ).hexdigest()
        return digest

    def _default(self) -> dict[str, Any]:
        return {
            "welcomed": False,
            "memory_mode": "active",
            "subscriptions": [],
            "quiet_hours": "22:30-08:00",
            "daily_limit": 1,
            "deliveries": {},
            "origin": "",
        }

    def get(self, key: str) -> dict[str, Any]:
        with self._lock:
            value = self._default()
            stored = self._data["users"].get(key)
            if isinstance(stored, dict):
                value.update(stored)
            return value

    def update(self, key: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            value = self.get(key)
            value.update(changes)
            self._data["users"][key] = value
            _atomic_json_write(self.path, self._data)
            return dict(value)

    def memory_mode(self, event: object) -> str:
        mode = str(self.get(self.key_for_event(event)).get("memory_mode", "active"))
        return mode if mode in MEMORY_MODES else "active"

    def set_memory_mode(self, event: object, mode: str) -> str:
        if mode not in MEMORY_MODES:
            raise ValueError(f"invalid memory mode: {mode}")
        self.update(self.key_for_event(event), memory_mode=mode)
        return mode

    def should_welcome(self, event: object) -> bool:
        try:
            if getattr(event.message_obj, "group", None):
                return False
        except Exception:
            return False
        return not bool(self.get(self.key_for_event(event)).get("welcomed"))

    def mark_welcomed(self, event: object) -> None:
        self.update(self.key_for_event(event), welcomed=True)

    def subscribe(self, event: object, topic: str) -> tuple[str, ...]:
        key = self.key_for_event(event)
        value = self.get(key)
        topics = {str(x) for x in value.get("subscriptions", []) if x}
        topics.add(topic)
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        self.update(key, subscriptions=sorted(topics), origin=origin)
        return tuple(sorted(topics))

    def unsubscribe(self, event: object, topic: str) -> tuple[str, ...]:
        key = self.key_for_event(event)
        value = self.get(key)
        topics = {str(x) for x in value.get("subscriptions", []) if x}
        topics.discard(topic)
        changes: dict[str, Any] = {"subscriptions": sorted(topics)}
        if not topics:
            # The active-message route is needed only while at least one opt-in
            # subscription exists.  Remove it after the final unsubscribe.
            changes["origin"] = ""
        self.update(key, **changes)
        return tuple(sorted(topics))

    def set_quiet_hours(self, event: object, value: str) -> str:
        if not _QUIET_RE.fullmatch(value or ""):
            raise ValueError("quiet hours must be HH:MM-HH:MM")
        self.update(self.key_for_event(event), quiet_hours=value)
        return value

    @staticmethod
    def _in_quiet_hours(spec: str, now: datetime) -> bool:
        if not _QUIET_RE.fullmatch(spec or ""):
            return False
        start, end = spec.split("-", 1)
        current = now.hour * 60 + now.minute
        start_min = int(start[:2]) * 60 + int(start[3:])
        end_min = int(end[:2]) * 60 + int(end[3:])
        if start_min == end_min:
            return True
        if start_min < end_min:
            return start_min <= current < end_min
        return current >= start_min or current < end_min

    def eligible(self, key: str, topic: str,
                 now: Optional[datetime] = None) -> bool:
        value = self.get(key)
        if topic not in value.get("subscriptions", []):
            return False
        if not value.get("origin"):
            return False
        now = now or datetime.now().astimezone()
        if self._in_quiet_hours(str(value.get("quiet_hours", "")), now):
            return False
        deliveries = value.get("deliveries", {}) or {}
        today = now.date().isoformat()
        return int(deliveries.get(today, 0)) < int(value.get("daily_limit", 1))

    def eligible_subscribers(self, topic: str,
                             now: Optional[datetime] = None) -> tuple[tuple[str, str], ...]:
        now = now or datetime.now().astimezone()
        result = []
        with self._lock:
            for key in tuple(self._data["users"]):
                if self.eligible(key, topic, now):
                    result.append((key, str(self.get(key)["origin"])))
        return tuple(result)

    def record_delivery(self, key: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now().astimezone()
        value = self.get(key)
        deliveries = dict(value.get("deliveries", {}) or {})
        today = now.date().isoformat()
        deliveries[today] = int(deliveries.get(today, 0)) + 1
        # Bound persistence: only retain the newest seven date buckets.
        deliveries = dict(sorted(deliveries.items())[-7:])
        self.update(key, deliveries=deliveries)


@dataclass
class ActiveTask:
    task: asyncio.Task[Any]
    started_at: float = field(default_factory=time.monotonic)
    phase: str = "preparing"


class ConversationTaskRegistry:
    """One cancellable active task per user/session."""

    def __init__(self):
        self._tasks: dict[str, ActiveTask] = {}

    def register(self, key: str, task: asyncio.Task[Any]) -> bool:
        active = self._tasks.get(key)
        if active is not None and not active.task.done():
            return False
        self._tasks[key] = ActiveTask(task=task)
        return True

    def mark_phase(self, key: str, phase: str) -> None:
        active = self._tasks.get(key)
        if active is not None and not active.task.done():
            active.phase = phase

    def running(self, key: str) -> Optional[ActiveTask]:
        active = self._tasks.get(key)
        if active is None or active.task.done():
            return None
        return active

    def cancel(self, key: str) -> bool:
        active = self.running(key)
        if active is None:
            return False
        active.task.cancel()
        return True

    def finish(self, key: str, task: asyncio.Task[Any]) -> None:
        active = self._tasks.get(key)
        if active is not None and active.task is task:
            self._tasks.pop(key, None)

    def cancel_all(self) -> int:
        count = 0
        for active in tuple(self._tasks.values()):
            if not active.task.done():
                active.task.cancel()
                count += 1
        self._tasks.clear()
        return count
