import pytest

@pytest.fixture
def anyio_backend():
    return "asyncio"


import os


@pytest.fixture(scope="session", autouse=True)
def _trace_dir_tmp(tmp_path_factory):
    """测试期间把 Trace 落盘重定向到临时目录，不污染生产 data/traces。"""
    d = tmp_path_factory.mktemp("traces")
    os.environ["DUDUDA_TRACE_DIR"] = str(d)
    return d


@pytest.fixture(autouse=True)
def _reset_mcp_process_state():
    """MCP 熔断和服务健康缓存不得跨测试泄漏。"""
    from dududa.mcp import registry as _reg
    _reg.reset_process_state()
    yield
    _reg.reset_process_state()
