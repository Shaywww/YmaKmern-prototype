# -*- coding: utf-8 -*-
"""Bounded group-message loop protection independent of any chat platform.

The guard deliberately consumes only normalized ingress facts.  Platform
adapters remain responsible for proving that ``explicit_at_bot`` is a real At
segment aimed at the current bot, rather than trusting a generic wake flag.

Configured sender IDs are an authoritative deny-list.  Dynamic repeat/burst
state, on the other hand, never blocks an explicit At.  That asymmetric rule
keeps a known external bot silent while ensuring a human can always recover
from a false-positive dynamic circuit by directly mentioning Dududa.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Iterable


DEFAULT_REPEAT_WINDOW_SECONDS = 15.0
DEFAULT_REPEAT_THRESHOLD = 3
DEFAULT_BURST_WINDOW_SECONDS = 10.0
DEFAULT_BURST_THRESHOLD = 6
DEFAULT_GROUP_REPEAT_WINDOW_SECONDS = 15.0
DEFAULT_GROUP_REPEAT_THRESHOLD = 4
DEFAULT_GROUP_REPEAT_MIN_SENDERS = 2
DEFAULT_SENDER_QUARANTINE_TTL_SECONDS = 60.0
DEFAULT_GROUP_CIRCUIT_TTL_SECONDS = 30.0
DEFAULT_MAX_KEYS = 4096


class IngressReason:
    """Stable string reasons returned by :class:`GroupIngressGuard`."""

    ALLOW = "allow"
    UNSCOPED = "unscoped"
    EMPTY = "empty"
    EXPLICIT_AT = "explicit_at"
    CONFIGURED_SENDER = "configured_sender"
    SENDER_QUARANTINE = "sender_quarantine"
    GROUP_CIRCUIT = "group_circuit"
    REPEAT = "repeat"
    BURST = "burst"
    GROUP_REPEAT = "group_repeat"


@dataclass(frozen=True)
class IngressDecision:
    """A silent ingress decision; callers should never reply to a drop."""

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0


@dataclass(frozen=True)
class GroupIngressStats:
    """Aggregate counters only; no message text, fingerprints, or IDs."""

    evaluated: int
    allowed: int
    dropped: int
    explicit_at_bypasses: int
    ignored_sender_count: int
    sender_windows: int
    repeat_windows: int
    group_repeat_windows: int
    sender_quarantines: int
    group_circuits: int


_AT_TOKEN_RE = re.compile(
    r"(?:\[At:\d+\]|\[CQ:at,[^\]]+\])", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_WHITESPACE_RE = re.compile(r"\s+")


def _content_fingerprint(text: object) -> str:
    """Return a privacy-conscious signature without retaining source text."""

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = _AT_TOKEN_RE.sub(" ", normalized)
    normalized = _URL_RE.sub(" <url> ", normalized)
    normalized = _UUID_RE.sub(" <id> ", normalized)
    normalized = _LONG_NUMBER_RE.sub(" <id> ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip().casefold()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def _positive_finite(value: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _threshold(value: int, name: str, *, minimum: int = 2) -> int:
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


class GroupIngressGuard:
    """Thread-safe, bounded TTL guard for ambient group traffic.

    ``evaluate`` is synchronous and contains no awaits, so an adapter can call
    it before creating UX tasks, progress notifications, traces, or model work.
    Message content is represented internally only by SHA-256 fingerprints.
    Each state table is independently capped by ``max_keys`` and each temporal
    window is capped by its threshold.
    """

    def __init__(
        self,
        *,
        ignored_sender_ids: Iterable[object] = (),
        repeat_window_seconds: float = DEFAULT_REPEAT_WINDOW_SECONDS,
        repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
        burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
        burst_threshold: int = DEFAULT_BURST_THRESHOLD,
        group_repeat_window_seconds: float = (
            DEFAULT_GROUP_REPEAT_WINDOW_SECONDS),
        group_repeat_threshold: int = DEFAULT_GROUP_REPEAT_THRESHOLD,
        group_repeat_min_senders: int = DEFAULT_GROUP_REPEAT_MIN_SENDERS,
        sender_quarantine_ttl_seconds: float = (
            DEFAULT_SENDER_QUARANTINE_TTL_SECONDS),
        group_circuit_ttl_seconds: float = (
            DEFAULT_GROUP_CIRCUIT_TTL_SECONDS),
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._repeat_window = _positive_finite(
            repeat_window_seconds, "repeat_window_seconds")
        self._repeat_threshold = _threshold(
            repeat_threshold, "repeat_threshold")
        self._burst_window = _positive_finite(
            burst_window_seconds, "burst_window_seconds")
        self._burst_threshold = _threshold(
            burst_threshold, "burst_threshold")
        self._group_repeat_window = _positive_finite(
            group_repeat_window_seconds, "group_repeat_window_seconds")
        self._group_repeat_threshold = _threshold(
            group_repeat_threshold, "group_repeat_threshold")
        self._group_repeat_min_senders = _threshold(
            group_repeat_min_senders, "group_repeat_min_senders")
        if self._group_repeat_min_senders > self._group_repeat_threshold:
            raise ValueError(
                "group_repeat_min_senders cannot exceed "
                "group_repeat_threshold")
        self._sender_quarantine_ttl = _positive_finite(
            sender_quarantine_ttl_seconds,
            "sender_quarantine_ttl_seconds")
        self._group_circuit_ttl = _positive_finite(
            group_circuit_ttl_seconds, "group_circuit_ttl_seconds")
        self._max_keys = int(max_keys)
        if self._max_keys <= 0:
            raise ValueError("max_keys must be > 0")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._ignored_sender_ids = frozenset(
            value for value in
            (str(item or "").strip() for item in ignored_sender_ids)
            if value)

        self._sender_windows: OrderedDict[
            tuple[str, str], deque[float]] = OrderedDict()
        self._repeat_windows: OrderedDict[
            tuple[str, str, str], deque[float]] = OrderedDict()
        self._group_repeat_windows: OrderedDict[
            tuple[str, str], deque[tuple[float, str]]] = OrderedDict()
        self._sender_quarantines: OrderedDict[
            tuple[str, str], float] = OrderedDict()
        self._group_circuits: OrderedDict[str, float] = OrderedDict()

        self._lock = threading.RLock()
        self._last_now = float("-inf")
        self._next_sweep = float("-inf")
        self._sweep_interval = min(
            5.0,
            self._repeat_window,
            self._burst_window,
            self._group_repeat_window,
            self._sender_quarantine_ttl,
            self._group_circuit_ttl,
        )
        self._evaluated = 0
        self._allowed = 0
        self._dropped = 0
        self._explicit_at_bypasses = 0

    def evaluate(
        self,
        *,
        group_id: object,
        sender_id: object,
        text: object,
        explicit_at_bot: bool,
        has_media: bool = False,
    ) -> IngressDecision:
        """Evaluate one message and atomically update dynamic guard state.

        Empty ``group_id``/``sender_id`` is treated as unscoped traffic and is
        allowed.  A configured sender is always denied in group scope.  For any
        other sender, a proven explicit At bypasses both an existing circuit
        and dynamic accounting, so it cannot extend or create a quarantine.
        """

        group = str(group_id or "").strip()
        sender = str(sender_id or "").strip()
        with self._lock:
            self._evaluated += 1
            now = self._now_locked()
            self._maybe_sweep_locked(now)

            if not group or not sender:
                return self._decision_locked(True, IngressReason.UNSCOPED)

            if sender in self._ignored_sender_ids:
                return self._decision_locked(
                    False, IngressReason.CONFIGURED_SENDER)

            if bool(explicit_at_bot):
                self._explicit_at_bypasses += 1
                return self._decision_locked(True, IngressReason.EXPLICIT_AT)

            sender_key = (group, sender)
            group_expiry = self._active_deadline_locked(
                self._group_circuits, group, now)
            if group_expiry is not None:
                return self._decision_locked(
                    False, IngressReason.GROUP_CIRCUIT,
                    group_expiry - now)

            sender_expiry = self._active_deadline_locked(
                self._sender_quarantines, sender_key, now)
            if sender_expiry is not None:
                return self._decision_locked(
                    False, IngressReason.SENDER_QUARANTINE,
                    sender_expiry - now)

            fingerprint = _content_fingerprint(text)
            if not fingerprint and not bool(has_media):
                return self._decision_locked(True, IngressReason.EMPTY)

            burst_hit = self._append_timestamp_locked(
                self._sender_windows,
                sender_key,
                now,
                window_seconds=self._burst_window,
                threshold=self._burst_threshold,
            )
            repeat_hit = False
            group_repeat_hit = False
            if fingerprint:
                repeat_hit = self._append_timestamp_locked(
                    self._repeat_windows,
                    (group, sender, fingerprint),
                    now,
                    window_seconds=self._repeat_window,
                    threshold=self._repeat_threshold,
                )
                group_repeat_hit = self._append_group_repeat_locked(
                    (group, fingerprint), sender, now)

            sender_deadline = 0.0
            if repeat_hit or burst_hit:
                sender_deadline = now + self._sender_quarantine_ttl
                self._set_deadline_locked(
                    self._sender_quarantines, sender_key, sender_deadline)
                # A completed quarantine is a fresh start.  Without clearing
                # the triggering history, a configuration whose TTL is shorter
                # than its observation window would immediately re-trip on the
                # first message after expiry.
                self._clear_sender_history_locked(sender_key)

            group_deadline = 0.0
            if group_repeat_hit:
                group_deadline = now + self._group_circuit_ttl
                self._set_deadline_locked(
                    self._group_circuits, group, group_deadline)
                self._clear_group_repeat_history_locked(group)

            if group_repeat_hit:
                return self._decision_locked(
                    False, IngressReason.GROUP_REPEAT,
                    group_deadline - now)
            if repeat_hit:
                return self._decision_locked(
                    False, IngressReason.REPEAT,
                    sender_deadline - now)
            if burst_hit:
                return self._decision_locked(
                    False, IngressReason.BURST,
                    sender_deadline - now)
            return self._decision_locked(True, IngressReason.ALLOW)

    def stats(self) -> GroupIngressStats:
        """Return aggregate state/counters without text, hashes, or IDs."""

        with self._lock:
            self._sweep_locked(self._now_locked())
            return GroupIngressStats(
                evaluated=self._evaluated,
                allowed=self._allowed,
                dropped=self._dropped,
                explicit_at_bypasses=self._explicit_at_bypasses,
                ignored_sender_count=len(self._ignored_sender_ids),
                sender_windows=len(self._sender_windows),
                repeat_windows=len(self._repeat_windows),
                group_repeat_windows=len(self._group_repeat_windows),
                sender_quarantines=len(self._sender_quarantines),
                group_circuits=len(self._group_circuits),
            )

    def sweep(self) -> int:
        """Remove expired dynamic keys and return the number removed."""

        with self._lock:
            return self._sweep_locked(self._now_locked())

    def clear(self) -> None:
        """Clear dynamic state and counters; configured IDs remain intact."""

        with self._lock:
            self._sender_windows.clear()
            self._repeat_windows.clear()
            self._group_repeat_windows.clear()
            self._sender_quarantines.clear()
            self._group_circuits.clear()
            self._evaluated = 0
            self._allowed = 0
            self._dropped = 0
            self._explicit_at_bypasses = 0
            self._next_sweep = float("-inf")

    # ---- Locked helpers -------------------------------------------------

    def _now_locked(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        # A defensive clamp keeps TTL semantics stable for a faulty test clock
        # or a platform clock wrapper that briefly moves backwards.
        if now < self._last_now:
            return self._last_now
        self._last_now = now
        return now

    def _decision_locked(
        self, allowed: bool, reason: str, retry_after_seconds: float = 0.0,
    ) -> IngressDecision:
        if allowed:
            self._allowed += 1
        else:
            self._dropped += 1
        return IngressDecision(
            allowed=allowed,
            reason=reason,
            retry_after_seconds=max(0.0, float(retry_after_seconds)),
        )

    def _maybe_sweep_locked(self, now: float) -> None:
        if now >= self._next_sweep:
            self._sweep_locked(now)

    def _sweep_locked(self, now: float) -> int:
        removed = 0
        removed += self._sweep_timestamp_store_locked(
            self._sender_windows, now, self._burst_window)
        removed += self._sweep_timestamp_store_locked(
            self._repeat_windows, now, self._repeat_window)

        for key, values in list(self._group_repeat_windows.items()):
            while (values and
                   now - values[0][0] >= self._group_repeat_window):
                values.popleft()
            if not values:
                self._group_repeat_windows.pop(key, None)
                removed += 1

        removed += self._sweep_deadlines_locked(
            self._sender_quarantines, now)
        removed += self._sweep_deadlines_locked(self._group_circuits, now)
        self._next_sweep = now + self._sweep_interval
        return removed

    @staticmethod
    def _sweep_timestamp_store_locked(
        store: OrderedDict, now: float, window_seconds: float,
    ) -> int:
        removed = 0
        for key, values in list(store.items()):
            while values and now - values[0] >= window_seconds:
                values.popleft()
            if not values:
                store.pop(key, None)
                removed += 1
        return removed

    @staticmethod
    def _sweep_deadlines_locked(store: OrderedDict, now: float) -> int:
        expired = [key for key, deadline in store.items()
                   if deadline <= now]
        for key in expired:
            store.pop(key, None)
        return len(expired)

    def _append_timestamp_locked(
        self,
        store: OrderedDict,
        key: object,
        now: float,
        *,
        window_seconds: float,
        threshold: int,
    ) -> bool:
        values = store.pop(key, None)
        if values is None:
            values = deque(maxlen=threshold)
        else:
            while values and now - values[0] >= window_seconds:
                values.popleft()
        values.append(now)
        store[key] = values
        self._bound_store_locked(store)
        return len(values) >= threshold

    def _append_group_repeat_locked(
        self, key: tuple[str, str], sender: str, now: float,
    ) -> bool:
        values = self._group_repeat_windows.pop(key, None)
        if values is None:
            values = deque(maxlen=self._group_repeat_threshold)
        else:
            while (values and
                   now - values[0][0] >= self._group_repeat_window):
                values.popleft()
        values.append((now, sender))
        self._group_repeat_windows[key] = values
        self._bound_store_locked(self._group_repeat_windows)
        return (
            len(values) >= self._group_repeat_threshold
            and len({item_sender for _, item_sender in values})
            >= self._group_repeat_min_senders
        )

    def _active_deadline_locked(
        self, store: OrderedDict, key: object, now: float,
    ) -> float | None:
        deadline = store.get(key)
        if deadline is None:
            return None
        if deadline <= now:
            store.pop(key, None)
            return None
        store.move_to_end(key)
        return deadline

    def _set_deadline_locked(
        self, store: OrderedDict, key: object, deadline: float,
    ) -> None:
        store.pop(key, None)
        store[key] = deadline
        self._bound_store_locked(store)

    def _clear_sender_history_locked(
        self, sender_key: tuple[str, str],
    ) -> None:
        self._sender_windows.pop(sender_key, None)
        group, sender = sender_key
        for key in list(self._repeat_windows):
            if key[0] == group and key[1] == sender:
                self._repeat_windows.pop(key, None)

    def _clear_group_repeat_history_locked(self, group: str) -> None:
        for key in list(self._group_repeat_windows):
            if key[0] == group:
                self._group_repeat_windows.pop(key, None)

    def _bound_store_locked(self, store: OrderedDict) -> None:
        while len(store) > self._max_keys:
            store.popitem(last=False)


__all__ = [
    "GroupIngressGuard",
    "GroupIngressStats",
    "IngressDecision",
    "IngressReason",
]
