# -*- coding: utf-8 -*-
"""Delivery 两段式完成协议（文档 2.3.15-2.3.16）。

契约：
- run() 有可见输出时停在 READY_TO_EMIT，返回 RuntimeResult；
- Output Adapter 实际发送后回传 DeliveryReceipt，Runtime 才确认投递；
- SUCCEEDED 回执 -> DELIVERY_ACKNOWLEDGED -> MEMORY_EVALUATED -> COMPLETED；
- FAILED/UNKNOWN 仍推进 DELIVERY_ACKNOWLEDGED，但不写"已告知"类记忆；
- 重复/错配/无时区回执幂等拒绝；
- 无可视输出（IGNORE/降级无回复）不伪造回执，只评估不依赖投递的候选。
"""
import sys
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
import time

import pytest

from dududa.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from dududa.core.state import RuntimePhase, RunOutcome, RuntimeState
from dududa.core.decision import SocialDecisionEngine
from dududa.core.delivery import DeliveryReceipt, DeliveryStatus
from dududa.core.memory import InMemoryRepository, MemoryScope, MemoryType
from dududa.runtime.orchestrator import RuntimeOrchestrator
from dududa.application import dududa_handlers as H


def _envelope(text="你好", **kwargs):
    defaults = dict(
        platform=Platform.QQ,
        kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="group_123",
            platform=Platform.QQ,
            kind=MessageKind.GROUP,
        ),
        sender=Actor(actor_id="user_1", platform=Platform.QQ, display_name="小明"),
        text=text,
        mentions=("bot_001",),
    )
    defaults.update(kwargs)
    return MessageEnvelope(**defaults)


def _orch(memory=None):
    return RuntimeOrchestrator(
        decision_engine=SocialDecisionEngine(reply_probability=1.0),
        memory_repo=memory or InMemoryRepository(),
    )


def _bot_scope():
    return MemoryScope(
        memory_type=MemoryType.SHORT_TERM, platform="qq", bot_id="dududa",
        conversation_id="group_123", actor_id="user_1")


class _FakeEvent:
    """最小 AstrBot 事件替身：只支持 extra 与 platform。"""

    def __init__(self, platform="aiocqhttp"):
        self._extra = {}
        self.platform = platform

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def get_session_id(self):
        return "group_123"


class _FakePlugin:
    def __init__(self, orch):
        self.runtime = orch
        self._pending_deliveries = {}
        self._store_memory_calls = []

    def _store_memory(self, event, *args, **kwargs):
        self._store_memory_calls.append((args, kwargs))


class TestTwoPhaseProtocol:
    @pytest.mark.asyncio
    async def test_run_stops_at_ready_to_emit(self):
        """run() 有可见输出时停在 READY_TO_EMIT，不自行完成。"""
        orch = _orch()
        result = await orch.run(_envelope("你好"))
        assert result.has_visible_output
        assert orch._last_state.phase == RuntimePhase.READY_TO_EMIT
        assert result.run_id == orch._last_state.run_id

    @pytest.mark.asyncio
    async def test_succeeded_receipt_completes_and_writes_memory(self):
        """SUCCEEDED 回执：推进三段并写入 bot 记忆。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.SUCCEEDED)
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.final_phase == RuntimePhase.COMPLETED.value
        assert comp.delivery_status == DeliveryStatus.SUCCEEDED
        assert comp.memory_write_receipts, "SUCCEEDED 应写入依赖投递的 bot 记忆"
        state = orch._last_state
        assert state.phase == RuntimePhase.COMPLETED
        phases = [t["to_phase"] for t in state.trace]
        assert "delivery_acknowledged" in phases
        assert "memory_evaluated" in phases
        assert "completed" in phases
        records = memory.query(_bot_scope(), limit=20)
        assert any("[YmaKmern]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_failed_receipt_advances_but_skips_bot_memory(self):
        """FAILED 回执：仍推进 DELIVERY_ACKNOWLEDGED，但不写"已告知"记忆。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.FAILED,
                                  error_message="send timeout")
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.delivery_status == DeliveryStatus.FAILED
        assert comp.final_phase == RuntimePhase.COMPLETED.value
        records = memory.query(_bot_scope(), limit=20)
        assert not any("[YmaKmern]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_unknown_receipt_skips_bot_memory(self):
        """UNKNOWN 回执：不把 unknown 当成功，不写依赖投递的记忆。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.UNKNOWN)
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.delivery_status == DeliveryStatus.UNKNOWN
        records = memory.query(_bot_scope(), limit=20)
        assert not any("[YmaKmern]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_duplicate_receipt_idempotent(self):
        """重复回执幂等：返回同一完成回执，不重复写记忆。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.SUCCEEDED)
        comp1 = await orch.acknowledge_delivery(receipt)
        comp2 = await orch.acknowledge_delivery(receipt)
        assert comp2 is comp1
        records = memory.query(_bot_scope(), limit=20)
        bot = [r for r in records if "[YmaKmern]" in r.content]
        assert len(bot) == 1

    @pytest.mark.asyncio
    async def test_mismatched_run_id_rejected(self):
        """悬挂/错配回执：幂等拒绝，不做任何推进或写入。"""
        orch = _orch()
        await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id="not-this-run",
                                  status=DeliveryStatus.SUCCEEDED)
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.memory_write_receipts == ()
        assert comp.run_id == "not-this-run"

    @pytest.mark.asyncio
    async def test_receipt_without_timezone_rejected(self):
        """回执时间必须带时区（文档 2.3.15）。"""
        from datetime import datetime
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.SUCCEEDED,
                                  acknowledged_at=datetime.now())  # naive
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.memory_write_receipts == ()
        records = memory.query(_bot_scope(), limit=20)
        assert not any("[YmaKmern]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_complete_without_delivery_ignored_run(self):
        """IGNORE 运行：不伪造回执，final_phase 保持 decided，无写入。"""
        orch = _orch()
        state = RuntimeState(run_id="r-ignore", phase=RuntimePhase.DECIDED,
                             outcome=RunOutcome.IGNORED)
        orch._last_state = state
        comp = await orch.complete_without_delivery()
        assert comp.delivery_status is None
        assert comp.final_phase == RuntimePhase.DECIDED.value
        assert comp.memory_write_receipts == ()

    @pytest.mark.asyncio
    async def test_complete_without_delivery_skips_ack_dependent(self):
        """无可视输出但 READY_TO_EMIT：只写不依赖投递的候选。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        # 构造"无回复但已到 READY_TO_EMIT"的等价状态
        state = orch._last_state
        comp = await orch.complete_without_delivery(state=state)
        assert comp.delivery_status is None
        # 依赖投递的 bot 候选不写
        records = memory.query(_bot_scope(), limit=20)
        assert not any("[YmaKmern]" in r.content for r in records)


class TestProductionTwoPhase:
    @pytest.mark.asyncio
    async def test_stash_then_hook_ack(self):
        """Phase A 暂存 -> Phase B 钩子确认：状态完成、pending 清空。"""
        orch = _orch()
        result = await orch.run(_envelope("你好"))
        event = _FakeEvent()
        plugin = _FakePlugin(orch)
        H._stash_pending_delivery(plugin, event, result, "你好呀～")
        assert result.run_id in plugin._pending_deliveries
        assert event.get_extra("dududa_run_id") == result.run_id
        await H.complete_delivery_after_send(plugin, event)
        assert result.run_id not in plugin._pending_deliveries
        comp = orch._completions[result.run_id]
        assert comp.delivery_status == DeliveryStatus.SUCCEEDED
        assert comp.final_phase == RuntimePhase.COMPLETED.value

    @pytest.mark.asyncio
    async def test_hook_noop_without_tag(self):
        """未打标的会话发送（其他插件/命令）：钩子直接返回。"""
        orch = _orch()
        plugin = _FakePlugin(orch)
        await H.complete_delivery_after_send(plugin, _FakeEvent())
        assert orch._completions == {}

    @pytest.mark.asyncio
    async def test_hook_unknown_run_id_noop(self):
        """已确认过/未知 run_id：幂等跳过。"""
        orch = _orch()
        result = await orch.run(_envelope("你好"))
        event = _FakeEvent()
        plugin = _FakePlugin(orch)
        H._stash_pending_delivery(plugin, event, result, "hi")
        await H.complete_delivery_after_send(plugin, event)
        # 第二次（重复钩子触发）不应报错也不应重复写
        await H.complete_delivery_after_send(plugin, event)
        assert result.run_id not in plugin._pending_deliveries

    @pytest.mark.asyncio
    async def test_prune_stale_acks_unknown(self):
        """超时未确认：按 UNKNOWN 回执收尾，不写"已送达"记忆。"""
        memory = InMemoryRepository()
        orch = _orch(memory)
        result = await orch.run(_envelope("你好"))
        plugin = _FakePlugin(orch)
        plugin._pending_deliveries[result.run_id] = (result, "hi", time.time() - 300)
        await H._prune_stale_deliveries(plugin, max_age=120)
        assert result.run_id not in plugin._pending_deliveries
        comp = orch._completions[result.run_id]
        assert comp.delivery_status == DeliveryStatus.UNKNOWN
        records = memory.query(_bot_scope(), limit=20)
        assert not any("[YmaKmern]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_prune_keeps_fresh(self):
        """未超时的 pending 不被误清理。"""
        orch = _orch()
        result = await orch.run(_envelope("你好"))
        plugin = _FakePlugin(orch)
        plugin._pending_deliveries[result.run_id] = (result, "hi", time.time())
        await H._prune_stale_deliveries(plugin, max_age=120)
        assert result.run_id in plugin._pending_deliveries
        assert orch._completions == {}
