"""测试 MessageEnvelope 与 Actor。"""
import sys
sys.path.insert(0, r"C:\Users\王\dududa20-prototype")

import pytest
from dududa.core.envelope import (
    MessageEnvelope,
    Actor,
    Platform,
    MessageKind,
    ConversationRef,
    Attachment,
    AttachmentKind,
    PreprocessedEnvelope,
)


class TestActor:
    def test_actor_creation(self):
        actor = Actor(
            actor_id="user_123",
            platform=Platform.QQ,
            display_name="测试用户",
            role="normal",
        )
        assert actor.actor_id == "user_123"
        assert not actor.is_privileged()

    def test_privileged_actor(self):
        admin = Actor(
            actor_id="admin_1",
            platform=Platform.QQ,
            display_name="管理员",
            role="admin",
        )
        assert admin.is_privileged()

        owner = Actor(
            actor_id="owner_1",
            platform=Platform.QQ,
            display_name="群主",
            role="owner",
        )
        assert owner.is_privileged()

    def test_actor_immutability(self):
        actor = Actor(actor_id="test", platform=Platform.QQ, display_name="test")
        with pytest.raises(Exception):
            actor.actor_id = "changed"  # type: ignore


class TestMessageEnvelope:
    def _make_envelope(self, **kwargs):
        defaults = {
            "platform": Platform.QQ,
            "kind": MessageKind.GROUP,
            "conversation": ConversationRef(
                conversation_id="group_456",
                platform=Platform.QQ,
                kind=MessageKind.GROUP,
            ),
            "sender": Actor(
                actor_id="user_123",
                platform=Platform.QQ,
                display_name="测试用户",
            ),
            "text": "你好",
        }
        defaults.update(kwargs)
        return MessageEnvelope(**defaults)

    def test_basic_envelope(self):
        env = self._make_envelope()
        assert env.platform == Platform.QQ
        assert env.text == "你好"
        assert env.envelope_id  # 自动生成

    def test_is_explicit_command_with_mention(self):
        env = self._make_envelope(mentions=("bot_001",))
        assert env.is_explicit_command()

    def test_is_explicit_command_with_slash(self):
        env = self._make_envelope(text="/help")
        assert env.is_explicit_command()

    def test_not_explicit_command(self):
        env = self._make_envelope(text="今天天气真好")
        assert not env.is_explicit_command()

    def test_is_mentioned(self):
        env = self._make_envelope(mentions=("bot_001", "user_456"))
        assert env.is_mentioned("bot_001")
        assert not env.is_mentioned("someone_else")

    def test_has_attachment(self):
        env = self._make_envelope(
            attachments=(
                Attachment(
                    kind=AttachmentKind.IMAGE,
                    content_ref="img://ref/1",
                    summary="一张猫的图片",
                ),
            )
        )
        assert env.has_attachment(AttachmentKind.IMAGE)
        assert not env.has_attachment(AttachmentKind.FILE)

    def test_reply_chain(self):
        inner = self._make_envelope(text="内层消息")
        outer = self._make_envelope(text="回复", reply_to=inner)
        chain = outer.reply_chain()
        assert len(chain) == 2
        assert chain[0].text == "回复"
        assert chain[1].text == "内层消息"

    def test_reply_chain_single(self):
        env = self._make_envelope()
        chain = env.reply_chain()
        assert len(chain) == 1


class TestPreprocessedEnvelope:
    def test_combine_text(self):
        env = MessageEnvelope(
            platform=Platform.QQ,
            kind=MessageKind.GROUP,
            conversation=ConversationRef(
                conversation_id="g1",
                platform=Platform.QQ,
                kind=MessageKind.GROUP,
            ),
            sender=Actor(
                actor_id="u1", platform=Platform.QQ, display_name="test"
            ),
            text="看这张图",
        )
        pre = PreprocessedEnvelope(
            envelope=env,
            ocr_text="图片中有文字：Hello World",
            image_description="一张包含文字的截图",
        )
        combined = pre.combined_text
        assert "看这张图" in combined
        assert "Hello World" in combined
        assert "截图" in combined
