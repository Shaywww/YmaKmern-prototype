"""嘟嘟哒 2.0 核心领域类型 —— 平台无关的消息信封与参与者。

MessageEnvelope 将平台原始事件转换为平台中立的标准化消息，
Actor 表示标准化后的参与者身份。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class Platform(str, Enum):
    """支持的输入平台。"""
    QQ = "qq"
    WECHAT_WORK = "wechat_work"  # 飞书等，规划中
    WEB_CHAT = "web_chat"        # 规划中


class MessageKind(str, Enum):
    """消息类型。"""
    GROUP = "group"
    PRIVATE = "private"
    LIGHT_INTERACTION = "light_interaction"  # 戳一戳、点赞等


class AttachmentKind(str, Enum):
    """附件类型。"""
    IMAGE = "image"
    FILE = "file"
    REPLY_REF = "reply_ref"  # 引用回复
    MENTION = "mention"      # @提及


@dataclass(frozen=True)
class Attachment:
    """平台无关的附件摘要。原始 URL / 文件句柄不进入 Core。"""
    kind: AttachmentKind
    content_ref: str          # 不透明引用，仅供 Adapter 解析
    summary: Optional[str] = None  # 受控摘要（OCR、文件摘要等）
    mime_type: Optional[str] = None


@dataclass(frozen=True)
class Actor:
    """平台无关的参与者身份。

    不携带原始 QQ 号、昵称原文或凭据。
    """
    actor_id: str             # 平台范围内的稳定 ID（脱敏后）
    platform: Platform
    display_name: str         # 脱敏/受控的显示名
    role: str = "normal"      # owner | admin | trusted | normal | muted
    deny_flags: tuple[str, ...] = ()  # 拒绝覆盖标记，如 ("muted",)

    def is_privileged(self) -> bool:
        return self.role in ("owner", "admin")

    def is_muted(self) -> bool:
        """muted 是 deny overlay：角色不是万能通行证。"""
        return self.role == "muted" or "muted" in self.deny_flags


@dataclass(frozen=True)
class ConversationRef:
    """会话引用。"""
    conversation_id: str
    platform: Platform
    kind: MessageKind


@dataclass(frozen=True)
class MessageEnvelope:
    """平台无关的标准化消息信封。

    这是 Agent Core 的唯一输入格式。原始 AstrBot Event、UMO、
    Provider 对象、CQ 码和凭据不能进入 Runtime State。
    """
    envelope_id: str = field(default_factory=lambda: uuid4().hex)
    platform: Platform = Platform.QQ
    kind: MessageKind = MessageKind.GROUP
    conversation: ConversationRef = field(
        default_factory=lambda: ConversationRef(
            conversation_id="unknown", platform=Platform.QQ, kind=MessageKind.GROUP
        )
    )
    sender: Actor = field(
        default_factory=lambda: Actor(
            actor_id="unknown", platform=Platform.QQ, display_name="unknown"
        )
    )
    text: str = ""
    attachments: tuple[Attachment, ...] = ()
    reply_to: Optional[MessageEnvelope] = None  # 显式回复链
    mentions: tuple[str, ...] = ()              # 被 @ 的 actor_id 列表
    platform_message_id: Optional[str] = None   # 平台消息 ID，用于去重
    received_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # --- 校验 ---

    def has_attachment(self, kind: AttachmentKind) -> bool:
        return any(a.kind == kind for a in self.attachments)

    def is_mentioned(self, actor_id: str) -> bool:
        return actor_id in self.mentions

    def is_explicit_command(self) -> bool:
        """是否是显式命令（@Bot 或以 / 开头）。"""
        return bool(self.mentions) or self.text.lstrip().startswith("/")

    def reply_chain(self) -> list[MessageEnvelope]:
        """展开回复链（最近优先）。"""
        chain: list[MessageEnvelope] = []
        current: Optional[MessageEnvelope] = self
        while current is not None:
            chain.append(current)
            current = current.reply_to
        return chain


@dataclass(frozen=True)
class PreprocessedEnvelope:
    """经过 Connector 校验与多模态预处理的标准消息。

    包含原始 Envelope 和经过 OCR/图片描述/文件摘要后的补充信息。
    """
    envelope: MessageEnvelope
    ocr_text: Optional[str] = None
    image_description: Optional[str] = None
    file_summary: Optional[str] = None
    reply_chain_evidence: tuple[str, ...] = ()  # 回复链中的关键消息摘要
    validated: bool = True
    validation_errors: tuple[str, ...] = ()

    @property
    def combined_text(self) -> str:
        """合并原始文本与预处理结果。"""
        parts = [self.envelope.text]
        if self.ocr_text:
            parts.append(f"[OCR: {self.ocr_text}]")
        if self.image_description:
            parts.append(f"[Image: {self.image_description}]")
        if self.file_summary:
            parts.append(f"[File: {self.file_summary}]")
        return " ".join(parts)

