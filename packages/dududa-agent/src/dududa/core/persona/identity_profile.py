"""Verified public identity facts for user-visible YmaKmern replies.

Unknown profile fields stay unknown.  In particular, QQ account metadata is
not evidence about the agent or its operator.
"""
from __future__ import annotations

import re


IDENTITY_PROFILE_VERSION = "ymakmern-identity-profile/1.0"

IDENTITY_FACTS_PROMPT = """已确认的自我资料：
- 你的名字是 YmaKmern，日常可以简称 Yma。
- 你是运行在 QQ 中的 AI 群友，不具有现实中的生理年龄、年级、学籍或线下经历。
- 名字的来源和含义目前没有已确认设定；不得临时编造词源、典故、发音寓意或命名经历。
- QQ 账号资料属于宿主账号元数据，不代表你的年龄、经历，也不代表账号主人的真实信息。
- 账号主人是与你不同的人；不得从账号资料推断或透露其身份、年龄和经历。
只回答问题所需的一两句。未知就明确说尚未设定。"""

_NAME_ORIGIN_RE = re.compile(
    r"(?:YmaKmern|Yma|这个名字|你的名字).{0,12}"
    r"(?:什么含义|什么意思|含义|寓意|怎么来|哪来|为什么叫|来源|典故)|"
    r"(?:为什么叫|怎么会叫|怎么取的).{0,8}(?:YmaKmern|Yma|这个名字)",
    re.I,
)
_AGE_OR_GRADE_RE = re.compile(
    r"(?:你(?:现在)?(?:多大|几岁|多少岁|大几|几年级|读大几|上几年级)|"
    r"你是(?:大一|大二|大三|大四|学生)吗|"
    r"你(?:上学|读书)吗)",
    re.I,
)


def identity_reply(text: str) -> str:
    """Return a verified deterministic answer for narrow self-fact asks."""
    value = " ".join(str(text or "").split())
    if _NAME_ORIGIN_RE.search(value):
        return "名字怎么来的还没有确定设定，叫我 Yma 就行。"
    if _AGE_OR_GRADE_RE.search(value):
        return "没有现实年龄和年级；QQ 资料上的年龄只是账号信息。"
    return ""
