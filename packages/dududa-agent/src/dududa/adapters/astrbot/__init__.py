"""AstrBot adapter for Dududa 2.0."""
from .plugin import DududaPlugin, create_plugin
from .input_adapter import AstrBotInputAdapter, ActorMappingConfig
from .output_adapter import AstrBotOutputAdapter
from .types import AstrMessageEvent, MessageEventResult, CommandResult

__all__ = [
    'DududaPlugin', 'create_plugin',
    'AstrBotInputAdapter', 'ActorMappingConfig',
    'AstrBotOutputAdapter',
    'AstrMessageEvent', 'MessageEventResult', 'CommandResult',
]
