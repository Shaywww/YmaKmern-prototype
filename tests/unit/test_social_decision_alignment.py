from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""P0 第 3 项：Social Decision 六动作对齐 + 稳定 reason code + 冷却（文档 2.5.4）。"""
import os, sys, types, time
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_soc", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.core.state import SocialAction
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
    monkeypatch.setattr(main, "GROUP_POLICY_FILE", str(tmp_path / "group_policy.json"))
    return main.Main(_make_context())


class TestSocialDecisionAlignment:
    def test_private_always_direct_reply(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("在吗", group=None, user="u1"))
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.HIGH_RELEVANCE.value

    def test_group_not_mentioned_ignore(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("大家好", group="g1", user="u2", at=False))
        assert action == SocialAction.IGNORE
        assert reason == DecisionReason.LOW_RELEVANCE.value

    def test_group_question_direct_reply(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 今天天气怎么样？", group="g1", user="u1"))
        assert action == SocialAction.USE_TOOLS
        assert reason == DecisionReason.EXPLICIT_COMMAND.value

    def test_textual_greeting_direct_reply(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 哈", group="g1", user="u1"))
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.GREETING_ONLY.value

    def test_tool_intent_use_tools(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 帮我查一下数据结构课程", group="g1", user="u1"))
        assert action == SocialAction.USE_TOOLS
        assert reason == DecisionReason.EXPLICIT_COMMAND.value

    def test_react_cooldown_ignores_second_greeting(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("@bot 😄", group="g1", user="u1")
        action1, _ = plugin._social_decision(ev)
        assert action1 == SocialAction.REACT
        action2, reason2 = plugin._social_decision(ev)
        assert action2 == SocialAction.IGNORE
        assert reason2 == DecisionReason.COOLDOWN_ACTIVE.value

    def test_questions_not_affected_by_cooldown(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        ev = _FakeEvent("@bot 😄", group="g1", user="u1")
        plugin._social_decision(ev)
        q = _FakeEvent("@bot 明天考什么？", group="g1", user="u1")
        action, reason = plugin._social_decision(q)
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.DIRECT_MENTION.value

    def test_fallback_normal_on_error(self, monkeypatch, tmp_path):
        plugin = _plugin(monkeypatch, tmp_path)
        def boom(self_, event):
            raise RuntimeError("boom")
        monkeypatch.setattr(main.DududaCore, "_social_decision_impl", boom)
        action, reason = plugin._social_decision(_FakeEvent("x"))
        assert action == SocialAction.ANSWER
        assert reason == "normal"
