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


def test_bot_turn_is_rendered_but_not_counted_as_human_sender():
    tracker = GroupConversationTracker(ttl_seconds=300)
    tracker.add(group_id="g", sender_id="u1", content="你攻击性太强了",
                now=1000)
    tracker.add(group_id="g", sender_id="bot:1", content="我就回了一句啊",
                is_bot=True, now=1001)
    rendered = tracker.render("g", now=1001)
    assert "成员1" in rendered
    assert "YmaKmern" in rendered
    assert "我就回了一句啊" in rendered
    assert tracker.stats("g", now=1001)["unique_senders"] == 1


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


def test_visual_types_are_counted_and_rendered_distinctly():
    tracker = GroupConversationTracker()
    tracker.add(group_id="g", sender_id="u1", content="客机",
                message_type="photo", now=1000)
    tracker.add(group_id="g", sender_id="u2", content="操作过程",
                message_type="video", now=1001)
    assert tracker.stats("g", now=1001)["media_count"] == 2
    rendered = tracker.render("g", now=1001)
    assert "实拍照片" in rendered and "视频" in rendered
    assert "T1 [" in rendered and "T2 [" in rendered


def test_quiet_capture_removes_raw_messages_before_summary():
    tracker = GroupConversationTracker(ttl_seconds=300)
    tracker.add(group_id="g", sender_id="u1", content="原始消息一",
                message_id="1", now=1000)
    tracker.add(group_id="g", sender_id="u2", content="原始消息二",
                message_id="2", now=1001)
    assert tracker.capture_for_summary(
        "g", expected_last_activity=1001, now=1299) == ()
    captured = tracker.capture_for_summary(
        "g", expected_last_activity=1001, now=1301)
    assert [item.content for item in captured] == ["原始消息一", "原始消息二"]
    assert tracker.snapshot("g", now=1301) == ()


def test_topic_capsule_uses_time_gradient_and_expires():
    tracker = GroupConversationTracker(
        ttl_seconds=300, topic_ttl_seconds=7200)
    capsule = tracker.set_topic_capsule(
        group_id="g", topic="实验课教室变更",
        summary="明天实验课可能换到综合楼，但还没有正式通知",
        core_points=("可能换到综合楼", "群通知尚未发布"),
        unresolved="等待正式通知确认", tone="serious",
        last_message_at=1000, now=1301)
    assert capsule is not None
    assert tracker.activate_capsule("g", capsule.capsule_id, now=1301)

    shallow = tracker.active_topic_context("g", now=1500)
    assert "核心信息" in shallow and "可能换到综合楼" in shallow
    weak = tracker.active_topic_context("g", now=2000)
    assert "概况" in weak and "核心信息" not in weak
    assert "尚待确认" in weak
    assert tracker.active_topic_context("g", now=8201) == ""


def test_only_two_recent_topic_capsules_are_kept():
    tracker = GroupConversationTracker(max_topic_capsules=2)
    for index in range(3):
        tracker.set_topic_capsule(
            group_id="g", topic=f"话题{index}", summary=f"摘要{index}",
            last_message_at=1000 + index, now=1000 + index)
    assert [item.topic for item in tracker.topic_capsules("g", now=1003)] == [
        "话题1", "话题2"]


def test_resumed_topic_counts_messages_for_incremental_refresh():
    tracker = GroupConversationTracker()
    capsule = tracker.set_topic_capsule(
        group_id="g", topic="旧话题", summary="旧摘要",
        last_message_at=1000, now=1100)
    tracker.add(group_id="g", sender_id="u1", content="继续聊",
                now=1101)
    assert tracker.activate_capsule("g", capsule.capsule_id, now=1101)
    for index in range(11):
        tracker.add(group_id="g", sender_id=f"u{index % 2}",
                    content=f"新消息{index}", now=1102 + index)
    assert tracker.active_message_count("g") == 12
    tracker.consume_active_messages("g", 12)
    assert tracker.active_message_count("g") == 0


def test_interaction_scene_prioritises_current_speaker_and_bot_exchange():
    tracker = GroupConversationTracker(capacity=12, ttl_seconds=300)
    tracker.add(group_id="g", sender_id="a", content="作业第三题我还没写",
                message_id="a1", now=1000)
    tracker.add(group_id="g", sender_id="bot", content="先把受力图画出来",
                message_id="b1", is_bot=True, now=1001)
    tracker.add(group_id="g", sender_id="b", content="晚上打游戏吗" * 15,
                message_id="b2", now=1002)
    tracker.add(group_id="g", sender_id="c", content="我先开一局" * 15,
                message_id="c1", now=1003)
    tracker.add(group_id="g", sender_id="a", content="还没写完",
                message_id="a2", now=1004)

    scene = tracker.interaction_scene(
        "g", current_message_id="a2", current_sender_id="a",
        current_text="还没写完", reply_target="bot", related_turn="T2",
        now=1005)
    assert scene is not None
    assert scene.current_speaker == "成员1"
    assert scene.reply_target == "YmaKmern"
    assert [item.content for _, item in scene.recent_relevant] == [
        "作业第三题我还没写"]
    assert scene.last_bot_utterance.content == "先把受力图画出来"
    assert scene.bot_engagement == "是"
    assert scene.recent_bot_streak == 0

    rendered = tracker.render_interaction_scene(
        "g", current_message_id="a2", current_sender_id="a",
        current_text="还没写完", reply_target="bot", related_turn="T2",
        awaited_input="上一轮明确要求补充的信息",
        now=1005, budget=900)
    assert len(rendered) <= 900
    assert "【当前互动现场" in rendered
    assert "本轮新消息" in rendered and "还没写完" in rendered
    assert "最近相关往来" in rendered and "作业第三题我还没写" in rendered
    assert "机器人上次发言：先把受力图画出来" in rendered
    assert "感知关联发言" not in rendered
    assert "正在等待的参数或回答" not in rendered
    assert "机器人最近连续发言条数" not in rendered
    assert "最近是否有人接机器人的话" not in rendered


def test_interaction_scene_keeps_explicit_quote_author_and_no_raw_id():
    tracker = GroupConversationTracker()
    quoted_alias = tracker.sender_alias("g", "qq-quoted", now=1000)
    tracker.add(group_id="g", sender_id="qq-current", content="这句什么意思",
                message_id="m2", now=1001)

    rendered = tracker.render_interaction_scene(
        "g", current_message_id="m2", current_sender_id="qq-current",
        current_text="这句什么意思", reply_target="other",
        quoted_author=quoted_alias, quoted_text="合着刚才全靠自由发挥",
        now=1002)
    assert f"明确引用：{quoted_alias}：合着刚才全靠自由发挥" in rendered
    assert f"当前回复对象：{quoted_alias}" in rendered
    assert "qq-quoted" not in rendered
    assert "qq-current" not in rendered


def test_wider_hot_queue_keeps_perception_turn_ids_at_t1_through_t7():
    tracker = GroupConversationTracker(capacity=12)
    for index in range(10):
        tracker.add(
            group_id="g", sender_id=f"u{index % 2}",
            content=f"消息{index}", message_id=f"m{index}", now=1000 + index)

    perception_view = tracker.render("g", now=1010)
    assert "消息3" in perception_view and "消息9" in perception_view
    assert "消息2" not in perception_view
    assert "T7 [" in perception_view
    assert "T8 [" not in perception_view

    scene = tracker.render_interaction_scene(
        "g", current_message_id="m9", current_sender_id="u1",
        current_text="消息9", now=1010, budget=1600)
    assert "H2 成员2" in scene
    assert "T1 成员2" in scene
