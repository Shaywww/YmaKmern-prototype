"""嘟嘟哒 2.0 Social Decision —— 判断是否参与、如何参与。

输出六种明确动作：ANSWER, REACT, ASK, IGNORE, DEFER, BLOCK。
决策基于 PerceptionResult、ContextSnapshot 和当前 Policy。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .state import SocialAction


class DocumentAction(str, Enum):
    """文档 2.5.4 六动作（对齐契约命名，增量兼容 SocialAction）。"""
    IGNORE = "ignore"
    REACT = "react"
    DIRECT_REPLY = "direct_reply"
    ASK_CLARIFICATION = "ask_clarification"
    USE_TOOLS = "use_tools"
    DEFER = "defer"


class DecisionReason(str, Enum):
    """决策原因码 —— 可审计、可追踪。"""
    # 参与原因
    DIRECT_MENTION = "direct_mention"         # 被 @
    REPLY_TO_BOT = "reply_to_bot"             # 回复 Bot 的消息
    KEYWORD_MATCH = "keyword_match"           # 关键词命中
    HIGH_RELEVANCE = "high_relevance"         # 高语义相关度
    EXPLICIT_COMMAND = "explicit_command"     # 显式命令

    # 不参与原因
    LOW_RELEVANCE = "low_relevance"           # 低相关度
    ALREADY_ANSWERED = "already_answered"     # 已有人回答
    COOLDOWN_ACTIVE = "cooldown_active"       # 冷却中
    NOT_IN_ALLOWLIST = "not_in_allowlist"     # 不在目标群/用户列表
    PERMISSION_DENIED = "permission_denied"   # 权限不足
    SAFETY_BLOCK = "safety_block"             # 安全阻止
    NO_TOOL_RESULT = "no_tool_result"         # 工具无结果

    # 暂缓
    NEEDS_CLARIFICATION = "needs_clarification"  # 需要澄清
    INSUFFICIENT_INFO = "insufficient_info"      # 信息不足


@dataclass(frozen=True)
class SocialDecision:
    """社交决策结果。

    输出必须包含明确的 action 和可审计的 reason_code。
    """
    action: SocialAction = SocialAction.IGNORE
    reason_codes: tuple[DecisionReason, ...] = ()
    confidence: float = 0.0
    should_use_tools: bool = False
    clarification_question: Optional[str] = None  # ASK 时的追问

    @property
    def should_reply(self) -> bool:
        return self.action in (SocialAction.DIRECT_REPLY, SocialAction.REACT)

    @property
    def should_ask(self) -> bool:
        return self.action == SocialAction.ASK_CLARIFICATION

    @property
    def is_blocked(self) -> bool:
        return self.action == SocialAction.BLOCK


    def document_action(self) -> DocumentAction:
        """对齐文档 2.5.4 六动作。

        SocialAction 规范值即文档动作，按值直映；
        BLOCK 观察行为 = 不回复，映射为 IGNORE（原 reason 保留）。
        """
        if self.action == SocialAction.BLOCK:
            return DocumentAction.IGNORE
        return DocumentAction(self.action.value)


class SocialDecisionEngine:
    """社交决策引擎。

    组合确定性规则（优先）与基于上下文的判断。
    当前实现基于规则；2.0 目标允许引入 Perception Model 辅助判断。
    """

    def __init__(
        self,
        allowlist_groups: Optional[set[str]] = None,
        keywords: Optional[set[str]] = None,
        cooldown_seconds: float = 10.0,
        reply_probability: float = 0.3,
    ):
        self._allowlist_groups = allowlist_groups or set()
        self._keywords = keywords or set()
        self._cooldown_seconds = cooldown_seconds
        self._reply_probability = reply_probability
        self._last_reply: dict[str, float] = {}  # conversation_id -> timestamp

    def decide(
        self,
        perception: Optional[Any] = None,
        context: Optional[Any] = None,
        now: Optional[float] = None,
    ) -> SocialDecision:
        """做出社交决策。

        确定性规则优先：@ -> 回复 -> 命令 -> 关键词 -> 权限 -> 冷却。
        """
        import time
        now = now or time.time()
        reasons: list[DecisionReason] = []

        # 1. 显式命令（最高优先级）
        if perception and perception.is_explicit_command:
            return SocialDecision(
                action=SocialAction.USE_TOOLS if perception.needs_tools
                else SocialAction.DIRECT_REPLY,
                reason_codes=(DecisionReason.EXPLICIT_COMMAND,),
                confidence=1.0,
                should_use_tools=perception.needs_tools,
            )

        # 2. 被 @
        if perception and perception.has_explicit_mention:
            return SocialDecision(
                action=SocialAction.USE_TOOLS if perception.needs_tools
                else SocialAction.DIRECT_REPLY,
                reason_codes=(DecisionReason.DIRECT_MENTION,),
                confidence=0.95,
                should_use_tools=perception.needs_tools,
            )

        # 3. 回复 Bot
        if perception and perception.has_reply_chain:
            return SocialDecision(
                action=SocialAction.DIRECT_REPLY,
                reason_codes=(DecisionReason.REPLY_TO_BOT,),
                confidence=0.9,
            )

        # 4. 安全检查（最高阻塞优先级）
        if perception and context:
            if self._is_safety_risk(perception):
                return SocialDecision(
                    action=SocialAction.BLOCK,
                    reason_codes=(DecisionReason.SAFETY_BLOCK,),
                )

        # 5. 权限检查
        if context and context.conversation:
            conv_id = context.conversation.conversation_id if hasattr(
                context.conversation, "conversation_id"
            ) else getattr(context.conversation, "conversation_id", "unknown")
            if self._allowlist_groups and conv_id not in self._allowlist_groups:
                return SocialDecision(
                    action=SocialAction.IGNORE,
                    reason_codes=(DecisionReason.NOT_IN_ALLOWLIST,),
                )

        # 6. 关键词匹配
        if perception and self._keywords:
            msg_text = perception.resolved_references.get("text", "")
            matched = any(kw in msg_text for kw in self._keywords)
            if matched:
                return SocialDecision(
                    action=SocialAction.USE_TOOLS if perception.needs_tools
                    else SocialAction.DIRECT_REPLY,
                    reason_codes=(DecisionReason.KEYWORD_MATCH,),
                    confidence=0.7,
                    should_use_tools=perception.needs_tools,
                )

        # 7. 歧义澄清（只问一个解除当前阻塞的问题）
        if perception and perception.ambiguities and perception.is_question():
            return SocialDecision(
                action=SocialAction.ASK_CLARIFICATION,
                reason_codes=(DecisionReason.NEEDS_CLARIFICATION,),
                confidence=0.6,
                clarification_question=f"{perception.ambiguities[0]}，能具体说一下吗？",
            )

        # 8. 冷却检查
        if context:
            conv_id = self._extract_conv_id(context)
            last = self._last_reply.get(conv_id, 0)
            if now - last < self._cooldown_seconds:
                return SocialDecision(
                    action=SocialAction.IGNORE,
                    reason_codes=(DecisionReason.COOLDOWN_ACTIVE,),
                )

        # 9. 概率参与
        import random
        if random.random() < self._reply_probability:
            return SocialDecision(
                action=SocialAction.DIRECT_REPLY if perception and perception.is_question()
                else SocialAction.REACT,
                reason_codes=(DecisionReason.HIGH_RELEVANCE,),
                confidence=0.5,
            )

        # 10. 默认不参与
        return SocialDecision(
            action=SocialAction.IGNORE,
            reason_codes=(DecisionReason.LOW_RELEVANCE,),
        )

    def record_reply(self, conversation_id: str, now: Optional[float] = None):
        import time
        self._last_reply[conversation_id] = now or time.time()

    @staticmethod
    def _is_safety_risk(perception: Any) -> bool:
        """安全检查桩：未来接入内容安全模型。"""
        return False  # 当前不作过滤，接入生产前需实现

    @staticmethod
    def _extract_conv_id(context: Any) -> str:
        if hasattr(context, "current_message"):
            msg = context.current_message
            if hasattr(msg, "conversation"):
                return msg.conversation.conversation_id
        return "unknown"
