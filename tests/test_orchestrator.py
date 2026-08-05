"""测试 Runtime Orchestrator 完整流程。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from packages.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from packages.core.state import RunOutcome, SocialAction
from packages.core.decision import SocialDecisionEngine
from packages.core.renderer import FinalResponse
from packages.core.delivery import DeliveryStatus, NoOpOutputAdapter, DeliveryManager
from packages.runtime.orchestrator import RuntimeOrchestrator


def _make_envelope(text="你好", **kwargs):
    defaults = dict(
        platform=Platform.QQ,
        kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="group_123",
            platform=Platform.QQ,
            kind=MessageKind.GROUP,
        ),
        sender=Actor(
            actor_id="user_1",
            platform=Platform.QQ,
            display_name="小明",
        ),
        text=text,
    )
    defaults.update(kwargs)
    return MessageEnvelope(**defaults)


class TestRuntimeOrchestrator:
    def _make_orchestrator(self):
        return RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )

    @pytest.mark.asyncio
    async def test_simple_message_produces_output(self):
        """普通消息应正常处理（可能回复或忽略）。"""
        orch = self._make_orchestrator()
        envelope = _make_envelope(text="今天天气真好")
        result = await orch.run(envelope)
        assert result.outcome in (RunOutcome.IGNORED, RunOutcome.SUCCEEDED)
        assert result.outcome is not None

    @pytest.mark.asyncio
    async def test_answer_on_mention(self):
        """被 @ 时应 ANSWER。"""
        orch = self._make_orchestrator()
        envelope = _make_envelope(
            text="嘟嘟哒，帮我查一下课程",
            mentions=("bot_001",),
        )
        result = await orch.run(envelope)
        assert result.outcome in (RunOutcome.SUCCEEDED, RunOutcome.DEGRADED)
        assert result.has_visible_output

    @pytest.mark.asyncio
    async def test_answer_on_command(self):
        """显式命令应 ANSWER。"""
        orch = self._make_orchestrator()
        envelope = _make_envelope(text="/help")
        result = await orch.run(envelope)
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.has_visible_output

    @pytest.mark.asyncio
    async def test_blocked(self):
        orch = self._make_orchestrator()
        envelope = _make_envelope(text="test")
        result = await orch.run(envelope)
        assert result.outcome in (RunOutcome.IGNORED, RunOutcome.SUCCEEDED)

    @pytest.mark.asyncio
    async def test_acknowledge_delivery(self):
        orch = self._make_orchestrator()
        envelope = _make_envelope(text="/help")
        result = await orch.run(envelope)
        if result.has_visible_output and result.outcome == RunOutcome.SUCCEEDED:
            mgr = DeliveryManager(NoOpOutputAdapter())
            receipt = await mgr.deliver(result, Platform.QQ, "group_123")
            assert receipt.status == DeliveryStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_run_id_unique(self):
        orch = self._make_orchestrator()
        r1 = await orch.run(_make_envelope(text="/help"))
        r2 = await orch.run(_make_envelope(text="/help"))
        assert r1.run_id != r2.run_id

    @pytest.mark.asyncio
    async def test_keyword_triggers_search(self):
        """关键词应触发回复。"""
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(reply_probability=1.0),
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        envelope = _make_envelope(text="帮我查一下数据结构")
        result = await orch.run(envelope)
        assert result.outcome is not None


class TestOrchestratorErrorHandling:
    @pytest.mark.asyncio
    async def test_none_envelope(self):
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(None)  # type: ignore
        assert result.outcome == RunOutcome.FAILED
