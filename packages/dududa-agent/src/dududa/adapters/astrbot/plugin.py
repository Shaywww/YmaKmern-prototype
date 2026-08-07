"""AstrBot plugin entry - Dududa 2.0 Agent Runtime for QQ."""
from __future__ import annotations
import logging
from typing import Optional
from .types import AstrMessageEvent, MessageEventResult, CommandResult, Plain
from .input_adapter import AstrBotInputAdapter, ActorMappingConfig
from .output_adapter import AstrBotOutputAdapter
from ...core.delivery import DeliveryManager
from ...core.envelope import Platform
from ...runtime.orchestrator import RuntimeOrchestrator
from ...core.persona.registry import PersonaRegistry
from ...core.capability import CapabilityRegistry
from ...mcp.registry import register_all_mcp_services

logger = logging.getLogger('dududa20.astrbot')
from ...core.idempotency import MessageIdempotencyRegistry

DUPLICATE_WINDOW = 1000
_processed_messages = MessageIdempotencyRegistry(
    ttl_seconds=600.0, max_keys=DUPLICATE_WINDOW)

# Connector 幂等键（空 platform/bot 兼容旧测试）：重复返回 True。
def _is_duplicate(message_id: str) -> bool:
    if not message_id: return False
    return not _processed_messages.check_and_register("", "", message_id)

class DududaPlugin:
    def __init__(self, actor_config=None, persona_registry=None, capability_registry=None):
        self.actor_config = actor_config or ActorMappingConfig()
        self.persona_registry = persona_registry or PersonaRegistry()
        self.capability_registry = capability_registry or CapabilityRegistry()
        register_all_mcp_services(self.capability_registry)
        self.input_adapter = AstrBotInputAdapter(self.actor_config)
        self.output_adapter = AstrBotOutputAdapter(rate_limit_delay=0.6)
        self.delivery_manager = DeliveryManager(self.output_adapter)
        self.orchestrator = RuntimeOrchestrator(capability_registry=self.capability_registry, delivery_manager=self.delivery_manager)
        self._enabled: bool = True

    async def on_group_message(self, event: AstrMessageEvent) -> MessageEventResult:
        return await self._handle_message(event)

    async def on_private_message(self, event: AstrMessageEvent) -> MessageEventResult:
        return await self._handle_message(event)

    async def on_admin_command(self, event: AstrMessageEvent) -> CommandResult:
        return await self._handle_admin(event)

    async def _handle_message(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._enabled:
            return self._empty_result()
        if _is_duplicate(event.message_id):
            return self._empty_result()
        try:
            preprocessed = self.input_adapter.to_preprocessed(event)
            result = await self.orchestrator.run(preprocessed)
            run_id = result.run_id
            self.output_adapter.bind_event(run_id, event)
            try:
                conv_id = event.group_id or event.session_id or 'unknown'
                await self.delivery_manager.deliver(result, Platform.QQ, conv_id)
            finally:
                self.output_adapter.unbind_event(run_id)
            return self._to_astrbot_result(result, event)
        except Exception:
            logger.exception('Error handling message')
            return self._error_result(event)

    async def _handle_admin(self, event: AstrMessageEvent) -> CommandResult:
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip('/').lower() if parts else ''
        if cmd == 'dududa_status':
            return CommandResult.from_text(
                'Dududa 2.0 running' + chr(10) +
                'Persona: ' + self.persona_registry.active_id + chr(10) +
                'MCP services: ' + str(sum(1 for _ in self.capability_registry.list_enabled()))
            )
        elif cmd == 'dududa_persona':
            if len(parts) > 1:
                target = parts[1].strip()
                if self.persona_registry.switch(target):
                    return CommandResult.from_text('Switched to: ' + target)
                return CommandResult.from_text('Unknown persona: ' + target)
            return CommandResult.from_text('Personas: ' + ', '.join(self.persona_registry.list_all()))
        elif cmd == 'dududa_enable':
            self._enabled = True
            return CommandResult.from_text('Dududa enabled')
        elif cmd == 'dududa_disable':
            self._enabled = False
            return CommandResult.from_text('Dududa disabled')
        return CommandResult.from_text('Unknown command: ' + cmd)

    def _to_astrbot_result(self, result, event: AstrMessageEvent) -> MessageEventResult:
        r = event.make_result()
        if result.final_response and result.final_response.text:
            r.set_text(result.final_response.text)
        return r

    def _empty_result(self) -> MessageEventResult:
        return MessageEventResult()

    def _error_result(self, event: AstrMessageEvent) -> MessageEventResult:
        r = event.make_result()
        r.set_text('(internal error, please try again)')
        return r

    async def shutdown(self):
        self._enabled = False

    def health_check(self) -> dict:
        return {'enabled': self._enabled, 'persona': self.persona_registry.active_id, 'services': sum(1 for _ in self.capability_registry.list_enabled())}

def create_plugin(actor_config=None, persona_registry=None, capability_registry=None) -> DududaPlugin:
    return DududaPlugin(actor_config=actor_config, persona_registry=persona_registry, capability_registry=capability_registry)
