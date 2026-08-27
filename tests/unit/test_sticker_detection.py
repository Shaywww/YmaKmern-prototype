# -*- coding: utf-8 -*-
"""NapCat/AstrBot sticker classification and reply-policy regression tests."""
from types import SimpleNamespace

import pytest

from dududa.application import dududa_handlers as handlers
from dududa.application.dududa_utils import (
    _detect_media,
    _detect_media_kind,
    _has_media_in_raw,
)
from dududa.core.group_context import GroupConversationTracker


def _event(raw, text=""):
    return SimpleNamespace(
        raw_message=raw,
        message_obj=SimpleNamespace(raw_message=None),
        get_messages=lambda: [],
        message_str=text,
    )


def test_market_face_metadata_is_sticker():
    event = _event({"message": [{
        "type": "image",
        "data": {
            "url": "https://example.com/face.webp",
            "summary": "[动画表情]",
            "emoji_id": "123",
            "emoji_package_id": "456",
            "key": "abc",
        },
    }]})
    assert _detect_media_kind(event) == "sticker"
    assert _has_media_in_raw(event)


def test_plain_image_is_not_forced_to_sticker():
    event = _event({"message": [{
        "type": "image",
        "data": {
            "url": "https://example.com/photo.jpg",
            "summary": "[图片]",
            "sub_type": 0,
        },
    }]})
    assert _detect_media_kind(event) == "image"


def test_raw_mface_is_sticker_and_media_fallback():
    event = _event({"message": [{
        "type": "mface",
        "data": {"url": "https://example.com/mface.gif", "file": "mface.gif"},
    }]})
    assert _detect_media_kind(event) == "sticker"
    assert _detect_media(event) == (
        "https://example.com/mface.gif", "mface.gif", True)


def test_raw_nested_url_is_media_fallback():
    event = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/a.png", "file": "a.png"},
    }]})
    assert _detect_media(event) == (
        "https://example.com/a.png", "a.png", True)
    assert handlers._remote_media_url(event) == "https://example.com/a.png"


class _InputAdapter:
    def to_preprocessed(self, event):
        return SimpleNamespace(combined_text=event.message_str)


class _Plugin:
    input_adapter = _InputAdapter()
    personas = SimpleNamespace(active=SimpleNamespace(
        display_name="嘟嘟哒", first_person="嘟嘟哒"))

    def __init__(self):
        self.system = ""
        self.user = ""
        self.memory = None

    async def _call_vision(self, system, user, b64, mime, **kwargs):
        self.system, self.user = system, user
        return "接住这个表情啦～(≧▽≦)"

    def _store_memory(self, event, incoming, outgoing, **kwargs):
        self.memory = incoming


@pytest.mark.asyncio
async def test_sticker_prompt_prefers_conversational_reaction():
    plugin = _Plugin()
    event = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/a.webp", "emoji_id": "1"},
    }]})
    reply = await handlers.handle_image(
        plugin, event, b"fake", "a.webp", "webp", media_kind="sticker")
    assert reply == "接住这个表情啦～(≧▽≦)"
    assert "自然接话" in plugin.system
    assert "不要逐项描述画面" in plugin.system
    assert "不得臆测表情产生的具体原因" in plugin.system
    assert "听到了八卦" in plugin.system
    assert "平台元数据已明确标记" in plugin.system
    assert plugin.user == "用户只发送了这个视觉内容，没有附带文字。"
    assert plugin.memory.startswith("[表情包《")


@pytest.mark.asyncio
async def test_ambiguous_image_asks_vision_to_classify_silently():
    plugin = _Plugin()
    event = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/a.jpg", "summary": "[图片]"},
    }]}, text="这是什么地方")
    await handlers.handle_image(
        plugin, event, b"fake", "a.jpg", "jpg", media_kind="image")
    assert "先在内部根据画面判断" in plugin.system
    assert "不要把判断标签输出给用户" in plugin.system
    assert plugin.user == "这是什么地方"


@pytest.mark.asyncio
async def test_context_only_vision_returns_structured_summary_without_memory():
    plugin = _Plugin()
    plugin.group_context = GroupConversationTracker()
    plugin.group_context.add(
        group_id="1059231626", sender_id="10001",
        content="[表情包，尚未识别]", message_type="sticker",
        message_id="visual-1")
    event = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/a.webp", "emoji_id": "1"},
    }]})
    event.group_id = "1059231626"
    event.message_id = "visual-1"

    async def structured_vision(system, user, b64, mime, **kwargs):
        plugin.system, plugin.user = system, user
        assert kwargs["skip_render"] is True
        return (
            '{"kind":"sticker","description":"一只猫歪头看着镜头",'
            '"visible_text":"啊？","emotion":"疑惑和调侃"}'
        )

    plugin._call_vision = structured_vision
    summary = await handlers.handle_image(
        plugin, event, b"fake", "a.webp", "webp",
        media_kind="sticker", context_only=True)

    assert summary == "表情包摘要：一只猫歪头看着镜头；配文“啊？”；表达疑惑和调侃"
    assert plugin.memory is None
    assert plugin.group_context.snapshot("1059231626")[-1].content == summary
