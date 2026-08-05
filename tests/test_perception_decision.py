"""测试 Perception 与 Decision。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from packages.core.perception import PerceptionResult, SpeechAct, EntityRef
from packages.core.decision import (
    SocialDecision, SocialDecisionEngine, DecisionReason, SocialAction
)


class TestPerceptionResult:
    def test_valid_result(self):
        pr = PerceptionResult(confidence=0.8)
        assert pr.is_valid

    def test_invalid_confidence(self):
        pr = PerceptionResult(confidence=1.5)
        assert not pr.is_valid

    def test_is_question(self):
        pr = PerceptionResult(speech_acts=(SpeechAct(act_type="question", confidence=0.9),))
        assert pr.is_question()

    def test_is_command(self):
        pr = PerceptionResult(is_explicit_command=True)
        assert pr.is_command()

    def test_is_addressed_to(self):
        pr = PerceptionResult(target_users=("bot_001",))
        assert pr.is_addressed_to("bot_001")
        assert not pr.is_addressed_to("someone_else")

    def test_is_addressed_to_all(self):
        pr = PerceptionResult()
        assert pr.is_addressed_to("anyone")


class TestSocialDecision:
    def test_should_reply(self):
        assert SocialDecision(action=SocialAction.ANSWER).should_reply
        assert SocialDecision(action=SocialAction.REACT).should_reply
        assert not SocialDecision(action=SocialAction.IGNORE).should_reply

    def test_is_blocked(self):
        assert SocialDecision(action=SocialAction.BLOCK).is_blocked


class TestSocialDecisionEngine:
    def test_explicit_command_trumps_all(self):
        engine = SocialDecisionEngine()
        perception = PerceptionResult(is_explicit_command=True, needs_tools=True)
        decision = engine.decide(perception=perception)
        # Doc 2.4.8: command needing tools -> canonical USE_TOOLS
        assert decision.action == SocialAction.USE_TOOLS
        assert decision.should_use_tools
        assert DecisionReason.EXPLICIT_COMMAND in decision.reason_codes

    def test_mention_priority(self):
        engine = SocialDecisionEngine()
        perception = PerceptionResult(has_explicit_mention=True)
        decision = engine.decide(perception=perception)
        assert decision.action == SocialAction.ANSWER
        assert DecisionReason.DIRECT_MENTION in decision.reason_codes

    def test_reply_chain_priority(self):
        engine = SocialDecisionEngine()
        perception = PerceptionResult(has_reply_chain=True)
        decision = engine.decide(perception=perception)
        assert decision.action == SocialAction.ANSWER
        assert DecisionReason.REPLY_TO_BOT in decision.reason_codes

    def test_keyword_match(self):
        engine = SocialDecisionEngine(keywords={"天气"})
        perception = PerceptionResult(resolved_references={"text": "今天天气怎么样"})
        decision = engine.decide(perception=perception)
        assert decision.action == SocialAction.ANSWER
        assert DecisionReason.KEYWORD_MATCH in decision.reason_codes

    def test_default_ignore(self):
        engine = SocialDecisionEngine(reply_probability=0.0)
        perception = PerceptionResult()
        decision = engine.decide(perception=perception)
        assert decision.action == SocialAction.IGNORE

    def test_cooldown(self):
        engine = SocialDecisionEngine(cooldown_seconds=999, reply_probability=1.0)
        engine.record_reply("conv1")

        class FakeConv:
            conversation_id = "conv1"

        class FakeMsg:
            conversation = FakeConv()

        class FakeContext:
            conversation = FakeConv()
            current_message = FakeMsg()

        decision = engine.decide(perception=PerceptionResult(), context=FakeContext())
        assert DecisionReason.COOLDOWN_ACTIVE in decision.reason_codes

    def test_record_reply(self):
        engine = SocialDecisionEngine(cooldown_seconds=10)
        engine.record_reply("conv1")
        assert "conv1" in engine._last_reply
