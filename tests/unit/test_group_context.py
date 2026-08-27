from dududa.core.group_context import GroupConversationTracker


def test_group_context_is_scoped_bounded_and_uses_ephemeral_aliases():
    tracker = GroupConversationTracker(capacity=5, ttl_seconds=300)
    for i in range(6):
        tracker.add(
            group_id="g1", sender_id=f"qq-{i % 2}", content=f"消息 {i}",
            message_id=str(i), now=1000 + i)
    tracker.add(
        group_id="g2", sender_id="qq-0", content="另一个群",
        message_id="x", now=1006)

    first = tracker.snapshot("g1", now=1006)
    second = tracker.snapshot("g2", now=1006)
    assert len(first) == 5
    assert [item.content for item in first] == [f"消息 {i}" for i in range(1, 6)]
    assert {item.sender_alias for item in first} == {"成员1", "成员2"}
    assert all("qq-" not in item.sender_alias for item in first)
    assert len(second) == 1


def test_group_context_expires_whole_topic_after_inactivity():
    tracker = GroupConversationTracker(ttl_seconds=300)
    tracker.add(
        group_id="g", sender_id="u1", content="旧话题", now=1000)
    tracker.add(
        group_id="g", sender_id="u2", content="新话题", now=1301)
    items = tracker.snapshot("g", now=1301)
    assert len(items) == 1
    assert items[0].content == "新话题"
    assert items[0].sender_alias == "成员1"


def test_media_summary_can_replace_placeholder():
    tracker = GroupConversationTracker()
    tracker.add(
        group_id="g", sender_id="u1", content="[表情包，尚未识别]",
        message_type="sticker", message_id="m1", now=1000)
    assert tracker.update_summary(
        group_id="g", message_id="m1",
        summary="狗头表情，表达调侃和保命", now=1001)
    item = tracker.snapshot("g", now=1001)[0]
    assert item.message_type == "sticker"
    assert item.content == "狗头表情，表达调侃和保命"


def test_consecutive_stickers_require_distinct_senders():
    tracker = GroupConversationTracker()
    tracker.add(group_id="g", sender_id="u1", content="a",
                message_type="sticker", now=1000)
    tracker.add(group_id="g", sender_id="u1", content="b",
                message_type="sticker", now=1001)
    assert not tracker.consecutive_media("g", now=1001)
    tracker.add(group_id="g", sender_id="u2", content="c",
                message_type="sticker", now=1002)
    assert tracker.consecutive_media("g", now=1002)

