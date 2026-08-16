"""用户画像（USER_PROFILE）与会话状态（SESSION_STATE）建模（文档 2.4.6 / 2.5.3）。

- UserProfile：跨会话长期偏好（称呼、偏好、事实、话题频次），按 platform+bot+actor 隔离；
- SessionState：单会话内状态（消息数、最近意图、活跃话题），按 conversation+actor 隔离；
- ProfileStore：JSON 持久化（原子写 + fail-closed 加载，约定与 JSONMemoryRepository 一致）；
- extract_profile_signals：规则提取（称呼/偏好/事实），无 LLM 调用，确定性可测。

默认文件 <repo>/data/profiles.json，可用环境变量 DUDUDA_PROFILE_FILE 覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("dududa20.profile")

_MAX_PREFERENCES = 12
_MAX_FACTS = 12
_MAX_TOPICS = 8
_SIGNAL_MAX_LEN = 24

# ---- 规则提取（确定性，无模型） ----

_NAME_RE = re.compile(
    r"(?:叫我|你可以叫我|称呼我(?:为)?|以后叫我|可以叫我|我叫|我的名字(?:是|叫))"
    r"\s*(?:就(?:行|好|可以)|的话)?\s*([一-龥A-Za-z0-9_-]{1,12})"
)
_PREF_RE = re.compile(
    r"(?:我喜欢|我爱|我最爱|超喜欢|最喜欢|偏爱|也喜欢|还喜欢)\s*"
    r"([^，。！？,.!?\n]{1,24})"
)
# 过泛偏好值（代词/天气/语气词）不入画像
_PREF_BAD = {"你", "它", "这", "那", "我", "他", "她", "天气", "吃", "玩",
             "吗", "呀", "吧", "啊", "呢", "哦", "这样", "那样"}
_LOC_SUFFIX = r"(?:省|市|县|区|镇|乡|州|盟)"
_LOC_PREFIX_RE = re.compile(
    r"(?:我现在在|我目前在|我当前在|我人在|我这几天在|我住在|我家在|住在|家住|家在)"
)
_LOC_REN_RE = re.compile(
    r"(?:我是|来自)\s*([一-龥]{2,6}?" + _LOC_SUFFIX + r"?人)"
)
_FACT_RE = re.compile(
    r"(?:我是|我在|我来自|我读|我在读|我住在|我学)\s*"
    r"([^，。！？,.!?\n]{1,24})"
)


def _strip_tail_particles(text: str) -> str:
    return re.sub(r"[吧啊呢呀哦啦]?$", "", text or "").strip()


def extract_location(text: str) -> str:
    """从消息提取用户所在地（带行政区划后缀才认，防误判）；无匹配返回空。

    「我现在在临泽县」/「我家在甘肃临泽县」-> 临泽县（取最具体一级）；
    「我是甘肃人」-> 甘肃。
    """
    if not text:
        return ""
    m = _LOC_PREFIX_RE.search(text)
    if m:
        rest = text[m.end():]
        seg = re.match(r"[\u4e00-\u9fff]{2,}", rest)
        if seg:
            cands = re.findall(
                r"[\u4e00-\u9fff]{1,4}(?:省|市|县|区|镇|乡|州|盟)",
                seg.group(0))
            if cands:
                return _strip_tail_particles(cands[-1])[:_SIGNAL_MAX_LEN]
    for m in _LOC_REN_RE.finditer(text):
        val = re.sub(r"人$", "", m.group(1)).strip()
        if val:
            return val[:_SIGNAL_MAX_LEN]
    return ""


def extract_profile_signals(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """从消息文本提取 (称呼, 偏好列表, 事实列表)；无匹配则空。"""
    if not text:
        return "", (), ()
    name = ""
    m = _NAME_RE.search(text)
    if m:
        name = re.sub(r"(?:就(?:行|好|可以))?[吧的哦啊呀啦呢]?$", "", m.group(1)).strip()
        if re.search(r"(?:的|同学|老师|朋友|学生|人|们)$", name) or len(name) < 1:
            name = ""
    prefs: list[str] = []
    for m in _PREF_RE.finditer(text):
        val = m.group(1).strip()
        if val in _PREF_BAD or not val:
            continue
        if val not in prefs:
            prefs.append(val[: _SIGNAL_MAX_LEN])
    facts: list[str] = []
    _LOC_PREFIX_WORDS = ("我住在", "我家在", "住在", "家住", "家在")
    for m in _FACT_RE.finditer(text):
        raw = m.group(0)
        if any(raw.startswith(w) for w in _LOC_PREFIX_WORDS):
            continue  # 位置类已结构化进 location 字段，不重复进事实
        val = m.group(1).strip()
        if val and val not in facts:
            facts.append(val[: _SIGNAL_MAX_LEN])
    return name, tuple(prefs), tuple(facts)


# ---- 数据模型 ----

@dataclass
class UserProfile:
    """跨会话长期偏好（USER_PROFILE）。"""
    actor_id: str
    platform: str = "qq"
    bot_id: str = "dududa"
    preferred_name: str = ""
    location: str = ""
    preferences: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    topic_counts: dict[str, int] = field(default_factory=dict)
    first_seen_ts: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def top_topics(self) -> tuple[str, ...]:
        return tuple(
            t for t, _ in sorted(
                self.topic_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )[:_MAX_TOPICS]

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "preferred_name": self.preferred_name,
            "location": self.location,
            "preferences": list(self.preferences),
            "facts": list(self.facts),
            "topic_counts": dict(self.topic_counts),
            "first_seen_ts": self.first_seen_ts,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserProfile":
        return cls(
            actor_id=str(data.get("actor_id", "")),
            platform=str(data.get("platform", "qq")),
            bot_id=str(data.get("bot_id", "dududa")),
            preferred_name=str(data.get("preferred_name", "")),
            location=str(data.get("location", "")),
            preferences=tuple(str(x) for x in data.get("preferences", ())),
            facts=tuple(str(x) for x in data.get("facts", ())),
            topic_counts={
                str(k): int(v) for k, v in (data.get("topic_counts") or {}).items()
            },
            first_seen_ts=float(data.get("first_seen_ts", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def summary_lines(self) -> tuple[str, ...]:
        """供 LLM 上下文使用的画像摘要（短、可审计）。"""
        lines: list[str] = []
        if self.preferred_name:
            lines.append(f"用户希望被称为「{self.preferred_name}」")
        if self.location:
            lines.append(f"用户所在地: {self.location}")
        if self.preferences:
            lines.append("用户偏好: " + "、".join(self.preferences[:_MAX_PREFERENCES]))
        if self.facts:
            lines.append("用户情况: " + "、".join(self.facts[:_MAX_FACTS]))
        return tuple(lines)


@dataclass
class SessionState:
    """单会话状态（SESSION_STATE）。"""
    conversation_id: str
    actor_id: str
    platform: str = "qq"
    bot_id: str = "dududa"
    message_count: int = 0
    last_intent: str = ""
    active_topics: tuple[str, ...] = ()
    last_ts: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "message_count": self.message_count,
            "last_intent": self.last_intent,
            "active_topics": list(self.active_topics),
            "last_ts": self.last_ts,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        return cls(
            conversation_id=str(data.get("conversation_id", "")),
            actor_id=str(data.get("actor_id", "")),
            platform=str(data.get("platform", "qq")),
            bot_id=str(data.get("bot_id", "dududa")),
            message_count=int(data.get("message_count", 0)),
            last_intent=str(data.get("last_intent", "")),
            active_topics=tuple(str(x) for x in data.get("active_topics", ())),
            last_ts=float(data.get("last_ts", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_profile_path() -> Path:
    override = os.environ.get("DUDUDA_PROFILE_FILE", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "data" / "profiles.json"


def _merge_topics(existing: tuple[str, ...], new_topics: tuple[str, ...]) -> tuple[str, ...]:
    merged = list(existing)
    for t in new_topics:
        if not t:
            continue
        if t in merged:
            merged.remove(t)
        merged.insert(0, t)
    return tuple(merged[:_MAX_TOPICS])


class ProfileStore:
    """用户画像 + 会话状态 JSON 存储（线程安全、原子写、fail-closed 加载）。"""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else default_profile_path()
        self._lock = threading.Lock()
        self._users: dict[str, UserProfile] = {}
        self._sessions: dict[str, SessionState] = {}
        self._load()

    # ---- 键 ----

    @staticmethod
    def _user_key(platform: str, bot_id: str, actor_id: str) -> str:
        return f"{platform}:{bot_id}:{actor_id}"

    @staticmethod
    def _session_key(conversation_id: str, actor_id: str) -> str:
        return f"{conversation_id}:{actor_id}"

    # ---- 读 ----

    def get_user(self, platform: str, bot_id: str, actor_id: str) -> Optional[UserProfile]:
        if not actor_id or actor_id == "unknown":
            return None
        with self._lock:
            return self._users.get(self._user_key(platform, bot_id, actor_id))

    def get_session(self, conversation_id: str, actor_id: str) -> Optional[SessionState]:
        if not conversation_id or not actor_id:
            return None
        with self._lock:
            return self._sessions.get(self._session_key(conversation_id, actor_id))

    # ---- 写（合并语义，幂等） ----

    def record_message(
        self,
        platform: str,
        bot_id: str,
        conversation_id: str,
        actor_id: str,
        text: str,
        intents: tuple[str, ...] = (),
        topics: tuple[str, ...] = (),
        engaged: bool = False,
        now: Optional[float] = None,
    ) -> None:
        """每条消息更新会话状态；engaged（@/命令/回复链）时学习用户画像信号。"""
        now = now if now is not None else time.time()
        if not actor_id or actor_id == "unknown":
            return
        platform = platform or "qq"
        bot_id = bot_id or "dududa"
        conversation_id = conversation_id or "unknown"

        with self._lock:
            s_key = self._session_key(conversation_id, actor_id)
            sess = self._sessions.get(s_key)
            if sess is None:
                sess = SessionState(
                    conversation_id=conversation_id, actor_id=actor_id,
                    platform=platform, bot_id=bot_id, last_ts=now)
            sess.message_count += 1
            if intents:
                sess.last_intent = intents[0]
            sess.active_topics = _merge_topics(sess.active_topics, tuple(topics) + tuple(intents))
            sess.last_ts = now
            sess.updated_at = now
            self._sessions[s_key] = sess

            if engaged:
                name, prefs, facts = extract_profile_signals(text or "")
                loc = extract_location(text or "")
                u_key = self._user_key(platform, bot_id, actor_id)
                user = self._users.get(u_key)
                if user is None:
                    user = UserProfile(
                        actor_id=actor_id, platform=platform, bot_id=bot_id,
                        first_seen_ts=now)
                if name:
                    user.preferred_name = name
                if loc:
                    user.location = loc
                if prefs:
                    merged = list(user.preferences)
                    for p in prefs:
                        if p not in merged:
                            merged.append(p)
                    user.preferences = tuple(merged[:_MAX_PREFERENCES])
                if facts:
                    merged = list(user.facts)
                    for f in facts:
                        if f not in merged:
                            merged.append(f)
                    user.facts = tuple(merged[:_MAX_FACTS])
                for t in tuple(topics) + tuple(intents):
                    if t:
                        user.topic_counts[t] = user.topic_counts.get(t, 0) + 1
                user.updated_at = now
                self._users[u_key] = user

        self._save()

    # ---- 持久化（原子写 + fail-closed 加载） ----

    def _save(self) -> None:
        data = {
            "users": [u.to_dict() for u in self._users.values()],
            "sessions": [s.to_dict() for s in self._sessions.values()],
        }
        tmp = str(self._path) + ".tmp"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, str(self._path))
        except OSError as e:
            logger.warning("Profile save failed (%s): %s", self._path, e)

    def _load(self) -> None:
        self._users = {}
        self._sessions = {}
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            quarantine = f"{self._path}.corrupt-{int(time.time())}"
            try:
                os.replace(str(self._path), quarantine)
            except OSError:
                quarantine = str(self._path)
            logger.warning("Profile file corrupted, quarantined to: %s", quarantine)
            return
        for item in data.get("users", ()) or ():
            try:
                user = UserProfile.from_dict(item)
            except (ValueError, KeyError, TypeError):
                continue
            if user.actor_id:
                self._users[self._user_key(user.platform, user.bot_id, user.actor_id)] = user
        for item in data.get("sessions", ()) or ():
            try:
                sess = SessionState.from_dict(item)
            except (ValueError, KeyError, TypeError):
                continue
            if sess.conversation_id and sess.actor_id:
                self._sessions[self._session_key(sess.conversation_id, sess.actor_id)] = sess

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "users": len(self._users),
            "sessions": len(self._sessions),
        }
