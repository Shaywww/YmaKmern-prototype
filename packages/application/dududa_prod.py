# -*- coding: utf-8 -*-
"""Phase 4 拆分：生产 Orchestrator / 决策引擎 / Capability Provider。

原 main.py 生产类原样迁移；通过 plugin（Main 实例）注入生产依赖。
"""
import logging

from packages.core.state import SocialAction, RuntimeState, RuntimePhase, RunOutcome, RuntimeBudget
from packages.core.renderer import FactAnchor, DraftResponse, FinalResponse
from packages.core.capability import CapProvider, ToolObservation
from packages.core.decision import SocialDecisionEngine, SocialDecision, DecisionReason
from packages.core.memory import MemoryCandidate, MemoryRecord, SensitivityLevel
from packages.runtime.orchestrator import RuntimeOrchestrator
from uuid import uuid4

from packages.application.dududa_utils import (
    _group_safe_observations, _redact_text, _contains_restricted,
)

from packages.application.dududa_log import get_logger as _get_logger
logger = _get_logger("dududa20")


class _ProdDecisionEngine(SocialDecisionEngine):
    """生产决策引擎：IGNORE/REACT 已由 _social_decision 前置过滤。

    工具话题 -> USE_TOOLS（走 Planner/MCP 工具链）；其余 -> ANSWER。
    """

    def decide(self, perception=None, context=None, now=None):
        needs = bool(perception and getattr(perception, "needs_tools", False))
        return SocialDecision(
            action=SocialAction.USE_TOOLS if needs else SocialAction.ANSWER,
            reason_codes=(DecisionReason.EXPLICIT_COMMAND if needs
                          else DecisionReason.HIGH_RELEVANCE,),
            confidence=0.9,
            should_use_tools=needs,
        )


class _ProdCapProvider(CapProvider):
    """把生产 _call_llm/_call_vision 包装为 CapProvider（chat/vision/file_reader）。"""

    def __init__(self, plugin, kind):
        self._plugin = plugin
        self._kind = kind

    async def execute(self, capability, arguments):
        try:
            text = str(arguments.get("text", "") or arguments.get("content", "") or "")
            if self._kind == "vision":
                reply = await self._plugin._call_vision(
                    f"{capability.name}。用户发来一张图片，请描述并提取文字。",
                    text,
                    str(arguments.get("image_b64", "")),
                    str(arguments.get("mime", "image/png")),
                )
            else:
                reply = await self._plugin._call_llm(
                    capability.name, text,
                    max_tokens=int(arguments.get("max_tokens", 1024)))
            return ToolObservation(
                step_id="", capability_id=capability.capability_id,
                success=bool(reply), data=reply or "", source="llm")
        except Exception as e:
            logger.warning("CapProvider %s error: %s", capability.capability_id, e)
            return ToolObservation(
                step_id="", capability_id=capability.capability_id,
                success=False, error=str(e))

    def health(self):
        return True


class _ProdOrchestrator(RuntimeOrchestrator):
    """生产版 Orchestrator。

    - 感知注入：使用生产 _perceive 的结果（话题 -> needs_tools）
    - 工具规划：仅执行 Planner 明确命中的意图模式，杜绝无关工具数据
    - 记忆作用域：per-event bot_id（多 bot 隔离），工具结果进 EPISODIC
    - 合成阶段：异步调用生产 _call_llm（人格 + 记忆前缀 + 工具数据）
    """

    def __init__(self, plugin, decision_engine, capability_registry, memory_repo,
                 renderer, planner_integration):
        super().__init__(
            context_builder=plugin.context_builder,
            decision_engine=decision_engine,
            capability_registry=capability_registry,
            memory_repo=memory_repo,
            renderer=renderer,
            planner_integration=planner_integration,
        )
        self._plugin = plugin
        self._pending_event = None
        self._injected_perception = None
        if self._tool_chain is not None:
            try:
                # 生产补充意图模式：'查一下XX课程' 类口语化查询
                self._tool_chain.planner.register_pattern(
                    ("查一下", "查查", "搜一下", "搜搜", "帮我查", "帮我搜", "查", "搜"),
                    {"name": "course_search", "goal": "Search courses by keyword",
                     "steps": [{"step_id": "s1", "capability_id": "mcp.course_schedule",
                                "arguments": {"action": "search"},
                                "purpose": "Search courses by keyword"}]},
                )
            except Exception:
                pass

    async def run(self, envelope, budget=None, policy=None,
                  perception=None, event=None, run_id=None, trace_id=None):
        """镜像 RuntimeOrchestrator.run()，但 COMPOSED 阶段改为生产异步合成。"""
        budget = budget or RuntimeBudget()
        self._pending_event = event
        self._injected_perception = perception
        if hasattr(envelope, "envelope"):
            envelope = envelope.envelope
        state = RuntimeState(
            envelope=envelope, budget=budget,
            run_id=run_id or uuid4().hex,
            trace_id=trace_id or uuid4().hex,
        )
        try:
            state = self._phase_validate(state)
            if state.phase == RuntimePhase.ABORTED:
                self._last_state = state
                return self._result_from_state(state, state.outcome or RunOutcome.FAILED)
            state = self._phase_preprocess(state)
            state = self._phase_scope(state)
            state = self._phase_context(state, policy)
            state = self._phase_perceive(state)
            state = self._phase_decide(state)
            if state.social_decision == SocialAction.BLOCK:
                self._last_state = state
                return self._result_from_state(state, RunOutcome.ABORTED)
            if state.social_decision == SocialAction.IGNORE:
                self._last_state = state
                return self._result_from_state(state, RunOutcome.IGNORED)
            limits = getattr(self._plugin, "limits", None)
            if limits is not None:
                actor_key = self._actor_key(state)
                rate = limits.check_message(
                    actor_key, run_id=state.run_id, trace_id=state.trace_id)
                if not rate.allowed:
                    # 2.5.9 Runtime 限流：不进入工具/LLM，直接返回提示
                    state = state.transition(
                        RuntimePhase.RENDERED,
                        final_response=FinalResponse(
                            text=getattr(limits, "RATE_LIMIT_HINT",
                                         "稍等一下再问我哦～")),
                        outcome=RunOutcome.SUCCEEDED)
                    self._last_state = state
                    return self._result_from_state(state, RunOutcome.SUCCEEDED)
            if state.social_decision in (
                SocialAction.ANSWER, SocialAction.ASK, SocialAction.USE_TOOLS,
            ):
                state = self._phase_list_capabilities(state)
            if self._should_use_tools(state):
                state = await self._phase_tool_chain(state)
                if state.outcome == RunOutcome.FAILED:
                    self._last_state = state
                    return self._result_from_state(state, RunOutcome.FAILED)
            else:
                state = state.transition(RuntimePhase.COMPOSED)
            state = await self._phase_compose_prod(state)
            if limits is not None:
                draft = state.draft_response
                if draft is not None:
                    est_tokens = max(
                        1, (len(draft.text or "")
                            + len(getattr(state.envelope, "text", "") or "")) // 4)
                    budget = limits.spend_tokens(
                        actor_key, est_tokens,
                        run_id=state.run_id, trace_id=state.trace_id)
                    if not budget.allowed:
                        # 2.5.9 日预算耗尽：替换草稿为提示，不继续消费模型
                        state = state.transition(
                            RuntimePhase.COMPOSED,
                            draft_response=DraftResponse(
                                text=getattr(limits, "BUDGET_HINT",
                                             "今天的对话额度用完啦～"),
                                fact_anchors=draft.fact_anchors,
                                target_users=draft.target_users))
            state = await self._phase_render(state)
            state = self._with_updates(
                state, memory_candidates=self._build_memory_candidates(state)
            )
            outcome = state.outcome or RunOutcome.SUCCEEDED
            state = state.transition(RuntimePhase.READY_TO_EMIT, outcome=outcome)
            self._last_state = state
            return self._result_from_state(state, outcome)
        except Exception as e:
            state = state.with_error(str(e)).transition(
                RuntimePhase.ABORTED, outcome=RunOutcome.FAILED)
            self._last_state = state
            return self._result_from_state(state, RunOutcome.FAILED)

    @staticmethod
    def _actor_key(state) -> str:
        """限流/预算的 key：会话 Actor（envelope.sender.actor_id）。"""
        try:
            sender = getattr(state.envelope, "sender", None)
            if sender is not None and getattr(sender, "actor_id", ""):
                return str(sender.actor_id)
        except Exception:
            pass
        return "unknown"

    def _phase_perceive(self, state):
        if self._injected_perception is not None:
            return state.transition(RuntimePhase.PERCEIVED,
                                    perception=self._injected_perception)
        return super()._phase_perceive(state)

    def _plan(self, state, candidates, max_steps, permissions):
        """生产：仅执行 Planner 明确命中的意图模式，杜绝无关工具数据。"""
        if self._tool_chain is not None:
            from packages.planner.planner import PlanningContext
            try:
                intent = self._intent_of(state)
                plan = self._tool_chain.planner.plan(PlanningContext(
                    user_intent=intent,
                    available_capabilities=candidates,
                    max_steps=max_steps,
                    permissions=permissions,
                ))
                if (plan is not None and getattr(plan, "steps", ())
                        and str(getattr(plan, "rationale", "")).startswith("Pattern")):
                    return self._enrich_plan_args(plan, intent)
            except Exception:
                pass
        return None

    @staticmethod
    def _enrich_plan_args(plan, intent):
        """把用户意图中的关键词注入计划参数，让 MCP 能真正查到数据。"""
        import re
        kw = re.sub(
            r"^(?:帮我|请|麻烦你|查一下|查查|搜一下|搜搜|找一下|找找|看看|看一下|查|搜|找)+",
            "", intent or "")
        kw = re.sub(r"(课程|课|信息|成绩|时间|安排|情况|资料)+$", "", kw).strip()
        kw = re.sub(r"[，。！？、\s]+$", "", kw)
        steps = []
        for s in plan.steps:
            args = dict(s.arguments or {})
            if args.get("action") == "search" and not args.get("keyword"):
                args["keyword"] = kw
            steps.append(type(s)(**{**s.__dict__, "arguments": args}))
        return type(plan)(**{**plan.__dict__, "steps": tuple(steps)})

    async def _phase_compose_prod(self, state):
        draft_text = await self._compose_prod_text(state)
        draft = DraftResponse(
            text=draft_text,
            fact_anchors=self._prod_anchors(state),
            target_users=((state.envelope.sender.actor_id,)
                          if state.envelope and state.envelope.sender else ()),
        )
        return state.transition(RuntimePhase.COMPOSED, draft_response=draft)

    async def _compose_prod_text(self, state) -> str:
        if state.social_decision == SocialAction.ASK:
            return "能再说详细一点吗？"
        if state.social_decision == SocialAction.BLOCK:
            return "抱歉，我暂时不能回答这个问题。"
        event = self._pending_event
        plugin = self._plugin
        if event is None:
            return self._build_draft_text(state)
        try:
            pre = plugin.input_adapter.to_preprocessed(event)
            combined = pre.combined_text.strip() if pre and pre.combined_text else ""
        except Exception:
            combined = getattr(getattr(state, "envelope", None), "text", "") or ""
        perception = state.perception
        p = plugin.personas.active
        extra = ""
        if perception and perception.is_question():
            extra = "用户提出了一个问题，请认真回答。"
        elif perception and perception.is_command():
            extra = f"用户请求执行操作: {', '.join(perception.candidate_intents)}。请给出有用的回复。"
        elif perception and any(a.act_type == "noun_query"
                                for a in perception.speech_acts):
            extra = "用户只发来一个词或短名词，视为在询问它的含义，请直接解释，不要当打招呼。"
        system = (
            f"你是{p.display_name}，自称{p.first_person}。你就是嘟嘟哒。"
            "用颜表情风格，短回复。"
            "★ 如果用户问之前讨论过的文件内容，必须基于对话记录如实回答，不准编造。"
            + (f" {extra}" if extra else "")
        )
        mem_prefix = plugin._read_memory(event)
        if any(kw in combined for kw in ["文件", "图片", "刚才", "之前", "刚刚", "那个", "这个"]):
            mem_prefix = plugin._read_memory(event, include_episodic=True)
        try:
            is_group = bool(getattr(getattr(event, "message_obj", None), "group", None))
        except Exception:
            is_group = False
        obs = [o for o in _group_safe_observations(state.tool_observations, is_group)
               if o.success and o.data is not None
               and str(o.data).strip() not in ("[]", "{}", "")]
        if obs:
            tool_block = "\n".join(
                f"[工具 {o.capability_id}]: {_redact_text(str(o.data)[:1200])}"
                for o in obs)
            user_msg = (
                f"{mem_prefix}{combined}\n\n"
                f"以下是通过工具查到的真实数据（必须基于这些数据如实回答，不准编造）：\n"
                f"{tool_block}"
            )
        else:
            user_msg = mem_prefix + combined
        _llm_kwargs = {}
        try:
            import inspect as _inspect
            if "run_id" in _inspect.signature(plugin._call_llm).parameters:
                _llm_kwargs = {"run_id": state.run_id, "trace_id": state.trace_id}
        except Exception:
            pass
        reply = await plugin._call_llm(system, user_msg,
                                       max_tokens=1024, temperature=0.5,
                                       **_llm_kwargs)
        return reply or ""

    @staticmethod
    def _prod_anchors(state):
        anchors = []
        for obs in state.tool_observations:
            if obs.success and obs.data is not None:
                anchors.append(FactAnchor(
                    field=obs.capability_id,
                    value=str(obs.data)[:60],
                    source=obs.source,
                ))
        return tuple(anchors)

    def _build_memory_candidates(self, state):
        """生产作用域：per-event bot_id（多 bot 隔离），工具结果进 EPISODIC。"""
        event = self._pending_event
        if event is None:
            return super()._build_memory_candidates(state)
        candidates = []
        for obs in state.tool_observations:
            if (obs.success and obs.data is not None
                    and str(obs.data).strip() not in ("[]", "{}", "")):
                content = _redact_text(str(obs.data)[:2000]).strip()
                if not content or _contains_restricted(content):
                    continue  # Restricted 数据不进 Memory
                candidates.append(MemoryCandidate(
                    proposed_record=MemoryRecord(
                        scope=self._plugin._make_scope(event, msg_type="file"),
                        content=content,
                        source="tool",
                        sensitivity=SensitivityLevel.INTERNAL,
                        evidence=(f"tool:{obs.capability_id}",),
                    ),
                    requires_delivery_ack=False,
                ))
        if state.final_response and state.final_response.text:
            bot_text = _redact_text(state.final_response.text[:500]).strip()
            if bot_text and not _contains_restricted(bot_text):
                candidates.append(MemoryCandidate(
                    proposed_record=MemoryRecord(
                        scope=self._plugin._make_scope(event),
                        content=f"[嘟嘟哒]: {bot_text}",
                        source="bot",
                        sensitivity=SensitivityLevel.INTERNAL,
                        evidence=(f"run:{state.run_id}",),
                    ),
                    requires_delivery_ack=True,
                ))
        return tuple(candidates)
