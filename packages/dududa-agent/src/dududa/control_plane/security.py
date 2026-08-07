"""Control Plane 安全基线（ADR-0001 CP-P0）。

- 鉴权：DUDUDA_CP_TOKEN 请求级 token（Authorization: Bearer 或 X-CP-Token），
  未配置 / 缺失 / 错误 -> 401（fail closed）；/health 豁免（存活探针）。
- 权限：写操作经 PermissionEngine（manage_config，仅 owner）；
  操作者身份与角色由 X-CP-Operator / X-CP-Role 声明（默认 cp_owner/owner）。
- 审计：每个受保护请求写 JSONL（DUDUDA_CP_AUDIT，默认 data/cp_audit.jsonl）。
- 脱敏：出参统一经共享 Redactor（凭证 / URL query / 嵌套结构）。
- Scope：非 owner 操作者只能看到 metadata.actor == 自己的 Trace 事件。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..safeguards.security import PermissionEngine, Redactor

logger = logging.getLogger("dududa20.control_plane.security")

TOKEN_ENV = "DUDUDA_CP_TOKEN"
AUDIT_ENV = "DUDUDA_CP_AUDIT"
HEALTH_PATH = "/health"

VALID_ROLES = ("owner", "admin", "trusted", "normal")


@dataclass(frozen=True)
class Operator:
    """已认证的 CP 操作者（PermissionEngine Actor 形状）。"""

    actor_id: str
    role: str = "owner"

    def is_muted(self) -> bool:
        return False


def _token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def token_ok(token: str) -> bool:
    """凭据校验：未配置 -> False（fail closed）；配置 -> constant-time 比较。"""
    expected = _token()
    if not expected:
        return False
    return secrets.compare_digest((token or "").strip(), expected)


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[len("bearer "):].strip()
    return request.headers.get("x-cp-token", "").strip()


def get_operator(request: Request) -> Operator:
    """已认证请求的操作者（middleware 已校验 token）。"""
    op = getattr(request.state, "operator", None)
    if op is None:
        raise HTTPException(401, "unauthorized")
    return op


def require_write(request: Request, app) -> Operator:
    """写操作：操作者必须通过 PermissionEngine manage_config（owner）。"""
    op = get_operator(request)
    result = app.state.permission_engine.authorize(
        op, "manage_config", scope_key="cp:control_plane",
        resource=str(request.url.path))
    if not result.allowed:
        raise HTTPException(
            403, f"forbidden: {','.join(str(r) for r in result.reason_codes)}")
    return op


def scope_filter_events(events: list, op: Operator) -> list:
    """非 owner 操作者只可见自己的 Trace 事件（metadata.actor）。"""
    if op.role == "owner":
        return list(events)
    return [e for e in events
            if str(getattr(e, "metadata", {}).get("actor", "")) == op.actor_id]


def redact_value(redactor: Redactor, value: Any) -> Any:
    """出参统一脱敏：返回清洗后的值（reason 不阻断）。"""
    return redactor.redact(value)[0]


class AuditLogger:
    """JSONL 审计（ADR-0001：受保护请求与写操作必有审计行）。"""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(
            path or os.environ.get(
                AUDIT_ENV,
                str(Path(__file__).resolve().parents[2]
                    / "data" / "cp_audit.jsonl")))
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def log(self, entry: dict[str, Any]) -> None:
        entry = dict(entry)
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def lines(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        with self._lock:
            return [json.loads(line) for line in
                    self._path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]


async def cp_auth_middleware(request: Request, call_next):
    """认证 + 审计中间件：/health 豁免，其余请求缺省拒绝。"""
    if request.url.path == HEALTH_PATH:
        return await call_next(request)
    provided = _extract_token(request)
    if not token_ok(provided):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    role = request.headers.get("x-cp-role", "owner").strip() or "owner"
    if role not in VALID_ROLES:
        return JSONResponse({"detail": "invalid role"}, status_code=401)
    request.state.operator = Operator(
        actor_id=request.headers.get(
            "x-cp-operator", "cp_owner").strip() or "cp_owner",
        role=role,
    )
    response = await call_next(request)
    try:
        request.app.state.audit_logger.log({
            "actor": request.state.operator.actor_id,
            "role": request.state.operator.role,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
        })
    except Exception as exc:  # 审计失败不阻断请求
        logger.warning("cp audit failed: %s", exc)
    return response