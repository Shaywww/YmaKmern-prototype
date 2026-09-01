"""Deterministic routing helpers for the USTC-only product domain.

YmaKmern is a USTC bot.  School context is therefore a product invariant,
not a user-profile slot that needs to be asked on every course query.  Keep
the vocabulary here so perception, model-plan gating and the production
planner cannot silently disagree about whether a course/review tool is
required.
"""
from __future__ import annotations

import re


USTC_REVIEW_MARKERS = (
    "评课", "评分高", "给分高", "评分", "给分", "口碑", "课程评价", "教师评价",
    "老师评价", "作业多", "作业少", "难度", "难不难", "收获",
    "值得选", "推荐老师", "老师怎么样", "老师好不好", "哪个老师好",
    "哪些老师", "哪位老师", "好拿分", "拿高分", "水课",
)

USTC_RECOMMENDATION_MARKERS = (
    "推荐几门课", "推荐几门课程", "推荐课程", "推荐课", "选什么课",
    "选哪些课", "哪些课值得", "什么课好", "有什么课推荐", "高分课",
    "好拿分", "拿高分", "水课",
)

USTC_CATALOG_MARKERS = (
    "查课", "课程", "课表", "选课", "开课", "课程号", "谁教",
    "哪个老师", "哪些老师", "上课时间", "上课地点", "全校课表",
    "开课表", "二分制", "二等级制", "两级制",
)

_USTC_OFFERING_DETAIL_MARKERS = (
    "查课", "课表", "开课", "课程号", "谁教", "上课时间", "上课地点",
    "全校课表", "开课表", "二分制", "二等级制", "两级制",
)

_FOLLOWUP_ONLY_RE = re.compile(
    r"^(?:查到了吗|查到没|查好了吗|有结果了吗|结果呢|怎么样了|"
    r"然后呢|那呢|这个呢|哪些呢)[？?。！!~～\s]*$"
)
_USER_LINE_RE = re.compile(r"^\s*\[用户\]\s*[:：]\s*(.+?)\s*$")


def is_ustc_review_query(text: str) -> bool:
    """Whether public USTC review/rating data is required."""
    value = str(text or "")
    return any(marker in value for marker in USTC_REVIEW_MARKERS)


def is_ustc_recommendation_query(text: str) -> bool:
    """Whether the user asks for course selection recommendations."""
    value = str(text or "")
    return any(marker in value for marker in USTC_RECOMMENDATION_MARKERS)


def is_ustc_catalog_query(text: str) -> bool:
    """Whether public USTC offering/catalog data is required."""
    value = str(text or "")
    return any(marker in value for marker in USTC_CATALOG_MARKERS)


def is_ustc_course_query(text: str) -> bool:
    return bool(
        is_ustc_review_query(text)
        or is_ustc_recommendation_query(text)
        or is_ustc_catalog_query(text)
    )


def _recent_user_turns(context: str, limit: int = 5) -> tuple[str, ...]:
    turns: list[str] = []
    for line in str(context or "").splitlines():
        match = _USER_LINE_RE.match(line)
        if match:
            turns.append(match.group(1).strip())
    return tuple(turns[-max(1, int(limit)):])


def contextualize_ustc_course_intent(text: str, context: str) -> str:
    """Resolve short course follow-ups against recent *user* turns only.

    The returned value is an internal planner intent.  It deliberately adds
    the product-level USTC scope, but never treats bot utterances or arbitrary
    data blocks as executable context.
    """
    raw = " ".join(str(text or "").split()).strip()
    if not raw:
        return raw
    turns = _recent_user_turns(context)
    prior_course = tuple(turn for turn in turns if is_ustc_course_query(turn))

    subject = ""
    for turn in reversed(turns):
        terms = _ustc_search_terms(turn)
        if terms:
            subject = terms
            break

    if is_ustc_course_query(raw):
        # Carry only the latest subject slot (for example “人工智能”), not all
        # previous routing words.  Thus a new “哪些老师评分高” request remains
        # review-only instead of inheriting an earlier recommendation plan.
        pieces = ([subject] if subject and not _ustc_search_terms(raw) else [])
        pieces.append(raw)
    elif prior_course and (
            _FOLLOWUP_ONLY_RE.fullmatch(raw)
            or len(raw) <= 16
            or raw in {"人工智能", "计算机", "数学", "物理", "化学"}):
        latest_course = prior_course[-1]
        if _FOLLOWUP_ONLY_RE.fullmatch(raw):
            pieces = ([subject] if subject else []) + [latest_course, raw]
        else:
            pieces = [latest_course, raw]
    else:
        return raw

    unique: list[str] = []
    for piece in pieces:
        value = " ".join(str(piece).split()).strip()
        if value and value not in unique:
            unique.append(value)
    combined = " ".join(unique)
    return f"USTC {combined}" if combined else raw


def ustc_tool_capabilities(text: str) -> tuple[str, ...]:
    """Return USTC tools in factual priority order for an effective intent."""
    value = str(text or "")
    review = is_ustc_review_query(value) or is_ustc_recommendation_query(value)
    recommendation = is_ustc_recommendation_query(value)
    catalog = is_ustc_catalog_query(value)
    capabilities: list[str] = []
    if review:
        capabilities.append("mcp.icourse_reviews")
    if (recommendation or (catalog and not review)
            or any(marker in value for marker in _USTC_OFFERING_DETAIL_MARKERS)):
        capabilities.append("mcp.course_schedule")
    return tuple(capabilities)


def _ustc_search_terms(text: str) -> str:
    raw = " ".join(str(text or "").split()).strip()
    value = re.sub(r"(?i)\bUSTC\b|中国科学技术大学|中科大", " ", raw)
    removable = sorted(
        set(USTC_RECOMMENDATION_MARKERS + USTC_REVIEW_MARKERS),
        key=len, reverse=True)
    for marker in removable:
        value = value.replace(marker, " ")
    value = re.sub(
        r"(?:给我)?推荐\s*(?:我)?\s*(?:几门|一些|点)?\s*"
        r"(?:好拿分|给分好|高分|好的?)?\s*(?:的)?\s*(?:课|课程)",
        " ", value)
    value = re.sub(r"(?:想)?选(?:什么|哪些|哪几门)?(?:课|课程)", " ", value)
    value = re.sub(
        r"(?:查到了吗|查到没|查好了吗|有结果了吗|结果呢|怎么样了|"
        r"帮我|请你?|麻烦你?|给我|查一下|查查|查询|搜索|搜一下)",
        " ", value)
    value = re.sub(r"(?:哪几个|哪一些|哪些|哪个|哪位)\s*老师", " ", value)
    value = re.sub(r"(?:老师|教师)\s*(?:的)?\s*$", " ", value)
    value = re.sub(r"(?:这门)?(?:课程|课)\s*$", " ", value)
    value = re.sub(r"[，。！？、?！~～：:；;]+", " ", value)
    return " ".join(value.split()).strip()


def ustc_query_has_subject(text: str) -> bool:
    """Whether a course/teacher/discipline term remains after routing words."""
    return bool(_ustc_search_terms(text))


def ustc_search_query(text: str) -> str:
    """Strip routing language while retaining a course/teacher/subject name."""
    raw = " ".join(str(text or "").split()).strip()
    return _ustc_search_terms(raw) or raw
