"""Deterministic product-scope gate for school-related QQ messages.

YmaKmern is a general-purpose QQ chat agent.  School and campus requests are
outside the deployed product scope and must be ignored before progress UI,
model calls, tool planning, or reply generation.  Keep this classifier small,
auditable, and independent from model output.
"""
from __future__ import annotations

import re


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("institution", re.compile(
        r"(?:学校|校园|大学|学院|高校|中学|高中|初中|小学|职校|"
        r"中国科学技术大学|中科大|科大|USTC|\.edu\.cn)", re.I)),
    ("people", re.compile(
        r"(?:老师|教师|教授|导师|辅导员|班主任|校长|学生|同学|"
        r"学长|学姐|学弟|学妹|研究生|本科生|博士生)")),
    ("teaching", re.compile(
        r"(?:上课|下课|课程|课表|选课|开课|评课|网课|慕课|课堂|"
        r"教学|教务|课程号|专业课|必修课|选修课|补课|挂科|逃课)")),
    ("assessment", re.compile(
        r"(?:考试|期中|期末|月考|高考|中考|考研|保研|作业|成绩|"
        r"绩点|学分|论文|答辩|毕设|毕业设计|奖学金|录取|招生|"
        r"分数线)")),
    ("campus_life", re.compile(
        r"(?:校历|学期|开学|放学|毕业|宿舍|食堂|教室|图书馆|"
        r"实验室|讲座|社团|学生会|第二课堂|军训|校招|校园卡|"
        r"学号|院系|专业排名|培养方案|毕业要求)")),
    ("school_service", re.compile(
        r"(?:评课社区|icourse|教务系统|校园通知|学校通知|开课表)",
        re.I)),
    ("english", re.compile(
        r"\b(?:school|campus|university|college|teacher|professor|student|"
        r"course|exam|homework|thesis|academic)\b", re.I)),
)


def school_silence_reason(text: str) -> str:
    """Return a stable rule category, or an empty string when in scope."""
    value = str(text or "").strip()
    if not value:
        return ""
    for reason, pattern in _RULES:
        if pattern.search(value):
            return reason
    return ""


def is_school_related_message(text: str) -> bool:
    """Whether a user-visible message belongs to the retired school domain."""
    return bool(school_silence_reason(text))
