from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""P0 用户 style 四维隔离（文档 2.5.8）：platform + Bot + user + Persona。

覆盖五层：
1) extract_style_signals 规则提取（称呼/语气/长度/表情，确定性无模型）；
2) UserStyleStore 四维键隔离 / 具名 selector 跨会话 / 非 engaged 不写 / 持久化 fail-closed；
3) ContextBuilder 把 UserStyle.summary_lines() 投影到 user_preference.style；
4) RuntimeOrchestrator _record_style 在 engaged 时学习（与画像同语义）；
5) 生产插件装配 style_store + dududa_style 命令。
"""
import sys
import types

sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_style", str(PLUGIN_MAIN))
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest

from dududa.core.style_store import (
    StyleSignals, UserStyle, UserStyleStore, extract_style_signals,
)
from dududa.core.context import ContextBuilder
from dududa.core.envelope import (
    MessageEnvelope, Actor, ConversationRef, MessageKind, Platform,
)
from dududa.core.state import RuntimeState, RuntimeBudget
from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
from dududa.core.memory import InMemoryRepository
from dududa.runtime.orchestrator import RuntimeOrchestrator
from dududa.application import dududa_commands


def _env(text="", conv="c1", actor="u1", mentions=()):
    return MessageEnvelope(
        platform=Platform.QQ, kind=MessageKind.GROUP,
        conversation=ConversationRef(
            conversation_id=conv, platform=Platform.QQ, kind=MessageKind.GROUP),
        sender=Actor(actor_id=actor, platform=Platform.QQ, display_name="t"),
        text=text, mentions=mentions,
    )


def _state(text="", conv="c1", actor="u1", mentions=()):
    return RuntimeState(
        envelope=_env(text, conv, actor, mentions), budget=RuntimeBudget())


# ---- 1. 规则提取 ----

class TestExtractSignals:
    def test_address(self):
        sig = extract_style_signals("以后叫我小明吧")
        assert sig.address == "小明"
        sig = extract_style_signals("你可以叫我 Dudu 同学")
        assert sig.address == "Dudu"
        sig = extract_style_signals("请叫我小美")
        assert sig.address == "小美"
        # 裸「叫我一下」不是风格式请求，不应提取
        sig = extract_style_signals("叫我一下")
        assert sig.address == ""

    def test_tone(self):
        sig = extract_style_signals("以后叫我小刚，说话正式一点")
        assert sig.address == "小刚"
        assert sig.tone == "formal"
        sig = extract_style_signals("回复随意点就行")
        assert sig.tone == "casual"
        sig = extract_style_signals("对我温柔点")
        assert sig.tone == "gentle"

    def test_length(self):
        assert extract_style_signals("回复简短点").length == "short"
        assert extract_style_signals("说重点").length == "short"
        assert extract_style_signals("详细点说").length == "detailed"
        assert extract_style_signals("展开说").length == "detailed"

    def test_emoji(self):
        assert extract_style_signals("别用表情").emoji == "off"
        assert extract_style_signals("别用颜文字").emoji == "off"
        assert extract_style_signals("多用表情").emoji == "on"
        assert extract_style_signals("卖萌一点").emoji == "on"

    def test_no_signals(self):
        sig = extract_style_signals("今天天气不错，帮我查一下课")
        assert sig.empty
        assert extract_style_signals("").empty


# ---- 2. UserStyleStore ----

class TestUserStyleStore:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "styles.json")
        store = UserStyleStore(path=path)
        store.record_message(
            "qq", "bot1", "c1", "u1", "dududa_default",
            "以后叫我小明，回复简短点，别用表情", engaged=True)
        again = UserStyleStore(path=path)
        style = again.get("qq", "bot1", "u1", "dududa_default")
        assert style is not None
        assert style.address == "小明"
        assert style.length == "short"
        assert style.emoji == "off"
        assert style.origin_conversation == "c1"
        assert style.visibility == "public"

    def test_four_dim_isolation(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明", engaged=True)
        store.record_message("qq", "bot2", "c1", "u1", "dududa_default",
                             "以后叫我小红", engaged=True)
        store.record_message("qq", "bot1", "c1", "u1", "dududa_serious",
                             "以后叫我小刚", engaged=True)
        store.record_message("qq", "bot1", "c1", "u2", "dududa_default",
                             "以后叫我小美", engaged=True)
        assert store.get("qq", "bot1", "u1", "dududa_default").address == "小明"
        assert store.get("qq", "bot2", "u1", "dududa_default").address == "小红"
        assert store.get("qq", "bot1", "u1", "dududa_serious").address == "小刚"
        assert store.get("qq", "bot1", "u2", "dududa_default").address == "小美"
        assert store.get("qq", "bot1", "u1", "dududa_mentor") is None
        assert store.get("qq", "bot9", "u1", "dududa_default") is None

    def test_named_selector_cross_conversation(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明", engaged=True)
        # 跨会话读取：具名 selector 不依赖来源会话，也不删除来源
        style = store.get("qq", "bot1", "u1", "dududa_default")
        assert style is not None
        assert style.origin_conversation == "c1"
        assert any(s.user_id == "u1" for s in store.list_for_persona(
            "qq", "bot1", "dududa_default"))
        assert store.list_for_persona("qq", "bot1", "dududa_serious") == ()

    def test_non_engaged_not_written(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明", engaged=False)
        assert store.get("qq", "bot1", "u1", "dududa_default") is None

    def test_no_signals_not_written(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "今天天气不错", engaged=True)
        assert store.get("qq", "bot1", "u1", "dududa_default") is None

    def test_unknown_user_not_written(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "unknown", "dududa_default",
                             "以后叫我小明", engaged=True)
        assert store.get("qq", "bot1", "unknown", "dududa_default") is None

    def test_merge_semantics(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明", engaged=True)
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "回复简短点", engaged=True)
        style = store.get("qq", "bot1", "u1", "dududa_default")
        assert style.address == "小明"
        assert style.length == "short"

    def test_field_cap(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我" + "长" * 40, engaged=True)
        style = store.get("qq", "bot1", "u1", "dududa_default")
        assert len(style.address) <= 32

    def test_visibility_private(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明", engaged=True, visibility="private")
        style = store.get("qq", "bot1", "u1", "dududa_default")
        assert style.visibility == "private"

    def test_corrupt_fail_closed(self, tmp_path):
        path = tmp_path / "styles.json"
        path.write_text("{not json", encoding="utf-8")
        store = UserStyleStore(path=str(path))
        assert store.status()["styles"] == 0
        assert list(tmp_path.glob("styles.json.corrupt-*"))


# ---- 3. ContextBuilder 投影 ----

class TestContextBuilder:
    def test_style_projected(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "dududa", "c1", "u1", "dududa_default",
                             "以后叫我小明，回复简短点", engaged=True)
        builder = ContextBuilder(
            memory_repo=InMemoryRepository(), style_store=store)
        snap = builder.build(_env(conv="c1", actor="u1"), persona_id="dududa_default")
        assert snap.user_preference is not None
        assert "称呼「小明」" in snap.user_preference.style
        assert "回复简短" in snap.user_preference.style

    def test_style_merges_with_profile(self, tmp_path):
        from dududa.core.profile import ProfileStore
        style_store = UserStyleStore(path=str(tmp_path / "s.json"))
        style_store.record_message("qq", "dududa", "c1", "u1", "dududa_default",
                                   "以后叫我小明，回复简短点", engaged=True)
        profile_store = ProfileStore(path=str(tmp_path / "p.json"))
        profile_store.record_message("qq", "dududa", "c1", "u1",
                                     "我喜欢数据结构", intents=("course_query",),
                                     engaged=True)
        builder = ContextBuilder(
            memory_repo=InMemoryRepository(), profile_store=profile_store,
            style_store=style_store)
        snap = builder.build(_env(conv="c1", actor="u1"), persona_id="dududa_default")
        assert snap.user_preference is not None
        assert "数据结构" in snap.user_preference.preferences
        assert "回复简短" in snap.user_preference.style

    def test_no_store_keeps_legacy(self):
        builder = ContextBuilder(memory_repo=InMemoryRepository())
        snap = builder.build(_env(conv="c1", actor="u1"))
        assert snap.user_preference is None


# ---- 4. Orchestrator 学习（engaged 同画像语义） ----

class TestOrchestratorWiring:
    @pytest.mark.asyncio
    async def test_engaged_learns_style(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            style_store=store)
        await orch.run(_state(
            "以后叫我小明，回复简短点", conv="c1", actor="u1",
            mentions=("bot",)))
        style = store.get("qq", "dududa", "u1", "dududa_default")
        assert style is not None
        assert style.address == "小明"
        assert style.length == "short"
        assert style.origin_conversation == "c1"

    @pytest.mark.asyncio
    async def test_non_engaged_not_learned(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            style_store=store)
        await orch.run(_state("以后叫我小明", conv="c1", actor="u1"))
        assert store.get("qq", "dududa", "u1", "dududa_default") is None

    @pytest.mark.asyncio
    async def test_reply_chain_learns(self, tmp_path):
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            style_store=store)
        env = _env("以后叫我小刚", conv="c1", actor="u1",
                     mentions=())
        env = MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(
                conversation_id="c1", platform=Platform.QQ, kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="t"),
            text="以后叫我小刚",
            reply_to=_env(conv="c1", actor="bot"),
        )
        state = RuntimeState(envelope=env, budget=RuntimeBudget())
        await orch.run(state)
        style = store.get("qq", "dududa", "u1", "dududa_default")
        assert style is not None
        assert style.address == "小刚"


# ---- 5. 生产装配 / _style_lines / 命令 ----

def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1"):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = group or f"private_{user}"
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(group=group, self_id="bot1")
        self.is_at_or_wake_command = True

    def get_platform_name(self): return "aiocqhttp"
    def get_message_type(self): return "group_message" if self.group_id else "private_message"
    def get_sender_id(self): return str(self.sender.user_id)
    def get_self_id(self): return "bot1"


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


class TestProdStyle:
    def test_style_lines_for_llm(self, tmp_path):
        from dududa.application.dududa_prod import _ProdOrchestrator
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        store.record_message("qq", "bot1", "c1", "u1", "dududa_default",
                             "以后叫我小明，回复简短点", engaged=True)
        fake_plugin = types.SimpleNamespace(
            personas=types.SimpleNamespace(active_id="dududa_default"))
        fake_plugin._get_bot_id = lambda event: "bot1"
        orch = object.__new__(_ProdOrchestrator)
        orch._style_store = store
        orch._plugin = fake_plugin
        orch._pending_event = None
        lines = orch._style_lines(_state(conv="c1", actor="u1"))
        assert any("称呼「小明」" in line for line in lines)
        assert any("回复简短" in line for line in lines)

    def test_no_store_no_lines(self, tmp_path):
        from dududa.application.dududa_prod import _ProdOrchestrator
        orch = object.__new__(_ProdOrchestrator)
        orch._style_store = None
        assert orch._style_lines(_state(conv="c1", actor="u1")) == ()

    def test_prod_record_style_persona_bot(self, tmp_path):
        from dududa.application.dududa_prod import _ProdOrchestrator
        store = UserStyleStore(path=str(tmp_path / "s.json"))
        fake_plugin = types.SimpleNamespace(
            personas=types.SimpleNamespace(active_id="dududa_serious"))
        orch = object.__new__(_ProdOrchestrator)
        orch._style_store = store
        orch._plugin = fake_plugin
        orch._pending_event = None
        from dududa.core.perception import PerceptionResult
        perception = PerceptionResult(
            has_explicit_mention=True, is_explicit_command=True)
        orch._record_style(_state("以后叫我小刚", conv="c1", actor="u1"),
                           perception, persona_id="dududa_serious",
                           bot_id="bot1")
        style = store.get("qq", "bot1", "u1", "dududa_serious")
        assert style is not None
        assert style.address == "小刚"
        assert store.get("qq", "bot1", "u1", "dududa_default") is None


class TestProdWiring:
    def test_style_store_wired(self, plugin):
        from dududa.core.style_store import UserStyleStore
        assert isinstance(plugin.style_store, UserStyleStore)
        assert plugin.runtime._style_store is plugin.style_store
        assert plugin.context_builder._style_store is plugin.style_store

    def test_style_store_path_tmp(self, plugin, tmp_path):
        status = plugin.style_store.status()
        assert status["path"] == str(tmp_path / "styles.json")

    @pytest.mark.asyncio
    async def test_cmd_style_empty(self, plugin):
        reply = await dududa_commands.cmd_style_impl(
            plugin, _FakeEvent("x", group="g1", user="u1"))
        assert "还没有记录" in reply

    @pytest.mark.asyncio
    async def test_cmd_style_shows_pref(self, plugin):
        plugin.style_store.record_message(
            "qq", "bot1", "c1", "u1", "dududa_default",
            "以后叫我小明，回复简短点", engaged=True)
        reply = await dududa_commands.cmd_style_impl(
            plugin, _FakeEvent("x", group="g1", user="u1"))
        assert "小明" in reply
        assert "简短" in reply
