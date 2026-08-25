# -*- coding: utf-8 -*-
"""Public USTC iCourse review lookup: parsing, caching and routing."""
import pytest

from dududa.core.capability import CapabilityRegistry
from dududa.mcp.icourse_reviews import (
    ICourseReviewsService,
    _clean_query,
    _parse_course_page,
    _parse_search_page,
)
from dududa.mcp.registry import register_all_mcp_services
from dududa.planner.integration import integrate_with_orchestrator
from dududa.planner.planner import PlanningContext


SEARCH_HTML = """
<html><body>
  <a href="/course/20951/" class="px16">微积分I（张瑞）</a>
  <a href="/teacher/18/">张瑞</a>
  <a href="/course/20951/" class="px16">重复项</a>
  <a href="/course/21348/" class="px16">微积分II（张瑞）</a>
</body></html>
"""


COURSE_HTML = """
<html><head>
  <meta name="description" content="9.5 分，20 人评价">
  <title>微积分I（张瑞） - USTC评课社区</title>
</head><body>
  <span class="small grey align-bottom left-pd-sm desktop">
    2026秋 2025秋 &nbsp;课程号：MATH101101
  </span>
  <ul>
    <li class="right-mg-md">课程难度：中等</li>
    <li class="right-mg-md">作业多少：中等</li>
    <li class="right-mg-md">给分好坏：超好</li>
    <li class="right-mg-md">收获大小：很多</li>
  </ul>
  <table><tr>
    <td><strong>课程类别：</strong>本科计划内课程</td>
    <td><strong>开课单位：</strong>数学科学学院</td>
  </tr><tr>
    <td><strong>课程层次：</strong>通修</td>
    <td><strong>学分：</strong>5.0</td>
  </tr></table>
  <div id="course-summary" class="showmore-text ck-content review-content">
    <div><h3>课堂体验</h3><p>讲课清晰，考试难度适中。</p></div>
  </div>
  <span id="review-anchor">点评</span>
  <ul><li class="right-mg-md">课程难度：简单</li></ul>
  <div class="review-content" id="review-content-1"><p>一条点评。</p></div>
</body></html>
"""


def test_search_parser_only_returns_unique_numeric_course_links():
    rows = _parse_search_page(SEARCH_HTML)
    assert [row["course_id"] for row in rows] == ["20951", "21348"]
    assert rows[0]["title"] == "微积分I（张瑞）"


def test_course_parser_extracts_public_aggregate_fields():
    row = _parse_course_page(
        COURSE_HTML, "https://icourse.club/course/20951/")
    assert row["course"] == "微积分I"
    assert row["teacher"] == "张瑞"
    assert row["rating"] == 9.5
    assert row["review_count"] == 20
    assert row["course_code"] == "MATH101101"
    assert row["metrics"]["课程难度"] == "中等"
    assert row["details"]["开课单位"] == "数学科学学院"
    assert "讲课清晰" in row["snippet"]
    assert "一条点评" not in row["snippet"]  # 优先使用页面汇总，不镜像点评


@pytest.mark.parametrize(("raw", "expected"), [
    ("在评课社区查一下微积分I 张瑞怎么样？", "微积分I 张瑞"),
    ("张瑞老师怎么样", "张瑞"),
    ("帮我查一下线性代数课程评价", "线性代数课程评价"),
])
def test_query_cleanup_keeps_course_and_teacher_names(raw, expected):
    assert _clean_query(raw) == expected


@pytest.mark.asyncio
async def test_service_caches_identical_queries(monkeypatch):
    service = ICourseReviewsService()
    calls = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return [{"title": "微积分I（张瑞）",
                 "link": "https://icourse.club/course/20951/",
                 "snippet": "评分 9.5/10（20 人评价）"}]

    monkeypatch.setattr(service, "_fetch_live", fake_fetch)
    first = await service.search("微积分I 张瑞", limit=9)
    second = await service.search("微积分I 张瑞", limit=9)
    assert first.success and second.success
    assert first.source == "live" and second.source == "cache"
    assert len(calls) == 1 and calls[0]["limit"] == 3


def test_registry_and_planner_route_review_queries():
    registry = CapabilityRegistry()
    assert register_all_mcp_services(registry) == 13
    capability = registry.get("mcp.icourse_reviews")
    assert capability is not None
    assert capability.schema.input_schema["properties"]["limit"]["maximum"] == 3

    integration = integrate_with_orchestrator(None, registry)
    candidates = registry.filter_candidates(permissions=(), max_count=30)
    plan = integration.planner.plan(PlanningContext(
        user_intent="评课社区查一下微积分I 张瑞怎么样",
        available_capabilities=candidates,
    ))
    assert plan.steps
    assert plan.steps[0].capability_id == "mcp.icourse_reviews"
    assert plan.steps[0].arguments["limit"] == 3
