# -*- coding: utf-8 -*-
"""Phase 4 拆分：应用用例层（DududaCore）。

不依赖 astrbot.api / star / AstrMessageEvent 类型；事件对象通过窄接口
（get_sender_id / get_session_id / message_obj / ...）传入。
依赖（memory / personas / renderer / provider / config）由 Main（适配器层）
装配注入，core 只通过注入对象与 config 工作。
"""
import os
import re
import json as _json
import logging
import random
import time
import httpx
from urllib.parse import urlparse

from dududa.core.trace_recorder import trace_recorder

from dududa.core.state import SocialAction, WriteGateDecision
from dududa.core.decision import DecisionReason
from dududa.core.memory import (
    MemoryCandidate, MemoryRecord, MemoryType, MemoryScope,
    SensitivityLevel, WriteGate,
)
from dududa.core.perception import PerceptionResult, SpeechAct, EntityRef
from dududa.core.renderer import DraftResponse, Persona as OCPersona
from dududa.router.router import ModelConfig, ModelRole, ModelError, ModelRequest
from dududa.core.envelope import Actor, Platform
from dududa.safeguards.security import (
    AuthorizationDecision, AuthorizationResult, AuthReason,
)

from dududa.application.dududa_utils import (
    _redact_text, _contains_restricted, _atomic_write_json,
    _has_media_in_raw, _IGNORE_PATTERNS, _is_greeting_text,
)

from dududa.application.dududa_log import get_logger as _get_logger
from dududa.application.user_experience import make_support_id
logger = _get_logger("dududa20")


def persona_to_oc(template):
    """PersonaTemplate -> OC Persona（纯转换，供装配层与用例层使用）。"""
    t = getattr(template, "traits", None)
    trait_names = {
        "warmth": "温暖", "assertiveness": "有主见", "humor": "幽默",
        "curiosity": "好奇", "politeness": "有分寸", "sassiness": "略嘴欠",
        "seriousness": "严谨",
    }
    traits = tuple(
        label for field, label in trait_names.items()
        if float(getattr(t, field, 0.0) or 0.0) >= 0.6
    ) if t else ()
    ft = getattr(template, "forbidden_topics", None)
    try:
        forbidden = tuple(ft) if ft else ()
    except TypeError:
        forbidden = ()
    return OCPersona(
        persona_id=getattr(template, "persona_id", "default"),
        version=getattr(template, "version", "1.0"),
        name=getattr(template, "display_name", "YmaKmern") or "YmaKmern",
        traits=traits,
        speaking_style=getattr(template, "speaking_style", "") or "",
        forbidden_topics=forbidden,
    )


_GROUP_SENSITIVE_ASKS = (
    "我的成绩", "我成绩", "我的课表", "我课表", "我的位置",
    "我的健康", "我健康", "私聊发我", "私聊我",
)


def _is_group_sensitive_ask(text: str) -> bool:
    """群聊隐私门（文档 2.5.9）：课表/成绩/健康/位置/私聊 类请求群聊默认不返回。"""
    t = (text or "").lower()
    return any(p in t for p in _GROUP_SENSITIVE_ASKS)


class DududaCore:
    """应用用例层：身份、权限、决策、感知、记忆、渲染与模型调用。"""

    _MEMORY_STRATEGIES = {
        "text":   MemoryType.SHORT_TERM,
        "bot":    MemoryType.BOT_UTTERANCE,
        "file":   MemoryType.EPISODIC,
        "image":  MemoryType.EPISODIC,
        "group":  MemoryType.GROUP_MEMORY,
    }

    def __init__(self, *, memory, personas, renderer, oc_renderer,
                 permission_engine, confirmations, cap_registry,
                 context_builder, input_adapter, llm_provider, config,
                 model_router=None, group_policy=None):
        self._memory = memory
        self._personas = personas
        self._renderer = renderer
        self._oc_renderer = oc_renderer
        self._permission_engine = permission_engine
        self._confirmations = confirmations
        self._cap_registry = cap_registry
        self._context_builder = context_builder
        self._input_adapter = input_adapter
        self._llm_provider = llm_provider
        self._model_router = model_router  # 8 类角色路由（文档 2.5.7），None = 旧路径
        self._group_policy = group_policy  # 群策略仓库（文档 2.5.2/2.5.4），None = 不启用
        self._cfg = config  # 保持引用：适配层可用动态代理（monkeypatch 兼容）
        self._pending_confirms = {}
        self._react_cooldown: dict = {}  # 群聊问候 10s 冷却（文档 2.5.4）
        self._load_confirmations()

    # ---- 身份与 Bot 隔离 ----

    def _is_self_message(self, event) -> bool:
        try:
            bot_id = str(event.get_self_id())
        except Exception:
            try:
                bot_id = str(getattr(event.message_obj, "self_id", "0"))
            except Exception:
                return False
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = str(getattr(getattr(event.message_obj, "sender", None), "user_id", "0"))
        return sender == bot_id

    def _get_bot_id(self, event) -> str:
        # Per-event bot_id for multi-bot isolation
        try:
            return str(event.get_self_id())
        except Exception:
            return str(getattr(event.message_obj, "self_id", "0"))

    # ---- 权限与持久确认 ----

    def _actor_for(self, event):
        """QQ 原始用户 -> 平台无关 Actor（角色由环境配置 + 群管理员边界决定）。"""
        uid = str(event.get_sender_id())
        role = "normal"
        if uid in self._cfg["MUTED_IDS"]:
            role = "muted"
        elif uid in self._cfg["OWNER_IDS"]:
            role = "owner"
        elif uid in self._cfg["ADMIN_IDS"]:
            role = "admin"
        elif uid in self._cfg["TRUSTED_IDS"]:
            role = "trusted"
        try:
            is_group = bool(getattr(event.message_obj, "group", None))
        except Exception:
            is_group = False
        if is_group and role == "normal":
            # 群级管理员边界：群主/管理员仅在群 Scope 内视为 admin
            try:
                sender_role = str(getattr(
                    getattr(event.message_obj, "sender", None), "role", "")).lower()
            except Exception:
                sender_role = ""
            if sender_role in ("owner", "admin"):
                role = "admin"
        try:
            nickname = str(getattr(event.get_sender(), "nickname", "") or "user")
        except Exception:
            nickname = "user"
        return Actor(actor_id=uid, platform=Platform.QQ,
                     display_name=nickname, role=role)

    def _scope_key(self, event, resource="") -> str:
        return f"{self._get_bot_id(event)}|{event.get_session_id()}|{resource}"

    @staticmethod
    def _same_scope_prefix(a: str, b: str) -> bool:
        return a.split("|", 2)[:2] == b.split("|", 2)[:2]

    def _authorize(self, event, action, resource="", payload=None,
                   capability_risk=None, requires_confirmation=False):
        actor = self._actor_for(event)
        scope_key = self._scope_key(event, resource)
        return self._permission_engine.authorize(
            actor, action, scope_key=scope_key, resource=resource,
            capability_risk=capability_risk,
            requires_confirmation=requires_confirmation)

    def _confirm_key(self, event, resource, payload) -> str:
        actor = self._actor_for(event)
        digest = self._confirmations.digest({"resource": resource, **payload})
        return f"{actor.actor_id}|{self._scope_key(event, resource)}|{digest}"

    def _authorize_manage(self, event, resource, payload):
        """管理操作授权：owner/admin 放行；trusted 走持久确认流；其余拒绝。"""
        actor = self._actor_for(event)
        if actor.is_muted():
            return (AuthorizationResult(
                AuthorizationDecision.DENY, (AuthReason.MUTED,)), None)
        if actor.role in ("owner", "admin"):
            return (AuthorizationResult(
                AuthorizationDecision.ALLOW,
                (AuthReason.OWNER_ALLOWED if actor.role == "owner"
                 else AuthReason.ROLE_ALLOWED,)), None)
        if actor.role == "trusted":
            key = self._confirm_key(event, resource, payload)
            conf = self._pending_confirms.get(key)
            if conf is not None:
                if self._consume_confirm(event, conf, resource, payload):
                    return (AuthorizationResult(
                        AuthorizationDecision.ALLOW,
                        (AuthReason.CONFIRMATION_OK,)), conf)
                if conf.is_expired or conf.is_consumed:
                    self._pending_confirms.pop(key, None)
                    self._save_confirmations()
                return (AuthorizationResult(
                    AuthorizationDecision.DENY, (AuthReason.ROLE_TOO_LOW,)), conf)
            conf = self._create_confirmation(event, resource, payload)
            return (AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                (AuthReason.CONFIRMATION_REQUIRED,)), conf)
        return (AuthorizationResult(
            AuthorizationDecision.DENY, (AuthReason.ROLE_TOO_LOW,)), None)

    def _create_confirmation(self, event, resource, payload):
        actor = self._actor_for(event)
        scope_key = self._scope_key(event, resource)
        conf = self._confirmations.create(
            actor, scope_key, "manage_config",
            {"resource": resource, **payload})
        self._pending_confirms[self._confirm_key(event, resource, payload)] = conf
        self._save_confirmations()
        return conf

    def _consume_confirm(self, event, conf, resource, payload) -> bool:
        actor = self._actor_for(event)
        scope_key = self._scope_key(event, resource)
        res = self._confirmations.consume(
            conf.confirmation_id, actor, scope_key,
            {"resource": resource, **payload})
        if res.allowed:
            self._save_confirmations()
        return res.allowed

    def _load_confirmations(self):
        """持久确认：进程重启后恢复未消费的确认。"""
        self._pending_confirms = {}
        try:
            if os.path.exists(self._cfg["CONFIRM_FILE"]):
                with open(self._cfg["CONFIRM_FILE"], "r", encoding="utf-8") as f:
                    data = _json.load(f)
                self._confirmations.restore(data.get("confirmations", []) or [])
                self._confirmations.prune()
            for item in self._confirmations.dump():
                if item.get("consumed_at"):
                    continue
                conf = self._confirmations.get(item["confirmation_id"])
                if conf is None:
                    continue
                key = f"{conf.actor_id}|{conf.scope_key}|{conf.payload_digest}"
                self._pending_confirms[key] = conf
        except Exception as e:
            logger.warning("Confirm load: %s", e)

    def _save_confirmations(self):
        try:
            self._confirmations.prune()
            _atomic_write_json(self._cfg["CONFIRM_FILE"],
                               {"confirmations": self._confirmations.dump()})
        except Exception as e:
            logger.warning("Confirm save: %s", e)

    # ---- 决策与感知 ----

    def _should_ignore(self, event) -> bool:
        try:
            if _has_media_in_raw(event): return False
            msgs = event.get_messages()
            if msgs and any("File" in str(getattr(c,"type","")) or "Image" in str(getattr(c,"type","")) for c in msgs):
                return False
            obj = getattr(event, "message_obj", None)
            is_group = bool(
                getattr(event, "group_id", None)
                or getattr(obj, "group_id", None)
                or getattr(obj, "group", None))
            if is_group and not getattr(event, "is_at_or_wake_command", True):
                # A non-zero reply_rate is an explicit per-group opt-in to
                # passive participation.  Keep it reachable; the social
                # decision engine owns the actual probability draw.
                getter = getattr(self, "_group_policy_for", None)
                policy = getter(event) if callable(getter) else None
                return not bool(
                    policy is not None
                    and policy.mode == "normal"
                    and policy.reply_rate > 0.0
                    and policy.interruption_cost < 1.0)
            if not is_group:
                text = (event.message_str or "").strip()
                if not text: return True
                if text in _IGNORE_PATTERNS: return True
        except Exception: pass
        return False

    def _group_policy_for(self, event):
        """当前群策略；未配置返回 None（调用方保持原有行为）。

        支持 GroupPolicyStore 实例或 callable(group_id) -> GroupPolicy|None。
        """
        try:
            gp = self._group_policy
            if gp is None:
                return None
            obj = getattr(event, "message_obj", None)
            raw_group = (getattr(event, "group_id", None)
                         or getattr(obj, "group_id", None)
                         or getattr(obj, "group", None))
            gid = str(getattr(raw_group, "group_id", None)
                      or getattr(raw_group, "id", None)
                      or raw_group or "")
            if not gid:
                return None
            if callable(gp):
                return gp(gid)
            getter = getattr(gp, "get", None)
            if getter is None:
                return None
            return getter(gid)
        except Exception:
            return None

    _TOOL_KW = ("帮我", "查", "搜", "算", "翻译", "了解", "介绍",
                 "是什么", "多少", "招生", "分数线", "排名",
                 "查询", "查查", "百度", "搜索", "找一下",
                 "新闻", "资讯", "热点", "天气", "气温", "下雨", "翻译一下",
                 "开课", "课程号", "谁教", "哪个老师", "上课时间", "上课地点")

    def _social_decision(self, event) -> tuple:
        try:
            return self._social_decision_impl(event)
        except Exception:
            # 生产兜底：任何异常都回落到普通回答，不吞消息也不崩
            return SocialAction.ANSWER, "normal"

    def _social_decision_impl(self, event) -> tuple:
        try:
            pre = self._input_adapter.to_preprocessed(event)
            combined = pre.combined_text.strip() if pre and pre.combined_text else ""
        except Exception:
            return SocialAction.ANSWER, "fallback"
        obj = getattr(event, "message_obj", None)
        is_group = bool(
            getattr(event, "group_id", None)
            or getattr(obj, "group_id", None)
            or getattr(obj, "group", None))
        if not is_group:
            clean_0 = re.sub(r"@\S+", "", combined).strip()
            if any(kw in clean_0 for kw in self._TOOL_KW):
                return SocialAction.USE_TOOLS, DecisionReason.EXPLICIT_COMMAND.value
            return SocialAction.DIRECT_REPLY, DecisionReason.HIGH_RELEVANCE.value
        # 群策略（文档 2.5.2/2.5.4）：mode / reply_rate / meme_rate 落地到回复策略
        policy = self._group_policy_for(event)
        if policy is not None and policy.mode == "off":
            return SocialAction.IGNORE, DecisionReason.GROUP_MODE_OFF.value
        # 群聊隐私门（文档 2.5.9）：课表/成绩/健康/位置/私聊 类请求群聊默认不返回
        if _is_group_sensitive_ask(combined):
            return SocialAction.IGNORE, DecisionReason.SENSITIVE_GROUP_REQUEST.value
        ambient_reason = ""
        try:
            ambient_reason = str(
                event.get_extra("dududa_ambient_reason_code") or "")
        except Exception:
            ambient_reason = str(getattr(
                event, "_dududa_ambient_reason_code", "") or "")
        if ambient_reason:
            return SocialAction.DIRECT_REPLY, ambient_reason
        mentioned = bool(getattr(event, "is_at_or_wake_command", True))
        if not mentioned:
            # 未点名群消息只能由 handlers 的 ambient 链提升；这里不再
            # 维护另一套均匀随机入口。
            return SocialAction.IGNORE, DecisionReason.LOW_RELEVANCE.value
        clean = re.sub(r"@\S+", "", combined).strip()
        # 显式工具/命令意图 -> USE_TOOLS（与 _perceive 的 command 词一致）
        if any(kw in clean for kw in self._TOOL_KW):
            return SocialAction.USE_TOOLS, DecisionReason.EXPLICIT_COMMAND.value
        # 纯问候/单表情 -> REACT（同会话 10s 冷却，文档 2.5.4 速率冷却）
        # meme_rate 控制表情回复比例；未命中回退文本回复（保证 @ 必回）。
        # 短名词（USTC/AI/课程名）不属于问候，走 DIRECT_REPLY 解释含义
        if len(clean) <= 1 or _is_greeting_text(clean):
            if (policy is not None and policy.meme_rate < 1.0
                    and random.random() >= policy.meme_rate):
                return SocialAction.DIRECT_REPLY, DecisionReason.GREETING_ONLY.value
            return self._react_with_cooldown(event)
        # 问句 -> DIRECT_REPLY
        if any(clean.endswith(q) for q in ("?", "？", "吗", "呢", "嘛", "么")):
            return SocialAction.DIRECT_REPLY, DecisionReason.DIRECT_MENTION.value
        return SocialAction.DIRECT_REPLY, DecisionReason.DIRECT_MENTION.value

    def _react_with_cooldown(self, event) -> tuple:
        now = time.time()
        conv = str(event.get_session_id())
        last = self._react_cooldown.get(conv, 0.0)
        if now - last < 10.0:
            return SocialAction.IGNORE, DecisionReason.COOLDOWN_ACTIVE.value
        self._react_cooldown[conv] = now
        return SocialAction.REACT, DecisionReason.GREETING_ONLY.value

    def _perceive(self, event) -> PerceptionResult:
        try:
            pre = self._input_adapter.to_preprocessed(event)
            combined = pre.combined_text.strip() if pre and pre.combined_text else ""
        except Exception:
            return PerceptionResult(confidence=0.0, ambiguities=("preprocess_failed",))
        if not combined:
            return PerceptionResult(confidence=0.3, ambiguities=("empty_text",))
        acts = []
        if any(combined.endswith(q) for q in ("?", "？", "吗", "呢", "嘛", "么")):
            acts.append(SpeechAct(act_type="question", confidence=0.8))
        if combined.startswith("/") or any(kw in combined for kw in self._TOOL_KW):
            acts.append(SpeechAct(act_type="command", confidence=0.7))
        if not acts:
            if _is_greeting_text(combined):
                acts.append(SpeechAct(act_type="greeting", confidence=0.5))
            else:
                acts.append(SpeechAct(act_type="statement", confidence=0.5))
                # 短名词/短语（无标点无空白）默认视为询问含义
                if len(combined) <= 16 and not re.search(
                        r"[，。！？、\s：:；;]", combined):
                    acts.append(SpeechAct(act_type="noun_query", confidence=0.6))
        entities = []
        for m in re.finditer(r"@([^\s@]+)", combined):
            # AstrBot may render a QQ mention as ``@昵称(123456789)``.  Keep
            # the visible nickname as the entity and never copy the numeric
            # platform id into perception evidence.
            name = re.sub(r"\(\d{5,12}\)$", "", m.group(1)).strip()
            if name:
                entities.append(EntityRef(
                    name=name, entity_type="person", confidence=0.9,
                    evidence=f"@{name}"))
        topics = []
        topic_kw = {"课程": "course", "开课": "course", "课程号": "course",
                    "上课时间": "course", "上课地点": "course",
                    "考试": "exam", "作业": "homework", "天气": "weather",
                    "文件": "file", "图片": "image", "成绩": "grade", "食堂": "canteen", "图书馆": "library",
                    "几点": "time", "时间": "time", "几号": "time", "星期几": "time",
                    "日期": "time", "什么时候了": "time", "现在是": "time", "现在几": "time",
                    "通知": "notice", "公告": "notice", "校历": "calendar", "放假": "calendar",
                    "节假日": "calendar", "学期": "calendar", "活动": "activity", "讲座": "activity",
                    "竞赛": "activity", "社团": "activity", "第二课堂": "activity",
                    "培养方案": "training", "毕业要求": "training", "选课": "training",
                    "新闻": "news", "资讯": "news", "热点": "news", "热搜": "news",
                    "学分": "training", "绩点": "grade", "分数": "grade",
                    "翻译": "translate", "翻译成": "translate", "译成": "translate",
                    "招生": "websearch", "录取": "websearch", "百科": "websearch",
                    "是什么": "websearch", "什么是": "websearch",
                    "啥是": "websearch", "啥叫": "websearch"}
        for kw, topic in topic_kw.items():
            if kw in combined:
                topics.append(topic)
        review_markers = (
            "评课", "课程评价", "值得选", "推荐吗", "推荐老师",
            "给分怎么样", "作业多吗", "难不难", "好不好", "怎么样",
        )
        if ("评课" in combined
                or (("老师" in combined or "课程" in combined or "课" in combined)
                    and any(marker in combined for marker in review_markers))):
            topics.append("course_review")
        intents = list(topics) if topics else ["chitchat"]
        # 工具意图门：命令词（_TOOL_KW）命中即触发工具链，与 _social_decision 对齐，
        # 避免「帮我查一下/查查XX」被判为纯闲聊；工具话题命中同样触发。
        has_command = any(a.act_type == "command" for a in acts)
        needs_tools = has_command or any(t in ("course", "course_review", "exam", "grade", "weather",
                                      "time", "notice", "activity", "calendar",
                                      "training", "news", "translate", "websearch")
                                      for t in topics)
        return PerceptionResult(
            speech_acts=tuple(acts),
            topics=tuple(topics),
            entities=tuple(entities),
            candidate_intents=tuple(intents),
            needs_tools=needs_tools,
            is_explicit_command=has_command,
            confidence=0.6,
        )

    # ---- 记忆 ----

    def _make_scope(self, event, msg_type="text") -> MemoryScope:
        mem_type = self._MEMORY_STRATEGIES.get(msg_type, MemoryType.SHORT_TERM)
        return MemoryScope(
            memory_type=mem_type, platform="qq",
            bot_id=self._get_bot_id(event),
            conversation_id=str(event.get_session_id()),
            actor_id=str(event.get_sender_id()),
            persona_id=self._personas.active_id,
        )

    def _store_memory(self, event, *contents: str, msg_type="text",
                      sensitivity=None, run_id="", trace_id=""):
        """写入记忆：先脱敏；Restricted 数据不落盘；私聊默认 PRIVATE。

        所有写入都经 WriteGate（文档 2.5.3）：ALLOW 才落盘，
        REJECT / REQUIRE_CONFIRMATION / DEFER_FOR_CONFLICT 一律不写。
        """
        try:
            scope = self._make_scope(event, msg_type=msg_type)
            if sensitivity is None:
                is_group = bool(getattr(event.message_obj, "group", None))
                sensitivity = (SensitivityLevel.INTERNAL if is_group
                               else SensitivityLevel.PRIVATE)
            recent_texts = {m.content for m in self._memory.query(scope, limit=10)}
            for c in contents:
                c = _redact_text(c or "").strip()
                if not c: continue
                if _contains_restricted(c):
                    logger.warning("Restricted content skipped (not stored)")
                    continue
                if len(c) > 3000: c = c[:3000]
                if c in recent_texts: continue
                recent_texts.add(c)
                record = MemoryRecord(
                    scope=scope,
                    source=("bot" if msg_type == "bot" else "message"),
                    content=c,
                    sensitivity=sensitivity, visibility=sensitivity,
                    evidence=(f"src:{msg_type}",))
                decision = WriteGate(self._memory).evaluate(
                    MemoryCandidate(proposed_record=record,
                                    metadata={"run_id": run_id,
                                              "trace_id": trace_id}))
                if decision == WriteGateDecision.ALLOW:
                    self._memory.write(record)
                else:
                    logger.debug("Memory write %s (skipped): %.60s",
                                 decision.value, c)
        except Exception as e: logger.warning("Memory write: %s", e)

    def _read_memory(self, event, limit=8, budget=2500, include_episodic=False):
        try:
            scope = self._make_scope(event)
            viewer = str(event.get_sender_id())
            recent = list(self._memory.query_visible(
                scope, viewer_actor_id=viewer, limit=limit))
            bot_scope = self._make_scope(event, msg_type="bot")
            recent += list(self._memory.query_visible(
                bot_scope, viewer_actor_id=viewer, limit=max(2, limit // 2)))
            profile_scope = MemoryScope(
                memory_type=MemoryType.USER_PROFILE,
                platform=scope.platform,
                bot_id=scope.bot_id,
                conversation_id=scope.conversation_id,
                actor_id=scope.actor_id,
                persona_id=scope.persona_id,
            )
            recent += list(self._memory.query_visible(
                profile_scope, viewer_actor_id=viewer, limit=2))
            if bool(getattr(event.message_obj, "group", None)):
                group_scope = MemoryScope(
                    memory_type=MemoryType.GROUP_MEMORY,
                    platform=scope.platform,
                    bot_id=scope.bot_id,
                    conversation_id=scope.conversation_id,
                    actor_id="group",
                    persona_id=scope.persona_id,
                )
                recent += list(self._memory.query_visible(
                    group_scope, viewer_actor_id=viewer, limit=2))
            if include_episodic:
                epi_scope = self._make_scope(event, msg_type="file")
                recent += list(self._memory.query_visible(
                    epi_scope, viewer_actor_id=viewer, limit=4))
            seen = set()
            deduped = []
            for m in sorted(recent, key=lambda item: item.created_at):
                if m.content not in seen:
                    seen.add(m.content)
                    deduped.append(m)
            recent = deduped[-limit:]
            if not recent: return ""
            files = [m for m in recent if "[文件" in m.content[:20] or "[图片" in m.content[:20]]
            chats = [m for m in recent if m not in files]
            ordered = files + chats
            lines, used = [], 0
            for m in ordered:
                snippet = _redact_text(m.content[:600])
                if m.scope.memory_type == MemoryType.BOT_UTTERANCE:
                    snippet = f"YmaKmern: {snippet}"
                elif m.scope.memory_type == MemoryType.GROUP_MEMORY and m.scope.actor_id == "group":
                    snippet = f"群聊话题摘要: {snippet}"
                lines.append(snippet)
                used += len(snippet)
                if used >= budget: break
            return "【近期对话】\n" + "\n---\n".join(lines) + "\n======\n"
        except Exception as e:
            logger.warning("Memory read: %s", e); return ""

    # ---- 渲染与模型 ----

    def _persona_to_oc(self, template):
        return persona_to_oc(template)

    def _render_response(self, raw_text: str, persona_tone: str = "", anchors=()) -> str:
        draft = DraftResponse(text=raw_text, fact_anchors=anchors)
        final = self._oc_renderer.render(draft)
        if final.fact_check_passed:
            return final.text
        return self._renderer.render(raw_text or "", persona_tone)

    def _persona_tone(self):
        p = self._personas.active
        return getattr(p, "tone", "neutral")

    async def _call_llm(self, system, user_msg, max_tokens=1024, temperature=0.5,
                        run_id="", trace_id="", skip_render=False):
        system = _redact_text(system or "")
        user_msg = _redact_text(user_msg or "")
        if _contains_restricted(user_msg):
            logger.warning("Restricted content blocked from LLM")
            return "这类敏感信息我不能处理哦，请不要发送密码、Token、Cookie 或登录凭证。"
        msgs = [{"role":"system","content":system},{"role":"user","content":user_msg}]
        # Primary: 角色化 Model Router（文档 2.5.7：RESPONSE_COMPOSITION + 降级）
        if self._model_router is not None:
            try:
                resp = await self._model_router.route_request(
                    ModelRequest(
                        role=ModelRole.RESPONSE_COMPOSITION, messages=msgs,
                        max_tokens=max_tokens, temperature=temperature,
                        metadata={"run_id": run_id, "trace_id": trace_id}),
                    provider=self._llm_provider,
                )
                reply = resp.text or ""
                if resp.degraded:
                    logger.warning("Router degraded for %s via %s",
                                   ModelRole.RESPONSE_COMPOSITION.value,
                                   resp.model_id)
            except ModelError as e:
                logger.warning("Router %s failed (%s), trying fallback...",
                               ModelRole.RESPONSE_COMPOSITION.value,
                               e.stable_code)
                reply = ""
            if reply:
                if not skip_render:
                    reply = self._render_response(reply, self._persona_tone())
                return reply or ""
        else:
            # 无 Router 装配（兼容/测试）：旧主路径
            try:
                reply = await self._llm_provider.complete(self._cfg["MODEL"], msgs,
                    ModelConfig(role=ModelRole.COMPOSER, model_id=self._cfg["MODEL"],
                                max_tokens=max_tokens, temperature=temperature))
                if not skip_render:
                    reply = self._render_response(reply or "", self._persona_tone())
                return reply or ""
            except Exception as e:
                logger.warning("Primary LLM (%s) failed: %s, trying fallback...", self._cfg["MODEL"], e)
        # Fallback: MHCoding GPT-5.5 via httpx
        try:
            try:
                _fb_base = str(self._cfg["FALLBACK_BASE"] or "").strip().rstrip("/")
            except (KeyError, TypeError, AttributeError):
                _fb_base = ""
            if _fb_base and _fb_base.count("/") == 2:
                _fb_base += "/v1"  # OpenAI 兼容网关 API 路径在 /v1 下
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{_fb_base}/chat/completions",
                    headers={"Authorization": f"Bearer {self._cfg['FALLBACK_KEY']}",
                             "Content-Type": "application/json"},
                    json={"model": self._cfg["FALLBACK_MODEL"], "messages": msgs,
                          "max_tokens": max_tokens, "temperature": temperature},
                )
                r.raise_for_status()
                try:
                    reply = r.json()["choices"][0]["message"]["content"]
                except Exception:
                    logger.error(
                        "Fallback non-JSON response from %s: %.150s",
                        _fb_base,
                        (r.text or "")[:150])
                    raise
            if not skip_render:
                reply = self._render_response(reply or "", self._persona_tone())
            return reply or ""
        except Exception as e2:
            logger.exception("Fallback LLM also failed: %s", e2)
            support_id = make_support_id("llm", e2, trace_id)
            return ("模型服务暂时没有响应，主线路和备用线路都已尝试。"
                    "你可以稍后重试，或让我换一种更简单的方式回答。"
                    f"\n错误编号：{support_id}")

    def _vision_provider_policy(self) -> tuple[bool, str]:
        """Return whether the configured endpoint is third-party and its host."""
        try:
            base = str(self._cfg["VISION_BASE"] or "")
        except (KeyError, TypeError, AttributeError):
            base = ""
        host = (urlparse(base).hostname or "").lower()
        raw = os.environ.get(
            "DUDUDA_VISION_TRUSTED_HOSTS", "api.openai.com")
        if isinstance(raw, str):
            trusted = {item.strip().lower() for item in raw.split(",")
                       if item.strip()}
        else:
            trusted = {str(item).strip().lower() for item in (raw or ())
                       if str(item).strip()}
        return host not in trusted, host

    async def _call_vision(self, system, user_text, image_b64, mime,
                           run_id="", trace_id="", skip_render=False,
                           group_id="", external_opt_in=False,
                           private_request=False):
        system = _redact_text(system or "")
        user_text = _redact_text(user_text or "")
        if _contains_restricted(user_text):
            logger.warning("Restricted content blocked from vision")
            return "这类敏感信息我不能处理哦，请不要发送密码、Token 或登录凭证。"
        third_party, provider_host = self._vision_provider_policy()
        globally_allowed = (
            os.environ.get("DUDUDA_VISION_ALLOW_THIRD_PARTY", "0") == "1")
        if third_party and not (
                globally_allowed and (external_opt_in or private_request)):
            logger.warning(
                "Third-party vision blocked | host=%s group=%s global=%s opt_in=%s",
                provider_host or "unknown", group_id or "private",
                globally_allowed, external_opt_in)
            trace_recorder.record(
                event="model_rejected", run_id=run_id, trace_id=trace_id,
                role=ModelRole.IMAGE_UNDERSTANDING.value,
                model_id=self._cfg["VISION_MODEL"], data_class="sensitive",
                provider_host=provider_host, error_kind="vision_not_authorized")
            return "图片功能当前未授权向这个视觉服务发送数据。"
        try:
            body = {
                "model": self._cfg["VISION_MODEL"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime};base64,{image_b64}", "detail": "auto"}},
                        {"type": "text", "text": user_text},
                    ]},
                ],
                "max_tokens": 1024, "temperature": 0.3,
            }
            _v_start = time.time()
            trace_recorder.record(
                event="model_request", run_id=run_id, trace_id=trace_id,
                role=ModelRole.IMAGE_UNDERSTANDING.value,
                model_id=self._cfg["VISION_MODEL"], data_class="sensitive",
                provider_host=provider_host, third_party=third_party)
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(
                    f"{self._cfg['VISION_BASE'].rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._cfg['VISION_KEY']}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
                reply = data["choices"][0]["message"]["content"]
                trace_recorder.record(
                    event="model_response", run_id=run_id, trace_id=trace_id,
                    role=ModelRole.IMAGE_UNDERSTANDING.value,
                    model_id=self._cfg["VISION_MODEL"],
                    degraded=False,
                    latency_ms=round((time.time() - _v_start) * 1000, 1),
                    error_kind="")
                if not skip_render:
                    reply = self._render_response(reply or "", self._persona_tone())
                return reply or ""
        except Exception as e:
            logger.exception("Vision error: %s", e)
            trace_recorder.record(
                event="model_error", run_id=run_id, trace_id=trace_id,
                role=ModelRole.IMAGE_UNDERSTANDING.value,
                model_id=self._cfg["VISION_MODEL"], error_kind="vision_failed")
            return "(\u3002\u2022\u0301\ufe3f\u2022\u0300\u3002) \u56fe\u7247\u770b\u4e0d\u4e86\u2026"
