# -*- coding: utf-8 -*-
"""P0 群策略（文档 2.5.2 / 2.5.4）：mode / reply_rate / meme_rate 落地到回复策略。

覆盖三层：
1) GroupPolicyStore 持久化 / 校验 / 投影；
2) SocialDecisionEngine 读取 context.policy（reply_rate / meme_rate / mode）；
3) 生产插件 _social_decision_impl 应用群策略 + 管理命令接线。
"""
import sys, types
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_gp", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.core.state import SocialAction
from dududa.core.decision import SocialDecisionEngine, DecisionReason
from dududa.core.context import PolicyView
from dududa.core.perception import PerceptionResult, SpeechAct
from dududa.core.group_policy import GroupPolicyStore, GroupPolicy, GROUP_MODES
from dududa.application import dududa_commands


# ---- 引擎层 context 桩 ----

class _PolicyContext:
    """带 policy 的 context（引擎步骤 5/8 兼容：conversation=None）。"""

    def __init__(self, policy):
        self.policy = policy
        self.current_message = None
        self.conversation = None


class _LegacyConv:
    conversation_id = "conv1"


class _LegacyMsg:
    conversation = _LegacyConv()


class _LegacyContext:
    """旧测试桩：没有 policy 属性（必须不报 AttributeError）。"""

    conversation = _LegacyConv()
    current_message = _LegacyMsg()


# ---- 生产插件桩（与 test_social_decision_alignment 同款） ----

def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _FakeEvent:
    def __init__(self, text, group="g1", user="u1", at=True):
        self.message_str = text
        self.message_id = "m1"
        self.session_id = group or f"private_{user}"
        self.group_id = group
        self.sender = types.SimpleNamespace(user_id=user, nickname="小明")
        self.message_obj = types.SimpleNamespace(
            group=group, message_id="m1",
            sender=types.SimpleNamespace(user_id=user), self_id="bot1")
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


class _OwnerActor:
    role = "owner"

    def is_muted(self):
        return False


class _NormalActor:
    role = "normal"

    def is_muted(self):
        return False


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(main, "GROUP_POLICY_FILE",
                        str(tmp_path / "group_policy.json"))
    p = main.Main(_make_context())
    # 每个用例独立冷却，避免问候用例互相干扰
    p._core._react_cooldown.clear()
    return p


# ---- 1. GroupPolicyStore ----

class TestGroupPolicyStore:
    def test_unconfigured_returns_none(self, tmp_path):
        store = GroupPolicyStore(str(tmp_path / "gp.json"))
        assert store.get("g1") is None
        assert store.all() == {}

    def test_set_get_roundtrip(self, tmp_path):
        store = GroupPolicyStore(str(tmp_path / "gp.json"))
        p = store.set("g1", mode="silent", reply_rate=0.3, meme_rate=0.8)
        assert isinstance(p, GroupPolicy)
        assert p.mode == "silent"
        assert p.reply_rate == 0.3
        assert p.meme_rate == 0.8
        got = store.get("g1")
        assert got.mode == "silent"
        assert got.reply_rate == 0.3
        assert got.meme_rate == 0.8

    def test_persist_across_reload(self, tmp_path):
        path = str(tmp_path / "gp.json")
        GroupPolicyStore(path).set("g1", mode="off", reply_rate=0.5)
        again = GroupPolicyStore(path)
        got = again.get("g1")
        assert got is not None
        assert got.mode == "off"
        assert got.reply_rate == 0.5

    def test_rate_clamped(self, tmp_path):
        store = GroupPolicyStore(str(tmp_path / "gp.json"))
        p = store.set("g1", reply_rate=1.5, meme_rate=-0.2)
        assert p.reply_rate == 1.0
        assert p.meme_rate == 0.0

    def test_invalid_mode_rejected(self, tmp_path):
        store = GroupPolicyStore(str(tmp_path / "gp.json"))
        with pytest.raises(ValueError):
            store.set("g1", mode="loud")

    def test_corrupt_file_recovers_empty(self, tmp_path):
        path = str(tmp_path / "gp.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        store = GroupPolicyStore(path)
        assert store.all() == {}

    def test_to_policy_view(self, tmp_path):
        store = GroupPolicyStore(str(tmp_path / "gp.json"))
        default = store.to_policy_view("unset")
        assert default.reply_rate == 1.0 and default.meme_rate == 1.0
        assert default.mode == "normal"
        store.set("g1", mode="silent", reply_rate=0.2, meme_rate=0.7)
        pv = store.to_policy_view("g1")
        assert pv.mode == "silent"
        assert pv.reply_rate == 0.2
        assert pv.meme_rate == 0.7


# ---- 2. SocialDecisionEngine 读 policy ----

class TestEnginePolicy:
    def test_reply_rate_zero_ignores(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult(),
                          context=_PolicyContext(PolicyView(reply_rate=0.0)))
        assert d.action == SocialAction.IGNORE
        assert DecisionReason.LOW_RELEVANCE in d.reason_codes

    def test_reply_rate_one_question_direct_reply(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        pr = PerceptionResult(
            speech_acts=(SpeechAct(act_type="question", confidence=0.9),))
        d = engine.decide(perception=pr,
                          context=_PolicyContext(PolicyView(reply_rate=1.0)))
        assert d.action == SocialAction.DIRECT_REPLY
        assert DecisionReason.HIGH_RELEVANCE in d.reason_codes

    def test_meme_rate_zero_blocks_react(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult(),
                          context=_PolicyContext(PolicyView(meme_rate=0.0)))
        assert d.action == SocialAction.IGNORE

    def test_meme_rate_one_reacts(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult(),
                          context=_PolicyContext(PolicyView(meme_rate=1.0)))
        assert d.action == SocialAction.REACT
        assert DecisionReason.HIGH_RELEVANCE in d.reason_codes

    def test_mode_off_silences_even_mention(self):
        engine = SocialDecisionEngine()
        pr = PerceptionResult(has_explicit_mention=True)
        d = engine.decide(perception=pr,
                          context=_PolicyContext(PolicyView(mode="off")))
        assert d.action == SocialAction.IGNORE
        assert DecisionReason.GROUP_MODE_OFF in d.reason_codes

    def test_mode_silent_suppresses_keyword(self):
        engine = SocialDecisionEngine(keywords={"天气"})
        pr = PerceptionResult(resolved_references={"text": "今天天气怎么样"})
        d = engine.decide(perception=pr,
                          context=_PolicyContext(PolicyView(mode="silent")))
        assert d.action == SocialAction.IGNORE
        assert DecisionReason.GROUP_MODE_SILENT in d.reason_codes

    def test_mode_silent_keeps_mention(self):
        engine = SocialDecisionEngine()
        pr = PerceptionResult(has_explicit_mention=True)
        d = engine.decide(perception=pr,
                          context=_PolicyContext(PolicyView(mode="silent")))
        assert d.action == SocialAction.DIRECT_REPLY

    def test_no_policy_legacy_context_ok(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult(),
                          context=_LegacyContext())
        assert d.action == SocialAction.REACT

    def test_no_policy_no_context_unchanged(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult())
        assert d.action == SocialAction.REACT


# ---- 3. 生产插件 _social_decision_impl 应用群策略 ----

class TestProdGroupPolicy:
    def test_unconfigured_preserves_behavior(self, plugin):
        # 未配置：@ 必回、未 @ 忽略（现状不变）
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 今天天气怎么样？", group="g1"))
        assert action == SocialAction.USE_TOOLS
        action, reason = plugin._social_decision(
            _FakeEvent("大家好", group="g1", at=False))
        assert action == SocialAction.IGNORE
        assert reason == DecisionReason.LOW_RELEVANCE.value

    def test_mode_off_silences_even_at(self, plugin):
        plugin.group_policy.set("g1", mode="off")
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 你好", group="g1"))
        assert action == SocialAction.IGNORE
        assert reason == DecisionReason.GROUP_MODE_OFF.value

    def test_mode_silent_no_passive_reply(self, plugin):
        plugin.group_policy.set("g1", mode="silent", reply_rate=1.0)
        action, reason = plugin._social_decision(
            _FakeEvent("大家好", group="g1", at=False))
        assert action == SocialAction.IGNORE
        assert reason == DecisionReason.LOW_RELEVANCE.value

    def test_normal_reply_rate_one_participates(self, plugin):
        plugin.group_policy.set("g1", mode="normal", reply_rate=1.0)
        action, reason = plugin._social_decision(
            _FakeEvent("大家好", group="g1", at=False))
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.HIGH_RELEVANCE.value

    def test_normal_reply_rate_zero_ignores(self, plugin):
        plugin.group_policy.set("g1", mode="normal", reply_rate=0.0)
        action, reason = plugin._social_decision(
            _FakeEvent("大家好", group="g1", at=False))
        assert action == SocialAction.IGNORE

    def test_meme_rate_zero_falls_back_to_text(self, plugin):
        plugin.group_policy.set("g1", mode="normal", meme_rate=0.0)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 哈", group="g1"))
        assert action == SocialAction.DIRECT_REPLY
        assert reason == DecisionReason.GREETING_ONLY.value

    def test_meme_rate_one_reacts(self, plugin):
        plugin.group_policy.set("g1", mode="normal", meme_rate=1.0)
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 哈", group="g1"))
        assert action == SocialAction.REACT
        assert reason == DecisionReason.GREETING_ONLY.value

    def test_private_ignores_group_policy(self, plugin):
        plugin.group_policy.set("g1", mode="off")
        action, reason = plugin._social_decision(
            _FakeEvent("在吗", group=None, user="u1"))
        assert action == SocialAction.DIRECT_REPLY

    def test_group_policy_view_projection(self, plugin):
        assert plugin._group_policy_view(_FakeEvent("x", group=None)) is None
        plugin.group_policy.set("g1", mode="silent", reply_rate=0.4, meme_rate=0.8)
        pv = plugin._group_policy_view(_FakeEvent("x", group="g1"))
        assert pv.mode == "silent"
        assert pv.reply_rate == 0.4
        assert pv.meme_rate == 0.8


# ---- 4. 管理命令 ----

class TestGroupPolicyCommands:
    @pytest.mark.asyncio
    async def test_cmd_group_show_unset(self, plugin):
        reply = await dududa_commands.cmd_group_impl(
            plugin, _FakeEvent("x", group="g1"), None)
        assert "未设置" in reply

    @pytest.mark.asyncio
    async def test_cmd_mode_owner_sets(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _OwnerActor())
        reply = await dududa_commands.cmd_group_mode_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "off")
        assert "已设置" in reply
        assert plugin.group_policy.get("g1").mode == "off"

    @pytest.mark.asyncio
    async def test_cmd_mode_denied_for_normal(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _NormalActor())
        reply = await dududa_commands.cmd_group_mode_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "off")
        assert "权限不足" in reply
        assert plugin.group_policy.get("g1") is None

    @pytest.mark.asyncio
    async def test_cmd_mode_invalid(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _OwnerActor())
        reply = await dududa_commands.cmd_group_mode_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "loud")
        assert "无效" in reply

    @pytest.mark.asyncio
    async def test_cmd_reply_rate(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _OwnerActor())
        reply = await dududa_commands.cmd_group_reply_rate_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "0.5")
        assert "reply_rate 已设置: 0.5" in reply
        assert plugin.group_policy.get("g1").reply_rate == 0.5

    @pytest.mark.asyncio
    async def test_cmd_meme_rate(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _OwnerActor())
        reply = await dududa_commands.cmd_group_meme_rate_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "0.2")
        assert "meme_rate 已设置: 0.2" in reply
        assert plugin.group_policy.get("g1").meme_rate == 0.2

    @pytest.mark.asyncio
    async def test_cmd_set_then_decision_applies(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin._core, "_actor_for",
                            lambda event: _OwnerActor())
        await dududa_commands.cmd_group_mode_impl(
            plugin, _FakeEvent("x", group="g1"), "g1", "off")
        action, reason = plugin._social_decision(
            _FakeEvent("@bot 你好", group="g1"))
        assert action == SocialAction.IGNORE
        assert reason == DecisionReason.GROUP_MODE_OFF.value
