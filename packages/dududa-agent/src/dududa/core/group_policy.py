# -*- coding: utf-8 -*-
"""群策略（文档 2.5.2 / 2.5.4）：mode / reply_rate / meme_rate 的存储与投影。

- GroupPolicyStore：按 group_id 持久化 JSON（原子写，进程内线程安全）。
- mode:
    normal  默认。@/命令/回复链必回；未被 @ 时按 reply_rate 概率被动参与。
    silent  只回 @/命令/回复链；不被动参与（reply_rate 被忽略）。
    off     群内完全沉默（@ 也不回）；框架命令（dududa_mode 等）仍可恢复。
- reply_rate: 0..1，未被 @ 时被动参与概率；默认 0.0（不主动插话，保持现状）。
- meme_rate:  0..1，问候/单表情等轻松消息走 REACT 表情回复的比例；
    未命中回退 DIRECT_REPLY 文本回复（保证 @ 消息必回）；默认 1.0（保持现状）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .context import PolicyView

GROUP_MODES = ("normal", "silent", "off")


def _clamp01(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, v))


@dataclass(frozen=True)
class GroupPolicy:
    """单个群的策略快照。"""
    mode: str = "normal"
    reply_rate: float = 0.0
    meme_rate: float = 1.0
    interruption_cost: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GroupPolicy":
        mode = raw.get("mode", "normal")
        if mode not in GROUP_MODES:
            mode = "normal"
        return cls(
            mode=mode,
            reply_rate=_clamp01(raw.get("reply_rate", 0.0)),
            meme_rate=_clamp01(raw.get("meme_rate", 1.0)),
            interruption_cost=_clamp01(raw.get("interruption_cost", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0) or 0.0),
        )

    def to_policy_view(self) -> PolicyView:
        """投影为引擎可读的 PolicyView（reply_rate/meme_rate/mode）。"""
        return PolicyView(
            reply_rate=self.reply_rate,
            meme_rate=self.meme_rate,
            mode=self.mode,
            interruption_cost=self.interruption_cost,
        )


class GroupPolicyStore:
    """按群持久化的策略仓库（JSON 文件，原子写）。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        self._groups: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._groups = {
                    str(k): v for k, v in data.items() if isinstance(v, dict)
                }
        except Exception:
            self._groups = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._groups, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ---- 读写 ----

    def get(self, group_id: str) -> Optional[GroupPolicy]:
        """未配置返回 None（调用方保持原有行为）。"""
        with self._lock:
            raw = self._groups.get(str(group_id))
        if not raw:
            return None
        return GroupPolicy.from_dict(raw)

    def set(self, group_id: str, mode: Optional[str] = None,
            reply_rate: Optional[float] = None,
            meme_rate: Optional[float] = None,
            interruption_cost: Optional[float] = None) -> GroupPolicy:
        gid = str(group_id)
        if mode is not None and mode not in GROUP_MODES:
            raise ValueError("mode 必须是 normal/silent/off 之一")
        with self._lock:
            raw = dict(self._groups.get(gid, {}))
            if mode is not None:
                raw["mode"] = mode
            if reply_rate is not None:
                raw["reply_rate"] = _clamp01(reply_rate)
            if meme_rate is not None:
                raw["meme_rate"] = _clamp01(meme_rate)
            if interruption_cost is not None:
                raw["interruption_cost"] = _clamp01(interruption_cost)
            raw["updated_at"] = time.time()
            self._groups[gid] = raw
            self._save()
            return GroupPolicy.from_dict(raw)

    def all(self) -> dict[str, GroupPolicy]:
        with self._lock:
            return {
                gid: GroupPolicy.from_dict(raw)
                for gid, raw in self._groups.items()
            }

    def to_policy_view(self, group_id: str) -> PolicyView:
        """引擎/上下文投影：未配置返回默认 PolicyView（引擎语义不变）。"""
        policy = self.get(group_id)
        if policy is None:
            return PolicyView()
        return policy.to_policy_view()

    @property
    def path(self) -> str:
        return self._path
