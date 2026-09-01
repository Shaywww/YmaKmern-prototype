"""Unified deterministic post-contract for emitted responses."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .quality_eval import (
    persona_contract_violations, strip_self_degrading_abuse,
    strip_unicode_emoji,
)
from .renderer import FactAnchor, ResponseKind, unsupported_numeric_claims


_PROGRESS_RE = re.compile(
    r"(?:正在(?:查|搜|看)|这就帮你|马上(?:帮你)?|稍等|等一下|"
    r"等我(?:一下)?|一小下|待会儿)"
)
_FUTURE_TASK_PROMISE_RE = re.compile(
    r"(?:过会儿|稍后|待会儿)(?:再)?(?:问我|戳我|试一次)|"
    r"我(?:会)?盯着(?:点)?|一有(?:结果|准信|消息).{0,12}(?:告诉|通知)你|"
    r"查好(?:了)?再(?:告诉|回复)你"
)
_TOOL_LEAK_RE = re.compile(
    r"(?:\bmcp\.[A-Za-z0-9_.-]+|\[工具|工具状态\s*[:：]|"
    r"(?:^|[（(\s:：])(?:None|null)(?:$|[）)\s,.，。]))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResponseContractResult:
    passed: bool
    violations: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()


_AUTO_REPAIRABLE_VIOLATIONS = frozenset({
    "self_degrading_abuse", "unicode_emoji",
})


def repair_response_style(
    text: str, result: ResponseContractResult
) -> tuple[str, tuple[str, ...]]:
    """Repair style-only violations while leaving hard failures untouched."""
    violations = frozenset(result.violations)
    if not violations or not violations.issubset(_AUTO_REPAIRABLE_VIOLATIONS):
        return str(text or ""), ()
    repaired = str(text or "")
    if "unicode_emoji" in violations:
        repaired = strip_unicode_emoji(repaired)
    if "self_degrading_abuse" in violations:
        repaired = strip_self_degrading_abuse(repaired)
    if not repaired or repaired == str(text or ""):
        return str(text or ""), ()
    return repaired, tuple(sorted(violations))


def is_progress_placeholder(text: str) -> bool:
    value = " ".join(str(text or "").split()).strip()
    if not value or len(value) > 160:
        return False
    promise = _PROGRESS_RE.search(value)
    factual = re.search(
        r"(?:-?\d+(?:\.\d+)?\s*(?:℃|%|km/h)|"
        r"晴|阴|多云|小雨|中雨|大雨|阵雨|下雪)", value)
    return bool(promise and not factual)


def validate_response_contract(
    text: str,
    *,
    kind: ResponseKind = ResponseKind.CHAT,
    facts: tuple[FactAnchor, ...] = (),
    allowed_text: str = "",
    has_tool_data: bool = False,
) -> ResponseContractResult:
    """Apply hard output constraints after model generation.

    Prompt instructions remain soft style guidance.  These checks are the
    enforceable boundary for progress promises, internal leaks, persona floor
    and quantified tool claims.
    """
    value = str(text or "").strip()
    violations: list[str] = []
    if not value:
        violations.append("empty")
    # User-visible generation is synchronous.  Real slow-task progress is
    # emitted by the handler, never by the final answer, so a final promise to
    # "keep checking" is false regardless of whether a tool returned data.
    if is_progress_placeholder(value):
        violations.append("progress_placeholder")
    if _FUTURE_TASK_PROMISE_RE.search(value):
        violations.append("future_task_promise")
    if _TOOL_LEAK_RE.search(value):
        violations.append("internal_tool_leak")
    for violation in persona_contract_violations(
            value, casual_chat=(kind == ResponseKind.CHAT)):
        if violation in {
                "customer_template", "self_degrading_abuse",
                "unicode_emoji"}:
            violations.append(violation)
    unsupported: tuple[str, ...] = ()
    if kind == ResponseKind.TOOL_ANSWER:
        unsupported = unsupported_numeric_claims(
            value, facts, allowed_text=allowed_text)
        if unsupported:
            violations.append("unsupported_numeric_claim")
    unique = tuple(dict.fromkeys(violations))
    return ResponseContractResult(
        passed=not unique,
        violations=unique,
        unsupported_claims=unsupported,
    )
