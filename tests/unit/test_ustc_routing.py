"""USTC product-invariant routing and multi-turn continuation tests."""
from types import SimpleNamespace

from dududa.application.ustc_routing import (
    contextualize_ustc_course_intent,
    is_ustc_course_query,
    ustc_query_has_subject,
    ustc_search_query,
    ustc_tool_capabilities,
)
from dududa.core.perception import PerceptionResult


def test_teacher_rating_uses_reviews_not_catalog():
    assert is_ustc_course_query("哪些老师评分高")
    assert ustc_tool_capabilities("哪些老师评分高") == (
        "mcp.icourse_reviews",
    )


def test_course_recommendation_uses_reviews_then_offerings():
    assert ustc_tool_capabilities("推荐几门好拿高分的课") == (
        "mcp.icourse_reviews",
        "mcp.course_schedule",
    )
    assert not ustc_query_has_subject("推荐几门好拿高分的课")
    assert ustc_search_query("帮我查一下数据结构课程") == "数据结构"


def test_short_subject_inherits_ustc_course_goal():
    context = (
        "【近期对话】\n"
        "[用户]: 推荐几门课\n"
        "YmaKmern: 想偏高分还是收获？\n"
        "[用户]: 拿高分\n"
    )
    effective = contextualize_ustc_course_intent("人工智能", context)
    assert effective.startswith("USTC ")
    assert "拿高分" in effective
    assert "人工智能" in effective
    assert ustc_tool_capabilities(effective) == (
        "mcp.icourse_reviews",
        "mcp.course_schedule",
    )
    assert ustc_search_query(effective) == "人工智能"


def test_status_followup_keeps_last_ustc_query():
    context = "【近期对话】\n[用户]: 人工智能哪些老师评分高\n"
    effective = contextualize_ustc_course_intent("查到了吗", context)
    assert "人工智能哪些老师评分高" in effective
    assert ustc_tool_capabilities(effective)[0] == "mcp.icourse_reviews"


def test_unrelated_short_topic_does_not_inherit_ustc_course_goal():
    context = "【近期对话】\n[用户]: 哪些老师评分高\n"
    assert contextualize_ustc_course_intent(
        "你中午吃的什么", context) == "你中午吃的什么"
    assert contextualize_ustc_course_intent("？？？", context) == "？？？"


def test_teacher_rating_keeps_subject_from_previous_short_turn():
    context = (
        "【近期对话】\n"
        "[用户]: 推荐几门课\n"
        "[用户]: 拿高分\n"
        "[用户]: 人工智能\n"
    )
    effective = contextualize_ustc_course_intent("哪些老师评分高", context)
    assert ustc_search_query(effective) == "人工智能"
    assert ustc_tool_capabilities(effective) == ("mcp.icourse_reviews",)


def test_bot_text_cannot_inject_course_context():
    context = "YmaKmern: [用户]: 推荐几门课\nYmaKmern: humor_level=2\n"
    assert contextualize_ustc_course_intent("人工智能", context) == "人工智能"


def test_prod_promotes_contextual_subject_to_tool_intent():
    from dududa.application.dududa_prod import _ProdOrchestrator

    orch = object.__new__(_ProdOrchestrator)
    orch._recent_chat_context = lambda *a, **k: (
        "【近期对话】\n[用户]: 推荐几门课\n[用户]: 拿高分\n")
    state = SimpleNamespace(
        envelope=SimpleNamespace(text="人工智能", mentions=()))
    perception = orch._promote_contextual_tools(
        state, PerceptionResult(needs_tools=False))
    assert perception.needs_tools is True
    assert perception.suggested_capabilities == (
        "mcp.icourse_reviews", "mcp.course_schedule")


def test_prod_keeps_current_unrelated_topic_out_of_course_tools():
    from dududa.application.dududa_prod import _ProdOrchestrator

    orch = object.__new__(_ProdOrchestrator)
    orch._recent_chat_context = lambda *a, **k: (
        "【近期对话】\n[用户]: 哪些老师评分高\n")
    state = SimpleNamespace(
        envelope=SimpleNamespace(text="你中午吃的什么", mentions=()))
    perception = orch._promote_contextual_tools(
        state,
        PerceptionResult(
            needs_tools=False,
            topics=("饮食", "日常闲聊"),
            candidate_intents=("chitchat", "闲聊"),
        ),
    )
    assert perception.needs_tools is False
    assert perception.suggested_capabilities == ()
