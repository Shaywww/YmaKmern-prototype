# -*- coding: utf-8 -*-
"""P0 Structured Output：Validator / 规则-模型 Merger / 安全降级（文档 2.5.4）。

- 模型感知/决策信号整体过 Schema 才有效，任一字段非法 -> 整包丢弃；
- 规则结果优先（平台事实 @/回复链/命令 永远以规则为准）；
- 模型缺失、非法或置信度不足 -> 只用规则（模型失败时减少主动回复）。
"""
import sys, types
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_so", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.core.state import SocialAction
from dududa.core.decision import SocialDecision, SocialDecisionEngine, DecisionReason
from dududa.core.perception import PerceptionResult, SpeechAct, EntityRef
from dududa.core.structured_output import (
    StructuredOutputValidator, PerceptionMerger,
    merge_perception_with_model, decision_from_signal,
)
from dududa.application import dududa_handlers


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


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(main, "GROUP_POLICY_FILE",
                        str(tmp_path / "group_policy.json"))
    p = main.Main(_make_context())
    p._core._react_cooldown.clear()
    return p


_VALID_PERCEPTION = {
    "confidence": 0.8,
    "speech_acts": [{"act_type": "question", "confidence": 0.9}],
    "topics": ["course"],
    "entities": [{"name": "数据结构", "entity_type": "course",
                  "confidence": 0.7, "evidence": "原文出现"}],
    "candidate_intents": ["course_query"],
    "suggested_capabilities": ["mcp.course_schedule"],
    "needs_tools": True,
    "ambiguities": [],
}


# ---- 1. StructuredOutputValidator：Perception ----

class TestPerceptionValidator:
    def test_valid_dict(self):
        sig = StructuredOutputValidator.validate_perception_signal(
            _VALID_PERCEPTION)
        assert sig is not None
        assert sig["confidence"] == 0.8
        assert sig["speech_acts"] == [
            {"act_type": "question", "confidence": 0.9}]
        assert sig["topics"] == ["course"]
        assert sig["entities"][0]["name"] == "数据结构"
        assert sig["needs_tools"] is True

    def test_valid_json_string(self):
        import json
        sig = StructuredOutputValidator.validate_perception_signal(
            json.dumps(_VALID_PERCEPTION, ensure_ascii=False))
        assert sig is not None

    def test_confidence_out_of_range(self):
        bad = dict(_VALID_PERCEPTION, confidence=1.5)
        assert StructuredOutputValidator.validate_perception_signal(bad) is None
        bad2 = dict(_VALID_PERCEPTION, confidence="x")
        assert StructuredOutputValidator.validate_perception_signal(bad2) is None

    def test_unknown_act_type_rejected(self):
        bad = dict(_VALID_PERCEPTION)
        bad["speech_acts"] = [{"act_type": "hypnosis", "confidence": 0.9}]
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_acts_not_list(self):
        bad = dict(_VALID_PERCEPTION, speech_acts="question")
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_act_not_dict(self):
        bad = dict(_VALID_PERCEPTION)
        bad["speech_acts"] = ["question"]
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_entity_missing_name(self):
        bad = dict(_VALID_PERCEPTION)
        bad["entities"] = [{"entity_type": "course", "confidence": 0.7}]
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_needs_tools_not_bool(self):
        bad = dict(_VALID_PERCEPTION, needs_tools="yes")
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_topics_non_str_item(self):
        bad = dict(_VALID_PERCEPTION, topics=["course", 42])
        assert StructuredOutputValidator.validate_perception_signal(bad) is None

    def test_garbage_and_non_dict(self):
        assert StructuredOutputValidator.validate_perception_signal(
            "not json {{{") is None
        assert StructuredOutputValidator.validate_perception_signal(
            ["list"]) is None
        assert StructuredOutputValidator.validate_perception_signal(None) is None


# ---- 2. StructuredOutputValidator：Decision ----

class TestDecisionValidator:
    def test_valid(self):
        sig = StructuredOutputValidator.validate_decision_signal({
            "action": "direct_reply",
            "reason_codes": ["direct_mention"],
            "confidence": 0.9,
            "should_use_tools": False,
        })
        assert sig is not None
        assert sig["action"] == SocialAction.DIRECT_REPLY
        assert sig["reason_codes"] == (DecisionReason.DIRECT_MENTION,)

    def test_alias_answer(self):
        sig = StructuredOutputValidator.validate_decision_signal(
            {"action": "answer", "reason_codes": ["high_relevance"]})
        assert sig is not None
        assert sig["action"] == SocialAction.DIRECT_REPLY

    def test_unknown_action(self):
        assert StructuredOutputValidator.validate_decision_signal(
            {"action": "explode"}) is None

    def test_unknown_reason(self):
        assert StructuredOutputValidator.validate_decision_signal(
            {"action": "ignore", "reason_codes": ["made_up_reason"]}) is None

    def test_reason_not_str(self):
        assert StructuredOutputValidator.validate_decision_signal(
            {"action": "ignore", "reason_codes": [42]}) is None

    def test_bad_confidence(self):
        assert StructuredOutputValidator.validate_decision_signal(
            {"action": "ignore", "confidence": 7}) is None

    def test_use_tools_not_bool(self):
        assert StructuredOutputValidator.validate_decision_signal(
            {"action": "use_tools", "should_use_tools": "yes"}) is None


# ---- 3. PerceptionMerger + 安全降级 ----

class TestPerceptionMerger:
    def _rule(self):
        return PerceptionResult(
            speech_acts=(SpeechAct(act_type="greeting", confidence=0.5),),
            topics=(),
            confidence=0.5,
            has_explicit_mention=True,
            is_explicit_command=False,
        )

    def test_no_signal_keeps_rule(self):
        rule = self._rule()
        assert PerceptionMerger().merge(rule, None) is rule

    def test_low_confidence_degrades_to_rule(self):
        rule = self._rule()
        low = dict(_VALID_PERCEPTION, confidence=0.3)
        merged = PerceptionMerger().merge(rule, low)
        assert merged == rule

    def test_merge_adds_model_signals(self):
        rule = self._rule()
        sig = StructuredOutputValidator.validate_perception_signal(
            _VALID_PERCEPTION)
        merged = PerceptionMerger().merge(rule, sig)
        acts = {a.act_type for a in merged.speech_acts}
        assert acts == {"greeting", "question"}   # 规则优先 + 模型补充
        assert merged.topics == ("course",)
        assert merged.candidate_intents == ("course_query",)
        assert merged.needs_tools is True
        assert merged.has_explicit_mention is True    # 平台事实保留
        assert merged.is_explicit_command is False
        assert merged.confidence == 0.8

    def test_rule_act_wins_conflict(self):
        rule = PerceptionResult(
            speech_acts=(SpeechAct(act_type="question", confidence=0.8),),
            confidence=0.5)
        sig = StructuredOutputValidator.validate_perception_signal(
            {"confidence": 0.9,
             "speech_acts": [{"act_type": "question", "confidence": 0.2}],
             "topics": [], "entities": [], "candidate_intents": [],
             "suggested_capabilities": [], "needs_tools": False,
             "ambiguities": []})
        merged = PerceptionMerger().merge(rule, sig)
        assert len(merged.speech_acts) == 1
        assert merged.speech_acts[0].confidence == 0.8  # 规则版本保留

    def test_entity_dedupe(self):
        rule = PerceptionResult(
            entities=(EntityRef(name="数据结构", entity_type="course",
                                confidence=0.9, evidence="rule"),))
        sig = StructuredOutputValidator.validate_perception_signal(
            {"confidence": 0.8,
             "speech_acts": [], "topics": [],
             "entities": [{"name": "数据结构", "entity_type": "course",
                           "confidence": 0.6, "evidence": "model"}],
             "candidate_intents": [], "suggested_capabilities": [],
             "needs_tools": False, "ambiguities": []})
        merged = PerceptionMerger().merge(rule, sig)
        assert len(merged.entities) == 1
        assert merged.entities[0].confidence == 0.9

    def test_invalid_raw_degrades(self):
        rule = self._rule()
        merged, used = merge_perception_with_model(rule, "{{{bad")
        assert merged == rule
        assert used is False

    def test_valid_raw_used(self):
        rule = self._rule()
        merged, used = merge_perception_with_model(rule, _VALID_PERCEPTION)
        assert used is True
        assert merged.topics == ("course",)


# ---- 4. decision_from_signal ----

class TestDecisionFromSignal:
    def test_valid(self):
        d = decision_from_signal({
            "action": "direct_reply", "reason_codes": ["direct_mention"],
            "confidence": 0.9})
        assert d is not None
        assert d.action == SocialAction.DIRECT_REPLY
        assert DecisionReason.DIRECT_MENTION in d.reason_codes

    def test_invalid_none(self):
        assert decision_from_signal({"action": "explode"}) is None
        assert decision_from_signal("garbage") is None

    def test_low_confidence_none(self):
        assert decision_from_signal(
            {"action": "ignore", "reason_codes": ["low_relevance"],
             "confidence": 0.1}) is None


# ---- 5. 引擎：规则静默时采用模型决策 ----

class TestEngineModelDecision:
    def test_model_decision_used_when_rules_silent(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        model_d = SocialDecision(
            action=SocialAction.IGNORE,
            reason_codes=(DecisionReason.LOW_RELEVANCE,), confidence=0.9)
        d = engine.decide(perception=PerceptionResult(),
                          model_decision=model_d)
        assert d is model_d

    def test_model_direct_reply(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        model_d = SocialDecision(
            action=SocialAction.DIRECT_REPLY,
            reason_codes=(DecisionReason.HIGH_RELEVANCE,), confidence=0.9)
        d = engine.decide(perception=PerceptionResult(),
                          model_decision=model_d)
        assert d.action == SocialAction.DIRECT_REPLY

    def test_rule_mention_wins_over_model(self):
        engine = SocialDecisionEngine()
        model_d = SocialDecision(
            action=SocialAction.IGNORE,
            reason_codes=(DecisionReason.LOW_RELEVANCE,), confidence=0.9)
        pr = PerceptionResult(has_explicit_mention=True)
        d = engine.decide(perception=pr, model_decision=model_d)
        assert d.action == SocialAction.DIRECT_REPLY
        assert DecisionReason.DIRECT_MENTION in d.reason_codes

    def test_model_block_not_accepted(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        model_d = SocialDecision(
            action=SocialAction.BLOCK,
            reason_codes=(DecisionReason.SAFETY_BLOCK,), confidence=0.9)
        d = engine.decide(perception=PerceptionResult(),
                          model_decision=model_d)
        assert d.action == SocialAction.REACT  # 回落到概率路径

    def test_no_model_unchanged(self):
        engine = SocialDecisionEngine(reply_probability=1.0)
        d = engine.decide(perception=PerceptionResult())
        assert d.action == SocialAction.REACT


# ---- 6. 生产接线：_perceive_with_model ----

class TestProdPerceiveWithModel:
    @pytest.mark.asyncio
    async def test_no_signal_fn_rule_only(self, plugin):
        event = _FakeEvent("今天过得怎么样", group="g1")
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged.topics == ()

    @pytest.mark.asyncio
    async def test_valid_model_signal_merged(self, plugin, monkeypatch):
        async def fake_signal(text, capabilities=()):
            return {
                "confidence": 0.8,
                "speech_acts": [{"act_type": "statement", "confidence": 0.9}],
                "topics": ["mood"], "entities": [],
                "candidate_intents": ["chitchat"],
                "suggested_capabilities": [], "needs_tools": False,
                "ambiguities": [],
            }
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("今天过得怎么样", group="g1")
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged.topics == ("mood",)
        acts = {a.act_type for a in merged.speech_acts}
        assert "statement" in acts

    @pytest.mark.asyncio
    async def test_garbage_model_output_degrades(self, plugin, monkeypatch):
        async def fake_signal(text, capabilities=()):
            return "not json {{{"
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("今天过得怎么样", group="g1")
        rule = plugin._perceive(event)
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged == rule

    @pytest.mark.asyncio
    async def test_signal_fn_exception_degrades(self, plugin, monkeypatch):
        async def fake_signal(text, capabilities=()):
            raise RuntimeError("model down")
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("今天过得怎么样", group="g1")
        rule = plugin._perceive(event)
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged == rule

    @pytest.mark.asyncio
    async def test_rule_keyword_skips_model_signal(self, plugin, monkeypatch):
        called = []

        async def fake_signal(text, capabilities=()):
            called.append(text)
            return {"confidence": 0.9, "speech_acts": [], "topics": [],
                    "entities": [], "candidate_intents": [], "ambiguities": [],
                    "suggested_capabilities": [], "needs_tools": False}
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("帮我查一下课程", group="g1")
        await dududa_handlers._perceive_with_model(plugin, event)
        assert called == []  # 规则关键词命中 -> 快速路径，不调模型

    @pytest.mark.asyncio
    async def test_short_text_skips_model_signal(self, plugin, monkeypatch):
        called = []

        async def fake_signal(text, capabilities=()):
            called.append(text)
            return {"confidence": 0.9, "speech_acts": [], "topics": [],
                    "entities": [], "candidate_intents": [], "ambiguities": [],
                    "suggested_capabilities": [], "needs_tools": False}
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("哈", group="g1")
        await dududa_handlers._perceive_with_model(plugin, event)
        assert called == []  # 超短文本 -> 快速路径，不调模型

    @pytest.mark.asyncio
    async def test_tool_plan_passed_through(self, plugin, monkeypatch):
        async def fake_signal(text, capabilities=()):
            return {
                "confidence": 0.8,
                "speech_acts": [{"act_type": "statement", "confidence": 0.9}],
                "topics": ["weather"], "entities": [],
                "candidate_intents": ["weather_query"],
                "suggested_capabilities": ["mcp.weather"], "needs_tools": True,
                "ambiguities": [],
                "tool_plan": {"steps": [{"capability_id": "mcp.weather",
                                         "arguments": {"q": "合肥"}}]},
            }
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("明天适合出门吗", group="g1")
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged.tool_plan == {"steps": [{"capability_id": "mcp.weather",
                                               "arguments": {"q": "合肥"}}]}

    @pytest.mark.asyncio
    async def test_bad_tool_plan_invalidates_signal(self, plugin, monkeypatch):
        async def fake_signal(text, capabilities=()):
            return {
                "confidence": 0.8,
                "speech_acts": [], "topics": [], "entities": [],
                "candidate_intents": [], "suggested_capabilities": [],
                "needs_tools": False, "ambiguities": [],
                "tool_plan": {"steps": [{"capability_id": 123}]},  # 结构非法
            }
        monkeypatch.setattr(plugin, "_perception_signal", fake_signal)
        event = _FakeEvent("明天适合出门吗", group="g1")
        rule = plugin._perceive(event)
        merged = await dududa_handlers._perceive_with_model(plugin, event)
        assert merged == rule  # 整包丢弃（fail closed）-> 只用规则

    @pytest.mark.asyncio
    async def test_flag_off_signal_returns_none(self, plugin):
        plugin._perception_model_enabled = False
        assert await plugin._perception_signal("你好") is None

    @pytest.mark.asyncio
    async def test_flag_on_parses_json(self, plugin, monkeypatch):
        plugin._perception_model_enabled = True

        async def fake_llm(system, user_msg, max_tokens=1024, temperature=0.5,
                           run_id="", trace_id="", skip_render=False):
            import json as _json
            return _json.dumps(_VALID_PERCEPTION, ensure_ascii=False)
        monkeypatch.setattr(plugin, "_call_llm", fake_llm)
        sig = await plugin._perception_signal("帮我查课程")
        assert isinstance(sig, dict)
        assert sig["topics"] == ["course"]

    @pytest.mark.asyncio
    async def test_flag_on_bad_json_none(self, plugin, monkeypatch):
        plugin._perception_model_enabled = True

        async def fake_llm(system, user_msg, max_tokens=1024, temperature=0.5,
                           run_id="", trace_id="", skip_render=False):
            return "not json"
        monkeypatch.setattr(plugin, "_call_llm", fake_llm)
        assert await plugin._perception_signal("你好") is None
