"""AstrBot API type stubs —— 抽象 AstrBot 真实 API 便于测试与解耦。

部署时替换为 from astrbot.api.all import * 即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

class EventMessageType(str, Enum):
    GROUP_MESSAGE = "group_message"
    PRIVATE_MESSAGE = "private_message"
    ADMIN_COMMAND = "admin_command"

class AstrBotPlatform(str, Enum):
    AIOCQHTTP = "aiocqhttp"
    QQ_OFFICIAL = "qq_official"
    LAGRANGE = "lagrange"
    UNKNOWN = "unknown"

@dataclass
class MessageComponent:
    type: str = "plain"
    text: str = ""
    url: str = ""
    qq: str = ""
    file: str = ""

class Plain(MessageComponent):
    def __init__(self, text: str):
        super().__init__(type="plain", text=text)

class Image(MessageComponent):
    def __init__(self, url: str = "", file: str = ""):
        super().__init__(type="image", url=url, file=file)

class At(MessageComponent):
    def __init__(self, qq: str):
        super().__init__(type="at", qq=qq)

class Reply(MessageComponent):
    def __init__(self, id: str):
        super().__init__(type="reply", qq=id)

@dataclass
class AstrSender:
    user_id: str = ""
    nickname: str = ""

@dataclass
class AstrMessageEvent:
    message_str: str = ""
    message_id: str = ""
    session_id: str = ""
    sender: AstrSender = field(default_factory=AstrSender)
    group_id: str = ""
    _message_type: EventMessageType = EventMessageType.GROUP_MESSAGE
    _platform: AstrBotPlatform = AstrBotPlatform.AIOCQHTTP
    _components: list[MessageComponent] = field(default_factory=list)

    def get_platform_name(self) -> str:
        return self._platform.value

    def get_group_id(self) -> str:
        return self.group_id

    def get_message_type(self) -> EventMessageType:
        return self._message_type

    def is_at_bot(self) -> bool:
        return any(isinstance(c, At) for c in self._components)

    def get_messages(self) -> list[MessageComponent]:
        return self._components

    def make_result(self) -> MessageEventResult:
        return MessageEventResult()

@dataclass
class MessageEventResult:
    message_chain: list[MessageComponent] = field(default_factory=list)
    use_t2i: bool = False
    _result_text: str = ""

    def set_text(self, text: str):
        self._result_text = text
        self.message_chain = [Plain(text)]

    def chain(self, *components: MessageComponent):
        self.message_chain = list(components)

@dataclass
class CommandResult:
    message_chain: list[MessageComponent] = field(default_factory=list)
    use_t2i: bool = False

    @staticmethod
    def from_text(text: str) -> CommandResult:
        return CommandResult(message_chain=[Plain(text)])

    @staticmethod
    def from_chain(*components: MessageComponent) -> CommandResult:
        return CommandResult(message_chain=list(components))

