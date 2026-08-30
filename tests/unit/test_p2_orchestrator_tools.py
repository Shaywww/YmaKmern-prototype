# -*- coding: utf-8 -*-
"""P2: Runtime Orchestrator 真实工具链（文档 2.5.5）—— 规划/校验/执行/回执。"""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import pytest
from dududa.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from dududa.core.state import RunOutcome, SocialAction, RuntimeBudget
from dududa.core.decision import (
    SocialDecisionEngine, SocialDecision, DecisionReason,
)
from dududa.core.capability import (
    Capability, CapabilitySchema, CapabilityRegistry, CapProvider,
    ProviderType, ToolObservation,
)
from dududa.core.memory import InMemoryRepository, MemoryType, ScopeSelector
from dududa.core.delivery import (
    DeliveryManager, NoOpOutputAdapter, DeliveryReceipt, DeliveryStatus,
)
from dududa.runtime.orchestrator import RuntimeOrchestrator


def _make_envelope(text="你好", mentions=()):
    return MessageEnvelope(
        platform=Platform.QQ,
        kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="group_123",
            platform=Platform.QQ,
            kind=MessageKind.GROUP,
        ),
        sender=Actor(actor_id="user_1", platform=Platform.QQ, display_name="小明"),
        text=text,
        mentions=mentions,
    )


class _EchoProvider(CapProvider):
    """成功 Provider：返回固定数据并计数。"""

    def __init__(self, payload="数据结构课程信息: CS2001"):
        self.payload = payload
        self.calls = 0

    async def execute(self, cap, args):
        self.calls += 1
        return ToolObservation(
            step_id="", capability_id=cap.capability_id,
            success=True, data=self.payload, source="test",
        )

    def health(self):
        return True


class _FailProvider(CapProvider):
    async def execute(self, cap, args):
        raise RuntimeError("simulated provider failure")

    def health(self):
        return True


class _UseToolsDecisionEngine(SocialDecisionEngine):
    """强制 USE_TOOLS：即使 perception.needs_tools=False 也触发工具链。"""

    def decide(self, perception=None, context=None, now=None):
        return SocialDecision(
            action=SocialAction.USE_TOOLS,
            reason_codes=(DecisionReason.KEYWORD_MATCH,),
            confidence=1.0,
        )


def _register(reg, cap_id, provider, schema=None):
    reg.register(
        Capability(
            capability_id=cap_id, name=cap_id, description="test capability",
            provider=ProviderType.BUILTIN,
            schema=schema or CapabilitySchema(),
        ),
        provider,
    )


class TestToolChainRealExecution:
    @pytest.mark.asyncio
    async def test_mcp_plan_execute_returns_data(self):
        """真实工具链：MCP 集成 -> 规划 -> 校验 -> 执行 -> 数据进 FinalResponse。"""
        from dududa.mcp.registry import register_all_mcp_services
        from dududa.planner.integration import integrate_with_orchestrator
        reg = CapabilityRegistry()
        register_all_mcp_services(reg)
        for _g in ("mcp.weather", "mcp.news", "mcp.translate"):
            reg.unregister(_g)  # 保持工具链核心测试隔离（不依赖新服务网络）
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            planner_integration=integrate_with_orchestrator(None, reg),
        )
        result = await orch.run(_make_envelope(text="帮我查一下现在时间"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.final_response is not None
        assert result.final_response.text.strip()
        assert result.trace_summary["tool_steps"] >= 1

    @pytest.mark.asyncio
    async def test_use_tools_without_text_signal(self):
        """USE_TOOLS 决策本身即可触发工具链（无需 查/搜// 文本信号）。"""
        reg = CapabilityRegistry()
        _register(reg, "test.echo", _EchoProvider("查询结果: ok"))
        orch = RuntimeOrchestrator(
            decision_engine=_UseToolsDecisionEngine(),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(_make_envelope(text="你好呀"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.final_response and "查询结果: ok" in result.final_response.text


class TestBudgetAndFallback:
    @pytest.mark.asyncio
    async def test_direct_fallback_without_integration(self):
        """无 Planner 集成时直连 Top-K 兜底仍可执行。"""
        reg = CapabilityRegistry()
        _register(reg, "test.echo", _EchoProvider("兜底结果: 42"))
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(_make_envelope(text="帮我查一下数据"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.final_response and "兜底结果: 42" in result.final_response.text
        assert result.trace_summary["tool_steps"] == 1

    @pytest.mark.asyncio
    async def test_budget_truncates_steps(self):
        """预算 max_tool_steps=1：计划被截断为 1 步。"""
        reg = CapabilityRegistry()
        for i in range(3):
            _register(reg, "test.cap%d" % i, _EchoProvider("data%d" % i))
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(
            _make_envelope(text="帮我查一下数据"),
            budget=RuntimeBudget(max_tool_steps=1, deadline_seconds=30),
        )
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.trace_summary["tool_steps"] == 1

    @pytest.mark.asyncio
    async def test_global_hard_cap_eight(self):
        """全局硬上限 8：预算给 999 也不会超过 8 步。"""
        reg = CapabilityRegistry()
        for i in range(10):
            _register(reg, "test.cap%d" % i, _EchoProvider("data%d" % i))
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(
            _make_envelope(text="帮我查一下数据"),
            budget=RuntimeBudget(max_tool_steps=999, deadline_seconds=30),
        )
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.trace_summary["tool_steps"] <= 8


class TestValidationAndDegradation:
    @pytest.mark.asyncio
    async def test_partial_failure_degrades(self):
        """部分失败 -> DEGRADED，且保留成功数据。"""
        reg = CapabilityRegistry()
        _register(reg, "test.good", _EchoProvider("成功数据: alpha"))
        _register(reg, "test.bad", _FailProvider())
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(
            _make_envelope(text="帮我查一下数据"),
            budget=RuntimeBudget(max_tool_steps=2, deadline_seconds=30),
        )
        assert result.outcome == RunOutcome.DEGRADED
        assert result.final_response and "成功数据: alpha" in result.final_response.text

    @pytest.mark.asyncio
    async def test_all_fail_clarify_fallback_text(self):
        """全部失败 -> CLARIFY 归一化为 SUCCEEDED + 兜底文案。"""
        reg = CapabilityRegistry()
        _register(reg, "test.bad", _FailProvider())
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(_make_envelope(text="帮我查一下数据"))
        assert result.outcome == RunOutcome.SUCCEEDED
        assert result.final_response and "暂时没查到" in result.final_response.text

    @pytest.mark.asyncio
    async def test_schema_violation_fail_closed(self):
        """计划校验 fail closed：Schema 缺参 -> DEGRADED，Provider 不执行。"""
        reg = CapabilityRegistry()
        provider = _EchoProvider("should not run")
        _register(
            reg, "test.schema", provider,
            schema=CapabilitySchema(input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            }),
        )
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )
        result = await orch.run(_make_envelope(text="帮我查一下数据"))
        assert result.outcome == RunOutcome.DEGRADED
        assert provider.calls == 0


class TestDeliveryAckAndMemory:
    @pytest.mark.asyncio
    async def test_success_receipt_writes_bot_memory_idempotent(self):
        """投递成功 -> 写 bot 记忆；重复回执幂等（WriteGate 去重）。"""
        repo = InMemoryRepository()
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            memory_repo=repo,
        )
        result = await orch.run(_make_envelope(text="/help"))
        assert result.outcome == RunOutcome.SUCCEEDED
        receipt = DeliveryReceipt(run_id=result.run_id, status=DeliveryStatus.SUCCEEDED)

        first = await orch.acknowledge_delivery(receipt)
        assert first.memory_write_receipts, "投递成功后应写入 bot 记忆"
        bots = repo.query_selector(ScopeSelector(memory_type=MemoryType.BOT_UTTERANCE))
        assert any(r.source == "bot" for r in bots)
        assert all("[YmaKmern]" not in r.content for r in bots)

        second = await orch.acknowledge_delivery(receipt)
        # 幂等：重复回执返回同一完成回执，不重复写记忆（文档 2.3.15）
        assert second is first
        bots2 = repo.query_selector(ScopeSelector(memory_type=MemoryType.BOT_UTTERANCE))
        assert sum(1 for r in bots2 if r.source == "bot") == 1

    @pytest.mark.asyncio
    async def test_failed_receipt_skips_bot_memory(self):
        """投递失败 -> 不写 '已告知' 记忆；工具事实记忆仍写入。"""
        repo = InMemoryRepository()
        reg = CapabilityRegistry()
        _register(reg, "test.echo", _EchoProvider("工具事实: X"))
        orch = RuntimeOrchestrator(
            decision_engine=SocialDecisionEngine(keywords={"查"}),
            capability_registry=reg,
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            memory_repo=repo,
        )
        result = await orch.run(_make_envelope(text="帮我查一下数据"))
        assert result.outcome == RunOutcome.SUCCEEDED
        receipt = DeliveryReceipt(
            run_id=result.run_id, status=DeliveryStatus.FAILED,
            error_message="send timeout",
        )
        completion = await orch.acknowledge_delivery(receipt)
        assert completion.memory_write_receipts, "工具记忆不依赖投递"
        epis = repo.query_selector(ScopeSelector(memory_type=MemoryType.EPISODIC))
        bots = repo.query_selector(ScopeSelector(memory_type=MemoryType.BOT_UTTERANCE))
        assert epis
        assert not bots

    @pytest.mark.asyncio
    async def test_mismatched_receipt_rejected(self):
        """run_id 不匹配的回执被拒绝，不写任何记忆。"""
        repo = InMemoryRepository()
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            memory_repo=repo,
        )
        result = await orch.run(_make_envelope(text="/help"))
        assert result.outcome == RunOutcome.SUCCEEDED
        bogus = DeliveryReceipt(run_id="bogus-run", status=DeliveryStatus.SUCCEEDED)
        completion = await orch.acknowledge_delivery(bogus)
        assert completion.memory_write_receipts == ()
        assert repo.count() == 0
