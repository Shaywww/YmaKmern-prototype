"""AstrBotOutputAdapter - RuntimeResult to AstrBot messages."""
from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from ...core.delivery import OutputAdapter, DeliveryReceipt, DeliveryStatus, RuntimeResult
from ...core.envelope import Platform
from .types import AstrMessageEvent, MessageEventResult, CommandResult, Plain, Image, At, Reply, MessageComponent

MAX_CHUNK_LENGTH = 4000
SEND_DELAY = 0.6
MAX_REPLY_LEN = 40

@dataclass
class AstrBotSendContext:
    event: AstrMessageEvent
    sent_components: list[MessageComponent] = field(default_factory=list)
    sent_at: Optional[datetime] = None

    @property
    def conversation_id(self) -> str:
        return self.event.group_id or self.event.session_id or "unknown"

    @property
    def is_group(self) -> bool:
        return bool(self.event.group_id)


class AstrBotOutputAdapter(OutputAdapter):
    def __init__(self, rate_limit_delay: float = SEND_DELAY):
        self._contexts: dict[str, AstrBotSendContext] = {}
        self._rate_limit_delay = rate_limit_delay
        self._last_send: float = 0.0

    def bind_event(self, run_id: str, event: AstrMessageEvent):
        self._contexts[run_id] = AstrBotSendContext(event=event)

    def unbind_event(self, run_id: str):
        self._contexts.pop(run_id, None)

    async def send(self, target_platform: Platform, conversation_id: str, result: RuntimeResult) -> DeliveryReceipt:
        ctx = self._contexts.get(result.run_id)
        if ctx is None or result.final_response is None:
            return DeliveryReceipt(run_id=result.run_id, status=DeliveryStatus.FAILED, error_code='NO_CONTEXT', error_message='No event bound or no response to send')
        try:
            components = self._build_components(result, ctx)
            await self._rate_limit()
            await self._dispatch(ctx.event, components)
            ctx.sent_components = components
            ctx.sent_at = datetime.now(timezone.utc)
            return DeliveryReceipt(run_id=result.run_id, status=DeliveryStatus.SUCCEEDED, acknowledged_at=datetime.now(timezone.utc), platform_message_ref=ctx.event.message_id)
        except Exception as e:
            return DeliveryReceipt(run_id=result.run_id, status=DeliveryStatus.FAILED, error_code='SEND_ERROR', error_message=str(e))

    async def send_reaction(self, target_platform: Platform, conversation_id: str, reaction: str) -> DeliveryReceipt:
        return DeliveryReceipt(run_id='reaction', status=DeliveryStatus.SUCCEEDED, acknowledged_at=datetime.now(timezone.utc))

    def _build_components(self, result: RuntimeResult, ctx: AstrBotSendContext) -> list[MessageComponent]:
        components: list[MessageComponent] = []
        if result.run_id in self._contexts and ctx.is_group:
            ref_text = ctx.event.message_str[:MAX_REPLY_LEN]
            if len(ctx.event.message_str) > MAX_REPLY_LEN:
                ref_text += "..."
            if ref_text:
                components.append(Plain("[Reply: " + ref_text + "]"))
        text = result.final_response.text if result.final_response else ''
        if text:
            chunks = self._chunk_text(text)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    components.append(Plain("---"))
                components.append(Plain(chunk))
        if result.final_response and result.final_response.metadata:
            mentions = result.final_response.metadata.get('mentions', [])
            for mention_id in mentions:
                components.append(At(qq=mention_id))
                components.append(Plain(" "))
        return components

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= MAX_CHUNK_LENGTH:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len(remaining) > MAX_CHUNK_LENGTH:
            split_at = remaining.rfind(chr(10), 0, MAX_CHUNK_LENGTH)
            if split_at == -1:
                split_at = MAX_CHUNK_LENGTH
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip(chr(10))
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _dispatch(self, event: AstrMessageEvent, components: list[MessageComponent]):
        result = event.make_result()
        if components:
            result.chain(*components)

    async def _rate_limit(self):
        now = time.monotonic()
        wait = self._last_send + self._rate_limit_delay - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = time.monotonic()
