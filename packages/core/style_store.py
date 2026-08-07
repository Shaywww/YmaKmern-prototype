# -*- coding: utf-8 -*-
"""用户 style 偏好（文档 2.5.8）：platform + Bot + user + Persona 四维隔离。

- UserStyle：跨会话长期表达偏好（称呼/语气/长度/表情），按 platform+bot+user+persona 隔离；
- origin_conversation / visibility 随偏好保留，跨会话读取走具名 selector get()，
  不通过删除来源 Scope 实现；
- extract_style_signals：规则提取（无 LLM 调用，确定性可测）。
默认文件 <repo>/data/styles.json，可用环境变量 DUDUDA_STYLE_FILE 覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("dududa20.style")

_FIELD_MAX_LEN = 32
_ORIGIN_MAX_LEN = 64

# ---- 规则提取（确定性，无模型） ----

# 称呼：排除裸「叫我」（多为命令），只收明确的风格式请求
_ADDRESS_RE = re.compile(
    r"(?:以后叫我|以后就叫我|你可以叫我|请叫我|称呼我|叫我一声)"
    r"\s*([一-龥A-Za-z0-9_\-]{1,6})"
)

_TONE_PATTERNS = {
    "formal": ("正式", "官方", "严肃", "正经"),
    "casual": ("随意", "轻松", "活泼", "皮一点", "俏皮", "搞笑"),
    "gentle": ("温柔", "亲切", "暖心"),
}

_LENGTH_PATTERNS = {
    "short": ("简短", "简洁", "短一点", "少说", "别啰嗦", "别废话", "说重点", "简单点", "别太长"),
    "detailed": ("详细", "详细点", "多说点", "展开说", "长一点", "具体点", "仔细说说"),
}

# 表情：先查关闭词，避免「别用表情」命中通用「表情」
_EMOJI_PATTERNS = {
    "off": ("别用表情", "不要表情", "少用表情", "别卖萌", "别用颜文字"),
    "on": ("表情", "颜文字", "卖萌", "可爱点", "萌一点"),
}


@dataclass(frozen=True)
class StyleSignals:
    """一次消息中提取到的风格信号（全空表示无信号）。"""
    address: str = ""
    tone: str = ""
    length: str = ""
    emoji: str = ""

    @property
    def empty(self) -> bool:
        return not (self.address or self.tone or self.length or self.emoji)


def extract_style_signals(text: str) -> StyleSignals:
    """从消息文本提取风格信号；无匹配返回空 StyleSignals。"""
    if not text:
        return StyleSignals()
    sig = StyleSignals()
    m = _ADDRESS_RE.search(text)
    if m:
        name = re.sub(r"[的啦哦啊呢呀吧]?$", "", m.group(1)).strip()
        if name:
            sig = StyleSignals(address=name[:_FIELD_MAX_LEN])
    for tone, kws in _TONE_PATTERNS.items():
        if any(k in text for k in kws):
            sig = StyleSignals(address=sig.address, tone=tone)
            break
    for length, kws in _LENGTH_PATTERNS.items():
        if any(k in text for k in kws):
            sig = StyleSignals(address=sig.address, tone=sig.tone, length=length)
            break
    for emoji, kws in _EMOJI_PATTERNS.items():
        if any(k in text for k in kws):
            sig = StyleSignals(address=sig.address, tone=sig.tone,
                               length=sig.length, emoji=emoji)
            break
    return sig


# ---- 数据模型 ----


@dataclass
class UserStyle:
    """用户 style 偏好（USER_STYLE）：四维键 platform+bot+user+persona。

    origin_conversation：最近一次来源会话（保留来源，不做删除式跨会话）；
    visibility：public/private（群聊公开 / 私聊私有）。
    """
    platform: str = "qq"
    bot_id: str = "dududa"
    user_id: str = ""
    persona_id: str = "dududa_default"
    address: str = ""
    tone: str = ""
    length: str = ""
    emoji: str = ""
    origin_conversation: str = ""
    visibility: str = "public"
    first_seen_ts: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    _TONE_LABELS = {"formal": "正式", "casual": "随意活泼", "gentle": "温柔"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
            "persona_id": self.persona_id,
            "address": self.address,
            "tone": self.tone,
            "length": self.length,
            "emoji": self.emoji,
            "origin_conversation": self.origin_conversation,
            "visibility": self.visibility,
            "first_seen_ts": self.first_seen_ts,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserStyle":
        return cls(
            platform=str(data.get("platform", "qq")),
            bot_id=str(data.get("bot_id", "dududa")),
            user_id=str(data.get("user_id", "")),
            persona_id=str(data.get("persona_id", "dududa_default")),
            address=str(data.get("address", "")),
            tone=str(data.get("tone", "")),
            length=str(data.get("length", "")),
            emoji=str(data.get("emoji", "")),
            origin_conversation=str(data.get("origin_conversation", "")),
            visibility=str(data.get("visibility", "public")),
            first_seen_ts=float(data.get("first_seen_ts", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def summary_lines(self) -> tuple[str, ...]:
        """供 LLM 上下文使用的风格摘要（不含来源，避免提示词噪音）。"""
        parts: list[str] = []
        if self.address:
            parts.append(f"称呼「{self.address}」")
        if self.tone in self._TONE_LABELS:
            parts.append(f"语气{self._TONE_LABELS[self.tone]}")
        if self.length == "short":
            parts.append("回复简短")
        elif self.length == "detailed":
            parts.append("回复详细")
        if self.emoji == "on":
            parts.append("可用颜文字/表情")
        elif self.emoji == "off":
            parts.append("少用表情")
        if not parts:
            return ()
        return (f"用户风格: {'；'.join(parts)}。",)

    def display(self) -> str:
        """完整视图（含来源会话与可见性），供 dududa_style 命令与审计。"""
        tone = self._TONE_LABELS.get(self.tone, self.tone or "未设置")
        length = ("简短" if self.length == "short"
                  else "详细" if self.length == "detailed" else "未设置")
        emoji = ("开" if self.emoji == "on"
                 else "关" if self.emoji == "off" else "未设置")
        return ("；".join([
            f"称呼: {self.address or '未设置'}",
            f"语气: {tone}",
            f"长度: {length}",
            f"表情: {emoji}",
            f"来源会话: {self.origin_conversation or '无'}",
            f"可见性: {self.visibility}",
        ]))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_style_path() -> Path:
    override = os.environ.get("DUDUDA_STYLE_FILE", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "data" / "styles.json"


class UserStyleStore:
    """用户 style JSON 存储：四维隔离键 + 具名 selector（线程安全、原子写、fail-closed）。"""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else default_style_path()
        self._lock = threading.Lock()
        self._styles: dict[str, UserStyle] = {}
        self._load()

    # ---- 键 ----

    @staticmethod
    def _key(platform: str, bot_id: str, user_id: str, persona_id: str) -> str:
        return f"{platform}:{bot_id}:{user_id}:{persona_id}"

    # ---- 具名 selector（跨会话读取只走这里，不修改来源 Scope） ----

    def get(self, platform: str, bot_id: str, user_id: str,
            persona_id: str = "dududa_default") -> Optional[UserStyle]:
        if not user_id or user_id == "unknown":
            return None
        with self._lock:
            return self._styles.get(self._key(
                platform or "qq", bot_id or "dududa", user_id,
                persona_id or "dududa_default"))

    def list_for_persona(self, platform: str, bot_id: str,
                         persona_id: str) -> tuple[UserStyle, ...]:
        """具名 selector：按 Persona 枚举全部用户风格（管理/审计用）。"""
        prefix = f"{platform}:{bot_id}:"
        suffix = f":{persona_id}"
        with self._lock:
            return tuple(s for k, s in self._styles.items()
                         if k.startswith(prefix) and k.endswith(suffix))

    # ---- 写（合并语义） ----

    def set(self, platform: str, bot_id: str, user_id: str, persona_id: str,
            *, origin_conversation: str = "", visibility: str = "public",
            address: Optional[str] = None, tone: Optional[str] = None,
            length: Optional[str] = None, emoji: Optional[str] = None,
            now: Optional[float] = None) -> UserStyle:
        now = now if now is not None else time.time()
        key = self._key(platform or "qq", bot_id or "dududa", user_id,
                        persona_id or "dududa_default")
        with self._lock:
            existing = self._styles.get(key)
            raw: dict[str, Any] = existing.to_dict() if existing else {
                "platform": platform or "qq", "bot_id": bot_id or "dududa",
                "user_id": user_id, "persona_id": persona_id or "dududa_default",
                "first_seen_ts": now,
            }
            if origin_conversation:
                raw["origin_conversation"] = str(origin_conversation)[:_ORIGIN_MAX_LEN]
            if visibility:
                raw["visibility"] = str(visibility)
            if address is not None:
                raw["address"] = str(address)[:_FIELD_MAX_LEN]
            if tone is not None:
                raw["tone"] = str(tone)[:_FIELD_MAX_LEN]
            if length is not None:
                raw["length"] = str(length)[:_FIELD_MAX_LEN]
            if emoji is not None:
                raw["emoji"] = str(emoji)[:_FIELD_MAX_LEN]
            raw["updated_at"] = now
            style = UserStyle.from_dict(raw)
            self._styles[key] = style
        self._save()
        return style

    def record_message(self, platform: str, bot_id: str, conversation_id: str,
                        user_id: str, persona_id: str, text: str,
                        engaged: bool = False, visibility: str = "public",
                        now: Optional[float] = None) -> None:
        """engaged（@/命令/回复链）时学习风格信号；无信号或非 engaged 不写入。"""
        if not user_id or user_id == "unknown":
            return
        if not engaged:
            return
        sig = extract_style_signals(text or "")
        if sig.empty:
            return
        self.set(platform, bot_id, user_id, persona_id,
                  origin_conversation=conversation_id or "",
                  visibility=visibility,
                  address=sig.address or None, tone=sig.tone or None,
                  length=sig.length or None, emoji=sig.emoji or None,
                  now=now)

    # ---- 持久化（原子写 + fail-closed 加载） ----

    def _save(self) -> None:
        data = {"styles": [s.to_dict() for s in self._styles.values()]}
        tmp = str(self._path) + ".tmp"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, str(self._path))
        except OSError as e:
            logger.warning("Style save failed (%s): %s", self._path, e)

    def _load(self) -> None:
        self._styles = {}
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
            logger.warning("Style file corrupted, quarantined to: %s", quarantine)
            return
        for item in data.get("styles", ()) or ():
            try:
                style = UserStyle.from_dict(item)
            except (ValueError, KeyError, TypeError):
                continue
            if style.user_id:
                self._styles[self._key(style.platform, style.bot_id,
                                       style.user_id, style.persona_id)] = style

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "styles": len(self._styles),
        }
