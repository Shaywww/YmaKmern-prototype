# -*- coding: utf-8 -*-
"""NapCat/AstrBot sticker classification and reply-policy regression tests."""
from io import BytesIO
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image
import imageio_ffmpeg

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


def test_plain_gif_and_video_are_distinct_media_kinds():
    gif = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/a.gif", "file": "a.gif"},
    }]})
    video = _event({"message": [{
        "type": "video",
        "data": {"url": "https://example.com/a.mp4", "file": "a.mp4"},
    }]})
    assert _detect_media_kind(gif) == "gif"
    assert _detect_media_kind(video) == "video"
    assert _has_media_in_raw(video)
    assert _detect_media(video) == (
        "https://example.com/a.mp4", "a.mp4", False)


def test_gif_sampling_builds_multiframe_contact_sheet():
    frames = [Image.new("RGB", (12, 10), color) for color in (
        "red", "green", "blue")]
    source = BytesIO()
    frames[0].save(
        source, format="GIF", save_all=True, append_images=frames[1:],
        duration=80, loop=0)
    sheet, count = handlers._gif_contact_sheet(source.getvalue())
    assert count == 3
    with Image.open(BytesIO(sheet)) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.width >= 24


def test_video_sampling_extracts_keyframe_contact_sheet(tmp_path):
    path = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel",
            "error", "-f", "lavfi", "-i",
            "testsrc=size=64x48:rate=4", "-t", "1.2", "-pix_fmt",
            "yuv420p", str(path),
        ],
        check=False, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    sheet, duration = handlers._video_contact_sheet(
        path.read_bytes(), "mp4")
    assert sheet and duration >= 1.0
    with Image.open(BytesIO(sheet)) as rendered:
        assert rendered.format == "JPEG"


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

    def _store_memory(self, event, *contents, **kwargs):
        if kwargs.get("msg_type") != "bot" and contents:
            self.memory = contents[0]


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
            '{"kind":"sticker","animated":false,'
            '"description":"一只猫歪头看着镜头",'
            '"visible_text":"啊？","emotion":"疑惑和调侃",'
            '"confidence":0.96}'
        )

    plugin._call_vision = structured_vision
    summary = await handlers.handle_image(
        plugin, event, b"fake", "a.webp", "webp",
        media_kind="sticker", context_only=True)

    assert summary == "表情包摘要：一只猫歪头看着镜头；配文“啊？”；表达疑惑和调侃"
    assert plugin.memory is None
    assert plugin.group_context.snapshot("1059231626")[-1].content == summary


@pytest.mark.asyncio
async def test_context_only_photo_type_is_propagated_to_group_context():
    plugin = _Plugin()
    plugin.group_context = GroupConversationTracker()
    plugin.group_context.add(
        group_id="1059231626", sender_id="10001",
        content="[图片，尚未识别]", message_type="image",
        message_id="photo-1")
    event = _event({"message": [{
        "type": "image",
        "data": {"url": "https://example.com/plane.jpg"},
    }]})
    event.group_id = "1059231626"
    event.message_id = "photo-1"

    async def structured_vision(system, user, b64, mime, **kwargs):
        return (
            '{"kind":"photo","animated":false,'
            '"description":"傍晚拍摄的一架客机","visible_text":"",'
            '"emotion":"分享","confidence":0.97}'
        )

    plugin._call_vision = structured_vision
    summary = await handlers.handle_image(
        plugin, event, b"fake", "plane.jpg", "jpg",
        media_kind="image", context_only=True)
    item = plugin.group_context.snapshot("1059231626")[-1]
    assert summary == "实拍照片摘要：傍晚拍摄的一架客机；表达分享"
    assert item.message_type == "photo"


@pytest.mark.asyncio
async def test_context_only_video_uses_gpt_keyframe_summary(monkeypatch):
    plugin = _Plugin()
    plugin.group_context = GroupConversationTracker()
    plugin.group_context.add(
        group_id="1059231626", sender_id="10001",
        content="[视频，尚未识别]", message_type="video",
        message_id="video-1")
    event = _event({"message": [{
        "type": "video",
        "data": {"url": "https://example.com/a.mp4"},
    }]})
    event.group_id = "1059231626"
    event.message_id = "video-1"

    monkeypatch.setattr(
        handlers, "_video_contact_sheet", lambda data, ext: (b"jpeg", 6.2))

    async def structured_vision(system, user, b64, mime, **kwargs):
        assert "关键帧" in system
        assert kwargs["skip_render"] is True
        return (
            '{"kind":"video","animated":true,'
            '"description":"几个人展示一种新吃法","visible_text":"",'
            '"emotion":"轻松分享","confidence":0.93}'
        )

    plugin._call_vision = structured_vision
    summary = await handlers.handle_video(
        plugin, event, b"video", "a.mp4", "mp4", context_only=True)
    item = plugin.group_context.snapshot("1059231626")[-1]
    assert summary == "视频摘要：几个人展示一种新吃法；表达轻松分享"
    assert item.message_type == "video"
