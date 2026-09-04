#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理历史 Trace 中的消息/回复/会话原文（一次性隐私清理）。

背景：旧版 TraceRecorder 会把 flow_start.msg（原始消息前 80 字）、
flow_end.reply（原始回复前 200 字）与 session（真实会话/群 ID）写进
data/traces/*.jsonl。新代码已改为只记录 msg_len / reply_len / session_hash，
并在落盘前统一 Redactor 脱敏；本脚本用于清理已经产生的历史文件。

默认处理 <repo>/data/traces（可用 --trace-dir 或环境变量 DUDUDA_TRACE_DIR 覆盖）。

用法：
    python ops/scrub_traces.py --dry-run                 # 只报告，不改写
    python ops/scrub_traces.py                           # 删除原文字段并脱敏剩余字符串
    python ops/scrub_traces.py --delete                  # 直接删除全部 trace 文件（轮转）
    python ops/scrub_traces.py --trace-dir /path/traces
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# 永远不应进入 Trace 的原始文本 / 身份字段。
DROP_KEYS = {
    "msg", "reply", "prompt", "completion", "messages",
    "user_message", "response", "content", "text", "args", "arguments",
}
IDENTITY_KEYS = {
    "session", "scope", "actor_id", "user_id", "sender_id",
    "conversation_id", "group_id",
}

# 与 safeguards.security.Redactor 保持一致的最小脱敏（脚本自包含，不依赖包导入）。
_CREDENTIAL_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"api[_-]?key[\"'=:\s]+[A-Za-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(
        r"(?:password|passwd|pwd|secret|access[_-]?token|refresh[_-]?token"
        r"|session[_-]?id|client[_-]?secret)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"cookie\s*[:=]\s*\S+", re.IGNORECASE),
)
_URL_AUTH_RE = re.compile(r"(https?://)([^/@\s]+)@")
_URL_QUERY_RE = re.compile(
    r"([?&])(token|key|secret|password|code|access_token|refresh_token|api_key|sign|sig)=[^&\s]+",
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    out = text
    for pat in _CREDENTIAL_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    out = _URL_AUTH_RE.sub(r"\1[REDACTED]@", out)
    out = _URL_QUERY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}=[REDACTED]", out)
    return out


def identifier_digest(value) -> str:
    return hashlib.sha256(
        str(value or "").encode("utf-8", "replace")).hexdigest()[:16]


def scrub_value(value):
    """Recursively remove content keys and pseudonymise identity keys."""
    if isinstance(value, str):
        return redact_text(value), 0
    if isinstance(value, dict):
        cleaned = {}
        changed = 0
        for key, item in value.items():
            name = str(key)
            normalized = name.casefold()
            if normalized in DROP_KEYS:
                changed += 1
                continue
            if normalized in IDENTITY_KEYS:
                cleaned[f"{name}_hash"] = identifier_digest(item)
                changed += 1
                continue
            cleaned[name], nested = scrub_value(item)
            changed += nested
        return cleaned, changed
    if isinstance(value, list):
        cleaned = []
        changed = 0
        for item in value:
            item, nested = scrub_value(item)
            cleaned.append(item)
            changed += nested
        return cleaned, changed
    return value, 0


def resolve_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("DUDUDA_TRACE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "data" / "traces"


def scrub_file(path: Path, delete: bool, dry_run: bool) -> tuple[int, bool]:
    """返回 (删除/脱敏的字段数, 是否删除了整个文件)。"""
    if delete:
        if not dry_run:
            path.unlink(missing_ok=True)
        return 0, True
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    out: list[str] = []
    dropped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            # A malformed line cannot be inspected structurally.  Keeping it
            # would preserve exactly the plaintext this command promises to
            # remove, so fail closed and discard it.
            dropped += 1
            continue
        obj, changed = scrub_value(obj)
        dropped += changed
        out.append(json.dumps(obj, ensure_ascii=False, default=str))
    if not dry_run:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(out) + ("\n" if out else ""),
                       encoding="utf-8")
        os.replace(tmp, path)
    return dropped, False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告，不改写任何文件")
    parser.add_argument("--delete", action="store_true",
                        help="直接删除全部 trace 文件（轮转清理）")
    parser.add_argument("--trace-dir", default=None,
                        help="trace 目录（默认 DUDUDA_TRACE_DIR 或 <repo>/data/traces）")
    args = parser.parse_args(argv)

    directory = resolve_dir(args.trace_dir)
    if not directory.exists():
        print(f"[scrub_traces] 目录不存在：{directory}", file=sys.stderr)
        print("请用 --trace-dir 指向实际的 DUDUDA_TRACE_DIR。", file=sys.stderr)
        return 2
    files = sorted(directory.glob("*.jsonl"))
    if not files:
        print(f"[scrub_traces] 无 trace 文件：{directory}")
        return 0

    total_dropped = 0
    total_deleted = 0
    for f in files:
        dropped, deleted = scrub_file(f, args.delete, args.dry_run)
        total_dropped += dropped
        total_deleted += 1 if deleted else 0
        print(f"  {'[dry-run] ' if args.dry_run else ''}{f.name}: "
              f"dropped={dropped}" + (" deleted" if deleted else ""))

    mode = "dry-run" if args.dry_run else ("deleted" if args.delete else "scrubbed")
    print(f"[scrub_traces] {mode} 完成：文件数={len(files)}，"
          f"删除原文字段={total_dropped}，删除文件={total_deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
