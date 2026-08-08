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
def _reset_mcp_breaker():
    """MCP 熔断器是进程级状态；某测试把服务打到 OPEN 会污染后续测试
    （list_healthy 会剔除熔断中的能力）。每个测试后全部复位。"""
    from dududa.mcp import registry as _reg
    yield
    for _sid in list(_reg._SERVICES):
        _reg.breaker.record_success(_sid)
