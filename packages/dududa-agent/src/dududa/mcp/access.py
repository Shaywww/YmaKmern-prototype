"""iCourse MCP 按群/按人选择性切换（文档 2.5.6）。

策略文件为 JSON，路径由环境变量 DUDUDA_MCP_ACCESS 指定，
默认 <repo>/data/mcp_access.json。文件不存在时不限制（与历史行为一致）；
配置文件一旦存在即启用策略（生产由 ensure_seed() 种下 default deny + owner 放行）。

配置结构::

    {
      "default_policy": "deny",              // "deny" | "allow"
      "groups": {"allow": ["g1"], "deny": ["g2"]},
      "users":  {"allow": ["u1"], "deny": ["u2"]}
    }

判定优先级（仅约束 iCourse 服务；clock 等非 iCourse 服务恒允许）::

    1) 用户 deny 名单      -> 拒绝
    2) 用户 allow 名单      -> 允许（个人放行优先于群）
    3) 群 deny 名单        -> 拒绝
    4) 群 allow 名单        -> 允许
    5) default_policy      -> 默认（fail closed：deny）

文件按 mtime 热加载：修改配置无需重启服务。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("dududa20.mcp.access")

# iCourse 教学服务集合（受按群/按人策略约束）
ICOURSE_SERVICE_IDS = frozenset({
    "course_schedule",
    "exam_schedule",
    "academic_calendar",
    "training_program",
    "second_classroom",
    "campus_notice",
    "academic_affairs",
})

# 配置文件不存在时的兼容默认：不限制（与历史行为一致，dev/CI 全绿）。
# 配置文件一旦存在（生产由 ensure_seed 自动种下），default_policy 默认 deny（fail closed）。
_LEGACY_CONFIG: dict[str, Any] = {
    "default_policy": "allow",
    "groups": {"allow": [], "deny": []},
    "users": {"allow": [], "deny": []},
}

_FILE_DEFAULT: dict[str, Any] = {
    "default_policy": "deny",
    "groups": {"allow": [], "deny": []},
    "users": {"allow": [], "deny": []},
}


def is_icourse_capability(capability_id: str) -> bool:
    """capability_id 形如 mcp.course_schedule；兼容裸服务名。"""
    svc = str(capability_id).split(".", 1)[-1]
    return svc in ICOURSE_SERVICE_IDS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    override = os.environ.get("DUDUDA_MCP_ACCESS", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "data" / "mcp_access.json"


def _as_str_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(x) for x in value)


def _normalize_group(conversation_id: str) -> str:
    """兼容 group_ 前缀与裸群号。"""
    cid = str(conversation_id or "").strip()
    if cid.startswith("group_"):
        return cid[len("group_"):]
    return cid


class MCPAccessPolicy:
    """按群/按人的 iCourse MCP 访问策略（fail closed）。"""

    def __init__(self, config_path: Optional[str] = None):
        self._path = Path(config_path) if config_path else default_config_path()
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._config: dict[str, Any] = dict(_LEGACY_CONFIG)
        self._configured: bool = False
        self._load_error: str = ""
        self.reload()

    # ---- 配置加载 ----

    def reload(self) -> None:
        try:
            if not self._path.exists():
                # 无配置文件：legacy 不限制（与历史行为一致）
                self._config = dict(_LEGACY_CONFIG)
                self._configured = False
                self._mtime = -1.0
                self._load_error = ""
                return
            mtime = self._path.stat().st_mtime
            with self._lock:
                if mtime == self._mtime:
                    return
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                cfg = dict(_FILE_DEFAULT)
                policy = str(raw.get("default_policy", "deny")).lower()
                cfg["default_policy"] = policy if policy in ("deny", "allow") else "deny"
                for key in ("groups", "users"):
                    entry = raw.get(key) or {}
                    cfg[key] = {
                        "allow": _as_str_list(entry.get("allow")),
                        "deny": _as_str_list(entry.get("deny")),
                    }
                self._config = cfg
                self._configured = True
                self._mtime = mtime
                self._load_error = ""
        except Exception as e:  # 配置损坏不阻断服务：fail closed（按文件默认 deny）
            self._config = dict(_FILE_DEFAULT)
            self._configured = True
            self._load_error = str(e)
            logger.warning("MCP access config load failed (%s): %s",
                           self._path, e)

    def _refresh(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_mtime != self._mtime:
                self.reload()
        except OSError:
            pass

    def ensure_seed(self, owner_ids: tuple[str, ...] = ()) -> bool:
        """首次运行时生成种子配置：default deny + owner 放行（幂等）。"""
        if self._path.exists():
            return False
        cfg = dict(_FILE_DEFAULT)
        cfg["users"] = {
            "allow": sorted({str(x) for x in owner_ids if str(x)}),
            "deny": [],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            self._mtime = self._path.stat().st_mtime
            self._config = cfg
            self._configured = True
            return True
        except OSError as e:
            logger.warning("MCP access seed write failed: %s", e)
            return False

    # ---- 判定 ----

    def is_allowed(self, capability_id: str,
                   conversation_id: str = "", actor_id: str = "") -> bool:
        return self.deny_reason(
            capability_id, conversation_id, actor_id)[0]

    def deny_reason(self, capability_id: str,
                    conversation_id: str = "",
                    actor_id: str = "") -> tuple[bool, str]:
        """返回 (是否允许, 原因)。原因为空表示允许。"""
        self._refresh()
        if not is_icourse_capability(capability_id):
            return True, ""
        cfg = self._config
        users = cfg.get("users", {})
        groups = cfg.get("groups", {})
        actor = str(actor_id or "")
        conv = _normalize_group(conversation_id or "")
        if not self._configured:
            return True, "no_config_allow"
        if actor and actor in users.get("deny", ()):
            return False, "user_deny"
        if actor and actor in users.get("allow", ()):
            return True, "user_allow"
        if conv and conv in groups.get("deny", ()):
            return False, "group_deny"
        if conv and conv in groups.get("allow", ()):
            return True, "group_allow"
        if cfg.get("default_policy") == "allow":
            return True, "default_allow"
        return False, "default_deny"

    # ---- 可观测 ----

    @property
    def path(self) -> str:
        return str(self._path)

    @property
    def default_policy(self) -> str:
        return str(self._config.get("default_policy", "deny"))

    def status(self) -> dict[str, Any]:
        self._refresh()
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "configured": self._configured,
            "default_policy": self.default_policy,
            "groups": self._config.get("groups", {}),
            "users": self._config.get("users", {}),
            "load_error": self._load_error or "",
        }


# 模块级单例（生产与测试共用；测试可 monkeypatch 或重建实例）
mcp_access = MCPAccessPolicy()
