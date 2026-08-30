"""用户画像（SESSION_STATE / USER_PROFILE）建模（文档 2.4.6 / 2.5.3）。"""
import json
import time

import pytest

from dududa.core.profile import (
    ProfileStore, UserProfile, SessionState, detect_emotional_tone,
    extract_profile_signals,
)
from dududa.core.context import ContextBuilder, ContextSnapshot
from dududa.core.envelope import (
    MessageEnvelope, Actor, ConversationRef, MessageKind, Platform,
)
from dududa.core.state import RuntimeState, RuntimeBudget
from dududa.core.delivery import DeliveryManager, NoOpOutputAdapter
from dududa.core.memory import InMemoryRepository
from dududa.runtime.orchestrator import RuntimeOrchestrator


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


class TestExtractSignals:
    def test_preferred_name(self):
        name, prefs, facts = extract_profile_signals("以后叫我小明吧")
        assert name == "小明"
        name, _, _ = extract_profile_signals("你可以叫我 Dudu 同学")
        assert name == "Dudu"
        name, _, _ = extract_profile_signals("你随便叫我就行")
        assert name == ""

    def test_preferences(self):
        _, prefs, _ = extract_profile_signals("我喜欢数据结构，偏爱早八的课")
        assert "数据结构" in prefs
        assert "早八的课" in prefs

    def test_facts(self):
        _, _, facts = extract_profile_signals("我是USTC的学生，我住在东区")
        assert any("USTC" in f for f in facts)
        assert not any("东区" in f for f in facts)  # 位置已结构化进 location

    def test_no_signals(self):
        assert extract_profile_signals("今天天气不错，帮我查一下课") == ("", (), ())

    def test_self_claim_name(self):
        name, _, _ = extract_profile_signals("我叫小明")
        assert name == "小明"
        name, _, _ = extract_profile_signals("我的名字叫阿伟")
        assert name == "阿伟"
        name, _, _ = extract_profile_signals("我是小明的同学")
        assert name == ""  # 陈述句不当作名字

    def test_preference_noise_filtered(self):
        _, prefs, _ = extract_profile_signals("我喜欢你，也喜欢数据结构")
        assert "你" not in prefs
        assert "数据结构" in prefs

    def test_location(self):
        from dududa.core.profile import extract_location
        assert extract_location("我住在东区") == "东区"
        assert "临泽县" in extract_location("我家在甘肃临泽县")
        assert extract_location("我现在在临泽县，29号回兰州") == "临泽县"
        assert extract_location("我目前在甘肃临泽县").endswith("临泽县")
        assert extract_location("我现在在学习高等数学") == ""
        assert extract_location("我是甘肃人") == "甘肃"
        assert extract_location("今天天气怎么样") == ""

    def test_emotion_signal_is_conservative(self):
        assert detect_emotional_tone("今天真的好烦，快崩溃了") == "negative"
        assert detect_emotional_tone("好耶，终于成功了") == "positive"
        assert detect_emotional_tone("这个方案不太好") == ""


class TestProfileStore:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "profiles.json")
        store = ProfileStore(path=path)
        store.record_message("qq", "dududa", "c1", "u1",
                             "叫我小明，我喜欢数据结构",
                             intents=("course_query",), engaged=True)
        store2 = ProfileStore(path=path)
        user = store2.get_user("qq", "dududa", "u1")
        assert user is not None
        assert user.preferred_name == "小明"
        assert "数据结构" in user.preferences
        assert user.topic_counts.get("course_query", 0) >= 1
        assert user.interaction_count == 1
        sess = store2.get_session("c1", "u1")
        assert sess is not None
        assert sess.message_count == 1
        assert sess.last_intent == "course_query"

    def test_user_isolation(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        store.record_message("qq", "dududa", "c1", "u1", "叫我A", engaged=True)
        store.record_message("qq", "dududa", "c1", "u2", "叫我B", engaged=True)
        assert store.get_user("qq", "dududa", "u1").preferred_name == "A"
        assert store.get_user("qq", "dududa", "u2").preferred_name == "B"
        assert store.get_user("qq", "dududa", "u9") is None

    def test_session_tracks_counts_and_topics(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        store.record_message("qq", "dududa", "c1", "u1", "a",
                             intents=("course_query",), engaged=False)
        store.record_message("qq", "dududa", "c1", "u1", "b",
                             intents=("time_query",), engaged=False)
        store.record_message("qq", "dududa", "c2", "u1", "c",
                             intents=("course_query",), engaged=False)
        sess = store.get_session("c1", "u1")
        assert sess.message_count == 2
        assert sess.last_intent == "time_query"
        assert sess.active_topics[0] == "time_query"  # 最新在前
        assert store.get_session("c2", "u1").message_count == 1
        assert store.get_session("c1", "u2") is None

    def test_non_engaged_no_profile(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        store.record_message("qq", "dududa", "c1", "u1",
                             "叫我小明", engaged=False)
        assert store.get_user("qq", "dududa", "u1") is None
        assert store.get_session("c1", "u1").message_count == 1

    def test_corrupt_file_fail_closed(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("{not json", encoding="utf-8")
        store = ProfileStore(path=str(path))
        assert store.status()["users"] == 0
        assert store.status()["sessions"] == 0
        assert list(tmp_path.glob("p.json.corrupt-*"))

    def test_merge_caps_preferences(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        for i in range(20):
            store.record_message("qq", "dududa", "c1", "u1",
                                 f"我喜欢话题{i}", engaged=True)
        user = store.get_user("qq", "dududa", "u1")
        assert len(user.preferences) <= 12

    def test_emotion_continuity_decays_and_persists(self, tmp_path):
        path = str(tmp_path / "p.json")
        store = ProfileStore(path=path)
        store.record_message("qq", "dududa", "c1", "u1",
                             "今天真的好烦", engaged=True)
        session = store.get_session("c1", "u1")
        assert session.emotional_tone == "negative"
        assert session.emotion_turns_remaining == 3
        store.record_message("qq", "dududa", "c1", "u1",
                             "然后呢", engaged=True)
        assert store.get_session("c1", "u1").emotion_turns_remaining == 2
        loaded = ProfileStore(path=path)
        assert loaded.get_session("c1", "u1").emotional_tone == "negative"
        assert loaded.get_user("qq", "dududa", "u1").interaction_count == 2


class TestContextBuilder:
    def test_user_preference_and_topics_projected(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        store.record_message("qq", "dududa", "c1", "u1",
                             "叫我小明，我喜欢数据结构",
                             intents=("course_query",), engaged=True)
        builder = ContextBuilder(
            memory_repo=InMemoryRepository(), profile_store=store)
        snap = builder.build(_env(conv="c1", actor="u1"))
        assert snap.user_preference is not None
        assert snap.user_preference.preferred_name == "小明"
        assert "数据结构" in snap.user_preference.preferences
        assert "course_query" in snap.conversation.active_topics

    def test_no_profile_store_keeps_legacy(self):
        builder = ContextBuilder(memory_repo=InMemoryRepository())
        snap = builder.build(_env(conv="c1", actor="u1"))
        assert snap.user_preference is None


class TestOrchestratorWiring:
    @pytest.mark.asyncio
    async def test_engaged_message_learns_profile(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            profile_store=store)
        await orch.run(_state(
            "叫我小明，我喜欢数据结构，帮我查一下课程",
            conv="c1", actor="u1", mentions=("bot",)))
        user = store.get_user("qq", "dududa", "u1")
        assert user is not None
        assert user.preferred_name == "小明"
        assert "数据结构" in user.preferences
        assert store.get_session("c1", "u1").message_count == 1

    @pytest.mark.asyncio
    async def test_non_engaged_updates_session_only(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            profile_store=store)
        await orch.run(_state("随便聊聊", conv="c1", actor="u1"))
        assert store.get_session("c1", "u1").message_count == 1
        assert store.get_user("qq", "dududa", "u1") is None

    @pytest.mark.asyncio
    async def test_multiple_messages_accumulate(self, tmp_path):
        store = ProfileStore(path=str(tmp_path / "p.json"))
        orch = RuntimeOrchestrator(
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
            profile_store=store)
        for text in ("帮我查一下课程", "现在几点", "明天考什么"):
            await orch.run(_state(text, conv="c1", actor="u1",
                                  mentions=("bot",)))
        sess = store.get_session("c1", "u1")
        assert sess.message_count == 3
        assert sess.active_topics[0] == "course_query"


class TestProdProfileLines:
    def test_profile_lines_for_llm(self, tmp_path):
        from dududa.application.dududa_prod import _ProdOrchestrator
        store = ProfileStore(path=str(tmp_path / "p.json"))
        store.record_message("qq", "dududa", "c1", "u1",
                             "叫我小明，我喜欢数据结构，我是USTC的学生",
                             intents=("course_query",), engaged=True)
        orch = object.__new__(_ProdOrchestrator)
        orch._profile_store = store
        lines = orch._profile_lines(_state(conv="c1", actor="u1"))
        assert any("小明" in line for line in lines)
        assert any("数据结构" in line for line in lines)
        assert any("USTC" in line for line in lines)
        assert any("最近话题" in line for line in lines)

    def test_no_profile_no_lines(self, tmp_path):
        from dududa.application.dududa_prod import _ProdOrchestrator
        orch = object.__new__(_ProdOrchestrator)
        orch._profile_store = ProfileStore(path=str(tmp_path / "none.json"))
        assert orch._profile_lines(_state(conv="c1", actor="u1")) == ()

    def test_dynamic_persona_uses_familiarity_time_and_emotion(self, tmp_path):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from dududa.application.dududa_prod import _ProdOrchestrator
        store = ProfileStore(path=str(tmp_path / "p.json"))
        for text in ("今天真的好烦", "还是有点难受", "然后呢"):
            store.record_message(
                "qq", "dududa", "c1", "u1", text, engaged=True)
        orch = object.__new__(_ProdOrchestrator)
        orch._profile_store = store
        orch._plugin = None
        orch._pending_event = None
        late = datetime(
            2026, 8, 30, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        lines = orch._dynamic_persona_lines(
            _state(conv="c1", actor="u1"), now=late)
        assert any("聊过几次" in line for line in lines)
        assert any("深夜低能量" in line for line in lines)
        assert any("低落或烦躁" in line for line in lines)
