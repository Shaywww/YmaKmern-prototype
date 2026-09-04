# -*- coding: utf-8 -*-
"""Trace 落盘：将 Flow/Run 事件追加到 data/traces/YYYY-MM-DD.jsonl。

P2 Phase 9（文档 2.5.10）：trace_id 关联事件持久化，供 Eval 与后续
WebUI/Trace Viewer 使用。纯标准库、平台无关；目录可用环境变量
DUDUDA_TRACE_DIR 覆盖（默认 <repo>/data/traces，按本文件位置推导，
不依赖进程 cwd）。记录失败绝不抛出，保证不阻断消息流。
"""
import json
import hashlib
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "traces"


def _trace_dir() -> Path:
    return Path(os.environ.get("DUDUDA_TRACE_DIR", str(_DEFAULT_DIR)))


_REDACTOR = None

# Content-bearing fields are never valid Trace metadata.  Keep this guard at
# the sink as well as at call sites so a future feature cannot accidentally
# reintroduce plaintext by passing ``msg=...`` or a nested model payload.
_RAW_CONTENT_KEYS = frozenset({
    "msg", "reply", "prompt", "completion", "messages", "user_message",
    "response", "content", "text", "args", "arguments",
})
_IDENTITY_KEYS = frozenset({
    "session", "scope", "actor_id", "user_id", "sender_id",
    "conversation_id", "group_id",
})


def _get_redactor():
    """惰性加载共享 Redactor，避免模块导入期的循环依赖。"""
    global _REDACTOR
    if _REDACTOR is None:
        from dududa.safeguards.security import Redactor
        _REDACTOR = Redactor()
    return _REDACTOR


def _identifier_digest(value) -> str:
    encoded = str(value or "").encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _sanitize_trace_value(value):
    """Drop raw content and pseudonymise identity fields recursively."""
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            name = str(key)
            normalized = name.casefold()
            if normalized in _RAW_CONTENT_KEYS:
                continue
            if normalized in _IDENTITY_KEYS:
                cleaned[f"{name}_hash"] = _identifier_digest(item)
                continue
            cleaned[name] = _sanitize_trace_value(item)
        return cleaned
    if isinstance(value, tuple):
        return tuple(_sanitize_trace_value(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_trace_value(item) for item in value]
    return value


class TraceRecorder:
    """线程安全的 JSONL 追加记录器。"""

    def __init__(self, directory=None):
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        """追加一条 trace 事件；任何异常都被吞掉，不阻断调用方。

        落盘前统一经共享 Redactor 脱敏（文档 2.4.24 / 2.5.10）：凭据、
        URL user-info/query 与嵌套结构不会进入 Trace。调用方仍应避免传入
        消息/回复原文，只传长度、哈希或枚举元数据。
        """
        try:
            fields = _sanitize_trace_value(fields)
            fields, _ = _get_redactor().redact(fields)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "ts_ms": int(time.time() * 1000),
                **fields,
            }
            directory = self._directory or _trace_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (time.strftime("%Y-%m-%d") + ".jsonl")
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def lines_for(self, day=None) -> list:
        """读取某天（默认今天）的全部 trace 事件。"""
        directory = self._directory or _trace_dir()
        day = day or time.strftime("%Y-%m-%d")
        path = directory / (day + ".jsonl")
        if not path.exists():
            return []
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out


trace_recorder = TraceRecorder()
