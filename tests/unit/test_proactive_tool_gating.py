# -*- coding: utf-8 -*-
"""P0 修复回归：工具意图门（needs_tools 对齐 _TOOL_KW）、LLM 规划失败规则兜底、
tool_result 失败错误详情、搜索相关性重排。"""
import pathlib, sys, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_ptg", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest

from dududa.core.capability import (
    Capability, CapabilityRegistry, CapabilityRisk, ProviderType, CapProvider,
)
from dududa.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from dududa.core.memory import InMemoryRepository
from dududa.core.persona.registry import PersonaRegistry
from dududa.core.renderer import Persona
from dududa.core.context import ContextBuilder
from dududa.core.state import RuntimeBudget, RuntimeState
from dududa.mcp.registry import register_all_mcp_services
from dududa.planner.integration import integrate_with_orchestrator
from dududa.planner.planner import PlannedStep
from dududa.planner.executor import ToolExecutor, ExecutionContext
from dududa.mcp.web_search_service import _rank_results
from dududa.core.trace_recorder import trace_recorder


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
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
        self.is_at_or_wake_command = True

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


class _FakePlugin:
    def __init__(self, memory=None):
        self.memory = memory or InMemoryRepository()
        self.personas = PersonaRegistry()
        self.input_adapter = main.AstrBotInputAdapter(
            main.ActorMappingConfig(hash_user_ids=True))
        self.context_builder = ContextBuilder(
            memory_repo=self.memory, capability_registry=CapabilityRegistry())
        self.llm_reply = "测试回复 (・ω・)"

    async def _call_llm(self, system, user_msg, max_tokens=1024, temperature=0.5,
                        run_id="", trace_id="", skip_render=False):
        return self.llm_reply

    def _read_memory(self, event, limit=8, budget=2500, include_episodic=False):
        return ""

    def _make_scope(self, event, msg_type="text"):
        from dududa.core.memory import MemoryType, MemoryScope
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
    import dududa.runtime.orchestrator as _orch_mod
    from dududa.mcp.access import MCPAccessPolicy
    _orch_mod.mcp_access = MCPAccessPolicy(
        config_path="/nonexistent/mcp_access_unittest_ptg.json")
    plugin = _FakePlugin(memory)
    orch = main._ProdOrchestrator(
        plugin=plugin,
        decision_engine=main._ProdDecisionEngine(),
        capability_registry=reg,
        memory_repo=memory,
        renderer=main.OCRenderer(persona=Persona(
            persona_id="t", version="1.0", name="嘟嘟哒")),
        planner_integration=integrate_with_orchestrator(None, reg),
    )
    return orch, plugin, reg


def _state(orch, text="test"):
    env = MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id="g1", platform=Platform.QQ,
            kind=MessageKind.GROUP),
        sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="小明"),
        text=text,
    )
    return RuntimeState(envelope=env, budget=RuntimeBudget(),
                        run_id="ptg-run-1", trace_id="ptg-tr-1")


def _plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(main, "GROUP_POLICY_FILE", str(tmp_path / "group_policy.json"))
    return main.Main(_make_context())


class TestToolIntentGate:
    """needs_tools 与 _TOOL_KW 对齐：查/招生/天气/新闻 等意图进入工具链。"""

    def _perceive(self, monkeypatch, tmp_path, text):
        return _plugin(monkeypatch, tmp_path)._perceive(
            _FakeEvent(text, group="g1"))

    def test_chacha_ustc(self, monkeypatch, tmp_path):
        p = self._perceive(monkeypatch, tmp_path, "帮我查一下USTC")
        assert p.needs_tools is True
        assert p.is_explicit_command is True

    def test_zhaosheng(self, monkeypatch, tmp_path):
        p = self._perceive(monkeypatch, tmp_path, "USTC今年招生怎么样")
        assert p.needs_tools is True

    def test_weather_with_city(self, monkeypatch, tmp_path):
        p = self._perceive(monkeypatch, tmp_path, "临泽县今天天气怎么样")
        assert p.needs_tools is True

    def test_news(self, monkeypatch, tmp_path):
        p = self._perceive(monkeypatch, tmp_path, "有什么新闻")
        assert p.needs_tools is True

    def test_greeting_no_tools(self, monkeypatch, tmp_path):
        p = self._perceive(monkeypatch, tmp_path, "你好呀")
        assert p.needs_tools is False


class TestRuleFallbackPlan:
    def _cands(self, reg):
        return reg.filter_candidates(permissions=(), max_count=24)

    def test_weather_city_extracted(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "临泽县今天天气怎么样")
        assert plan is not None
        step = plan.steps[0]
        assert step.capability_id == "mcp.weather"
        assert step.arguments.get("q") == "临泽县"

    def test_generic_chacha_to_web_search(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "帮我查一下USTC")
        assert plan is not None
        assert plan.steps[0].capability_id == "mcp.web_search"
        assert "USTC" in plan.steps[0].arguments.get("q", "")

    def test_zhaosheng_to_web_search(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "USTC今年招生怎么样")
        assert plan is not None
        assert plan.steps[0].capability_id == "mcp.web_search"

    def test_news_fallback(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "科技方面有什么新闻")
        assert plan is not None
        assert plan.steps[0].capability_id == "mcp.news"

    def test_time_fallback(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "现在几点")
        assert plan is not None
        assert plan.steps[0].capability_id == "mcp.clock"

    def test_self_intro_not_searched(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "介绍一下你自己")
        assert plan is None

    def test_plain_chat_no_fallback(self):
        orch, _plugin, reg = _make_orchestrator()
        plan = orch._rule_fallback_plan(_state(orch), self._cands(reg),
                                        "你好呀")
        assert plan is None


class TestLLMPlanFallback:
    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rules(self):
        orch, plugin, reg = _make_orchestrator()

        async def boom(system, user_msg, **kw):
            raise RuntimeError("provider_unavailable")

        plugin._call_llm = boom
        state = _state(orch, "临泽县今天天气怎么样")
        plan = await orch._llm_plan(state, self._cands(reg), 4, ())
        assert plan is not None
        assert plan.steps[0].capability_id == "mcp.weather"
        assert plan.rationale.startswith("RuleFallback")

    @pytest.mark.asyncio
    async def test_llm_empty_plan_kept(self):
        orch, plugin, reg = _make_orchestrator()
        plugin.llm_reply = '{"steps":[]}'
        plan = await orch._llm_plan(_state(orch), self._cands(reg), 4, ())
        assert plan is not None
        assert len(plan.steps) == 0  # 模型明确不需要工具 -> 不兜底

    def _cands(self, reg):
        return reg.filter_candidates(permissions=(), max_count=24)


class TestWeatherCityGuard:
    """LLM 规划填的天气城市必须来自消息文本，否则默认合肥（防猜城市）。"""

    def _plan(self, intent, args):
        orch, _plugin, reg = _make_orchestrator()
        from dududa.planner.planner import GeneratedPlan, PlannedStep
        plan = GeneratedPlan(
            goal=intent,
            steps=(PlannedStep(step_id="s1", capability_id="mcp.weather",
                               arguments=args, purpose="p"),))
        cands = reg.filter_candidates(permissions=(), max_count=24)
        out = orch._ensure_step_args(plan, intent, cands)
        return out.steps[0].arguments

    def test_llm_guessed_city_overridden_to_default(self):
        args = self._plan("今天天气怎么样", {"city": "长庆镇", "action": "search"})
        assert args.get("q") == "合肥"
        assert "city" not in args

    def test_llm_mentioned_city_kept(self):
        args = self._plan("临泽县今天天气怎么样", {"city": "北京", "action": "search"})
        assert args.get("q") == "临泽县"

    def test_llm_english_city_kept_when_mentioned(self):
        args = self._plan("Beijing weather", {"city": "Beijing", "action": "search"})
        assert args.get("q") == "Beijing"

    def test_no_city_gets_default(self):
        args = self._plan("今天天气怎么样", {"action": "search"})
        assert args.get("q") == "合肥"

    def test_empty_web_search_query_is_filled_from_intent(self):
        orch, _plugin, reg = _make_orchestrator()
        from dududa.planner.planner import GeneratedPlan, PlannedStep
        plan = GeneratedPlan(
            goal="hotel",
            steps=(PlannedStep(
                step_id="s1", capability_id="mcp.web_search",
                arguments={"action": "search", "q": ""}, purpose="p"),))
        out = orch._ensure_step_args(
            plan, "兰州盛达希尔顿酒店怎么样",
            reg.filter_candidates(permissions=(), max_count=24))
        assert out.steps[0].arguments["q"] == "兰州盛达希尔顿酒店怎么样"

    def test_profile_location_is_default_city(self):
        from dududa.core.profile import ProfileStore
        import tempfile
        store = ProfileStore(path=tempfile.mktemp(suffix=".json"))
        store.record_message("qq", "dududa", "g1", "u1",
                             "我住在临泽县", engaged=True)
        orch, _plugin, reg = _make_orchestrator()
        orch._profile_store = store
        cands = reg.filter_candidates(permissions=(), max_count=24)
        plan = orch._rule_fallback_plan(_state(orch, "今天天气怎么样"),
                                        cands, "今天天气怎么样")
        assert plan is not None
        assert plan.steps[0].arguments.get("q") == "临泽县"
        args = self._plan("今天天气怎么样", {"action": "search"})
        assert args.get("q") == "合肥"  # 无画像时仍默认合肥

    @pytest.mark.asyncio
    async def test_llm_plan_includes_recent_context(self):
        orch, plugin, reg = _make_orchestrator()
        seen = {}

        async def capture(system, user_msg, **kw):
            seen["user"] = user_msg
            return '{"steps":[]}'

        plugin._call_llm = capture
        plugin._read_memory = lambda *a, **k: (
            "【近期对话】\n[用户]: USTC今年招生怎么样\n======\n")
        orch._pending_event = _FakeEvent("本科", group="g1")
        cands = reg.filter_candidates(permissions=(), max_count=24)
        await orch._llm_plan(_state(orch, "本科"), cands, 4, ())
        assert "最近对话" in seen["user"]
        assert "USTC今年招生怎么样" in seen["user"]


class _BoomProvider(CapProvider):
    async def execute(self, cap, args):
        raise TimeoutError("connection timed out")

    def health(self):
        return True


class TestToolFailureDetail:
    @pytest.mark.asyncio
    async def test_tool_result_records_error_kind(self):
        reg = CapabilityRegistry()
        reg.register(Capability(
            capability_id="flaky.tool", name="f", description="f",
            provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY,
            idempotent=False), _BoomProvider())
        ex = ToolExecutor(reg)
        ctx = ExecutionContext(max_steps=2, max_retries_per_step=0,
                               run_id="ptg-err-run-1", trace_id="ptg-err-tr-1")
        from dududa.planner.planner import GeneratedPlan
        plan_steps = (PlannedStep(step_id="s1", capability_id="flaky.tool",
                                  arguments={}, purpose="p"),)
        results = await ex.execute_plan(GeneratedPlan(goal="g",
                                                      steps=plan_steps), ctx)
        assert results and not results[0].success
        recs = [x for x in trace_recorder.lines_for()
                if x.get("run_id") == "ptg-err-run-1"
                and x.get("event") == "tool_result"]
        assert recs
        last = recs[-1]
        assert last["success"] is False
        assert last["error_kind"] == "timeout"
        assert "timed out" in last["error"]


class TestSearchRanking:
    def test_video_deprioritized_without_video_intent(self):
        results = [
            {"title": "USTC 官网", "link": "https://www.ustc.edu.cn/",
             "snippet": "中国科学技术大学"},
            {"title": "腾讯视频 USTC", "link": "https://v.qq.com/x/page/x.html",
             "snippet": "USTC 相关视频"},
            {"title": "USTC - Wikipedia", "link": "https://en.wikipedia.org/wiki/USTC",
             "snippet": "University of Science and Technology of China"},
        ]
        ranked = _rank_results(results, "USTC")
        links = [r["link"] for r in ranked]
        assert links[0] == "https://www.ustc.edu.cn/"
        assert "https://v.qq.com/x/page/x.html" not in links[:2]

    def test_video_kept_when_video_intent(self):
        results = [
            {"title": "USTC 宣传视频", "link": "https://v.qq.com/x/page/y.html",
             "snippet": "USTC 官方宣传视频"},
            {"title": "USTC 官网", "link": "https://www.ustc.edu.cn/",
             "snippet": "中国科学技术大学"},
        ]
        ranked = _rank_results(results, "USTC 宣传视频")
        links = [r["link"] for r in ranked]
        assert "https://v.qq.com/x/page/y.html" in links

    def test_dedupe_by_domain(self):
        results = [
            {"title": "A", "link": "https://www.ustc.edu.cn/a", "snippet": "x"},
            {"title": "B", "link": "https://www.ustc.edu.cn/b", "snippet": "y"},
            {"title": "C", "link": "https://other.com/c", "snippet": "z"},
        ]
        ranked = _rank_results(results, "USTC")
        assert len(ranked) == 2
