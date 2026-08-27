# -*- coding: utf-8 -*-
"""Versioned, review-only meme candidate library.

Matches merely nominate a message for semantic review.  They never authorize a
reply by themselves.  Unknown-phrase statistics contain no sender/group ids or
surrounding messages and can only become custom entries through admin action.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class MemeCandidate:
    key: str
    tier: str
    meaning: str
    evidence: str
    confidence: float


_BASIC = {
    "yyds": ("永远的神，用于夸赞", ("yyds", "永远的神")),
    "emo": ("表达低落、感伤或自嘲", ("emo", "我emo了", "开始emo")),
    "打工人": ("对工作状态的自嘲", ("打工人", "打工魂")),
    "摸鱼": ("工作或学习间隙偷闲", ("摸鱼", "摸会儿鱼", "划水")),
    "栓q": ("谐音 thank you，常用于无奈吐槽", ("栓q", "栓Q", "thank you")),
    "绝绝子": ("强调很绝，语义可褒可贬", ("绝绝子", "绝绝紫")),
}

_HOT = {
    "包的": ("表示肯定、没问题", ("包的", "包稳的")),
    "已老实": ("表示被现实教育后的自嘲", ("已老实", "求放过")),
    "city不city": ("询问是否时髦、有城市感的玩笑表达", ("city不city",)),
}


def _normalise(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _pinyin(value: str) -> str:
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(value, errors="ignore")).lower()
    except Exception:
        return _normalise(value)


class MemeLibrary:
    def __init__(self, state_path: str | None = None):
        self._state_path = str(state_path or "")
        self._custom: dict[
            str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        self._unknown: dict[str, int] = {}
        self._dirty_observations = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle) or {}
            custom = raw.get("custom_by_group", {})
            for group_id, entries in custom.items():
                if not isinstance(entries, dict):
                    continue
                target = self._custom.setdefault(str(group_id), {})
                for key, item in entries.items():
                    if not isinstance(item, dict):
                        continue
                    meaning = str(item.get("meaning", "") or "").strip()
                    aliases = tuple(
                        str(v).strip() for v in item.get("aliases", ())
                        if str(v).strip())
                    if key and meaning:
                        target[str(key)] = (
                            meaning, aliases or (str(key),))
            self._unknown = {
                str(k): max(0, int(v)) for k, v in
                dict(raw.get("unknown_counts", {})).items()
                if str(k).strip()
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _save_locked(self) -> None:
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp = self._state_path + ".tmp"
            payload = {
                "version": 1,
                "custom_by_group": {
                    group_id: {
                        key: {"meaning": meaning, "aliases": list(aliases)}
                        for key, (meaning, aliases) in sorted(entries.items())
                    }
                    for group_id, entries in sorted(self._custom.items())
                },
                "unknown_counts": dict(sorted(
                    self._unknown.items(), key=lambda item: (-item[1], item[0]))[:500]),
            }
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
            self._dirty_observations = 0
        except OSError:
            return

    def _entries(self, group_id: str = ""):
        for tier, source in (("basic", _BASIC), ("hot", _HOT)):
            for key, (meaning, aliases) in source.items():
                yield tier, key, meaning, aliases
        for key, (meaning, aliases) in self._custom.get(
                str(group_id or ""), {}).items():
            yield "custom", key, meaning, aliases

    def match(self, text: str, *, group_id: str = "") -> MemeCandidate | None:
        value = _normalise(text)
        if len(value) < 2 or len(value) > 180:
            return None
        phonetic = _pinyin(value)
        best = None
        for tier, key, meaning, aliases in self._entries(group_id):
            for alias in aliases:
                norm_alias = _normalise(alias)
                if not norm_alias:
                    continue
                if norm_alias in value:
                    score, evidence = 1.0, alias
                else:
                    alias_pinyin = _pinyin(norm_alias)
                    if len(alias_pinyin) < 5:
                        continue
                    if alias_pinyin in phonetic:
                        score = 0.92
                    else:
                        score = SequenceMatcher(
                            None, alias_pinyin, phonetic).ratio()
                    if score < 0.88:
                        continue
                    evidence = f"拼音近似:{alias}"
                candidate = MemeCandidate(
                    key=key, tier=tier, meaning=meaning,
                    evidence=evidence, confidence=score)
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        return best

    def observe_unknown(self, text: str, *, group_id: str = "") -> None:
        value = " ".join(str(text or "").split()).strip()
        norm = _normalise(value)
        if (not norm or len(norm) < 2 or len(norm) > 18
                or re.search(r"\d{5,}", norm)
                or self.match(value, group_id=group_id) is not None):
            return
        with self._lock:
            self._unknown[norm] = self._unknown.get(norm, 0) + 1
            self._dirty_observations += 1
            if self._dirty_observations >= 20:
                self._save_locked()

    def candidates(self, *, min_count: int = 3, limit: int = 30
                   ) -> tuple[tuple[str, int], ...]:
        with self._lock:
            items = [(phrase, count) for phrase, count in self._unknown.items()
                     if count >= max(1, int(min_count))]
        items.sort(key=lambda item: (-item[1], item[0]))
        return tuple(items[:max(1, int(limit))])

    def add_custom(self, group_id: str, key: str, meaning: str,
                   aliases=()) -> bool:
        gid = str(group_id or "").strip()
        name = " ".join(str(key or "").split()).strip()[:40]
        desc = " ".join(str(meaning or "").split()).strip()[:200]
        alias_values = tuple(dict.fromkeys(
            [name] + [" ".join(str(v).split()).strip()[:40]
                      for v in aliases if str(v).strip()]))
        if not gid or not name or not desc:
            return False
        with self._lock:
            self._custom.setdefault(gid, {})[name] = (desc, alias_values)
            self._unknown.pop(_normalise(name), None)
            self._save_locked()
        return True

    def remove_custom(self, group_id: str, key: str) -> bool:
        gid = str(group_id or "").strip()
        with self._lock:
            entries = self._custom.get(gid, {})
            removed = entries.pop(str(key or "").strip(), None) is not None
            if not entries:
                self._custom.pop(gid, None)
            if removed:
                self._save_locked()
            return removed

    def custom_entries(
        self, group_id: str
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        with self._lock:
            return tuple((key, value[0], value[1])
                         for key, value in sorted(
                             self._custom.get(str(group_id or ""), {}).items()))
