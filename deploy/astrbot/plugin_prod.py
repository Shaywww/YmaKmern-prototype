"""
Dududa 2.0 - AstrBot 生产版插件
部署时替换 packages/adapters/astrbot/plugin.py

使用方法：
  1. pip install astrbot
  2. 把此文件放到 ~/.astrbot/plugins/dududa20/main.py
  3. 把 metadata.yaml 放到同目录
  4. astrbot start
"""
from astrbot.api.all import *

from packages.adapters.astrbot.input_adapter import AstrBotInputAdapter, ActorMappingConfig
from packages.adapters.astrbot.output_adapter import AstrBotOutputAdapter
from packages.core.delivery import DeliveryManager
from packages.core.envelope import Platform
from packages.runtime.orchestrator import RuntimeOrchestrator
from packages.core.persona.registry import PersonaRegistry
from packages.core.capability import CapabilityRegistry
from packages.mcp.registry import register_all_mcp_services

import logging
logger = logging.getLogger("dududa20")

DUPLICATE_WINDOW = 1000
_processed_messages = set()


class Main(StarPlugin):
    """Dududa 2.0 AstrBot 插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.actor_config = ActorMappingConfig(
            hash_user_ids=True,
            owner_ids=(config.get("admin_qq", ""),) if config else (),
        )
        self.persona_registry = PersonaRegistry()
        self.capability_registry = CapabilityRegistry()
        register_all_mcp_services(self.capability_registry)

        self.input_adapter = AstrBotInputAdapter(self.actor_config)
        self.output_adapter = AstrBotOutputAdapter(rate_limit_delay=0.6)
        self.delivery_manager = DeliveryManager(self.output_adapter)
        self.orchestrator = RuntimeOrchestrator(
            capability_registry=self.capability_registry,
            delivery_manager=self.delivery_manager,
        )
        self._enabled = True

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息"""
        if not self._enabled:
            return

        # 去重
        msg_id = event.message_id or ""
        if msg_id in _processed_messages:
            return
        _processed_messages.add(msg_id)
        if len(_processed_messages) > DUPLICATE_WINDOW:
            _processed_messages.clear()

        try:
            preprocessed = self.input_adapter.to_preprocessed(event)
            result = await self.orchestrator.run(preprocessed)

            if result.final_response and result.final_response.text:
                yield event.plain_result(result.final_response.text)
        except Exception:
            logger.exception("Error in on_message")
            yield event.plain_result("(出了点问题，稍后再试...)")

    @command("dududa_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看机器人状态"""
        yield event.plain_result(
            f"Dududa 2.0 运行中\n"
            f"人格: {self.persona_registry.active_id}\n"
            f"MCP 服务: {sum(1 for _ in self.capability_registry.list_enabled())}"
        )

    @command("dududa_persona")
    async def cmd_persona(self, event: AstrMessageEvent, target: str = None):
        """切换或查看人格: /dududa_persona [人格名]"""
        if target:
            if self.persona_registry.switch(target):
                yield event.plain_result(f"人格已切换: {target}")
            else:
                yield event.plain_result(f"未知人格: {target}")
        else:
            personas = ", ".join(self.persona_registry.list_all())
            yield event.plain_result(f"可用人格: {personas}")

    @command("dududa_enable")
    async def cmd_enable(self, event: AstrMessageEvent):
        self._enabled = True
        yield event.plain_result("嘟嘟哒已启用")

    @command("dududa_disable")
    async def cmd_disable(self, event: AstrMessageEvent):
        self._enabled = False
        yield event.plain_result("嘟嘟哒已禁用")