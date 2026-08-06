# -*- coding: utf-8 -*-
"""Perception 落盘：结构化 PerceptionRecord 追加到 data/perceptions/YYYY-MM-DD.jsonl。

P0/P1（文档 2.5.4 / 2.5.10）：每条消息的感知结果入库，供 Eval、用户画像与
后续 WebUI 使用。纯标准库、平台无关；目录可用环境变量 DUDUDA_PERCEPTION_DIR
覆盖（默认 <repo>/data/perceptions，按本文件位置推导，不依赖进程 cwd）。
记录失败绝不抛出，保证不阻断消息流。
"""
import json
import os
import threading
import time
from pathlib import Path

from .perception import PerceptionResult

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "perceptions"


def _perception_dir() -> Path:
    return Path(os.environ.get("DUDUDA_PERCEPTION_DIR", str(_DEFAULT_DIR)))


class PerceptionStore:
    """线程安全的 JSONL 追加记录器（与 TraceRecorder 同模式）。"""

    def __init__(self, directory=None):
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        """追加一条 PerceptionRecord；任何异常都被吞掉，不阻断调用方。"""
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "ts_ms": int(time.time() * 1000),
                **fields,
            }
            directory = self._directory or _perception_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (time.strftime("%Y-%m-%d") + ".jsonl")
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def lines_for(self, day=None) -> list:
        """读取某天（默认今天）的全部 PerceptionRecord。"""
        directory = self._directory or _perception_dir()
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
                    out.append(json.loads(line))
        except Exception:
            pass
        return out


perception_store = PerceptionStore()


def record_state_perception(perception, state, source: str = "rule") -> None:
    """把一次感知结果结构化入库（绑定 run/trace 与会话上下文）。

    任何异常都被吞掉，不阻断 Orchestrator 主流程。
    """
    try:
        envelope = getattr(state, "envelope", None)
        platform = ""
        conversation_id = ""
        actor_id = ""
        text = ""
        if envelope is not None:
            platform = getattr(getattr(envelope, "platform", None),
                               "value", "") or ""
            conv = getattr(envelope, "conversation", None)
            if conv is not None:
                conversation_id = getattr(conv, "conversation_id", "") or ""
            sender = getattr(envelope, "sender", None)
            if sender is not None:
                actor_id = getattr(sender, "actor_id", "") or ""
            text = getattr(envelope, "text", "") or ""
        record = perception.to_record(
            run_id=getattr(state, "run_id", "") or "",
            trace_id=getattr(state, "trace_id", "") or "",
            platform=platform,
            conversation_id=conversation_id,
            actor_id=actor_id,
            text=text,
            source=source,
        )
        perception_store.record(**record.to_dict())
    except Exception:
        pass