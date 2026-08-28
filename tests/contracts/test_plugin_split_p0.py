"""Phase 4 —— 插件拆分骨架验证。

生产 main.py 的目标形态：薄 Adapter（事件/命令）+ Orchestrator +
Core 领域包。此处验证 DududaPlugin 组装与命令处理可脱离真实
AstrBot 环境运行（types.py 为本地桩）。
"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import asyncio
import pytest

from dududa.adapters.astrbot.plugin import DududaPlugin, create_plugin
from dududa.adapters.astrbot.types import AstrMessageEvent, AstrSender


class TestPluginAssembly:
    def test_construct_wiring(self):
        p = DududaPlugin()
        assert p.input_adapter is not None
        assert p.output_adapter is not None
        assert p.delivery_manager is not None
        assert p.orchestrator is not None
        assert p.capability_registry is not None
        assert p.persona_registry is not None

    def test_factory(self):
        assert isinstance(create_plugin(), DududaPlugin)

    def test_health_check(self):
        p = DududaPlugin()
        health = p.health_check()
        assert health["enabled"] is True
        assert health["persona"]
        assert health["services"] >= 0


class TestCommandHandling:
    def test_status_command(self):
        p = DududaPlugin()
        ev = AstrMessageEvent(
            message_str="/ymakmern_status",
            message_id="m1",
            sender=AstrSender(user_id="u1", nickname="u1"),
        )
        result = asyncio.run(p._handle_admin(ev))
        assert "YmaKmern" in result.message_chain[0].text

    def test_persona_switch_command(self):
        p = DududaPlugin()
        ev = AstrMessageEvent(message_str="/dududa_persona")
        result = asyncio.run(p._handle_admin(ev))
        assert "Personas" in result.message_chain[0].text

    def test_unknown_command(self):
        p = DududaPlugin()
        ev = AstrMessageEvent(message_str="/nope_xyz")
        result = asyncio.run(p._handle_admin(ev))
        assert "Unknown command" in result.message_chain[0].text

    def test_disable_then_ignore_message(self):
        p = DududaPlugin()
        p._enabled = False
        ev = AstrMessageEvent(message_str="hello")
        result = asyncio.run(p._handle_message(ev))
        assert len(result.message_chain) == 0

    def test_duplicate_message_ignored(self):
        p = DududaPlugin()
        ev = AstrMessageEvent(
            message_str="first", message_id="dup-1",
            sender=AstrSender(user_id="u1", nickname="u1"),
        )
        asyncio.run(p._handle_message(ev))
        # 同 message_id 再次进入 -> 去重忽略
        ev2 = AstrMessageEvent(
            message_str="second", message_id="dup-1",
            sender=AstrSender(user_id="u1", nickname="u1"),
        )
        result = asyncio.run(p._handle_message(ev2))
        assert len(result.message_chain) == 0
