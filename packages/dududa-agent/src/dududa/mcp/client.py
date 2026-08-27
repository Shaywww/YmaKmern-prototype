"""Unified MCP Client —— 统一 MCP 客户端（文档 2.5.6）。

- StdioMCPTransport：stdio JSON-RPC 2.0（newline framing），
  initialize 握手、tools/list 发现、tools/call 调用、超时与关闭；
- UnifiedMCPClient：Schema 缓存、重试、熔断（circuit breaker）、
  错误归一化（McpError.kind）与调用审计（JSONL）；
- McpServerRegistry：server_id -> client + allow/deny 工具表（deny 优先），
  未注册/未允许的工具拒绝调用（default deny）；
- UnifiedMCPProvider：显式 Capability mapping；未映射的 action、
  未注册 server 或调用失败时降级到 mock Provider（可观测，不静默）。

iCourse 真实工具（10 个）中，crawl*/export_dataset/check_robots 属
高频/副作用/进程可见路径，不映射给普通用户（文档 2.5.6 安全修复）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("dududa20.mcp.client")

from ..core.trace_recorder import trace_recorder  # noqa: E402


class McpErrorKind(str, Enum):
    """归一化错误类型。"""
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    TOOL_ERROR = "tool_error"
    DENIED = "denied"
    BUSY = "busy"          # 熔断打开，快速失败
    UNKNOWN = "unknown"


class McpError(Exception):
    """归一化 MCP 错误。"""

    def __init__(self, kind: McpErrorKind, message: str):
        super().__init__(f"{kind.value}: {message}")
        self.kind = kind
        self.message = message


class StdioMCPTransport:
    """stdio JSON-RPC 2.0 传输（每行一个 JSON 消息）。"""

    def __init__(self, cmd: str, args: tuple[str, ...] = (),
                 timeout: float = 10.0):
        self._cmd = cmd
        self._args = tuple(args)
        self._timeout = timeout
        self._proc = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self.request_count = 0  # 可观测性（测试/审计用）

    @property
    def running(self) -> bool:
        return (self._proc is not None
                and self._proc.returncode is None)

    async def start(self) -> None:
        if self.running:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._cmd, *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as e:
            raise McpError(McpErrorKind.CONNECTION,
                           f"cannot start {self._cmd}: {e}") from e
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while self._proc is not None and self._proc.stdout is not None:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._dispatch(msg)
        except Exception:
            pass
        finally:
            self._fail_pending(McpError(
                McpErrorKind.CONNECTION, "transport closed"))

    def _dispatch(self, msg: dict) -> None:
        rid = msg.get("id")
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        if msg.get("error") is not None:
            err = msg["error"]
            fut.set_exception(McpError(
                McpErrorKind.TOOL_ERROR,
                str(err.get("message", err)) if isinstance(err, dict) else str(err)))
        else:
            fut.set_result(msg.get("result") or {})

    def _fail_pending(self, err: McpError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def request(self, method: str,
                      params: Optional[dict] = None) -> dict:
        await self.start()
        self._next_id += 1
        rid = self._next_id
        payload: dict[str, Any] = {
            "jsonrpc": "2.0", "id": rid, "method": method,
        }
        if params:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        self.request_count += 1
        try:
            if self._proc is None or self._proc.stdin is None:
                raise McpError(McpErrorKind.CONNECTION, "transport not running")
            self._proc.stdin.write(
                (json.dumps(payload) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            if not fut.done():
                fut.set_exception(McpError(
                    McpErrorKind.TIMEOUT,
                    f"{method} timed out after {self._timeout}s"))
            raise McpError(McpErrorKind.TIMEOUT,
                           f"{method} timed out after {self._timeout}s")
        except (BrokenPipeError, ConnectionResetError) as e:
            self._fail_pending(McpError(
                McpErrorKind.CONNECTION, f"pipe broken: {e}"))
            raise McpError(McpErrorKind.CONNECTION, f"pipe broken: {e}") from e
        finally:
            self._pending.pop(rid, None)

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        self._proc = None
        self._fail_pending(McpError(
            McpErrorKind.CONNECTION, "transport closed"))


class UnifiedMCPClient:
    """统一 MCP Client：发现、Schema 缓存、重试、熔断、归一化与审计。

    懒启动：构造时不 spawn 进程，首次调用时建立会话。
    """

    def __init__(
        self,
        cmd: str,
        args: tuple[str, ...] = (),
        timeout: float = 10.0,
        max_retries: int = 2,
        breaker_failures: int = 5,
        breaker_reset: float = 30.0,
        audit_path: Optional[str] = None,
        export_root: Optional[str] = None,
        data_dir: Optional[str] = None,
        crawl_limit: int = 0,
        crawl_max_steps: int = 0,
        transport_factory: Optional[Callable[[], StdioMCPTransport]] = None,
    ):
        self._cmd = cmd
        self._args = tuple(args)
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._breaker_threshold = max(1, int(breaker_failures))
        self._breaker_reset = max(0.0, float(breaker_reset))
        self._audit_path = audit_path
        # doc 2.5.6: export/import/crawl/refresh side-effect path args
        # must resolve inside export root; crawl calls get quota + step caps.
        self._export_root = (os.path.abspath(export_root)
                             if export_root else None)
        self._data_dir = (os.path.abspath(data_dir) if data_dir else None)
        self._crawl_limit = max(0, int(crawl_limit))
        self._crawl_max_steps = max(0, int(crawl_max_steps))
        self._crawl_window_start = 0.0
        self._crawl_count = 0
        self._transport_factory = transport_factory or (
            lambda: StdioMCPTransport(cmd, self._args, timeout))
        self._transport: Optional[StdioMCPTransport] = None
        self._schema_cache: Optional[tuple[dict, ...]] = None
        self._breaker_failures = 0
        self._breaker_open_until = 0.0
        self._started = False

    # -- 审计 --

    def _audit(self, event: str, **kw: Any) -> None:
        if not self._audit_path:
            return
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": round(time.time(), 3), "event": event, **kw},
                    ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -- 熔断 --

    def _breaker_open(self) -> bool:
        if self._breaker_open_until > time.time():
            return True
        if self._breaker_open_until != 0.0:
            # 半开：到期后允许试探，重置计数
            self._breaker_open_until = 0.0
            self._breaker_failures = 0
        return False

    def _record_failure(self) -> None:
        self._breaker_failures += 1
        if self._breaker_failures >= self._breaker_threshold:
            self._breaker_open_until = time.time() + self._breaker_reset
            logger.warning("MCP circuit breaker OPEN for %.0fs",
                           self._breaker_reset)

    def _record_success(self) -> None:
        self._breaker_failures = 0

    # -- 2.5.6 security: export root / crawl quota --

    _FS_SIDE_EFFECT_PREFIXES = ("export", "import", "crawl", "refresh")

    def _render_path_value(self, value: str) -> str:
        """Render {export_root}/{data_dir} placeholders; reject if unset."""
        if "{export_root}" in value:
            if self._export_root is None:
                raise McpError(
                    McpErrorKind.DENIED,
                    "export_root not configured; {export_root} unresolvable")
            value = value.replace("{export_root}", self._export_root)
        if "{data_dir}" in value:
            data_dir = self._data_dir or os.path.join(os.getcwd(), "data")
            value = value.replace("{data_dir}", data_dir)
        return value

    def _looks_like_path(self, value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if "{" in value or value.startswith(("/", "~", ".", "\\")):
            return True
        return "/" in value or "\\" in value or os.path.isabs(value)

    def _render_side_effect_paths(self, tool: str,
                                  arguments: Optional[dict]) -> Optional[dict]:
        """Render path placeholders + validate inside export root.

        Path args of export/import/crawl/refresh tools must stay inside
        export root after rendering; the rendered dict is passed to the
        server. Query tools (search_*/get_*) are never checked so
        keyword-like params are not false-positived.
        """
        if not tool.startswith(self._FS_SIDE_EFFECT_PREFIXES):
            return None
        changed = False
        out = dict(arguments or {})
        for key, value in (arguments or {}).items():
            if not self._looks_like_path(value):
                continue
            resolved = self._render_path_value(value)
            abs_path = os.path.abspath(resolved)
            bases = [b for b in (self._export_root, self._data_dir) if b]
            if not bases:
                raise McpError(
                    McpErrorKind.DENIED,
                    f"{tool}: export_root not configured, "
                    f"path arg {key!r} denied")
            inside = False
            for base in bases:
                rel = os.path.relpath(abs_path, base)
                if not (os.path.isabs(rel) or rel == ".."
                        or rel.startswith(".." + os.sep)):
                    inside = True
                    break
            if not inside:
                raise McpError(
                    McpErrorKind.DENIED,
                    f"{tool}: path arg {key!r} outside allowed roots: "
                    f"{abs_path}")
            if resolved != value:
                out[key] = resolved
                changed = True
        return out if changed else None

    def _check_crawl_quota(self, tool: str) -> None:
        """crawl rate limit: fixed 60s window, 0 = unlimited."""
        if self._crawl_limit <= 0 or not tool.startswith("crawl"):
            return
        now = time.time()
        if now - self._crawl_window_start >= 60.0:
            self._crawl_window_start = now
            self._crawl_count = 0
        if self._crawl_count >= self._crawl_limit:
            self._audit("crawl_quota", tool=tool,
                        limit=self._crawl_limit, window_seconds=60.0)
            raise McpError(McpErrorKind.DENIED,
                           f"crawl rate limit exceeded: {tool}")
        self._crawl_count += 1

    def _apply_crawl_steps(self, tool: str,
                           arguments: Optional[dict]) -> Optional[dict]:
        """crawl step/page cap: clamp over-limit int args."""
        if not tool.startswith("crawl") or self._crawl_max_steps <= 0:
            return arguments
        out = dict(arguments or {})
        for key in ("limit", "max_pages", "pages", "depth", "count"):
            if (key in out and isinstance(out[key], int)
                    and out[key] > self._crawl_max_steps):
                out[key] = self._crawl_max_steps
        return out

    # -- 会话 --

    async def _get_transport(self) -> StdioMCPTransport:
        if self._transport is None:
            self._transport = self._transport_factory()
        if not self._transport.running:
            await self._transport.start()
            self._started = True
        return self._transport

    # -- 发现与 Schema 缓存 --

    async def initialize(self) -> dict:
        t = await self._get_transport()
        return await t.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "dududa20", "version": "2.0"},
        })

    async def list_tools(self, refresh: bool = False) -> tuple[dict, ...]:
        if self._schema_cache is not None and not refresh:
            return self._schema_cache
        t = await self._get_transport()
        res = await t.request("tools/list")
        tools = tuple(res.get("tools", []) or ())
        self._schema_cache = tools
        return tools

    # -- 调用 --

    async def call_tool(self, name: str,
                        arguments: Optional[dict] = None) -> dict:
        # 2.5.6: path render/validation + crawl quota run before transport
        try:
            rendered = self._render_side_effect_paths(name, arguments)
            if rendered is not None:
                arguments = rendered
            self._check_crawl_quota(name)
            arguments = self._apply_crawl_steps(name, arguments)
        except McpError as e:
            self._audit("denied", tool=name, reason=e.message)
            raise
        if self._breaker_open():
            self._audit("breaker_open", tool=name)
            raise McpError(McpErrorKind.BUSY, "circuit breaker open")
        last_err: Optional[McpError] = None
        for attempt in range(self._max_retries + 1):
            try:
                t = await self._get_transport()
                res = await t.request("tools/call", {
                    "name": name,
                    "arguments": arguments or {},
                })
                self._record_success()
                self._audit("call", tool=name, ok=True)
                return res
            except McpError as e:
                last_err = e
                self._record_failure()
                self._audit("call", tool=name, ok=False, error=str(e))
                if (e.kind in (McpErrorKind.TOOL_ERROR, McpErrorKind.DENIED,
                               McpErrorKind.BUSY, McpErrorKind.CONNECTION)
                        or attempt >= self._max_retries):
                    break
                await asyncio.sleep(0.3 * (attempt + 1))
            except Exception as e:  # 兜底归一化
                last_err = McpError(McpErrorKind.UNKNOWN, str(e))
                self._record_failure()
                self._audit("call", tool=name, ok=False, error=str(e))
                break
        raise last_err or McpError(McpErrorKind.UNKNOWN, "call failed")

    def health(self) -> str:
        """idle=未启动（懒加载） | connected | degraded | breaker_open。"""
        if self._breaker_open():
            return "breaker_open"
        if self._started:
            if self._transport is not None and self._transport.running:
                return "connected"
            return "degraded"
        return "idle"

    async def close(self) -> None:
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        self._schema_cache = None
        self._started = False


class McpServerRegistry:
    """MCP Server 注册表：server_id -> client + allow/deny 工具表。

    default deny：未注册 server、未在 allow 列表中的工具、
    命中 deny 列表的工具一律拒绝调用。
    """

    def __init__(self):
        self._servers: dict[str, UnifiedMCPClient] = {}
        self._allow: dict[str, set[str]] = {}
        self._deny: dict[str, set[str]] = {}

    def register(self, server_id: str, client: UnifiedMCPClient,
                 allow: Optional[tuple[str, ...]] = None,
                 deny: Optional[tuple[str, ...]] = None) -> None:
        self._servers[server_id] = client
        self._allow[server_id] = set(allow or ())
        self._deny[server_id] = set(deny or ())

    def ready(self, server_id: str) -> bool:
        return server_id in self._servers

    async def call(self, server_id: str, tool: str,
                   arguments: Optional[dict] = None,
                   run_id: str = "", trace_id: str = "") -> dict:
        start = time.time()
        try:
            client = self._servers.get(server_id)
            if client is None:
                raise McpError(McpErrorKind.CONNECTION,
                               f"unknown server: {server_id}")
            if tool in self._deny.get(server_id, ()):
                logger.warning("MCP tool denied by policy: %s/%s",
                               server_id, tool)
                raise McpError(McpErrorKind.DENIED, f"tool denied: {tool}")
            allow = self._allow.get(server_id)
            if allow and tool not in allow:
                raise McpError(McpErrorKind.DENIED,
                               f"tool not allowed: {tool}")
            result = await client.call_tool(tool, arguments)
        except McpError as e:
            trace_recorder.record(
                event="mcp_call", run_id=run_id, trace_id=trace_id,
                server_id=server_id, tool=tool, ok=False,
                error_kind=e.kind.value,
                latency_ms=round((time.time() - start) * 1000, 1))
            raise
        trace_recorder.record(
            event="mcp_call", run_id=run_id, trace_id=trace_id,
            server_id=server_id, tool=tool, ok=True,
            latency_ms=round((time.time() - start) * 1000, 1))
        return result

    async def list_tools(self, server_id: str = "icourse",
                         refresh: bool = False) -> tuple[dict, ...]:
        client = self._servers.get(server_id)
        if client is None:
            return ()
        return await client.list_tools(refresh)

    def health(self, server_id: str = "icourse") -> str:
        client = self._servers.get(server_id)
        return client.health() if client is not None else "unregistered"


def extract_mcp_result(result: dict) -> tuple[Any, bool]:
    """MCP tools/call result -> (data, is_error)。

    支持 text / json / structuredContent 三种载荷。
    """
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "json" and item.get("json") is not None:
                parts.append(json.dumps(item["json"], ensure_ascii=False))
            elif item.get("text") is not None:
                parts.append(str(item.get("text")))
        else:
            parts.append(str(item))
    data: Any = "".join(parts) if parts else None
    if data is None and result.get("structuredContent") is not None:
        data = result["structuredContent"]
    is_error = bool(result.get("isError")) or (
        data is None and not result.get("structuredContent"))
    return data, is_error


# ---- iCourse 显式 Capability mapping（文档 2.5.6） ----
# 真实工具：icourse_stats / search_courses / get_course / get_reviews /
# search_site_courses；crawl*/export_dataset/check_robots 不对普通用户开放。
_ICOURSE_ALLOW_TOOLS = (
    "icourse_stats", "search_courses", "get_course",
    "get_reviews", "search_site_courses",
)
_ICOURSE_DENY_TOOLS = (
    "crawl_course", "crawl_courses", "crawl_latest_reviews",
    "export_dataset", "check_robots",
)
# action -> 工具名；None 或缺失 -> 该 action 不映射，降级 mock
_CAP_TOOL_MAP: dict[str, dict[str, Optional[str]]] = {
    "mcp.campus_notice": {
        "default": "search_site_courses",
        "search": "search_site_courses",
        "get_pinned": None,
        "get_recent": None,
    },
    "mcp.exam_schedule": {},
    "mcp.academic_calendar": {},
    "mcp.training_program": {},
    "mcp.second_classroom": {},
}


class UnifiedMCPProvider:
    """把统一 MCP Client 包装为 CapProvider（显式映射 + 可观测降级）。"""

    def __init__(self, registry: McpServerRegistry, server_id: str,
                 cap_id: str, mock_provider: Any, mapping: dict):
        self._registry = registry
        self._server_id = server_id
        self._cap_id = cap_id
        self._mock = mock_provider
        self._mapping = mapping or {}

    def _resolve_tool(self, arguments: dict) -> Optional[str]:
        action = str(arguments.get("action", "search"))
        mapping = self._mapping
        if action in mapping:
            return mapping[action]   # 显式 None -> 不映射，降级 mock
        return mapping.get("default")

    async def execute(self, capability, arguments: dict,
                      run_id: str = "", trace_id: str = ""):
        tool = self._resolve_tool(arguments or {})
        if tool is None or not self._registry.ready(self._server_id):
            return await self._mock.execute(capability, arguments)
        svc_args = {k: v for k, v in (arguments or {}).items()
                    if k != "action"}
        try:
            result = await self._registry.call(
                self._server_id, tool, svc_args,
                run_id=run_id, trace_id=trace_id)
            data, is_error = extract_mcp_result(result)
            if is_error:
                logger.warning("MCP %s returned error -> mock fallback", tool)
                return await self._mock.execute(capability, arguments)
            from ..core.capability import ToolObservation
            return ToolObservation(
                step_id="", capability_id=self._cap_id,
                success=True, data=data, source="mcp")
        except McpError as e:
            logger.warning("MCP %s unavailable (%s) -> mock fallback",
                           tool, e.kind.value)
            return await self._mock.execute(capability, arguments)

    def health(self) -> bool:
        return self._registry.health(self._server_id) != "breaker_open"


class ProviderFactory:
    """provider_factory 兼容包装：registry + client 状态查询。"""

    def __init__(self, registry: McpServerRegistry,
                 client: UnifiedMCPClient):
        self.registry = registry
        self.client = client

    def __call__(self, svc):
        from .registry import MCPProvider
        cap_id = f"mcp.{svc.name}"
        # Public course offerings have their own revision-aware snapshot source.
        # Do not route them through the separate iCourse review MCP server.
        if cap_id == "mcp.course_schedule":
            return MCPProvider(svc, server_id=svc.name)
        mapping = _CAP_TOOL_MAP.get(cap_id) or {}
        return UnifiedMCPProvider(
            self.registry, "icourse", cap_id, MCPProvider(svc), mapping)

    async def list_tools(self, refresh: bool = False):
        return await self.registry.list_tools("icourse", refresh)

    def health(self) -> str:
        return self.registry.health("icourse")

    async def close(self) -> None:
        await self.client.close()


def create_unified_provider_factory(
    env: Optional[dict] = None,
) -> ProviderFactory:
    """由环境变量构建统一 MCP Client（懒启动，不 spawn 进程）。

    环境变量：
      ICOURSE_MCP_CMD       iCourse stdio MCP server 启动命令
                            （默认 python3 -m icourse_mcp）
      ICOURSE_MCP_ARGS      附加参数（空格分隔）
      DUDUDA_MCP_TIMEOUT    单次请求超时秒数（默认 10）
      DUDUDA_MCP_RETRIES    重试次数（默认 2）
      DUDUDA_MCP_BREAKER    熔断阈值（默认 5）
      DUDUDA_MCP_AUDIT      调用审计 JSONL 路径（默认关闭）
      DUDUDA_MCP_EXPORT_ROOT 副作用工具允许的导出根目录（默认关闭校验）
      DUDUDA_MCP_DATA_DIR    {data_dir} 占位符解析目录（默认 <cwd>/data）
      DUDUDA_MCP_CRAWL_LIMIT crawl* 每分钟调用上限（默认 0 = 不限）
      DUDUDA_MCP_CRAWL_STEPS crawl* 步数/页数上限（默认 0 = 不限）
    """
    env = env if env is not None else os.environ
    cmd = env.get("ICOURSE_MCP_CMD", "") or "python3 -m icourse_mcp"
    args = tuple(a for a in env.get("ICOURSE_MCP_ARGS", "").split() if a)
    try:
        timeout = float(env.get("DUDUDA_MCP_TIMEOUT", "10"))
    except ValueError:
        timeout = 10.0
    try:
        retries = int(env.get("DUDUDA_MCP_RETRIES", "2"))
    except ValueError:
        retries = 2
    try:
        breaker = int(env.get("DUDUDA_MCP_BREAKER", "5"))
    except ValueError:
        breaker = 5
    audit = env.get("DUDUDA_MCP_AUDIT", "") or None
    export_root = env.get("DUDUDA_MCP_EXPORT_ROOT", "") or None
    data_dir = env.get("DUDUDA_MCP_DATA_DIR", "") or None
    try:
        crawl_limit = int(env.get("DUDUDA_MCP_CRAWL_LIMIT", "0"))
    except ValueError:
        crawl_limit = 0
    try:
        crawl_steps = int(env.get("DUDUDA_MCP_CRAWL_STEPS", "0"))
    except ValueError:
        crawl_steps = 0

    client = UnifiedMCPClient(
        cmd=cmd, args=args, timeout=timeout, max_retries=retries,
        breaker_failures=breaker, audit_path=audit,
        export_root=export_root, data_dir=data_dir,
        crawl_limit=crawl_limit, crawl_max_steps=crawl_steps)
    registry = McpServerRegistry()
    registry.register("icourse", client,
                      allow=_ICOURSE_ALLOW_TOOLS,
                      deny=_ICOURSE_DENY_TOOLS)
    return ProviderFactory(registry, client)
