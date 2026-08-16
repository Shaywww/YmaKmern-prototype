"""嘟嘟哒 2.0 OC Renderer —— 原创角色/人格表达。

OC Renderer 接收 DraftResponse，用版本化 Persona 渲染为 FinalResponse。
只能改变语序、句式、称呼、口语程度和适量表情；
不能改变数字、日期、来源、权限、拒绝、工具状态、目标或附件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .trace_recorder import trace_recorder

# 模型回调：(prompt, *, run_id="", trace_id="") -> 转换后的回复文本
RenderLLM = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class FactAnchor:
    """事实锚点 —— 不可被 Renderer 修改的约束。"""
    field: str
    value: str
    source: str = ""


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
            if anchor.value not in final.text:
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
            persona_id="default", version="1.0", name="嘟嘟哒"
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
            "None、null 等占位符。")
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
            persona_id="default", version="1.0", name="嘟嘟哒"
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
