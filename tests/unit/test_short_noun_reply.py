from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""短名词回复质量：USTC 这类短名词应被解释，不当问候/套话（P1-3 收尾）。"""
import sys, types
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_noun", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.application.dududa_utils import _is_greeting_text
from dududa.application.dududa_prod import _ProdOrchestrator
from dududa.core.state import SocialAction, RuntimeState
from dududa.core.decision import DecisionReason


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", bot="bot1",
                 session=None, at=True):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = session if session is not None else (group or f"private_{user}")
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id=bot)
        self._platform = "aiocqhttp"
        self._mtype = "group_message" if group else "private_message"
        self._components = []
        self.is_at_or_wake_command = at

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


def _plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    return main.Main(_make_context())


class TestGreetingDetection:
    def test_real_greetings(self):
        for t in ("你好", "您好", "嗨", "哈喽", "hello", "hi", "hey",
                  "在吗", "早上好", "晚安", "哈哈", "233", "哈", "😄😄"):
            assert _is_greeting_text(t), t

    def test_short_nouns_not_greeting(self):
        for t in ("USTC", "AI", "GPT", "数据结构", "今天天气不错", "this is a test"):
            assert not _is_greeting_text(t), t


class TestShortNounDecision:
    def test_ustc_direct_reply(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot USTC", group="g1", user="u1"))
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.DIRECT_MENTION.value

    def test_ai_not_react(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, _ = plugin._social_decision(
            _FakeEvent("@bot AI", group="g1", user="u1"))
        assert action == SocialAction.DIRECT_REPLY

    def test_greeting_still_react(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 你好", group="g1", user="u1"))
        assert action == SocialAction.REACT
        assert reason == DecisionReason.GREETING_ONLY.value

    def test_question_ending_me(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, _ = plugin._social_decision(
            _FakeEvent("@bot USTC是什么", group="g1", user="u1"))
        assert action == SocialAction.USE_TOOLS


class TestPerceptionNounQuery:
    def test_ustc_noun_query(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        pr = plugin._perceive(_FakeEvent("USTC", group="g1", user="u1"))
        acts = [a.act_type for a in pr.speech_acts]
        assert "noun_query" in acts
        assert "greeting" not in acts

    def test_greeting_perception(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        pr = plugin._perceive(_FakeEvent("你好", group="g1", user="u1"))
        acts = [a.act_type for a in pr.speech_acts]
        assert "greeting" in acts
        assert "noun_query" not in acts

    def test_question_perception(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        pr = plugin._perceive(_FakeEvent("USTC是什么", group="g1", user="u1"))
        acts = [a.act_type for a in pr.speech_acts]
        assert "question" in acts
        assert "noun_query" not in acts


class TestComposePrompt:
    def _orch(self, plugin):
        from unittest import mock
        plugin._call_llm = mock.AsyncMock(return_value="答复")
        plugin._read_memory = mock.Mock(return_value="")
        plugin.personas = mock.Mock()
        plugin.personas.active = types.SimpleNamespace(
            display_name="嘟嘟哒", first_person="我")
        return _ProdOrchestrator(
            plugin=plugin,
            decision_engine=mock.Mock(),
            capability_registry=mock.Mock(),
            memory_repo=mock.Mock(),
            renderer=mock.Mock(),
            planner_integration=None,
        )

    @pytest.mark.asyncio
    async def test_noun_query_gets_explain_extra(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        orch = self._orch(plugin)
        ev = _FakeEvent("USTC", group="g1", user="u1")
        orch._pending_event = ev
        state = RuntimeState(envelope=types.SimpleNamespace(text="USTC"),
                             perception=plugin._perceive(ev))
        reply = await orch._compose_prod_text(state)
        assert reply == "答复"
        system, user_msg = plugin._call_llm.call_args.args[:2]
        assert user_msg.strip() == "USTC"
        assert "直接解释" in system
        assert "不要当打招呼" in system

    @pytest.mark.asyncio
    async def test_question_no_noun_extra(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        orch = self._orch(plugin)
        ev = _FakeEvent("USTC是什么", group="g1", user="u1")
        orch._pending_event = ev
        state = RuntimeState(envelope=types.SimpleNamespace(text="USTC是什么"),
                             perception=plugin._perceive(ev))
        await orch._compose_prod_text(state)
        system, user_msg = plugin._call_llm.call_args.args[:2]
        assert "认真回答" in system
        assert "不要当打招呼" not in system

    @pytest.mark.asyncio
    async def test_greeting_no_noun_extra(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        orch = self._orch(plugin)
        ev = _FakeEvent("你好", group="g1", user="u1")
        orch._pending_event = ev
        state = RuntimeState(envelope=types.SimpleNamespace(text="你好"),
                             perception=plugin._perceive(ev))
        await orch._compose_prod_text(state)
        system, _ = plugin._call_llm.call_args.args[:2]
        assert "不要当打招呼" not in system