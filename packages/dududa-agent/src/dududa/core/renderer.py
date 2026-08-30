"""嘟嘟哒 2.0 OC Renderer —— 原创角色/人格表达。

OC Renderer 接收 DraftResponse，用版本化 Persona 渲染为 FinalResponse。
只能改变语序、句式、称呼、口语程度和适量表情；
不能改变数字、日期、来源、权限、拒绝、工具状态、目标或附件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal, InvalidOperation
import re
from typing import Awaitable, Callable, Optional

from .trace_recorder import trace_recorder

# 模型回调：(prompt, *, run_id="", trace_id="") -> 转换后的回复文本
RenderLLM = Callable[..., Awaitable[str]]


class ResponseKind(str, Enum):
    """Response contract selected by the orchestrator, never by the model."""

    CHAT = "chat"
    TOOL_ANSWER = "tool_answer"


@dataclass(frozen=True)
class FactAnchor:
    """事实锚点 —— 不可被 Renderer 修改的约束。"""
    field: str
    value: str
    source: str = ""
    kind: str = "text"       # text | number | date | bool
    canonical: str = ""      # normalized value used by deterministic checks
    semantic: str = ""       # score | count | temperature | ...


@dataclass(frozen=True)
class DraftResponse:
    """Response Composer 生成的草稿回复。

    包含事实锚点、引用、警告、拒绝、目标用户、附件和不可修改约束。
    工具没有返回可靠数据时，Composer 只能说明不可用、时效或缺失，
    不能让聊天模型补出"看起来合理"的评分、日期或结论。
    """
    text: str = ""
    fact_anchors: tuple[FactAnchor, ...] = ()
    citations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    target_users: tuple[str, ...] = ()  # 空 = 全员
    attachments: tuple[str, ...] = ()
    immutable_constraints: tuple[str, ...] = ()  # 不可修改的硬约束
    # Keep this last so existing positional constructors stay compatible.
    kind: ResponseKind = ResponseKind.CHAT


_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*(?:[-/.\u5e74])\s*(\d{1,2})\s*"
    r"(?:[-/.\u6708])\s*(\d{1,2})\s*(?:\u65e5)?(?!\d)"
)
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?<![\w.])-?\d+(?:\.\d+)?\s*"
    r"(?:%|\u2103|\u5ea6|\u5206|\u4eba|\u540d|\u4e2a|\u95e8|\u6b21|\u5929|\u5c0f\u65f6|\u5206\u949f|\u5143|"
    r"\u516c\u91cc|km/h|km|kph|\u5b66\u5206|\u6761|\u665a|\u6444\u6c0f\u5ea6)",
    re.IGNORECASE,
)
_LABELED_NUMBER_RE = re.compile(
    r"(?:\u8bc4\u5206|\u5f97\u5206|\u6e29\u5ea6|\u6c14\u6e29|\u4f53\u611f|\u6e7f\u5ea6|\u98ce\u901f|\u4ef7\u683c|\u5bb9\u91cf|"
    r"\u9009\u8bfe\u4eba\u6570|\u8bc4\u4ef7\u6570|\u5b66\u5206)\s*(?:\u4e3a|\u662f|\u7ea6|[:\uff1a])?\s*"
    r"-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_NEGATION_PREFIXES = ("没有", "并非", "不是", "无", "没", "不")


def _canonical_number(value: str) -> str:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return ""
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return ""
    rendered = format(number.normalize(), "f")
    return "0" if rendered in ("-0", "") else rendered


def _canonical_date(value: str) -> str:
    match = _DATE_RE.search(str(value))
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s\u3000,\uff0c.\u3002:：;；()\uff08\uff09]+", "", str(value)).lower()


def _semantic_from_field(field: str) -> str:
    value = str(field or "").lower()
    if any(key in value for key in ("score", "rating", "grade")):
        return "score"
    if any(key in value for key in ("temp", "temperature", "feels_like")):
        return "temperature"
    if "humidity" in value:
        return "percentage"
    if any(key in value for key in ("wind", "kph", "speed")):
        return "speed"
    if any(key in value for key in ("credit", "\u5b66\u5206")):
        return "credits"
    if any(key in value for key in (
            "count", "reviews", "review_count", "capacity", "enrolled",
            "total", "items")):
        return "count"
    if any(key in value for key in ("price", "cost", "amount")):
        return "price"
    return ""


def _semantic_from_claim(value: str) -> str:
    normalized = str(value or "").lower().replace(" ", "")
    if any(key in normalized for key in ("评分", "得分")):
        return "score"
    if any(key in normalized for key in ("温度", "气温", "体感", "℃", "摄氏度")):
        return "temperature"
    if "湿度" in normalized or "%" in normalized:
        return "percentage"
    if any(key in normalized for key in ("风速", "km/h", "kph")):
        return "speed"
    if "学分" in normalized:
        return "credits"
    if any(key in normalized for key in (
            "人", "名", "个", "门", "次", "条", "晚", "容量", "选课人数", "评价数")):
        return "count"
    if "元" in normalized or "价格" in normalized:
        return "price"
    if any(key in normalized for key in ("公里", "km")):
        return "distance"
    if any(key in normalized for key in ("天", "小时", "分钟")):
        return "duration"
    return ""


def fact_anchor_present(anchor: FactAnchor, text: str) -> bool:
    """Check a fact with format-tolerant number/date normalization."""
    if not text:
        return False
    canonical = anchor.canonical or (
        _canonical_date(anchor.value) if anchor.kind == "date"
        else _canonical_number(anchor.value) if anchor.kind == "number"
        else _normalize_text(anchor.value)
    )
    if anchor.kind == "date":
        return canonical in {_canonical_date(m.group(0)) for m in _DATE_RE.finditer(text)}
    if anchor.kind == "number":
        return canonical in {_canonical_number(m.group(0)) for m in _NUMBER_RE.finditer(text)}
    needle = _normalize_text(anchor.value)
    haystack = _normalize_text(text)
    if not needle or needle not in haystack:
        return False
    if not needle.startswith(_NEGATION_PREFIXES):
        start = haystack.find(needle)
        prefix = haystack[max(0, start - 3):start]
        if any(prefix.endswith(item) for item in _NEGATION_PREFIXES):
            return False
    return True


def extract_atomic_facts(data, *, source: str = "", field: str = "",
                         limit: int = 96) -> tuple[FactAnchor, ...]:
    """Flatten structured tool output into bounded, typed atomic facts."""
    facts: list[FactAnchor] = []

    def visit(value, path: str) -> None:
        if len(facts) >= limit or value is None:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else str(key))
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value[:20]):
                visit(item, f"{path}[{index}]" if path else f"[{index}]")
            return
        if isinstance(value, bool):
            facts.append(FactAnchor(path or field or "value", str(value), source,
                                    kind="bool", canonical=str(value).lower()))
            return
        rendered = str(value).strip()
        if not rendered or len(rendered) > 160:
            return
        date = _canonical_date(rendered)
        if date:
            kind, canonical = "date", date
        elif isinstance(value, (int, float, Decimal)) or re.fullmatch(
                r"-?\d+(?:\.\d+)?", rendered) or _NUMBER_WITH_UNIT_RE.fullmatch(rendered):
            kind, canonical = "number", _canonical_number(rendered)
        else:
            kind, canonical = "text", _normalize_text(rendered)
        facts.append(FactAnchor(path or field or "value", rendered, source,
                                kind=kind, canonical=canonical,
                                semantic=_semantic_from_field(path or field)))

    visit(data, field)
    return tuple(facts)


def referenced_facts(text: str, facts: tuple[FactAnchor, ...]) -> tuple[FactAnchor, ...]:
    """Keep only tool facts actually used by the composed answer."""
    selected: list[FactAnchor] = []
    seen: set[tuple[str, str]] = set()
    for fact in facts:
        if fact.kind == "text" and len(_normalize_text(fact.value)) < 2:
            continue
        if fact_anchor_present(fact, text):
            key = (fact.kind, fact.canonical or fact.value)
            if key not in seen:
                seen.add(key)
                selected.append(fact)
    return tuple(selected)


def unsupported_numeric_claims(text: str, facts: tuple[FactAnchor, ...],
                               *, allowed_text: str = "") -> tuple[str, ...]:
    """Return quantified claims that are absent from tool data and user input."""
    supported_numbers: dict[str, set[str]] = {}
    for fact in facts:
        if fact.kind != "number":
            continue
        semantic = fact.semantic or _semantic_from_field(fact.field)
        supported_numbers.setdefault(semantic, set()).add(
            fact.canonical or _canonical_number(fact.value))
    supported_dates = {
        fact.canonical or _canonical_date(fact.value)
        for fact in facts if fact.kind == "date"
    }
    for match in _NUMBER_RE.finditer(allowed_text or ""):
        supported_numbers.setdefault("", set()).add(
            _canonical_number(match.group(0)))
    for match in _DATE_RE.finditer(allowed_text or ""):
        supported_dates.add(_canonical_date(match.group(0)))

    errors: list[str] = []
    date_spans: list[tuple[int, int]] = []
    for match in _DATE_RE.finditer(text or ""):
        date_spans.append(match.span())
        canonical = _canonical_date(match.group(0))
        if canonical and canonical not in supported_dates:
            errors.append(match.group(0))
    labeled_matches = list(_LABELED_NUMBER_RE.finditer(text or ""))
    labeled_spans = [match.span() for match in labeled_matches]
    claim_matches: list[tuple[re.Match[str], bool]] = [
        (match, True) for match in labeled_matches
    ]
    claim_matches.extend(
        (match, False) for match in _NUMBER_WITH_UNIT_RE.finditer(text or "")
    )
    seen_spans: set[tuple[int, int]] = set()
    for match, is_labeled in sorted(claim_matches, key=lambda item: item[0].start()):
        if any(start <= match.start() < end for start, end in date_spans):
            continue
        if not is_labeled and any(
                max(match.start(), start) < min(match.end(), end)
                for start, end in labeled_spans):
            continue
        if match.span() in seen_spans:
            continue
        seen_spans.add(match.span())
        canonical = _canonical_number(match.group(0))
        semantic = _semantic_from_claim(match.group(0))
        supported = supported_numbers.get(semantic, set())
        if canonical and canonical not in supported:
            errors.append(match.group(0).strip())
    return tuple(dict.fromkeys(errors))


@dataclass(frozen=True)
class Persona:
    """版本化人格定义。"""
    persona_id: str
    version: str
    name: str = ""
    traits: tuple[str, ...] = ()           # 性格特征
    speaking_style: str = ""               # 说话风格描述
    forbidden_topics: tuple[str, ...] = () # 禁止话题
    max_emojis_per_message: int = 2

    def render_prompt(self) -> str:
        """生成渲染 Prompt。"""
        parts = [f"你是 {self.name}。"]
        if self.traits:
            parts.append(f"性格特征：{'、'.join(self.traits)}。")
        if self.speaking_style:
            parts.append(f"说话风格：{self.speaking_style}。")
        parts.append(
            "重要约束：你只能调整语序、句式、称呼、口语程度和适量表情。"
            "绝对不能修改数字、日期、来源、权限、拒绝结论、工具状态、"
            "目标用户或附件内容。"
        )
        if self.forbidden_topics:
            parts.append(
                f"绝对不讨论以下话题：{'、'.join(self.forbidden_topics)}。"
            )
        return "\n".join(parts)


@dataclass(frozen=True)
class FinalResponse:
    """经过人格渲染的最终回复。

    包含渲染后的文本和事实校验结果。
    """
    text: str = ""
    persona_id: str = ""
    persona_version: str = ""
    fact_check_passed: bool = False
    fact_check_errors: tuple[str, ...] = ()
    emoji_count: int = 0


class RenderValidator:
    """渲染校验器 —— 对比 Fact Anchor、引用、拒绝和目标。

    校验失败最多在预算内修复一次；
    仍失败时返回确定性模板或事实安全的未人格化 Draft。
    """

    def validate(
        self, draft: DraftResponse, final: FinalResponse, persona: Persona
    ) -> tuple[bool, tuple[str, ...]]:
        """校验渲染结果。返回 (passed, errors)。"""
        errors: list[str] = []

        # 1. 事实锚点检查
        for anchor in draft.fact_anchors:
            if not fact_anchor_present(anchor, final.text):
                errors.append(
                    f"Fact anchor '{anchor.field}={anchor.value}' "
                    f"not preserved in rendered text"
                )

        # 2. 引用检查
        for citation in draft.citations:
            if citation not in final.text:
                errors.append(f"Citation '{citation}' lost in rendering")

        # 3. 拒绝保留检查
        for refusal in draft.refusals:
            keyword_check = any(
                w in final.text.lower()
                for w in refusal.lower().split()
                if len(w) > 2
            )
            if not keyword_check:
                errors.append(
                    f"Refusal '{refusal}' may have been softened in rendering"
                )

        # 4. 表情数量检查
        if final.emoji_count > persona.max_emojis_per_message:
            errors.append(
                f"Too many emojis: {final.emoji_count} > "
                f"{persona.max_emojis_per_message}"
            )

        return len(errors) == 0, tuple(errors)


class OCRenderer:
    """OC (Original Character) 渲染器。

    接收 DraftResponse、版本化 Persona 和受限 RenderContext，
    输出 FinalResponse。
    """

    def __init__(self, persona: Optional[Persona] = None,
                 llm: Optional[RenderLLM] = None,
                 max_repairs: int = 1):
        self._persona = persona
        self._llm = llm
        self._max_repairs = max(0, int(max_repairs))
        self._validator = RenderValidator()

    def render(self, draft: DraftResponse) -> FinalResponse:
        """渲染草稿为最终回复。

        当前实现是确定性的：直接返回草稿内容并标记事实校验通过。
        2.0 目标中，这里会调用模型基于 Persona 进行风格转换。
        """
        persona = self._persona or Persona(
            persona_id="default", version="1.0", name="YmaKmern"
        )

        # 确定性的基础渲染：保留草稿全文
        rendered_text = draft.text

        # 文本过滤
        for topic in persona.forbidden_topics:
            if topic in rendered_text:
                rendered_text = draft.text  # 回退到原文
                break

        # 校验
        final = FinalResponse(
            text=rendered_text,
            persona_id=persona.persona_id,
            persona_version=persona.version,
            fact_check_passed=False,
            emoji_count=self._count_emojis(rendered_text),
        )

        passed, errors = self._validator.validate(draft, final, persona)

        return FinalResponse(
            text=rendered_text if passed else draft.text,
            persona_id=persona.persona_id,
            persona_version=persona.version,
            fact_check_passed=passed,
            fact_check_errors=errors,
            emoji_count=self._count_emojis(rendered_text if passed else draft.text),
        )

    # ---- 2.5.8 hybrid：模型风格转换 + 事实保持 ----

    def _build_render_prompt(self, persona: Persona,
                             draft: DraftResponse) -> str:
        """构造风格转换 Prompt：人格约束 + 草稿 + 不可变锚点。"""
        parts = [persona.render_prompt()]
        parts.append("以下是需要你按人格重新表达的草稿回复：")
        parts.append(draft.text)
        if draft.fact_anchors:
            parts.append("不可修改的事实锚点（必须逐字保留）：")
            for a in draft.fact_anchors:
                parts.append(f"- {a.field}: {a.value}")
        if draft.citations:
            parts.append("引用（必须保留）：" + "；".join(draft.citations))
        if draft.refusals:
            parts.append("拒绝结论（不得软化）：" + "；".join(draft.refusals))
        if draft.immutable_constraints:
            parts.append("硬约束（不得违反）："
                         + "；".join(draft.immutable_constraints))
        parts.append(
            "只输出转换后的回复文本本身，不要任何解释、前缀或引号。"
            "不得新增草稿中不存在的来源、工具状态、内部字段，也不得输出 "
            "None、null 等占位符。只可使用 (≧▽≦)、^^~ 等纯文本颜文字，"
            "严禁使用 Unicode 彩色 Emoji（例如 😋、😊、😂）。")
        return "\n".join(parts)

    async def _invoke_llm(self, prompt: str,
                          run_id: str, trace_id: str) -> str:
        """调用模型回调；兼容 (prompt) 与 (prompt, run_id, trace_id) 签名。"""
        try:
            return await self._llm(prompt, run_id=run_id, trace_id=trace_id)
        except TypeError:
            return await self._llm(prompt)

    def _final(self, text: str, persona: Persona,
               passed: bool, errors: tuple[str, ...]) -> FinalResponse:
        return FinalResponse(
            text=text, persona_id=persona.persona_id,
            persona_version=persona.version,
            fact_check_passed=passed, fact_check_errors=errors,
            emoji_count=self._count_emojis(text),
        )

    async def render_hybrid(self, draft: DraftResponse,
                            run_id: str = "", trace_id: str = "") -> FinalResponse:
        """hybrid 渲染：LLM 按 Persona 做风格转换，校验失败最多修复一次，
        仍失败回退确定性渲染（原文，事实安全）。llm 未配置时等同 render。"""
        start = time.time()
        persona = self._persona or Persona(
            persona_id="default", version="1.0", name="YmaKmern"
        )
        if self._llm is None:
            return self.render(draft)

        prompt = self._build_render_prompt(persona, draft)
        text = ""
        try:
            text = (await self._invoke_llm(
                prompt, run_id, trace_id) or "").strip()
        except Exception:
            text = ""
        if not text:
            return self.render(draft)

        errors: tuple[str, ...] = ()
        for attempt in range(self._max_repairs + 1):
            final = self._final(text, persona, False, ())
            passed, errors = self._validator.validate(draft, final, persona)
            if passed:
                trace_recorder.record(
                    event="render_result", run_id=run_id, trace_id=trace_id,
                    passed=True, fallback=False, attempts=attempt + 1,
                    latency_ms=round((time.time() - start) * 1000, 1))
                return self._final(text, persona, True, ())
            if attempt >= self._max_repairs:
                break
            repair_prompt = (
                prompt
                + "\n\n上一次转换不符合要求：\n"
                + "\n".join(errors)
                + "\n请修正后重新输出。")
            try:
                text = (await self._invoke_llm(
                    repair_prompt, run_id, trace_id) or "").strip()
            except Exception:
                text = ""
            if not text:
                break

        fallback = self.render(draft)
        trace_recorder.record(
            event="render_result", run_id=run_id, trace_id=trace_id,
            passed=True, fallback=True,
            attempts=self._max_repairs + 1,
            errors=list(errors)[:3],
            latency_ms=round((time.time() - start) * 1000, 1))
        return fallback

    @staticmethod
    def _count_emojis(text: str) -> int:
        """简单表情计数。"""
        import re
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map
            "\U0001F1E0-\U0001F1FF"  # flags
            "\U0001F000-\U0001F0FF"  # mahjong tiles & playing cards
            "\U0001F100-\U0001F251"  # enclosed alphanumeric supplement
            "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
            "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A
            "\U00002600-\U000027BF"  # misc symbols & dingbats
            "\U0000FE0F"             # variation selector-16
            "]",
            flags=re.UNICODE,
        )
        return len(emoji_pattern.findall(text))
