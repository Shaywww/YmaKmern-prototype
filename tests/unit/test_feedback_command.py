from types import SimpleNamespace

import pytest

from dududa.application.dududa_commands import cmd_feedback_impl, cmd_help_impl
from dududa.evolution import ShadowEvolution


@pytest.mark.asyncio
async def test_feedback_command_is_explicit_and_shadow_only(tmp_path):
    plugin = SimpleNamespace(evolution=ShadowEvolution(tmp_path), cap_registry=None)
    usage = await cmd_feedback_impl(plugin, "")
    assert "不会自动修改" in usage

    reply = await cmd_feedback_impl(plugin, "图片被当成普通照片了")
    assert "已记录脱敏改进反馈" in reply
    assert "不会自动修改、启用或部署" in reply
    assert plugin.evolution.status()["experience_count"] == 1

    help_text = await cmd_help_impl(plugin)
    assert "/dududa_feedback" in help_text


@pytest.mark.asyncio
async def test_feedback_command_lazily_creates_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("DUDUDA_EVOLUTION_DIR", str(tmp_path))
    plugin = SimpleNamespace(cap_registry=None)
    reply = await cmd_feedback_impl(plugin, "搜索结果缺少来源")
    assert "已记录脱敏改进反馈" in reply
    assert plugin.evolution.status()["mode"] == "shadow"
