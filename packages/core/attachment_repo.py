# -*- coding: utf-8 -*-
"""受信 Attachment Repository（文档 2.4.2 Multimodal Preprocessor）。

附件正文先进入受信仓库；Core 只接收 opaque content_ref 与受限摘要。
仓库职责：不透明引用、TTL、有界容量、按 会话+用户 隔离、fail-closed。
原始 URL / base64 / 文件正文不进入 Trace（本模块不记录内容）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

DEFAULT_TTL_SECONDS = 60.0
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_BYTES_PER_ENTRY = 20 * 1024 * 1024   # 单条 20MB
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024      # 总量 200MB


@dataclass(frozen=True)
class AttachmentRef:
    """Core 可见的不透明引用：ref + 受限元数据（无路径 / URL / 正文）。"""
    ref: str
    name: str
    mime: str
    kind: str                 # image | file
    size: int                 # 已物化字节数（URL 惰性条目为 0）
    summary: str = ""         # 受限摘要（OCR / 图片描述，Core 可用）


@dataclass(frozen=True)
class AttachmentRecord:
    """仓库取出的完整记录（仅受信边界持有，不进入 Core 状态）。"""
    ref: str
    name: str
    mime: str
    kind: str
    data: bytes = b""         # 已物化字节
    source_url: str = ""      # 仅 http(s)，惰性下载
    summary: str = ""
    platform: str = ""
    conversation_id: str = ""
    actor_id: str = ""
    created_at: float = 0.0


@dataclass
class _Entry:
    scope: tuple[str, str, str]
    name: str
    mime: str
    kind: str
    data: bytes = b""
    source_url: str = ""
    summary: str = ""
    created_at: float = 0.0


class AttachmentRepository:
    """有界 TTL 受信附件仓库（进程内，线程安全，fail-closed）。

    put 失败（参数非法 / 超大 / 超配额 / 已满）一律返回 None，不部分写入；
    get / take 对未知引用、过期条目、越界会话或用户一律返回 None。
    """

    def __init__(self,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_bytes_per_entry: int = DEFAULT_MAX_BYTES_PER_ENTRY,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES):
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._max_bytes_per_entry = int(max_bytes_per_entry)
        self._max_total_bytes = int(max_total_bytes)
        self._entries: dict[str, _Entry] = {}
        self._scope_refs: dict[tuple[str, str, str], list[str]] = {}
        self._total_bytes = 0
        self._lock = threading.Lock()

    # ---- 写入 ----

    def put(self, platform: str, conversation_id: str, actor_id: str, *,
            name: str, mime: str = "", kind: str = "file",
            data: bytes = b"", source_url: str = "",
            summary: str = "") -> Optional[AttachmentRef]:
        """入仓。返回不透明 AttachmentRef；失败返回 None（fail-closed）。

        来源必须且只能提供一种：data（已物化字节）或 source_url（http(s) 惰性）。
        """
        if not name or kind not in ("image", "file"):
            return None
        if bool(data) == bool(source_url):
            return None
        if source_url and not (source_url.startswith("http://")
                               or source_url.startswith("https://")):
            return None
        if self._max_entries <= 0:
            return None
        size = len(data)
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            if size > self._max_bytes_per_entry:
                return None
            if size and self._total_bytes + size > self._max_total_bytes:
                return None
            if len(self._entries) >= self._max_entries:
                oldest = min(self._entries,
                             key=lambda r: self._entries[r].created_at)
                self._delete_locked(oldest)
            ref = uuid4().hex
            scope = (str(platform), str(conversation_id), str(actor_id))
            self._entries[ref] = _Entry(
                scope=scope, name=name, mime=mime, kind=kind,
                data=bytes(data), source_url=source_url, summary=summary,
                created_at=now,
            )
            self._scope_refs.setdefault(scope, []).append(ref)
            self._total_bytes += size
            return AttachmentRef(ref=ref, name=name, mime=mime, kind=kind,
                                 size=size, summary=summary)

    # ---- 读取（全部要求会话+用户，越界即 None）----

    def get(self, ref: str, platform: str, conversation_id: str,
            actor_id: str) -> Optional[AttachmentRecord]:
        with self._lock:
            self._evict_locked(time.monotonic())
            return self._get_locked(ref, platform, conversation_id, actor_id)

    def take(self, ref: str, platform: str, conversation_id: str,
             actor_id: str) -> Optional[AttachmentRecord]:
        """取出并删除（配对场景：take-once）。"""
        with self._lock:
            self._evict_locked(time.monotonic())
            rec = self._get_locked(ref, platform, conversation_id, actor_id)
            if rec is not None:
                self._delete_locked(ref)
            return rec

    def take_scope(self, platform: str, conversation_id: str,
                   actor_id: str) -> Optional[AttachmentRecord]:
        """取该会话+用户最新一条并删除（群图配对，等价旧单槽 slot.pop）。"""
        with self._lock:
            self._evict_locked(time.monotonic())
            scope = (str(platform), str(conversation_id), str(actor_id))
            refs = self._scope_refs.get(scope) or []
            if not refs:
                return None
            ref = refs[-1]
            rec = self._get_locked(ref, platform, conversation_id, actor_id)
            if rec is not None:
                self._delete_locked(ref)
            return rec

    # ---- 维护 ----

    def sweep(self) -> int:
        """清除过期条目，返回清除数。"""
        with self._lock:
            return self._evict_locked(time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._scope_refs.clear()
            self._total_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    # ---- 内部 ----

    def _get_locked(self, ref: str, platform: str, conversation_id: str,
                    actor_id: str) -> Optional[AttachmentRecord]:
        entry = self._entries.get(ref)
        if entry is None:
            return None
        scope = (str(platform), str(conversation_id), str(actor_id))
        if entry.scope != scope:
            return None
        return AttachmentRecord(
            ref=ref, name=entry.name, mime=entry.mime, kind=entry.kind,
            data=entry.data, source_url=entry.source_url,
            summary=entry.summary, platform=scope[0],
            conversation_id=scope[1], actor_id=scope[2],
            created_at=entry.created_at,
        )

    def _evict_locked(self, now: float) -> int:
        expired = [r for r, e in self._entries.items()
                   if now - e.created_at >= self._ttl]
        for r in expired:
            self._delete_locked(r)
        return len(expired)

    def _delete_locked(self, ref: str) -> None:
        entry = self._entries.pop(ref, None)
        if entry is None:
            return
        self._total_bytes -= len(entry.data)
        refs = self._scope_refs.get(entry.scope)
        if refs:
            try:
                refs.remove(ref)
            except ValueError:
                pass
            if not refs:
                del self._scope_refs[entry.scope]


__all__ = ["AttachmentRef", "AttachmentRecord", "AttachmentRepository"]
