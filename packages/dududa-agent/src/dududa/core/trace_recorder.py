# -*- coding: utf-8 -*-
"""Trace 落盘：将 Flow/Run 事件追加到 data/traces/YYYY-MM-DD.jsonl。

P2 Phase 9（文档 2.5.10）：trace_id 关联事件持久化，供 Eval 与后续
WebUI/Trace Viewer 使用。纯标准库、平台无关；目录可用环境变量
DUDUDA_TRACE_DIR 覆盖（默认 <repo>/data/traces，按本文件位置推导，
不依赖进程 cwd）。记录失败绝不抛出，保证不阻断消息流。
"""
import json
import os
import threading
import time
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "traces"


def _trace_dir() -> Path:
    return Path(os.environ.get("DUDUDA_TRACE_DIR", str(_DEFAULT_DIR)))


class TraceRecorder:
    """线程安全的 JSONL 追加记录器。"""

    def __init__(self, directory=None):
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        """追加一条 trace 事件；任何异常都被吞掉，不阻断调用方。"""
        try:
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
