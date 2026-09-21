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
    turn_text: str = ""
    superseded: bool = False
    silent_cancel: bool = False


@dataclass
class _ConversationOperation:
    """Shared lifetime for every replacement task in one user turn.

    A newer bubble may replace an in-flight draft, but it must not buy a new
    deadline or another progress notification.  Keeping this state separate
    from ``ActiveTask`` makes those limits survive the hand-off.
    """

    operation_id: str
    started_at: float
    deadline_at: float
    progress_sent: bool = False


@dataclass
class _BufferedTurn:
    leader: asyncio.Task[Any]
    messages: list[str]
    opened_at: float = field(default_factory=time.monotonic)
    revision: int = 1


class ConversationTurnBuffer:
    """Briefly coalesce adjacent bubbles from one speaker into one turn.

    The first caller is the leader and waits only for the configured quiet
    window.  Followers append their text and return immediately.  This is an
    in-memory latency feature, not durable conversation history.
    """

    def __init__(self):
        self._turns: dict[str, _BufferedTurn] = {}
        self._revisions: dict[str, int] = {}

    def revision(self, key: str) -> int:
        return int(self._revisions.get(key, 0))

    def collecting(self, key: str) -> bool:
        active = self._turns.get(key)
        return bool(active is not None and not active.leader.done())

    async def merge(
        self,
        key: str,
        text: str,
        *,
        quiet_seconds: float = 0.0,
        max_seconds: float = 1.2,
    ) -> Optional[str]:
        """Return merged text for the leader, ``None`` for followers."""
        task = asyncio.current_task()
        if task is None:
            return str(text or "")
        value = str(text or "").strip()
        self._revisions[key] = self.revision(key) + 1
        active = self._turns.get(key)
        if active is not None and not active.leader.done():
            if value:
                active.messages.append(value)
            active.revision += 1
            return None

        state = _BufferedTurn(leader=task, messages=[value] if value else [])
        self._turns[key] = state
        quiet = max(0.0, float(quiet_seconds))
        maximum = max(quiet, float(max_seconds))
        try:
            if quiet:
                while True:
                    before = state.revision
                    remaining = maximum - (time.monotonic() - state.opened_at)
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(quiet, remaining))
                    if state.revision == before:
                        break
            return "\n".join(item for item in state.messages if item).strip()
        finally:
            if self._turns.get(key) is state:
                self._turns.pop(key, None)


@dataclass
class _PendingReplacement:
    waiter: asyncio.Task[Any]
    messages: list[str]


class ConversationTaskRegistry:
    """One cancellable active task per user/session."""

    def __init__(self, operation_timeout_seconds: float = 45.0):
        self._tasks: dict[str, ActiveTask] = {}
        self._replacements: dict[str, _PendingReplacement] = {}
        self._operations: dict[str, _ConversationOperation] = {}
        self._operation_timeout_seconds = max(
            1.0, float(operation_timeout_seconds))
        self.turn_buffer = ConversationTurnBuffer()

    def register(self, key: str, task: asyncio.Task[Any],
                 turn_text: str = "") -> bool:
        active = self._tasks.get(key)
        if active is not None and not active.task.done():
            return False
        now = time.monotonic()
        if key not in self._operations:
            seed = f"{key}|{now:.9f}|{id(task)}"
            operation_id = hashlib.sha256(
                seed.encode("utf-8", "replace")).hexdigest()[:16]
            self._operations[key] = _ConversationOperation(
                operation_id=operation_id,
                started_at=now,
                deadline_at=now + self._operation_timeout_seconds,
            )
        self._tasks[key] = ActiveTask(task=task, turn_text=str(turn_text or ""))
        return True

    def operation_id(self, key: str) -> str:
        operation = self._operations.get(key)
        return operation.operation_id if operation is not None else ""

    def remaining_seconds(self, key: str) -> Optional[float]:
        operation = self._operations.get(key)
        if operation is None:
            return None
        return max(0.0, operation.deadline_at - time.monotonic())

    def claim_progress_notice(self, key: str) -> bool:
        """Grant at most one progress bubble for a replacement chain."""
        operation = self._operations.get(key)
        if operation is None or operation.progress_sent:
            return False
        operation.progress_sent = True
        return True

    def queue_replacement(
        self, key: str, task: asyncio.Task[Any], text: str,
    ) -> tuple[bool, Optional[asyncio.Task[Any]]]:
        """Queue newer text while a turn is generating.

        Exactly one caller waits to become the replacement leader.  Further
        bubbles only extend that pending turn and return immediately.
        """
        active = self.running(key)
        if active is None:
            return True, None
        active.superseded = True
        pending = self._replacements.get(key)
        value = str(text or "").strip()
        if pending is None or pending.waiter.done():
            if (active.turn_text and value
                    and (value == active.turn_text
                         or value.startswith(active.turn_text + "\n"))):
                messages = [value]
            else:
                messages = [active.turn_text] if active.turn_text else []
                if value:
                    messages.append(value)
            self._replacements[key] = _PendingReplacement(task, messages)
            return True, active.task
        if value:
            pending.messages.append(value)
        return False, active.task

    def take_replacement(self, key: str, task: asyncio.Task[Any]) -> str:
        pending = self._replacements.get(key)
        if pending is None or pending.waiter is not task:
            return ""
        self._replacements.pop(key, None)
        # Preserve the semantic turn without letting repeated replacement
        # tasks grow the prompt without bound.  Exact duplicate bubbles are
        # removed while the newest six distinct messages win.
        distinct: list[str] = []
        for item in pending.messages:
            value = str(item or "").strip()
            if value and (not distinct or value != distinct[-1]):
                distinct.append(value)
        bounded = distinct[-6:]
        while len("\n".join(bounded)) > 1200 and len(bounded) > 1:
            bounded.pop(0)
        return "\n".join(bounded).strip()

    def is_superseded(self, key: str, task: asyncio.Task[Any]) -> bool:
        active = self._tasks.get(key)
        return bool(active is not None and active.task is task
                    and active.superseded)

    def is_silent_cancel(self, key: str, task: asyncio.Task[Any]) -> bool:
        active = self._tasks.get(key)
        return bool(active is not None and active.task is task
                    and active.silent_cancel)

    def mark_phase(self, key: str, phase: str) -> None:
        active = self._tasks.get(key)
        if active is not None and not active.task.done():
            active.phase = phase

    def running(self, key: str) -> Optional[ActiveTask]:
        active = self._tasks.get(key)
        if active is None or active.task.done():
            return None
        return active

    def cancel(self, key: str, *, silent: bool = False) -> bool:
        active = self.running(key)
        pending = self._replacements.pop(key, None)
        cancelled = False
        if active is not None:
            active.silent_cancel = bool(silent)
            active.task.cancel()
            cancelled = True
        if pending is not None and not pending.waiter.done():
            pending.waiter.cancel()
            cancelled = True
        self._operations.pop(key, None)
        return cancelled

    def finish(self, key: str, task: asyncio.Task[Any]) -> None:
        active = self._tasks.get(key)
        if active is not None and active.task is task:
            self._tasks.pop(key, None)
            pending = self._replacements.get(key)
            if pending is not None and pending.waiter.done():
                self._replacements.pop(key, None)
            if key not in self._replacements:
                self._operations.pop(key, None)

    def cancel_all(self) -> int:
        count = 0
        for active in tuple(self._tasks.values()):
            if not active.task.done():
                active.task.cancel()
                count += 1
        self._tasks.clear()
        for pending in tuple(self._replacements.values()):
            if not pending.waiter.done():
                pending.waiter.cancel()
                count += 1
        self._replacements.clear()
        self._operations.clear()
        return count
