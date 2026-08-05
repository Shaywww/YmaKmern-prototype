# -*- coding: utf-8 -*-
"""P4: 生产 Orchestrator 接入 —— 工具链 + 投递回执 + 多 bot 记忆隔离。

覆盖：
- _ProdDecisionEngine：needs_tools -> USE_TOOLS，其余 -> ANSWER
- _ProdCapProvider：把 _call_llm/_call_vision 包装为 CapProvider
- _enrich_plan_args：口语化查询关键词注入（'帮我查一下数据结构课程' -> '数据结构'）
- _ProdOrchestrator：模式化工具执行、LLM 合成、生产记忆作用域、回执落盘
"""
import pathlib, sys, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_p4", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from packages.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from packages.core.perception import PerceptionResult
from packages.core.state import RuntimeBudget
from packages.core.memory import InMemoryRepository, MemoryType, MemoryScope
from packages.core.delivery import DeliveryReceipt, DeliveryStatus
from packages.core.renderer import Persona
from packages.core.capability import CapabilityRegistry
from packages.core.context import ContextBuilder
from packages.core.persona.registry import PersonaRegistry
from packages.mcp.registry import register_all_mcp_services
from packages.planner.integration import integrate_with_orchestrator


def _make_envelope(text="你好"):
    return MessageEnvelope(
        platform=Platform.QQ,
        kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="g1", platform=Platform.QQ, kind=MessageKind.GROUP,
        ),
        sender=Actor(actor_id="user_1", platform=Platform.QQ, display_name="小明"),
        text=text,
    )


class _FakeEvent:
    """生产事件替身：满足 input_adapter + _make_scope 所需接口。"""

    def __init__(self, text, group="g1", user="u1", bot="bot1"):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = group
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message"
        self._components = []

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


class _FakePlugin:
    """生产插件的最小替身：只实现 Orchestrator 用到的接口。"""

    def __init__(self, memory=None):
        self.memory = memory or InMemoryRepository()
        self.personas = PersonaRegistry()
        self.input_adapter = main.AstrBotInputAdapter(
            main.ActorMappingConfig(hash_user_ids=True))
        self.context_builder = ContextBuilder(
            memory_repo=self.memory, capability_registry=CapabilityRegistry())
        self.last_user_msg = ""
        self.llm_reply = "测试回复 (・ω・)"

    async def _call_llm(self, system, user_msg, max_tokens=1024, temperature=0.5):
        self.last_user_msg = user_msg
        return self.llm_reply

    def _read_memory(self, event, limit=8, budget=2500, include_episodic=False):
        return ""

    def _make_scope(self, event, msg_type="text"):
        mem_type = (MemoryType.EPISODIC if msg_type == "file"
                    else MemoryType.GROUP_MEMORY if event.message_obj.group
                    else MemoryType.SHORT_TERM)
        return MemoryScope(
            memory_type=mem_type, platform="qq",
            bot_id=event.get_self_id(),
            conversation_id=event.get_session_id(),
            actor_id=event.get_sender_id(),
        )


def _make_orchestrator(memory=None):
    memory = memory or InMemoryRepository()
    reg = CapabilityRegistry()
    register_all_mcp_services(reg)
    plugin = _FakePlugin(memory)
    plugin.context_builder = ContextBuilder(
        memory_repo=memory, capability_registry=reg)
    orch = main._ProdOrchestrator(
        plugin=plugin,
        decision_engine=main._ProdDecisionEngine(),
        capability_registry=reg,
        memory_repo=memory,
        renderer=main.OCRenderer(persona=Persona(
            persona_id="t", version="1.0", name="嘟嘟哒")),
        planner_integration=integrate_with_orchestrator(None, reg),
    )
    return orch, plugin, memory, reg


class TestProdDecisionEngine:
    def test_tools_topic_uses_tools(self):
        eng = main._ProdDecisionEngine()
        d = eng.decide(perception=PerceptionResult(
            needs_tools=True, topics=("course",)))
        assert d.action == main.SocialAction.USE_TOOLS
        assert d.should_use_tools

    def test_chat_answers(self):
        eng = main._ProdDecisionEngine()
        d = eng.decide(perception=PerceptionResult())
        assert d.action == main.SocialAction.ANSWER


class TestProdCapProvider:
    @pytest.mark.asyncio
    async def test_chat_provider_wraps_call_llm(self):
        plugin = _FakePlugin()
        provider = main._ProdCapProvider(plugin, "chat")
        cap = main.Capability(capability_id="chat", name="智能对话",
                              description="test", provider=main.ProviderType.BUILTIN)
        obs = await provider.execute(cap, {"text": "你好"})
        assert obs.success
        assert obs.data == plugin.llm_reply
        assert "你好" in plugin.last_user_msg

    @pytest.mark.asyncio
    async def test_vision_provider_wraps_call_vision(self):
        plugin = _FakePlugin()
        async def fake_vision(system, text, b64, mime):
            return "图片描述 (・ω・)"
        plugin._call_vision = fake_vision
        provider = main._ProdCapProvider(plugin, "vision")
        cap = main.Capability(capability_id="vision", name="图片识别",
                              description="test", provider=main.ProviderType.BUILTIN)
        obs = await provider.execute(cap, {"image_b64": "AAA", "mime": "image/png"})
        assert obs.success
        assert obs.data == "图片描述 (・ω・)"

    def test_health_ok(self):
        provider = main._ProdCapProvider(_FakePlugin(), "chat")
        assert provider.health() is True


class TestEnrichPlanArgs:
    def test_strips_verbs_and_suffixes(self):
        from packages.planner.planner import GeneratedPlan, PlannedStep
        plan = GeneratedPlan(goal="g", steps=(PlannedStep(
            step_id="s1", capability_id="mcp.course_schedule",
            arguments={"action": "search"}, purpose="p"),))
        out = main._ProdOrchestrator._enrich_plan_args(plan, "帮我查一下数据结构课程")
        assert out.steps[0].arguments["keyword"] == "数据结构"

    def test_keeps_existing_keyword(self):
        from packages.planner.planner import GeneratedPlan, PlannedStep
        plan = GeneratedPlan(goal="g", steps=(PlannedStep(
            step_id="s1", capability_id="mcp.course_schedule",
            arguments={"action": "search", "keyword": "操作系统"}, purpose="p"),))
        out = main._ProdOrchestrator._enrich_plan_args(plan, "帮我查一下课程")
        assert out.steps[0].arguments["keyword"] == "操作系统"


class TestProdOrchestrator:
    @pytest.mark.asyncio
    async def test_plain_chat_no_tools_composes_via_llm(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("今天心情不错")
        result = await orch.run(
            _make_envelope("今天心情不错"),
            budget=RuntimeBudget(deadline_seconds=20),
            perception=PerceptionResult(needs_tools=False),
            event=event,
        )
        assert result.outcome == main.RunOutcome.SUCCEEDED
        assert result.final_response and result.final_response.text == plugin.llm_reply
        assert orch._last_state.tool_observations == ()

    @pytest.mark.asyncio
    async def test_tool_query_runs_mcp_and_injects_data(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("帮我查一下数据结构课程")
        result = await orch.run(
            _make_envelope("帮我查一下数据结构课程"),
            budget=RuntimeBudget(max_tool_steps=4, deadline_seconds=20),
            perception=PerceptionResult(
                needs_tools=True, topics=("course",),
                candidate_intents=("course_query",),
                speech_acts=(main.SpeechAct("command", 0.9),)),
            event=event,
        )
        assert result.final_response and result.final_response.text == plugin.llm_reply
        assert "[工具 mcp.course_schedule]" in plugin.last_user_msg
        assert "数据结构" in plugin.last_user_msg
        assert len(orch._last_state.tool_observations) >= 1
        obs = orch._last_state.tool_observations[0]
        assert obs.success and obs.data

    @pytest.mark.asyncio
    async def test_no_pattern_falls_back_to_plain_chat(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("今天天气怎么样")
        result = await orch.run(
            _make_envelope("今天天气怎么样"),
            budget=RuntimeBudget(deadline_seconds=20),
            perception=PerceptionResult(needs_tools=True, topics=("weather",)),
            event=event,
        )
        # 无 Planner 模式命中 -> 不执行工具，走纯 LLM 对话
        assert orch._last_state.tool_observations == ()
        assert result.final_response.text == plugin.llm_reply

    @pytest.mark.asyncio
    async def test_ack_writes_memory_with_production_scope(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("你好")
        result = await orch.run(
            _make_envelope("你好"),
            budget=RuntimeBudget(deadline_seconds=20),
            perception=PerceptionResult(needs_tools=False),
            event=event,
        )
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.SUCCEEDED)
        comp = await orch.acknowledge_delivery(receipt)
        assert comp.memory_write_receipts, "投递成功后应写入 bot 记忆"
        scope = plugin._make_scope(event)
        records = memory.query(scope, limit=10)
        assert any("[嘟嘟哒]" in r.content for r in records)
        assert all(r.scope.bot_id == "bot1" for r in records)

    @pytest.mark.asyncio
    async def test_failed_receipt_skips_bot_memory(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("你好")
        result = await orch.run(
            _make_envelope("你好"),
            budget=RuntimeBudget(deadline_seconds=20),
            perception=PerceptionResult(needs_tools=False),
            event=event,
        )
        receipt = DeliveryReceipt(
            run_id=result.run_id, status=DeliveryStatus.FAILED,
            error_message="send timeout")
        comp = await orch.acknowledge_delivery(receipt)
        scope = plugin._make_scope(event)
        records = memory.query(scope, limit=10)
        assert not any("[嘟嘟哒]" in r.content for r in records)

    @pytest.mark.asyncio
    async def test_tool_memory_episodic_and_bot_scoped(self):
        orch, plugin, memory, reg = _make_orchestrator()
        event = _FakeEvent("帮我查一下数据结构课程")
        result = await orch.run(
            _make_envelope("帮我查一下数据结构课程"),
            budget=RuntimeBudget(max_tool_steps=4, deadline_seconds=20),
            perception=PerceptionResult(needs_tools=True, topics=("course",)),
            event=event,
        )
        receipt = DeliveryReceipt(run_id=result.run_id,
                                  status=DeliveryStatus.SUCCEEDED)
        await orch.acknowledge_delivery(receipt)
        epis = memory.query(plugin._make_scope(event, msg_type="file"), limit=10)
        assert any(r.scope.bot_id == "bot1" and r.source == "tool" for r in epis)
