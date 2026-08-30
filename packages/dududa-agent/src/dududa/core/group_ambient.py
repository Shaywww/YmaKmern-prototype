# -*- coding: utf-8 -*-
"""Conservative, opt-in ambient participation for busy group chats."""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from .decision import DecisionReason


_QUESTION_RE = re.compile(
    r"(?:[?？]\s*$|"
    r"(?:请问|求问|想问|谁知道|有没有人知道|为什么|为啥|怎么|咋|如何|"
    r"什么|啥|谁|哪里|哪儿|哪个|哪种|几时|几点|多久|多少|能不能|"
    r"可不可以|可以吗|行不行|是不是|对不对|怎么样|咋样))"
)
_RECALL_RE = re.compile(r"(?:撤回了?一条消息|recalled a message)", re.I)
_EMOTIONAL_BID_RE = re.compile(
    r"(?:我(?:今天|最近|这几天)?|今天|最近|这几天|真的|有点|好|太|快要)?"
    r"(?:好烦|烦死了?|累死了?|好累|崩溃了?|绷不住了?|难受|委屈|"
    r"心态炸了?|压力好大|想哭|受不了了?|撑不住了?)"
)
_SLEEP_CLOSING_RE = re.compile(
    r"(?:晚安|睡了|睡觉去了?|准备睡|先睡|下线了?|不聊了)"
)
_TOPIC_PATTERNS = (
    ("takeout", re.compile(r"(?:外卖|点餐|点个饭|叫个饭)")),
    ("off_work", re.compile(r"(?:下班|放工)")),
    ("milk_tea", re.compile(r"(?:奶茶|果茶)")),
    ("slacking", re.compile(
        r"(?:摸(?:会儿|一会儿|会|点)?鱼|划(?:会儿|一会儿|会|点)?水)")),
    ("movie", re.compile(r"(?:电影|影院|看剧|追剧)")),
)


@dataclass(frozen=True)
class AmbientDecision:
    should_reply: bool
    reason: str
    message_count: int = 0
    unique_senders: int = 0
    reason_code: str = DecisionReason.LOW_RELEVANCE.value


class GroupAmbientTracker:
    """Track a short traffic window and atomically reserve reply slots."""

    def __init__(self, *, window_seconds: float = 240.0,
                 min_messages: int = 15, min_unique_senders: int = 3,
                 cooldown_seconds: float = 1800.0, daily_limit: int = 2,
                 late_night_silence_seconds: float = 1800.0,
                 topic_reply_rate: float = 0.35,
                 topic_min_messages: int = 4,
                 topic_min_unique_senders: int = 2,
                 random_source=None,
                 state_path: str | None = None):
        self.window_seconds = max(30.0, float(window_seconds))
        self.min_messages = max(2, int(min_messages))
        self.min_unique_senders = max(2, int(min_unique_senders))
        self.cooldown_seconds = max(60.0, float(cooldown_seconds))
        self.daily_limit = max(1, int(daily_limit))
        self.late_night_silence_seconds = max(
            300.0, float(late_night_silence_seconds))
        self.topic_reply_rate = min(1.0, max(0.0, float(topic_reply_rate)))
        self.topic_min_messages = max(2, int(topic_min_messages))
        self.topic_min_unique_senders = max(
            2, int(topic_min_unique_senders))
        self._random = random_source or random.random
        self._messages: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self._last_activity: dict[str, float] = {}
        self._last_reply: dict[str, float] = {}
        self._daily: dict[str, tuple[str, int]] = {}
        self._state_path = str(state_path or "")
        self._lock = threading.RLock()
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                groups = (json.load(handle) or {}).get("groups", {})
            for gid, raw in groups.items():
                if not isinstance(raw, dict):
                    continue
                self._last_reply[str(gid)] = float(raw.get("last_reply", 0.0))
                self._daily[str(gid)] = (
                    str(raw.get("day", "")), max(0, int(raw.get("used", 0))))
        except (FileNotFoundError, AttributeError, ValueError, TypeError,
                json.JSONDecodeError):
            return

    def _save_state_locked(self) -> None:
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp = self._state_path + ".tmp"
            groups = {}
            for gid in set(self._last_reply) | set(self._daily):
                day, used = self._daily.get(gid, ("", 0))
                groups[gid] = {
                    "last_reply": self._last_reply.get(gid, 0.0),
                    "day": day,
                    "used": used,
                }
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "groups": groups}, handle,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except OSError:
            # Persistence failure must not take the bot offline. In-process
            # limits remain enforced until restart.
            return

    @staticmethod
    def is_clear_question(text: str) -> bool:
        value = " ".join(str(text or "").split()).strip()
        if len(value) < 3 or len(value) > 220:
            return False
        if value.startswith(("/", "[CQ:")) or _RECALL_RE.search(value):
            return False
        return bool(_QUESTION_RE.search(value))

    @staticmethod
    def is_emotional_bid(text: str) -> bool:
        """Recognise explicit bids for empathy, not generic negative words."""
        value = " ".join(str(text or "").split()).strip()
        if len(value) < 3 or len(value) > 160:
            return False
        if value.startswith(("/", "[CQ:")) or _RECALL_RE.search(value):
            return False
        return bool(_EMOTIONAL_BID_RE.search(value))

    @staticmethod
    def topic_category(text: str) -> str:
        """Return a narrow persona topic category, or an empty string."""
        value = " ".join(str(text or "").split()).strip()
        if len(value) < 2 or len(value) > 160:
            return ""
        if value.startswith(("/", "[CQ:")) or _RECALL_RE.search(value):
            return ""
        for category, pattern in _TOPIC_PATTERNS:
            if pattern.search(value):
                return category
        return ""

    def note_activity(self, *, group_id: str,
                      now: float | None = None) -> float | None:
        """Record group activity that is handled outside ``observe``.

        Directed conversations, native scenes and media do not pass through
        the ambient text evaluator. They still make the group active and must
        therefore reset the silence clock used by the late-night check-in
        rule. Traffic counters remain unchanged, so an explicit conversation
        cannot manufacture a busy-group ambient trigger.
        """
        gid = str(group_id or "")
        if not gid:
            return None
        ts = time.time() if now is None else float(now)
        with self._lock:
            previous = self._last_activity.get(gid)
            self._last_activity[gid] = ts
            return float(previous) if previous is not None else None

    def observe(self, *, group_id: str, sender_id: str, text: str,
                now: float | None = None) -> AmbientDecision:
        """Record one eligible human text message and evaluate the current one."""
        ts = time.time() if now is None else float(now)
        gid, uid = str(group_id or ""), str(sender_id or "")
        if not gid or not uid:
            return AmbientDecision(False, "missing_identity")
        value = " ".join(str(text or "").split()).strip()
        if not value or value.startswith("/") or _RECALL_RE.search(value):
            return AmbientDecision(False, "ignored_message")

        with self._lock:
            queue = self._messages[gid]
            previous_activity = self._last_activity.get(gid)
            self._last_activity[gid] = ts
            queue.append((ts, uid))
            cutoff = ts - self.window_seconds
            while queue and queue[0][0] < cutoff:
                queue.popleft()
            count = len(queue)
            unique = len({sender for _, sender in queue})

            emotional_bid = self.is_emotional_bid(value)
            clear_question = self.is_clear_question(value)
            topic_category = self.topic_category(value)
            late_night = bool(
                not emotional_bid
                and not clear_question
                and previous_activity is not None
                and ts - previous_activity >= self.late_night_silence_seconds
                and datetime.fromtimestamp(ts).hour in (0, 1, 2, 3, 4)
                and not _SLEEP_CLOSING_RE.search(value)
            )
            if emotional_bid:
                reason = "emotional_checkin"
            elif clear_question:
                if count < self.min_messages:
                    return AmbientDecision(False, "not_busy", count, unique)
                if unique < self.min_unique_senders:
                    return AmbientDecision(False, "too_few_senders", count, unique)
                reason = "busy_unanswered_question"
            elif late_night:
                reason = "late_night_checkin"
            elif topic_category:
                if count < self.topic_min_messages:
                    return AmbientDecision(
                        False, "topic_not_active", count, unique)
                if unique < self.topic_min_unique_senders:
                    return AmbientDecision(
                        False, "topic_too_few_senders", count, unique)
                if (self.topic_reply_rate <= 0.0
                        or self._random() >= self.topic_reply_rate):
                    return AmbientDecision(
                        False, "topic_sampled_out", count, unique)
                reason = f"topic_{topic_category}"
            else:
                return AmbientDecision(False, "latest_not_question", count, unique)
            return self._reserve_locked(
                gid=gid, ts=ts, reason=reason,
                message_count=count, unique_senders=unique)

    def reserve_scene(self, *, group_id: str, reason: str,
                      now: float | None = None) -> AmbientDecision:
        """Reserve a native scene reply under the shared cooldown/quota.

        New-member notices, red packets and poll cards do not contain normal
        human text, so they cannot go through :meth:`observe`.  Keeping them on
        the same limiter prevents several kinds of proactive reply from
        combining into a burst.
        """
        ts = time.time() if now is None else float(now)
        gid = str(group_id or "")
        scene_reason = str(reason or "").strip()
        if not gid or not scene_reason:
            return AmbientDecision(False, "missing_identity")
        with self._lock:
            return self._reserve_locked(
                gid=gid, ts=ts, reason=scene_reason,
                message_count=0, unique_senders=0)

    def _reserve_locked(self, *, gid: str, ts: float, reason: str,
                        message_count: int,
                        unique_senders: int) -> AmbientDecision:
        last_reply = self._last_reply.get(gid)
        if (last_reply is not None
                and ts - last_reply < self.cooldown_seconds):
            return AmbientDecision(
                False, "cooldown", message_count, unique_senders,
                DecisionReason.COOLDOWN_ACTIVE.value)

        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        saved_day, used = self._daily.get(gid, (day, 0))
        if saved_day != day:
            used = 0
        if used >= self.daily_limit:
            self._daily[gid] = (day, used)
            return AmbientDecision(
                False, "daily_limit", message_count, unique_senders,
                DecisionReason.DAILY_LIMIT.value)

        # Reserve atomically so simultaneous messages cannot create a burst.
        self._last_reply[gid] = ts
        self._daily[gid] = (day, used + 1)
        self._save_state_locked()
        return AmbientDecision(
            True, reason, message_count, unique_senders,
            DecisionReason.AMBIENT_WAKE.value)

    def status(self, group_id: str, *, now: float | None = None) -> dict:
        ts = time.time() if now is None else float(now)
        gid = str(group_id or "")
        with self._lock:
            cutoff = ts - self.window_seconds
            recent = [item for item in self._messages.get(gid, ())
                      if item[0] >= cutoff]
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            saved_day, used = self._daily.get(gid, (day, 0))
            if saved_day != day:
                used = 0
            last_reply = self._last_reply.get(gid)
            remaining = (0.0 if last_reply is None else max(
                0.0, self.cooldown_seconds - (ts - last_reply)))
        return {
            "message_count": len(recent),
            "unique_senders": len({sender for _, sender in recent}),
            "cooldown_remaining": int(remaining),
            "daily_used": used,
            "daily_limit": self.daily_limit,
        }
