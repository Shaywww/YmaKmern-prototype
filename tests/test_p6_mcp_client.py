# -*- coding: utf-8 -*-
"""P6: 统一 MCP Client（文档 2.5.6）。

覆盖：
- UnifiedMCPClient：initialize 握手、tools/list 发现与 Schema 缓存、
  超时重试、熔断（打开/半开）、调用审计 JSONL、懒启动
- McpServerRegistry：default deny、allow/deny 表（deny 优先）、未知 server
- extract_mcp_result：text / json / structuredContent / isError
- UnifiedMCPProvider：显式映射；未映射 action / 未就绪 / MCP 错误 -> mock 降级
- create_unified_provider_factory：env 解析、懒启动、icourse 安全表
- register_all_mcp_services(provider_factory=...) 集成
- 真实 stdio 子进程握手（仅服务器/Linux 上执行）
"""
import os, sys, json, asyncio, time
sys.path.insert(0, "/opt/dududa20-prototype")

import pytest

from packages.mcp.client import (
    McpError, McpErrorKind, StdioMCPTransport, UnifiedMCPClient,
    McpServerRegistry, UnifiedMCPProvider, ProviderFactory,
    create_unified_provider_factory, extract_mcp_result,
    _ICOURSE_ALLOW_TOOLS, _ICOURSE_DENY_TOOLS, _CAP_TOOL_MAP,
)
from packages.mcp.registry import register_all_mcp_services, MCPProvider
from packages.core.trace_recorder import trace_recorder
from packages.core.capability import CapabilityRegistry, ToolObservation


class _FakeTransport:
    """可编程 stdio 传输替身：记录请求、按脚本响应或抛错。"""

    def __init__(self, responses=None, fail=None, fail_n=0):
        self._responses = responses or {}
        self._fail = fail
        self._fail_n = fail_n
        self.requests = []          # [(method, params), ...]
        self.request_count = 0
        self.started = 0
        self.closed = False
        self._running = False

    @property
    def running(self):
        return self._running

    async def start(self):
        self.started += 1
        self._running = True

    async def request(self, method, params=None):
        self.request_count += 1
        self.requests.append((method, params))
        if self._fail_n > 0:
            self._fail_n -= 1
            raise self._fail
        return self._responses.get(method, {"ok": True})

    async def close(self):
        self._running = False
        self.closed = True


class _FakeMock:
    """mock Provider 替身：记录调用次数，返回固定 ToolObservation。"""

    def __init__(self, data="mock-data"):
        self.calls = 0
        self._data = data

    async def execute(self, capability, arguments):
        self.calls += 1
        return ToolObservation(
            step_id="", capability_id="mock.cap",
            success=True, data=self._data, source="mock")


def _client(responses=None, fail=None, fail_n=0, **kw):
    t = _FakeTransport(responses=responses, fail=fail, fail_n=fail_n)
    c = UnifiedMCPClient(cmd="fake-cmd", transport_factory=lambda: t, **kw)
    return c, t


# ---- 错误归一化 ----

class TestMcpError:
    def test_kind_and_message(self):
        e = McpError(McpErrorKind.TIMEOUT, "slow")
        assert e.kind == McpErrorKind.TIMEOUT
        assert e.message == "slow"
        assert str(e) == "timeout: slow"

    def test_all_kinds_defined(self):
        for k in ("timeout", "connection", "protocol", "tool_error",
                  "denied", "busy", "unknown"):
            assert McpErrorKind(k).value == k


# ---- 握手 / 发现 / Schema 缓存 / 懒启动 ----

class TestDiscovery:
    @pytest.mark.asyncio
    async def test_initialize_handshake(self):
        c, t = _client(responses={"initialize": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake", "version": "1.0"}}})
        res = await c.initialize()
        method, params = t.requests[0]
        assert method == "initialize"
        assert params["protocolVersion"] == "2024-11-05"
        assert params["clientInfo"]["name"] == "dududa20"
        assert res["serverInfo"]["name"] == "fake"

    @pytest.mark.asyncio
    async def test_lazy_start_no_process_on_construct(self):
        c, t = _client()
        assert c.health() == "idle"
        assert t.started == 0
        await c.initialize()
        assert t.started == 1
        assert c.health() == "connected"

    @pytest.mark.asyncio
    async def test_list_tools_discovers_and_caches(self):
        tools = ({"name": "a", "inputSchema": {"type": "object"}},)
        c, t = _client(responses={"tools/list": {"tools": list(tools)}})
        assert await c.list_tools() == tools
        assert await c.list_tools() == tools        # 命中缓存
        assert t.request_count == 1
        assert await c.list_tools(refresh=True) == tools
        assert t.request_count == 2

    @pytest.mark.asyncio
    async def test_close_resets_cache_and_transport(self):
        c, t = _client(responses={"tools/list": {"tools": [{"name": "a"}]}})
        await c.list_tools()
        await c.close()
        assert t.closed and not t.running
        assert c.health() == "idle"
        await c.list_tools()
        assert t.request_count == 2

    @pytest.mark.asyncio
    async def test_health_degraded_when_transport_died(self):
        c, t = _client()
        await c.initialize()
        t._running = False
        assert c.health() == "degraded"
        await c.close()
        assert c.health() == "idle"


# ---- 重试 / 熔断 / 审计 ----

class TestCallRetryBreaker:
    @pytest.mark.asyncio
    async def test_timeout_retries_then_succeeds(self):
        c, t = _client(
            fail=McpError(McpErrorKind.TIMEOUT, "slow"), fail_n=1,
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            max_retries=3)
        res = await c.call_tool("ping", {"q": 1})
        assert res["content"][0]["text"] == "ok"
        assert t.request_count == 2   # 1 次失败 + 1 次成功

    @pytest.mark.asyncio
    async def test_timeout_retries_exhausted_raises(self):
        c, t = _client(
            fail=McpError(McpErrorKind.TIMEOUT, "slow"), fail_n=99,
            max_retries=2)
        with pytest.raises(McpError) as ei:
            await c.call_tool("ping")
        assert ei.value.kind == McpErrorKind.TIMEOUT
        assert t.request_count == 3   # max_retries + 1

    @pytest.mark.asyncio
    async def test_tool_error_no_retry(self):
        c, t = _client(
            fail=McpError(McpErrorKind.TOOL_ERROR, "bad tool"), fail_n=99,
            max_retries=3)
        with pytest.raises(McpError) as ei:
            await c.call_tool("ping")
        assert ei.value.kind == McpErrorKind.TOOL_ERROR
        assert t.request_count == 1

    @pytest.mark.asyncio
    async def test_connection_error_no_retry(self):
        c, t = _client(
            fail=McpError(McpErrorKind.CONNECTION, "pipe broken"), fail_n=99,
            max_retries=3)
        with pytest.raises(McpError) as ei:
            await c.call_tool("ping")
        assert ei.value.kind == McpErrorKind.CONNECTION
        assert t.request_count == 1

    @pytest.mark.asyncio
    async def test_breaker_opens_and_fast_fails(self):
        fail = McpError(McpErrorKind.TIMEOUT, "boom")
        c, t = _client(fail=fail, fail_n=99, max_retries=0,
                       breaker_failures=3, breaker_reset=60)
        for _ in range(3):
            with pytest.raises(McpError):
                await c.call_tool("ping")
        assert c.health() == "breaker_open"
        with pytest.raises(McpError) as ei:
            await c.call_tool("ping")   # 熔断打开，快速失败
        assert ei.value.kind == McpErrorKind.BUSY
        assert t.request_count == 3     # 熔断后不再触达传输层

    @pytest.mark.asyncio
    async def test_breaker_half_open_recovers(self):
        # 前 3 次失败 -> 熔断打开；到期后半开试探，成功后恢复
        fail = McpError(McpErrorKind.TIMEOUT, "boom")
        t = _FakeTransport(fail=fail, fail_n=3,
                           responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        c = UnifiedMCPClient(cmd="fake-cmd", max_retries=0,
                             breaker_failures=3, breaker_reset=0.05,
                             transport_factory=lambda: t)
        for _ in range(3):
            with pytest.raises(McpError):
                await c.call_tool("ping")
        assert c.health() == "breaker_open"
        await asyncio.sleep(0.08)
        assert c.health() != "breaker_open"   # 半开：允许试探
        res = await c.call_tool("ping")
        assert res["content"][0]["text"] == "ok"
        assert c.health() == "connected"
        assert t.request_count == 4

    @pytest.mark.asyncio
    async def test_audit_writes_jsonl(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            audit_path=str(audit))
        await c.call_tool("ping", {"a": 1})
        lines = audit.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "call" and rec["tool"] == "ping" and rec["ok"] is True

    @pytest.mark.asyncio
    async def test_audit_records_failure(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        c, _t = _client(
            fail=McpError(McpErrorKind.CONNECTION, "down"), fail_n=99,
            max_retries=0, audit_path=str(audit))
        with pytest.raises(McpError):
            await c.call_tool("ping")
        rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
        assert rec["event"] == "call" and rec["ok"] is False

    @pytest.mark.asyncio
    async def test_audit_breaker_open_event(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        fail = McpError(McpErrorKind.TIMEOUT, "boom")
        c, _t = _client(fail=fail, fail_n=99, max_retries=0,
                        breaker_failures=1, audit_path=str(audit))
        with pytest.raises(McpError):
            await c.call_tool("ping")
        with pytest.raises(McpError):
            await c.call_tool("ping")   # 熔断打开
        events = [json.loads(l)["event"] for l in audit.read_text(encoding="utf-8").splitlines()]
        assert "breaker_open" in events


# ---- 注册表：default deny ----

class TestRegistry:
    @pytest.mark.asyncio
    async def test_unknown_server_rejected(self):
        reg = McpServerRegistry()
        with pytest.raises(McpError) as ei:
            await reg.call("nope", "t")
        assert ei.value.kind == McpErrorKind.CONNECTION

    @pytest.mark.asyncio
    async def test_deny_wins_over_allow(self):
        c, t = _client()
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",), deny=("ok_tool",))
        with pytest.raises(McpError) as ei:
            await reg.call("s", "ok_tool")
        assert ei.value.kind == McpErrorKind.DENIED
        assert t.request_count == 0   # 策略层拒绝，不触达传输

    @pytest.mark.asyncio
    async def test_not_in_allow_rejected(self):
        c, t = _client()
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",))
        with pytest.raises(McpError) as ei:
            await reg.call("s", "other")
        assert ei.value.kind == McpErrorKind.DENIED

    @pytest.mark.asyncio
    async def test_allow_passes_through(self):
        c, t = _client(responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",))
        res = await reg.call("s", "ok_tool", {"k": "v"})
        assert t.requests[-1][0] == "tools/call"
        assert t.requests[-1][1]["name"] == "ok_tool"
        assert res["content"][0]["text"] == "ok"

    @pytest.mark.asyncio
    async def test_list_tools_and_health_unregistered(self):
        reg = McpServerRegistry()
        assert await reg.list_tools("nope") == ()
        assert reg.health("nope") == "unregistered"

    @pytest.mark.asyncio
    async def test_list_tools_delegates(self):
        c, t = _client(responses={"tools/list": {"tools": [{"name": "a"}]}})
        reg = McpServerRegistry()
        reg.register("s", c)
        assert await reg.list_tools("s") == ({"name": "a"},)
        assert t.request_count == 1


# ---- extract_mcp_result ----

class TestExtractResult:
    def test_text_content(self):
        data, err = extract_mcp_result(
            {"content": [{"type": "text", "text": "你好"}]})
        assert data == "你好" and err is False

    def test_json_content(self):
        data, err = extract_mcp_result(
            {"content": [{"type": "json", "json": {"a": 1}}]})
        assert json.loads(data) == {"a": 1} and err is False

    def test_structured_content(self):
        data, err = extract_mcp_result({"structuredContent": {"k": "v"}})
        assert data == {"k": "v"} and err is False

    def test_mixed_content_joined(self):
        data, err = extract_mcp_result({"content": [
            {"type": "text", "text": "A"},
            {"type": "json", "json": {"b": 2}},
        ]})
        assert data.startswith("A") and json.loads(data[1:]) == {"b": 2}

    def test_is_error_flag(self):
        data, err = extract_mcp_result(
            {"content": [{"type": "text", "text": "err"}], "isError": True})
        assert err is True

    def test_empty_is_error(self):
        data, err = extract_mcp_result({})
        assert data is None and err is True

    def test_plain_string_fallback(self):
        data, err = extract_mcp_result({"content": ["plain"]})
        assert data == "plain" and err is False


# ---- UnifiedMCPProvider：显式映射 + mock 降级 ----

class TestProviderFallback:
    def _prov(self, t, mock=None, mapping=None, allow=None, deny=None,
               server="icourse"):
        c = UnifiedMCPClient(cmd="fake-cmd", max_retries=0,
                             transport_factory=lambda: t)
        reg = McpServerRegistry()
        reg.register(server, c, allow=allow, deny=deny)
        mock = mock or _FakeMock()
        prov = UnifiedMCPProvider(reg, server, "mcp.course_schedule",
                                  mock, mapping or {})
        return prov, mock, c

    @pytest.mark.asyncio
    async def test_mapped_action_uses_mcp(self):
        t = _FakeTransport(responses={"tools/call": {
            "content": [{"type": "text", "text": "课程数据"}]}})
        prov, mock, _c = self._prov(
            t, mapping={"search": "search_courses", "default": "search_courses"},
            allow=_ICOURSE_ALLOW_TOOLS)
        obs = await prov.execute(None, {"action": "search", "keyword": "数据结构"})
        assert obs.success and obs.source == "mcp"
        assert obs.data == "课程数据"
        assert mock.calls == 0

    @pytest.mark.asyncio
    async def test_explicit_none_mapping_falls_back(self):
        t = _FakeTransport()
        prov, mock, _c = self._prov(
            t, mapping={"get_personal_schedule": None,
                        "default": "search_courses"},
            allow=_ICOURSE_ALLOW_TOOLS)
        obs = await prov.execute(None, {"action": "get_personal_schedule"})
        assert obs.source == "mock" and mock.calls == 1
        assert t.request_count == 0

    @pytest.mark.asyncio
    async def test_empty_mapping_falls_back(self):
        t = _FakeTransport()
        prov, mock, _c = self._prov(t, mapping={}, allow=_ICOURSE_ALLOW_TOOLS)
        obs = await prov.execute(None, {"action": "search"})
        assert obs.source == "mock" and mock.calls == 1

    @pytest.mark.asyncio
    async def test_not_ready_falls_back(self):
        # server 未注册 -> ready=False -> mock
        mock = _FakeMock()
        reg = McpServerRegistry()
        prov = UnifiedMCPProvider(reg, "missing", "mcp.x", mock, {})
        obs = await prov.execute(None, {"action": "search"})
        assert obs.source == "mock" and mock.calls == 1

    @pytest.mark.asyncio
    async def test_mcp_error_falls_back(self):
        t = _FakeTransport(fail=McpError(McpErrorKind.CONNECTION, "down"),
                           fail_n=99)
        prov, mock, _c = self._prov(
            t, mapping={"search": "search_courses", "default": "search_courses"},
            allow=_ICOURSE_ALLOW_TOOLS)
        obs = await prov.execute(None, {"action": "search"})
        assert obs.source == "mock" and mock.calls == 1

    @pytest.mark.asyncio
    async def test_denied_tool_falls_back(self):
        t = _FakeTransport()
        prov, mock, _c = self._prov(
            t, mapping={"search": "search_courses", "default": "search_courses"},
            allow=_ICOURSE_ALLOW_TOOLS, deny=("search_courses",))
        obs = await prov.execute(None, {"action": "search"})
        assert obs.source == "mock" and mock.calls == 1

    @pytest.mark.asyncio
    async def test_tool_error_result_falls_back(self):
        t = _FakeTransport(responses={"tools/call": {
            "content": [{"type": "text", "text": "工具执行失败"}],
            "isError": True}})
        prov, mock, _c = self._prov(
            t, mapping={"search": "search_courses", "default": "search_courses"},
            allow=_ICOURSE_ALLOW_TOOLS)
        obs = await prov.execute(None, {"action": "search"})
        assert obs.source == "mock" and mock.calls == 1

    def test_health_ok_when_not_breaker(self):
        prov, _mock, _c = self._prov(_FakeTransport())
        assert prov.health() is True


# ---- factory / 集成 ----

class TestFactory:
    def test_env_parsing(self):
        factory = create_unified_provider_factory({
            "ICOURSE_MCP_CMD": "my-mcp",
            "ICOURSE_MCP_ARGS": "--x 1 --y",
            "DUDUDA_MCP_TIMEOUT": "5",
            "DUDUDA_MCP_RETRIES": "1",
            "DUDUDA_MCP_BREAKER": "3",
            "DUDUDA_MCP_AUDIT": "/tmp/a.jsonl",
        })
        assert factory.client._cmd == "my-mcp"
        assert factory.client._args == ("--x", "1", "--y")
        assert factory.client._timeout == 5.0
        assert factory.client._max_retries == 1
        assert factory.client._breaker_threshold == 3
        assert factory.client._audit_path == "/tmp/a.jsonl"
        assert factory.health() == "idle"   # 懒启动

    def test_env_defaults_and_bad_values(self):
        factory = create_unified_provider_factory({})
        assert factory.client._cmd == "python3 -m icourse_mcp"
        assert factory.client._timeout == 10.0
        assert factory.client._max_retries == 2
        assert factory.client._breaker_threshold == 5
        factory2 = create_unified_provider_factory({
            "DUDUDA_MCP_TIMEOUT": "abc",
            "DUDUDA_MCP_RETRIES": "x",
            "DUDUDA_MCP_BREAKER": "-1",
        })
        assert factory2.client._timeout == 10.0
        assert factory2.client._max_retries == 2
        assert factory2.client._breaker_threshold == 1

    @pytest.mark.asyncio
    async def test_icourse_policy_deny_before_start(self):
        factory = create_unified_provider_factory({})
        # deny 表在策略层拦截，不触达传输（不 spawn 进程）
        with pytest.raises(McpError) as ei:
            await factory.registry.call("icourse", "crawl_course")
        assert ei.value.kind == McpErrorKind.DENIED
        with pytest.raises(McpError) as ei2:
            await factory.registry.call("icourse", "export_dataset")
        assert ei2.value.kind == McpErrorKind.DENIED
        # 未注册 server
        with pytest.raises(McpError) as ei3:
            await factory.registry.call("other", "x")
        assert ei3.value.kind == McpErrorKind.CONNECTION
        assert factory.health() == "idle"

    @pytest.mark.asyncio
    async def test_factory_close(self):
        factory = create_unified_provider_factory({})
        await factory.close()
        assert factory.client._transport is None

    @pytest.mark.asyncio
    async def test_register_all_with_factory_mock_fallback(self):
        factory = create_unified_provider_factory({})
        reg = CapabilityRegistry()
        n = register_all_mcp_services(reg, provider_factory=factory)
        assert n == 6
        cap = reg.get("mcp.exam_schedule")
        assert cap is not None
        provider = reg.get_provider("mcp.exam_schedule")
        assert isinstance(provider, UnifiedMCPProvider)
        # exam_schedule 映射为空 -> 任意 action 不触达 MCP -> mock 降级，
        # 服务层返回 mock 考试数据（确定性，不 spawn 真实进程）
        obs = await provider.execute(cap, {"action": "get_exams_by_course", "course_id": "CS2001"})
        assert obs.success
        assert obs.source == "mock"
        assert obs.data
        await factory.close()

    def test_mappings_cover_allow_tools_only(self):
        # 所有映射的工具都必须在 allow 表内
        for cap_id, mapping in _CAP_TOOL_MAP.items():
            for tool in mapping.values():
                if tool is not None:
                    assert tool in _ICOURSE_ALLOW_TOOLS, f"{cap_id}: {tool}"
        # deny 工具绝不进映射
        for cap_id, mapping in _CAP_TOOL_MAP.items():
            for tool in mapping.values():
                if tool is not None:
                    assert tool not in _ICOURSE_DENY_TOOLS


# ---- 2.5.6: export root / crawl 限频 / trace 审计 ----

class TestExportRootSecurity:
    @pytest.mark.asyncio
    async def test_side_effect_path_inside_root_ok(self, tmp_path):
        root = tmp_path / "export"
        root.mkdir()
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            export_root=str(root))
        await c.call_tool("export_dataset", {"path": str(root / "out.json")})
        assert t.requests[-1][1]["arguments"]["path"] == str(root / "out.json")

    @pytest.mark.asyncio
    async def test_absolute_path_outside_root_denied(self, tmp_path):
        root = tmp_path / "export"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        c, t = _client(export_root=str(root))
        with pytest.raises(McpError) as ei:
            await c.call_tool("export_dataset",
                              {"path": str(outside / "x.json")})
        assert ei.value.kind == McpErrorKind.DENIED
        assert t.request_count == 0

    @pytest.mark.asyncio
    async def test_placeholder_export_root_rendered(self, tmp_path):
        root = tmp_path / "export"
        root.mkdir()
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            export_root=str(root))
        await c.call_tool("export_dataset", {"path": "{export_root}/out.json"})
        assert t.requests[-1][1]["arguments"]["path"] == str(root / "out.json")

    @pytest.mark.asyncio
    async def test_placeholder_without_root_denied(self):
        c, t = _client()
        with pytest.raises(McpError) as ei:
            await c.call_tool("export_dataset", {"path": "{export_root}/x"})
        assert ei.value.kind == McpErrorKind.DENIED
        assert t.request_count == 0

    @pytest.mark.asyncio
    async def test_no_export_root_denies_absolute(self):
        c, t = _client()
        with pytest.raises(McpError) as ei:
            await c.call_tool("export_dataset", {"path": "/tmp/x.json"})
        assert ei.value.kind == McpErrorKind.DENIED

    @pytest.mark.asyncio
    async def test_data_dir_placeholder_rendered(self, tmp_path):
        root = tmp_path / "export"
        root.mkdir()
        dd = tmp_path / "data"
        dd.mkdir()
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            export_root=str(root), data_dir=str(dd))
        await c.call_tool("crawl_course", {"output": "{data_dir}/c.json"})
        assert t.requests[-1][1]["arguments"]["output"] == str(dd / "c.json")

    @pytest.mark.asyncio
    async def test_query_tool_keyword_not_checked(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        await c.call_tool("search_courses", {"keyword": "AI/ML 入门"})
        assert t.request_count == 1

    @pytest.mark.asyncio
    async def test_dotdot_escape_denied(self, tmp_path):
        root = tmp_path / "export"
        root.mkdir()
        c, t = _client(export_root=str(root))
        with pytest.raises(McpError) as ei:
            await c.call_tool("export_dataset", {"path": "../escape.json"})
        assert ei.value.kind == McpErrorKind.DENIED


class TestCrawlQuota:
    @pytest.mark.asyncio
    async def test_limit_enforced(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            crawl_limit=2)
        assert (await c.call_tool("crawl_course", {"course_id": "1"}))["content"]
        assert (await c.call_tool("crawl_course", {"course_id": "2"}))["content"]
        with pytest.raises(McpError) as ei:
            await c.call_tool("crawl_course", {"course_id": "3"})
        assert ei.value.kind == McpErrorKind.DENIED
        assert "rate limit" in ei.value.message
        assert t.request_count == 2

    @pytest.mark.asyncio
    async def test_window_resets(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            crawl_limit=1)
        assert await c.call_tool("crawl_course", {})
        with pytest.raises(McpError):
            await c.call_tool("crawl_course", {})
        c._crawl_window_start = 0.0
        assert await c.call_tool("crawl_course", {})
        assert t.request_count == 2

    @pytest.mark.asyncio
    async def test_non_crawl_unlimited(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            crawl_limit=1)
        for _i in range(5):
            await c.call_tool("search_courses", {"keyword": "x"})
        assert t.request_count == 5

    @pytest.mark.asyncio
    async def test_max_steps_clamped(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            crawl_max_steps=3)
        await c.call_tool("crawl_courses", {"limit": 10, "pages": 5, "depth": 2})
        args = t.requests[-1][1]["arguments"]
        assert args["limit"] == 3
        assert args["pages"] == 3
        assert args["depth"] == 2

    @pytest.mark.asyncio
    async def test_quota_denied_audited(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        c, _t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}},
            crawl_limit=1, audit_path=str(audit))
        await c.call_tool("crawl_course", {})
        with pytest.raises(McpError):
            await c.call_tool("crawl_course", {})
        events = [json.loads(l)["event"]
                  for l in audit.read_text(encoding="utf-8").splitlines()]
        assert "crawl_quota" in events


class TestMcpCallTrace:
    @pytest.mark.asyncio
    async def test_registry_call_records_trace_with_run_id(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",))
        await reg.call("s", "ok_tool", {"k": "v"},
                       run_id="run-mcp-1", trace_id="tr-mcp-1")
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "mcp_call"
                 and x.get("run_id") == "run-mcp-1"]
        assert len(lines) == 1
        rec = lines[0]
        assert rec["ok"] is True
        assert rec["server_id"] == "s"
        assert rec["tool"] == "ok_tool"
        assert rec["trace_id"] == "tr-mcp-1"
        assert rec["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_registry_denied_records_trace_error(self):
        c, _t = _client()
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",))
        with pytest.raises(McpError):
            await reg.call("s", "other", run_id="run-mcp-2")
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "mcp_call"
                 and x.get("run_id") == "run-mcp-2"]
        assert len(lines) == 1
        rec = lines[0]
        assert rec["ok"] is False
        assert rec["error_kind"] == "denied"
        assert rec["tool"] == "other"

    @pytest.mark.asyncio
    async def test_provider_execute_passes_run_id(self):
        c, t = _client(
            responses={"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        reg = McpServerRegistry()
        reg.register("s", c, allow=("ok_tool",))
        mock = _FakeMock()
        prov = UnifiedMCPProvider(reg, "s", "cap.x", mock,
                                  {"default": "ok_tool"})
        obs = await prov.execute(None, {"k": "v"},
                                 run_id="run-mcp-3", trace_id="tr-mcp-3")
        assert obs.success
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "mcp_call"
                 and x.get("run_id") == "run-mcp-3"]
        assert len(lines) == 1 and lines[0]["tool"] == "ok_tool"

    def test_factory_env_256_security_options(self):
        factory = create_unified_provider_factory({
            "DUDUDA_MCP_EXPORT_ROOT": "/data/export",
            "DUDUDA_MCP_CRAWL_LIMIT": "3",
            "DUDUDA_MCP_CRAWL_STEPS": "5",
        })
        assert factory.client._export_root == "/data/export"
        assert factory.client._crawl_limit == 3
        assert factory.client._crawl_max_steps == 5


# ---- 真实 stdio 子进程（仅服务器/Linux） ----

_FAKE_SERVER_SCRIPT = r"""
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    if msg.get("method") == "initialize":
        out = {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-icourse", "version": "1.0"}}}
    elif msg.get("method") == "tools/list":
        out = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "ping", "description": "p",
             "inputSchema": {"type": "object"}}]}}
    elif msg.get("method") == "tools/call":
        out = {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": "pong"}]}}
    else:
        out = {"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": "unknown method"}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
"""


@pytest.mark.skipif(sys.platform == "win32",
                    reason="stdio 子进程测试在服务器(Linux)上执行")
@pytest.mark.asyncio
async def test_real_stdio_handshake_call():
    client = UnifiedMCPClient(cmd=sys.executable,
                              args=("-c", _FAKE_SERVER_SCRIPT), timeout=5)
    try:
        res = await client.initialize()
        assert res["serverInfo"]["name"] == "fake-icourse"
        tools = await client.list_tools()
        assert len(tools) == 1 and tools[0]["name"] == "ping"
        data, is_error = extract_mcp_result(
            await client.call_tool("ping", {"q": "hi"}))
        assert data == "pong" and not is_error
        assert client.health() == "connected"
    finally:
        await client.close()
