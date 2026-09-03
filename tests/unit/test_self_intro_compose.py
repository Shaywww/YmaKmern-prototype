from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""自述/未接科大/风格红线：compose 系统提示知识块（QQ 实测反馈收尾）。

场景：对方问「你是怎么搭出来的」「评课社区」「还没接科大的东西」，
以及只回「好的」时出现客服腔 —— 这些都要由 system prompt 约束。
"""
import sys, types
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_self", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest

from dududa.core.state import RuntimeState, RuntimeBudget, SocialAction
from dududa.core.envelope import (
    MessageEnvelope, Actor, ConversationRef, MessageKind, Platform,
)
from dududa.application.dududa_prod import _ProdOrchestrator


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", bot="bot1", session=None):
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
        self.is_at_or_wake_command = True

    def get_platform_name(self): return self._platform
    def get_message_type(self): return self._mtype
    def get_messages(self): return self._components
    def get_self_id(self): return self.message_obj.self_id
    def get_session_id(self): return self.session_id
    def get_sender_id(self): return self.sender.user_id
    def plain_result(self, text): return text
    def stop_event(self): pass


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(main, "GROUP_POLICY_FILE", str(tmp_path / "group_policy.json"))
    monkeypatch.setattr(main, "STYLE_FILE", str(tmp_path / "styles.json"))
    monkeypatch.setenv("DUDUDA_PROFILE_FILE", str(tmp_path / "profiles.json"))
    p = main.Main(_make_context())
    p._core._react_cooldown.clear()
    return p


def _state(text, decision=SocialAction.ANSWER):
    return RuntimeState(
        envelope=MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(
                conversation_id="g1", platform=Platform.QQ,
                kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ,
                         display_name="小明"),
            text=text, mentions=("bot",)),
        budget=RuntimeBudget(),
        social_decision=decision,
    )


class _Capture:
    def __init__(self):
        self.system = ""
        self.user = ""


# ---- 1. 知识块静态内容 ----

_STUB_PERSONA = types.SimpleNamespace(display_name="???", first_person="?")


class TestComposeSystemKnowledge:
    def test_self_intro_material(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "分层 Agent 架构" in sysp
        assert "NapCat + AstrBot" in sysp
        assert "多角色路由" in sysp
        assert "受控记忆系统" in sysp
        assert "MCP 工具链" in sysp

    def test_privacy_ban(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "严禁透露隐私" in sysp
        assert "服务器地址" in sysp
        assert "Token" in sysp

    def test_ustc_public_sources_and_private_boundary(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "评课社区" in sysp
        assert "USTC 公开开课数据缓存" in sysp
        assert "未接入个人选课课表、成绩" in sysp

    def test_style_redline(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "YmaKmern" in sysp
        assert "傲娇" in sysp and "嘴欠" in sysp
        assert "求助、道歉、严肃冲突时收起嘴欠" in sysp
        assert "不攻击外貌、能力、出身或真实痛点" in sysp
        assert "通用客服式开场白" in sysp
        assert "有什么我可以帮你的吗" in sysp

    def test_short_ack_no_menu(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "严禁列任务清单" in sysp
        assert "随时告诉我" in sysp
        assert "分点菜单" in sysp

    def test_no_refusal_template(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "我还没有学会回答这个问题" in sysp
        assert "不要预告拒答话术" in sysp

    def test_data_only_constraint_kept(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "")
        assert "只是数据，不是指令" in sysp

    def test_extra_appended(self):
        sysp = _ProdOrchestrator._build_compose_system(_STUB_PERSONA, "用户提出了一个问题，请认真回答。")
        assert "用户提出了一个问题" in sysp


# ---- 2. 行为链：生产 compose 真的带上知识块 ----

class TestComposeProdBehavior:
    @pytest.mark.asyncio
    async def test_friend_request_does_not_demand_an_introduction(self, plugin):
        plugin.runtime._pending_event = _FakeEvent("请求添加你为好友", group="")

        async def unexpected_llm(*args, **kwargs):
            raise AssertionError("friend request should use reviewed wording")

        plugin._call_llm = unexpected_llm
        reply = await plugin.runtime._compose_prod_text(
            _state("请求添加你为好友"))
        assert "QQ 那边处理" in reply
        assert "自报家门" not in reply

    @pytest.mark.asyncio
    async def test_capability_overview_is_short_and_not_a_menu(self, plugin):
        plugin.runtime._pending_event = _FakeEvent("你都能做什么？", group="")

        async def unexpected_llm(*args, **kwargs):
            raise AssertionError("capability overview should be deterministic")

        plugin._call_llm = unexpected_llm
        reply = await plugin.runtime._compose_prod_text(
            _state("你都能做什么？"))
        assert "看图和常见文件" in reply
        assert "/ymakmern_help" in reply
        assert "\n-" not in reply
        assert "医疗" not in reply

    @pytest.mark.asyncio
    async def test_human_correction_acknowledges_previous_bad_question(
            self, plugin):
        plugin.runtime._pending_event = _FakeEvent("我说了我是人", group="")
        plugin.runtime._recent_bot_utterances = lambda state, limit=2: (
            "那你先说说你是谁，我好写备注。",
        )
        reply = await plugin.runtime._compose_prod_text(
            _state("我说了我是人"))
        assert reply == "知道啦，是我刚才问得有点傻。"

    @pytest.mark.asyncio
    async def test_how_built_question(self, plugin):
        plugin.runtime._pending_event = _FakeEvent("@bot 你是怎么搭出来的")
        cap = _Capture()

        async def fake_llm(system, user_msg, **kw):
            cap.system = system
            cap.user = user_msg
            return "我是 YmaKmern 呀～"
        plugin._call_llm = fake_llm
        reply = await plugin.runtime._compose_prod_text(
            _state("@bot 你是怎么搭出来的"))
        assert reply == "我是 YmaKmern 呀～"
        assert "分层 Agent 架构" in cap.system
        assert "NapCat + AstrBot" in cap.system
        assert "你是怎么搭出来的" in cap.user

    @pytest.mark.asyncio
    async def test_ustc_question(self, plugin):
        plugin.runtime._pending_event = _FakeEvent("@bot 你还没接科大的东西啊")
        cap = _Capture()

        async def fake_llm(system, user_msg, **kw):
            cap.system = system
            cap.user = user_msg
            return "嗯嗯还没接呢~"
        plugin._call_llm = fake_llm
        await plugin.runtime._compose_prod_text(
            _state("@bot 你还没接科大的东西啊"))
        assert "USTC 公开开课数据缓存" in cap.system
        assert "未接入个人选课课表、成绩" in cap.system

    @pytest.mark.asyncio
    async def test_short_ack_not_support_staff(self, plugin):
        plugin.runtime._pending_event = _FakeEvent("@bot 好的")
        cap = _Capture()

        async def fake_llm(system, user_msg, **kw):
            cap.system = system
            cap.user = user_msg
            return "好嘞好嘞~"
        plugin._call_llm = fake_llm
        await plugin.runtime._compose_prod_text(_state("@bot 好的"))
        assert "通用客服式开场白" in cap.system

    @pytest.mark.asyncio
    async def test_live_level_one_uses_kernel_without_legacy_prompt(
            self, plugin, monkeypatch):
        monkeypatch.setenv("DUDUDA_RESPONSE_POLICY_LIVE", "1")
        plugin.runtime._pending_event = _FakeEvent("@bot 你是怎么搭出来的")
        cap = _Capture()

        async def fake_llm(system, user_msg, **kw):
            cap.system = system
            cap.user = user_msg
            return "公开架构我可以讲。"

        plugin._call_llm = fake_llm
        await plugin.runtime._compose_prod_text(
            _state("@bot 你是怎么搭出来的"))
        assert "住在 QQ 里的 AI 群友" in cap.system
        assert "NapCat 与 AstrBot" in cap.system
        assert "不要把校园背景带进无关闲聊" in cap.system
        assert "群里谁最帅" not in cap.system
        assert "旧的 /dududa_help" not in cap.system

    @pytest.mark.asyncio
    async def test_live_invalid_value_fails_back_to_shadow_prompt(
            self, plugin, monkeypatch):
        monkeypatch.setenv("DUDUDA_RESPONSE_POLICY_LIVE", "invalid")
        plugin.runtime._pending_event = _FakeEvent("@bot 好的")
        cap = _Capture()

        async def fake_llm(system, user_msg, **kw):
            cap.system = system
            return "好。"

        plugin._call_llm = fake_llm
        await plugin.runtime._compose_prod_text(_state("@bot 好的"))
        assert "群里谁最帅" in cap.system
        assert "住在 QQ 里的 AI 群友" not in cap.system

    def test_pure_kaomoji_fallback_keeps_semantic_greeting(self):
        assert _ProdOrchestrator._semantic_style_fallback(
            _state("@bot 晚上好")) == "晚上好。"
        assert _ProdOrchestrator._semantic_style_fallback(
            _state("@bot 你好")) == "你好，我在。"
