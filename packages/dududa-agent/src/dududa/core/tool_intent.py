"""Deterministic intent boundaries for general-purpose tools.

These predicates answer whether a tool can satisfy the user's request. They
must stay narrower than topic detection: mentioning time or asking about
someone's routine is not a request for the current clock.
"""
from __future__ import annotations

import re


_MENTION_RE = re.compile(r"@\S+")
_CLOCK_CONTEXT_RE = re.compile(
    r"(?:作息|几点睡|几点起|几点醒|等到几点|熬到几点|忙到几点|"
    r"上课|下课|集合|出发|到达|开门|关门|下班|吃饭|天亮)"
)
_CURRENT_CLOCK_RE = re.compile(
    r"(?:(?:北京|当地|本地)时间)?(?:现在|当前|此刻|今天)"
    r"\s*(?:是|为)?\s*(?:几点|几号|星期几|周几|什么时间|什么日期)|"
    r"(?:北京|当地|本地)时间(?:现在|当前|此刻)?"
    r"\s*(?:是|为)?\s*(?:几点|几号|星期几|周几)|"
    r"(?:现在|当前|此刻)(?:的)?(?:时间|日期)"
    r"\s*(?:是|为)?\s*(?:多少|什么)"
)
_BARE_CLOCK_QUERY_RE = re.compile(
    r"(?:(?:请问|问一下|告诉我|看一下|查一下|帮我查一下)\s*)?"
    r"(?:现在|当前|此刻)?\s*(?:是|为)?\s*"
    r"(?:几点(?:了)?|今天几号|今天星期几|今天周几|"
    r"时间(?:是)?(?:多少|什么)|日期(?:是)?(?:多少|什么))"
    r"(?:呀|啊|呢|吗|吧)?\s*[？?。！!]*"
)


def is_explicit_clock_query(text: str) -> bool:
    """Return whether ``mcp.clock`` can directly answer this message.

    The clock returns the current date/time. Schedule questions (``几点睡``),
    playful references (``天亮还要等到几点``), and routine talk therefore
    stay in chat even though they contain a time word.
    """
    value = _MENTION_RE.sub(" ", str(text or ""))
    value = " ".join(value.split()).strip()
    if not value:
        return False
    if _CLOCK_CONTEXT_RE.search(value):
        return False
    return bool(
        _BARE_CLOCK_QUERY_RE.fullmatch(value)
        or _CURRENT_CLOCK_RE.search(value)
    )
