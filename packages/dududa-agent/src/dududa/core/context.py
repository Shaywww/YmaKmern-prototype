"""嘟嘟哒 2.0 Context Builder。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .envelope import Actor, MessageEnvelope, ConversationRef
from .state import RuntimeBudget


@dataclass(frozen=True)
class ContextMemoryScope:
    """上下文检索范围。"""
    scope_type: str = "short_term"
    conversation: ConversationRef = field(
        default_factory=lambda: ConversationRef(
            conversation_id="unknown", platform="qq", kind="group"
        )
    )
    actor: Actor = field(
        default_factory=lambda: Actor(
            actor_id="unknown", platform="qq", display_name="unknown"
        )
    )

    def to_key(self) -> str:
        return f"{self.scope_type}|{self.conversation.conversation_id}|{self.actor.actor_id}"


@dataclass(frozen=True)
class PolicyView:
    """群/会话的策略投影。"""
    allowlist_groups: tuple[str, ...] = ()
    denylist_groups: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    reply_rate: float = 1.0
    meme_rate: float = 1.0
    mode: str = "normal"
    interruption_cost: float = 0.0  # 打断成本 0~1：越高越少被动参与（文档 2.5.4）


@dataclass(frozen=True)
class UserPreference:
    """用户偏好投影。"""
    actor_id: str = ""
    style: Optional[str] = None
    preferred_name: Optional[str] = None
    remembered_facts: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaRef:
    """Persona 引用。"""
    persona_id: str = ""
    version: str = ""
    traits: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationContext:
    """当前对话上下文摘要。"""
    recent_messages: tuple[str, ...] = ()
    active_topics: tuple[str, ...] = ()
    reply_chain_summary: Optional[str] = None


@dataclass(frozen=True)
class ContextSnapshot:
    """本次推理的只读上下文快照。"""
    current_message: MessageEnvelope = field(
        default_factory=lambda: MessageEnvelope(
            conversation=ConversationRef(
                conversation_id="unknown", platform="qq", kind="group"
            ),
            sender=Actor(actor_id="unknown", platform="qq", display_name="unknown"),
        )
    )
    conversation: ConversationContext = field(default_factory=ConversationContext)
    policy: PolicyView = field(default_factory=PolicyView)
    user_preference: Optional[UserPreference] = None
    persona: Optional[PersonaRef] = None
    authorized_memories: tuple[Any, ...] = ()
    available_capability_summaries: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class ContextBuilder:
    """上下文组装器。"""

    def __init__(
        self,
        memory_repo: Optional[Any] = None,
        capability_registry: Optional[Any] = None,
        profile_store: Optional[Any] = None,
        style_store: Optional[Any] = None,
    ):
        self._memory_repo = memory_repo
        self._capability_registry = capability_registry
        self._profile_store = profile_store
        self._style_store = style_store

    def build(
        self,
        envelope: MessageEnvelope,
        conversation_context: Optional[ConversationContext] = None,
        policy: Optional[PolicyView] = None,
        budget: Optional[RuntimeBudget] = None,
        persona_id: str = "dududa_default",
    ) -> ContextSnapshot:
        # 权限
        permissions: list[str] = []
        if envelope.sender.is_privileged():
            permissions.append("admin")

        # Memory
        authorized_memories: list[Any] = []
        if self._memory_repo is not None:
            from .memory import MemoryScope as MemScope, MemoryType
            scope = MemScope(
                memory_type=MemoryType.SHORT_TERM,
                platform=envelope.platform.value,
                bot_id="dududa",
                conversation_id=envelope.conversation.conversation_id,
                actor_id=envelope.sender.actor_id,
            )
            results = self._memory_repo.query(scope)
            bot_scope = MemScope(
                memory_type=MemoryType.BOT_UTTERANCE,
                platform=envelope.platform.value,
                bot_id="dududa",
                conversation_id=envelope.conversation.conversation_id,
                actor_id=envelope.sender.actor_id,
            )
            profile_scope = MemScope(
                memory_type=MemoryType.USER_PROFILE,
                platform=envelope.platform.value,
                bot_id="dududa",
                conversation_id=envelope.conversation.conversation_id,
                actor_id=envelope.sender.actor_id,
            )
            results = tuple(sorted(
                (*results, *self._memory_repo.query(bot_scope),
                 *self._memory_repo.query(profile_scope)),
                key=lambda item: item.created_at,
                reverse=True,
            ))
            max_items = (budget.max_context_tokens // 200) if budget else 20
            authorized_memories = list(results[:max_items])

        # Capability summaries
        capability_summaries: tuple[str, ...] = ()
        if self._capability_registry is not None:
            capability_summaries = self._capability_registry.summaries()

        # 用户画像 / 会话状态（文档 2.4.6：SESSION_STATE / USER_PROFILE）
        user_pref: Optional[UserPreference] = None
        conv = conversation_context or ConversationContext()
        if self._profile_store is not None:
            try:
                actor = envelope.sender.actor_id
                conv_id = envelope.conversation.conversation_id
                user = self._profile_store.get_user(
                    envelope.platform.value, "dududa", actor)
                if user is not None:
                    user_pref = UserPreference(
                        actor_id=actor,
                        preferred_name=user.preferred_name or None,
                        remembered_facts=user.facts,
                        preferences=user.preferences,
                    )
                sess = self._profile_store.get_session(conv_id, actor)
                if sess is not None and sess.active_topics:
                    conv = ConversationContext(
                        recent_messages=conv.recent_messages,
                        active_topics=sess.active_topics,
                        reply_chain_summary=conv.reply_chain_summary,
                    )
            except Exception:
                pass  # 画像存储异常不阻断推理

        # 用户 style（文档 2.5.8）：四维键具名 selector，投影到 UserPreference.style
        if self._style_store is not None:
            try:
                style = self._style_store.get(
                    envelope.platform.value, "dududa",
                    envelope.sender.actor_id,
                    persona_id or "dududa_default")
                if (style is not None
                        and style.visible_in_context(
                            envelope.conversation.conversation_id,
                            is_group=envelope.kind.value == "group")):
                    style_lines = style.summary_lines()
                    if style_lines:
                        base = user_pref
                        user_pref = UserPreference(
                            actor_id=envelope.sender.actor_id,
                            style="\n".join(style_lines),
                            preferred_name=base.preferred_name if base else None,
                            remembered_facts=base.remembered_facts if base else (),
                            preferences=base.preferences if base else (),
                        )
            except Exception:
                pass  # style 存储异常不阻断推理

        return ContextSnapshot(
            current_message=envelope,
            conversation=conv,
            policy=policy or PolicyView(),
            user_preference=user_pref,
            authorized_memories=tuple(authorized_memories),
            available_capability_summaries=capability_summaries,
            permissions=tuple(permissions),
        )
