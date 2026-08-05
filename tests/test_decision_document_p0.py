"""P1 —— Social Decision 对齐文档六动作（文档 2.5.4）。"""
from types import SimpleNamespace

from packages.core.decision import (
    DecisionReason, DocumentAction, SocialDecision, SocialDecisionEngine,
)
from packages.core.state import SocialAction


def ctx(conv="g1"):
    return SimpleNamespace(
        current_message=SimpleNamespace(
            conversation=SimpleNamespace(conversation_id=conv),
        ),
        conversation=SimpleNamespace(conversation_id=conv),
    )


def perception(**kw):
    base = dict(
        is_explicit_command=False,
        needs_tools=False,
        has_explicit_mention=False,
        has_reply_chain=False,
        ambiguities=(),
        is_question=lambda: False,
        resolved_references={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestDocumentActionMapping:
    def test_direct_reply(self):
        d = SocialDecision(action=SocialAction.DIRECT_REPLY)
        assert d.document_action() == DocumentAction.DIRECT_REPLY

    def test_use_tools(self):
        d = SocialDecision(action=SocialAction.USE_TOOLS)
        assert d.document_action() == DocumentAction.USE_TOOLS

    def test_react(self):
        d = SocialDecision(action=SocialAction.REACT)
        assert d.document_action() == DocumentAction.REACT

    def test_ask_clarification(self):
        d = SocialDecision(action=SocialAction.ASK_CLARIFICATION)
        assert d.document_action() == DocumentAction.ASK_CLARIFICATION

    def test_ignore(self):
        d = SocialDecision(action=SocialAction.IGNORE)
        assert d.document_action() == DocumentAction.IGNORE

    def test_defer(self):
        d = SocialDecision(action=SocialAction.DEFER)
        assert d.document_action() == DocumentAction.DEFER

    def test_block_maps_to_ignore_with_reason_kept(self):
        d = SocialDecision(
            action=SocialAction.BLOCK,
            reason_codes=(DecisionReason.SAFETY_BLOCK,),
        )
        assert d.document_action() == DocumentAction.IGNORE
        assert DecisionReason.SAFETY_BLOCK in d.reason_codes

    def test_answer_alias_maps_to_direct_reply(self):
        """兼容别名 ANSWER == DIRECT_REPLY（同值）。"""
        d = SocialDecision(action=SocialAction.ANSWER)
        assert d.document_action() == DocumentAction.DIRECT_REPLY

    def test_ask_alias_maps_to_ask_clarification(self):
        d = SocialDecision(action=SocialAction.ASK)
        assert d.document_action() == DocumentAction.ASK_CLARIFICATION


class TestEngineDocumentActions:
    def test_mention_is_direct_reply(self):
        engine = SocialDecisionEngine(
            allowlist_groups={"g1"}, cooldown_seconds=0, reply_probability=0.0,
        )
        d = engine.decide(perception(has_explicit_mention=True), ctx())
        assert d.document_action() == DocumentAction.DIRECT_REPLY
        assert d.should_reply

    def test_command_with_tools_is_use_tools(self):
        engine = SocialDecisionEngine(
            allowlist_groups={"g1"}, cooldown_seconds=0, reply_probability=0.0,
        )
        d = engine.decide(
            perception(is_explicit_command=True, needs_tools=True), ctx(),
        )
        assert d.document_action() == DocumentAction.USE_TOOLS

    def test_ambiguous_question_asks_clarification(self):
        engine = SocialDecisionEngine(
            allowlist_groups={"g1"}, cooldown_seconds=0, reply_probability=0.0,
        )
        d = engine.decide(
            perception(ambiguities=("数据结构",), is_question=lambda: True), ctx(),
        )
        assert d.document_action() == DocumentAction.ASK_CLARIFICATION
        assert DecisionReason.NEEDS_CLARIFICATION in d.reason_codes

    def test_no_reason_defaults_to_ignore(self):
        engine = SocialDecisionEngine(
            allowlist_groups={"g1"}, cooldown_seconds=0, reply_probability=0.0,
        )
        d = engine.decide(perception(), ctx())
        assert d.document_action() == DocumentAction.IGNORE

    def test_allowlist_deny_is_ignore(self):
        engine = SocialDecisionEngine(
            allowlist_groups={"other"}, cooldown_seconds=0, reply_probability=0.0,
        )
        d = engine.decide(perception(), ctx("g1"))
        assert d.document_action() == DocumentAction.IGNORE
        assert DecisionReason.NOT_IN_ALLOWLIST in d.reason_codes
