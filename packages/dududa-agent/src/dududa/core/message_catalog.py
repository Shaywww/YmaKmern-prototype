"""Deterministic, versioned catalogue for non-generative user messages."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


SELECTOR_VERSION = "message-selector/1.0"


class MessageKey(str, Enum):
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_NO_RESULT = "tool_no_result"
    USER_CANCELLED = "user_cancelled"
    MODEL_UNAVAILABLE = "model_unavailable"
    MEDIA_UNREADABLE = "media_unreadable"
    GREETING_ACK = "greeting_ack"
    EMPTY_SEMANTIC_REPLY = "empty_semantic_reply"
    PERMISSION_DENIED = "permission_denied"
    SAFETY_CLARIFICATION = "safety_clarification"


@dataclass(frozen=True)
class MessageVariant:
    variant_id: str
    text: str


@dataclass(frozen=True)
class MessageSelection:
    key: MessageKey
    variant: MessageVariant
    selector_version: str
    seed_source: str


DEFAULT_MESSAGES: Mapping[MessageKey, tuple[MessageVariant, ...]] = {
    MessageKey.TOOL_TIMEOUT: (
        MessageVariant("tool_timeout.1", "这次查询超时了，稍后再试一次。"),
        MessageVariant("tool_timeout.2", "查询没能按时完成，过一会儿再试吧。"),
    ),
    MessageKey.TOOL_NO_RESULT: (
        MessageVariant("tool_no_result.1", "这次没有查到可靠结果，我先不乱猜。"),
        MessageVariant("tool_no_result.2", "查询没有拿到可用数据，暂时没法给出结论。"),
    ),
    MessageKey.USER_CANCELLED: (
        MessageVariant("user_cancelled.1", "好，这次任务已经取消。"),
    ),
    MessageKey.MODEL_UNAVAILABLE: (
        MessageVariant("model_unavailable.1", "我这会儿有点卡，稍后再试一次。"),
    ),
    MessageKey.MEDIA_UNREADABLE: (
        MessageVariant("media_unreadable.1", "这份内容没有识别清楚，可以重新发一次。"),
    ),
    MessageKey.GREETING_ACK: (
        MessageVariant("greeting_ack.1", "你好呀～"),
        MessageVariant("greeting_ack.2", "在呢～"),
    ),
    MessageKey.EMPTY_SEMANTIC_REPLY: (
        MessageVariant("empty_semantic_reply.1", "这句我没接稳，换个说法再来一次。"),
    ),
    MessageKey.PERMISSION_DENIED: (
        MessageVariant("permission_denied.1", "这项操作没有权限执行。"),
    ),
    MessageKey.SAFETY_CLARIFICATION: (
        MessageVariant("safety_clarification.1", "你现在是否处于立即危险中？"),
    ),
}

_FIXED_KEYS = frozenset({
    MessageKey.PERMISSION_DENIED,
    MessageKey.SAFETY_CLARIFICATION,
})


class MessageCatalog:
    def __init__(self, messages=DEFAULT_MESSAGES,
                 selector_version: str = SELECTOR_VERSION):
        self._messages = dict(messages)
        self.selector_version = str(selector_version)

    def select(
        self,
        key: MessageKey,
        *,
        policy_version: str,
        platform_message_id: str = "",
        idempotency_key: str = "",
        conversation_event_id: str = "",
        run_id: str = "",
    ) -> MessageSelection:
        variants = tuple(self._messages.get(key, ()))
        if not variants:
            raise KeyError(f"no variants registered for {key.value}")
        seed_value, source = self._seed_identity(
            platform_message_id=platform_message_id,
            idempotency_key=idempotency_key,
            conversation_event_id=conversation_event_id,
            run_id=run_id,
        )
        if key in _FIXED_KEYS:
            index = 0
            source = "fixed"
        else:
            payload = (
                f"{key.value}|{policy_version}|{self.selector_version}|"
                f"{seed_value}").encode("utf-8")
            index = int.from_bytes(
                hashlib.sha256(payload).digest()[:8], "big") % len(variants)
        return MessageSelection(
            key=key,
            variant=variants[index],
            selector_version=self.selector_version,
            seed_source=source,
        )

    @staticmethod
    def _seed_identity(*, platform_message_id: str,
                       idempotency_key: str,
                       conversation_event_id: str,
                       run_id: str) -> tuple[str, str]:
        candidates = (
            (platform_message_id, "platform_message_id"),
            (idempotency_key, "idempotency_key"),
            (conversation_event_id, "conversation_event_id"),
            (run_id, "run_id"),
        )
        for value, source in candidates:
            canonical = str(value or "").strip()
            if canonical:
                return canonical, source
        return "missing-idempotency-key", "deterministic_default"
