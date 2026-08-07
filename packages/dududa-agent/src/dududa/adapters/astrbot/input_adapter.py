"""AstrBotInputAdapter —— 将 AstrBot 原始事件转换为平台无关的 MessageEnvelope。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from ...core.envelope import (
    Platform, MessageKind, MessageEnvelope, ConversationRef,
    Actor, Attachment, AttachmentKind, PreprocessedEnvelope,
)
from .types import (
    AstrMessageEvent, EventMessageType, AstrBotPlatform, MessageComponent,
)

PLATFORM_MAP = {
    AstrBotPlatform.AIOCQHTTP: Platform.QQ,
    AstrBotPlatform.QQ_OFFICIAL: Platform.QQ,
    AstrBotPlatform.LAGRANGE: Platform.QQ,
    AstrBotPlatform.UNKNOWN: Platform.QQ,
}

MESSAGE_TYPE_MAP = {
    EventMessageType.GROUP_MESSAGE: MessageKind.GROUP,
    EventMessageType.PRIVATE_MESSAGE: MessageKind.PRIVATE,
    EventMessageType.ADMIN_COMMAND: MessageKind.PRIVATE,
}

@dataclass(frozen=True)
class ActorMappingConfig:
    """Actor identity mapping config with optional privacy hashing."""
    hash_user_ids: bool = True
    owner_ids: tuple[str, ...] = ()
    admin_ids: tuple[str, ...] = ()
    trusted_ids: tuple[str, ...] = ()
    muted_ids: tuple[str, ...] = ()

    def resolve_role(self, user_id: str) -> str:
        if user_id in self.owner_ids: return "owner"
        if user_id in self.admin_ids: return "admin"
        if user_id in self.trusted_ids: return "trusted"
        if user_id in self.muted_ids: return "muted"
        return "normal"


class AstrBotInputAdapter:
    """将 AstrMessageEvent 转为平台无关的 MessageEnvelope / PreprocessedEnvelope。

    职责：
    - 平台枚举映射（AstrBot platform → Core Platform）
    - 消息类型映射（群聊/私聊）
    - Actor 身份脱敏与角色判定
    - 附件提取（图片、文件、引用回复、@提及）
    - 多模态预处理钩子（OCR、图片描述等，可选）
    - 生成 PreprocessedEnvelope
    """

    def __init__(self, actor_config: Optional[ActorMappingConfig] = None):
        self.actor_config = actor_config or ActorMappingConfig()

    # ── 主入口 ────────────────────────────────────────

    def to_envelope(self, event: AstrMessageEvent) -> MessageEnvelope:
        """将 AstrBot 事件转为标准 MessageEnvelope。"""
        platform = PLATFORM_MAP.get(
            AstrBotPlatform(event.get_platform_name()),
            Platform.QQ,
        )
        kind = MESSAGE_TYPE_MAP.get(
            event.get_message_type(),
            MessageKind.GROUP,
        )

        sender = self._build_actor(event, platform)
        conversation = self._build_conversation(event, platform, kind)
        attachments = self._extract_attachments(event)
        mentions = self._extract_mentions(event)

        return MessageEnvelope(
            envelope_id=uuid4().hex,
            platform=platform,
            kind=kind,
            conversation=conversation,
            sender=sender,
            text=event.message_str,
            attachments=tuple(attachments),
            mentions=tuple(mentions),
            platform_message_id=getattr(event, "message_id", "") or getattr(event, "session_id", "") or None,
            received_at=datetime.now(timezone.utc),
            reply_to=self._extract_reply(event),
        )

    def to_preprocessed(self, event: AstrMessageEvent) -> PreprocessedEnvelope:
        """直接生成带预处理的信封。"""
        envelope = self.to_envelope(event)
        components = event.get_messages()
        ocr_text = self._extract_ocr(components)
        image_desc = self._extract_image_description(components)
        return PreprocessedEnvelope(
            envelope=envelope,
            ocr_text=ocr_text,
            image_description=image_desc,
            file_summary=None,
            validated=True,
        )

    # ── Actor 构建 ────────────────────────────────────

    def _build_actor(self, event, platform: Platform) -> Actor:
        # 兼容不同平台：AstrMessageEvent stub vs QQOfficial vs QQWebhook
        raw_id = "unknown"
        nickname = "unknown"
        sender = getattr(event, 'sender', None)
        if sender:
            raw_id = getattr(sender, 'user_id', '') or 'unknown'
            nickname = getattr(sender, 'nickname', '') or raw_id
        else:
            raw_id = str(getattr(event, 'get_sender_id', lambda: 'unknown')()) or 'unknown'
            nickname = str(getattr(event, 'get_sender_name', lambda: raw_id)()) or raw_id
        actor_id = self._hash_id(raw_id) if self.actor_config.hash_user_ids else raw_id
        return Actor(
            actor_id=actor_id,
            platform=platform,
            display_name=nickname,
            role=self.actor_config.resolve_role(raw_id),
        )

    def _hash_id(self, user_id: str) -> str:
        """用户 ID 脱敏哈希。"""
        import hashlib
        return hashlib.sha256(f"dududa:{user_id}".encode()).hexdigest()[:16]

    # ── 会话构建 ──────────────────────────────────────

    def _build_conversation(
        self, event: AstrMessageEvent, platform: Platform, kind: MessageKind
    ) -> ConversationRef:
        if kind == MessageKind.GROUP and getattr(event, "get_group_id", lambda: "")() or getattr(event, "group_id", ""):
            conv_id = getattr(event, "get_group_id", lambda: "")() or getattr(event, "group_id", "")
        else:
            conv_id = getattr(event, "get_session_id", lambda: "")() or getattr(event, "session_id", "") or getattr(event, "get_sender_id", lambda: "")() or "unknown" or "unknown"
        return ConversationRef(
            conversation_id=conv_id,
            platform=platform,
            kind=kind,
        )

    # ── 附件提取 ──────────────────────────────────────

    def _extract_attachments(self, event: AstrMessageEvent) -> list[Attachment]:
        attachments: list[Attachment] = []
        for comp in event.get_messages():
            att = self._component_to_attachment(comp)
            if att:
                attachments.append(att)
        return attachments

    def _component_to_attachment(self, comp: MessageComponent) -> Optional[Attachment]:
        if comp.type == "image":
            return Attachment(
                kind=AttachmentKind.IMAGE,
                content_ref=comp.url or comp.file or "image",
                summary=f"[Image: {comp.url or comp.file}]" if (comp.url or comp.file) else "[Image]",
            )
        if comp.type == "reply":
            return Attachment(
                kind=AttachmentKind.REPLY_REF,
                content_ref=comp.qq or "reply",
                summary=f"[Reply to: {comp.qq}]",
            )
        return None

    def _extract_mentions(self, event: AstrMessageEvent) -> list[str]:
        mentions: list[str] = []
        for comp in event.get_messages():
            if comp.type == "at" and comp.qq:
                raw_id = comp.qq
                actor_id = self._hash_id(raw_id) if self.actor_config.hash_user_ids else raw_id
                mentions.append(actor_id)
        return mentions

    # ── 多模态预处理 ──────────────────────────────────

    def _extract_reply(self, event: AstrMessageEvent) -> Optional[MessageEnvelope]:
        """提取回复链（Reply 组件）。

        Connector 契约：回复必须指向同一会话。当平台在 Reply 载荷里
        显式给出 group_id 且与当前会话不一致时，用引用会话构造 reply_to，
        供 Orchestrator / Handler 做跨会话拒绝。
        """
        try:
            comps = event.get_messages() or ()
        except Exception:
            return None
        for comp in comps:
            if getattr(comp, "type", "") != "reply":
                continue
            rid = str(getattr(comp, "id", "") or getattr(comp, "message_id", "") or "")
            if not rid:
                continue
            platform = PLATFORM_MAP.get(
                AstrBotPlatform(event.get_platform_name()), Platform.QQ)
            cur_group = str(getattr(event, "group_id", "") or "")
            src_group = str(getattr(comp, "group_id", "") or "")
            if src_group and cur_group and src_group != cur_group:
                kind, conv_id = MessageKind.GROUP, src_group
            else:
                kind = MessageKind.GROUP if cur_group else MessageKind.PRIVATE
                conv_id = cur_group or (getattr(event, "session_id", "") or "unknown")
            return MessageEnvelope(
                envelope_id=uuid4().hex,
                platform=platform,
                kind=kind,
                conversation=ConversationRef(
                    conversation_id=conv_id, platform=platform, kind=kind),
                sender=Actor(actor_id="", platform=platform, display_name=""),
                text="", mentions=(), received_at=datetime.now(timezone.utc),
                platform_message_id=rid,
            )
        return None

    def _extract_ocr(self, components: list[MessageComponent]) -> Optional[str]:
        """图片 OCR 提取 —— 接入时替换为真实 OCR 服务。"""
        return None

    def _extract_image_description(self, components: list[MessageComponent]) -> Optional[str]:
        """图片描述提取 —— 接入时替换为视觉模型。"""
        return None

