"""嘟嘟哒 2.0 Runtime Orchestrator —— 控制中枢。"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Optional
from uuid import uuid4

from ..core.envelope import MessageEnvelope, PreprocessedEnvelope
from ..core.state import (
    RuntimePhase, RunOutcome, SocialAction, RuntimeState,
    RuntimeBudget, ToolStep, ToolPlan,
)
from ..core.context import ContextBuilder, ContextSnapshot, PolicyView
from ..core.perception import PerceptionResult, SpeechAct
from ..core.perception_store import record_state_perception
from ..core.decision import SocialDecision, SocialDecisionEngine, DecisionReason
from ..core.idempotency import MessageIdempotencyRegistry
from ..core.capability import (
    CapabilityRegistry, CapabilityCandidate, ToolObservation,
    ToolPlanValidator, ValidatorAction,
)
from ..core.memory import (
    MemoryRepository, InMemoryRepository, WriteGate,
    MemoryCandidate, MemoryRecord, MemoryType, SensitivityLevel,
)
from ..core.memory import MemoryScope as MemScope
from ..core.memory import WriteGateDecision
from ..core.renderer import (
    OCRenderer, DraftResponse, FactAnchor, FinalResponse, Persona,
)
from ..core.delivery import (
    RuntimeResult, DeliveryReceipt, CompletionReceipt,
    DeliveryManager, NoOpOutputAdapter,
)
from ..safeguards.security import Redactor
from ..core.trace_recorder import trace_recorder
from ..mcp.access import mcp_access  # iCourse 按群/按人策略（文档 2.5.6）

_TOOL_HARD_CAP = 8  # 全局硬上限（文档 2.5.5：默认 4 步、硬上限 8）
_REDACTOR = Redactor()  # 工具结果脱敏（文档 2.5.9）

from packages.application.dududa_log import get_logger as _get_logger
logger = _get_logger("dududa20")  # 与插件日志同源，进 journalctl


class RuntimeOrchestrator:
    """Agent Runtime 控制中枢。"""

    def __init__(
        self,
        context_builder: Optional[ContextBuilder] = None,
        decision_engine: Optional[SocialDecisionEngine] = None,
        capability_registry: Optional[CapabilityRegistry] = None,
        memory_repo: Optional[MemoryRepository] = None,
        renderer: Optional[OCRenderer] = None,
        delivery_manager: Optional[DeliveryManager] = None,
        planner_integration=None,
        profile_store: Optional[Any] = None,
        style_store: Optional[Any] = None,
        idempotency_registry: Optional[MessageIdempotencyRegistry] = None,
        confirmation_store: Optional[Any] = None,
    ):
        self._context_builder = context_builder or ContextBuilder()
        self._decision_engine = decision_engine or SocialDecisionEngine()
        self._capability_registry = capability_registry or CapabilityRegistry()
        self._memory_repo = memory_repo or InMemoryRepository()
        self._renderer = renderer or OCRenderer()
        self._delivery_manager = delivery_manager or DeliveryManager(NoOpOutputAdapter())
        self._write_gate = WriteGate(self._memory_repo)
        self._tool_chain = planner_integration
        self._profile_store = profile_store  # SESSION_STATE / USER_PROFILE（文档 2.4.6）
        self._style_store = style_store  # USER_STYLE 四维隔离（文档 2.5.8）
        self._idempotency_registry = idempotency_registry  # Connector 幂等键判重（文档 2.4.1）
        self._last_state: Optional[RuntimeState] = None
        self._completions: dict[str, CompletionReceipt] = {}
        self._pending_cancellation: Optional[Any] = None  # run(…) 传入的外部取消信号（文档 2.5.5）
        self._confirmation_store = confirmation_store  # 持久确认（文档 2.4.23/2.4.12）

    async def run(
        self,
        envelope: MessageEnvelope,
        budget: Optional[RuntimeBudget] = None,
        policy: Optional[PolicyView] = None,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        cancellation: Optional[Any] = None,
        confirmation_ids: Optional[Any] = None,
    ) -> RuntimeResult:
        budget = budget or RuntimeBudget()
        # 兼容：允许直接传入 PreprocessedEnvelope（Connector 预处理产物）
        if hasattr(envelope, "envelope"):
            envelope = envelope.envelope
        state = RuntimeState(
            envelope=envelope, budget=budget,
            run_id=run_id or uuid4().hex,
            trace_id=trace_id or uuid4().hex,
            confirmation_ids=tuple(confirmation_ids or ()),
        )
        if self._dedupe_envelope(envelope):
            # 幂等键重复：该消息已被处理过（已有人回答）-> 不回复、可审计
            state = state.transition(
                RuntimePhase.ABORTED, outcome=RunOutcome.IGNORED,
                decision_reason=DecisionReason.ALREADY_ANSWERED.value,
                social_decision=SocialAction.IGNORE,
            )
            self._last_state = state
            return self._result_from_state(state, RunOutcome.IGNORED)

        self._pending_cancellation = cancellation
        if cancellation is not None and cancellation.is_set():
            # 入口即取消：不进入任何阶段（文档 2.4.3：deadline/cancellation 到达后不再开始新调用）
            state = state.transition(
                RuntimePhase.CANCELLED, outcome=RunOutcome.CANCELLED,
                decision_reason="cancelled_at_entry",
            )
            self._last_state = state
            return self._result_from_state(state, RunOutcome.CANCELLED)

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

            if state.social_decision in (
                SocialAction.ANSWER, SocialAction.ASK, SocialAction.USE_TOOLS,
            ):
                state = self._phase_list_capabilities(state)

            if self._should_use_tools(state):
                state = await self._phase_tool_chain(state)
                if state.outcome in (RunOutcome.FAILED, RunOutcome.CANCELLED):
                    self._last_state = state
                    return self._result_from_state(state, state.outcome or RunOutcome.FAILED)
            else:
                state = state.transition(RuntimePhase.COMPOSED)

            state = self._phase_compose(state)
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


    def _dedupe_envelope(self, envelope: MessageEnvelope) -> bool:
        """Connector 幂等键 (platform, bot_id, message_id) 判重（文档 2.4.1）。

        未注入注册表或缺少 platform_message_id 时放行；
        判重异常不阻断主流程（降级为放行）。
        """
        reg = self._idempotency_registry
        if reg is None:
            return False
        mid = getattr(envelope, "platform_message_id", None)
        if not mid:
            return False
        try:
            plat = getattr(getattr(envelope, "platform", None), "value", "qq")
        except Exception:
            plat = "qq"
        try:
            return not reg.check_and_register(plat, "dududa", mid)
        except Exception as e:
            logger.warning("Idempotency check failed: %s", e)
            return False

    # ---- 阶段实现 ----

    def _phase_validate(self, state: RuntimeState) -> RuntimeState:
        envelope = state.envelope
        if envelope is None or not isinstance(envelope, MessageEnvelope):
            return state.transition(RuntimePhase.ABORTED, outcome=RunOutcome.FAILED,
                                     errors=("No valid envelope",))
        if state.budget.is_expired():
            return state.transition(RuntimePhase.ABORTED, outcome=RunOutcome.ABORTED,
                                     errors=("Budget expired",))
        # Connector 契约：回复链跨会话 -> 拒绝（不回复、不处理）
        reply = getattr(envelope, "reply_to", None)
        if reply is not None:
            src = getattr(getattr(reply, "conversation", None),
                          "conversation_id", None)
            dst = getattr(envelope.conversation, "conversation_id", None)
            if src is not None and src != dst:
                return state.transition(
                    RuntimePhase.ABORTED, outcome=RunOutcome.IGNORED,
                    errors=("Cross-session reply rejected",))
        return state.transition(RuntimePhase.VALIDATED)

    def _phase_preprocess(self, state: RuntimeState) -> RuntimeState:
        return state.transition(RuntimePhase.PREPROCESSED,
            preprocessed=PreprocessedEnvelope(envelope=state.envelope))

    def _phase_scope(self, state: RuntimeState) -> RuntimeState:
        return state.transition(RuntimePhase.SCOPED)

    def _phase_context(self, state: RuntimeState, policy: Optional[PolicyView] = None,
                         persona_id: str = "dududa_default") -> RuntimeState:
        snapshot = self._context_builder.build(
            envelope=state.envelope, policy=policy, budget=state.budget,
            persona_id=persona_id,
        )
        return state.transition(RuntimePhase.CONTEXT_BUILT, context_snapshot=snapshot, scope=snapshot)

    def _phase_perceive(self, state: RuntimeState) -> RuntimeState:
        """结构化感知：平台事实优先（@/命令/回复链），文本信号补充。"""
        envelope = state.envelope
        text = envelope.text if envelope else ""
        is_explicit = bool(envelope) and envelope.is_explicit_command()
        is_question = text.rstrip().endswith("?") or text.rstrip().endswith("？") or "吗" in text
        speech_acts: tuple[SpeechAct, ...] = ()
        if is_explicit:
            speech_acts = (SpeechAct(act_type="command", confidence=1.0),)
        elif is_question:
            speech_acts = (SpeechAct(act_type="question", confidence=0.8),)
        perception = PerceptionResult(
            has_explicit_mention=bool(envelope.mentions),
            has_reply_chain=envelope.reply_to is not None,
            is_explicit_command=is_explicit,
            needs_tools=("查" in text or "搜" in text or "/" in text
                         or any(k in text for k in
                                ("几点", "时间", "几号", "星期几", "日期",
                                 "什么时候了", "现在是", "现在几"))),
            target_users=envelope.mentions or (),
            resolved_references={"text": text},
            speech_acts=speech_acts,
            candidate_intents=("course_query",) if ("查" in text or "搜" in text) else (),
            confidence=1.0,
        )
        record_state_perception(perception, state, source="rule")
        self._record_profile(state, perception)
        self._record_style(state, perception)
        return state.transition(RuntimePhase.PERCEIVED, perception=perception)

    def _phase_decide(self, state: RuntimeState) -> RuntimeState:
        decision = self._decision_engine.decide(
            perception=state.perception, context=state.context_snapshot,
        )
        return state.transition(RuntimePhase.DECIDED, social_decision=decision.action,
            decision_reason=";".join(r.value for r in decision.reason_codes))

    def _phase_list_capabilities(self, state: RuntimeState) -> RuntimeState:
        permissions = self._permissions_of(state)
        # 生产注册表 10+ 能力：默认 top_k=8 会截掉后注册的 mcp.clock/campus_notice，
        # 导致「现在几点」无候选可规划、降级为闲聊。放宽候选上限；
        # 步数仍由 max_steps 与全局硬上限约束（文档 2.5.5）。
        candidates = self._capability_registry.filter_candidates(
            permissions=permissions, max_count=24)
        # iCourse MCP 按群/按人策略（文档 2.5.6）：未放行的服务不进候选
        candidates = self._scope_filter_candidates(state, candidates)
        return state.transition(RuntimePhase.CAPABILITIES_LISTED, capability_candidates=candidates)

    def _scope_filter_candidates(
        self, state: RuntimeState, candidates: tuple[CapabilityCandidate, ...]
    ) -> tuple[CapabilityCandidate, ...]:
        """按群/按人策略过滤 iCourse 候选（fail closed）。

        非 iCourse 能力（clock 等）恒允许；被拒能力在规划前剔除。
        """
        conv = self._conversation_id(state)
        actor = self._actor_id(state)
        kept: list[CapabilityCandidate] = []
        denied: list[str] = []
        for cand in candidates:
            if mcp_access.is_allowed(cand.capability.capability_id, conv, actor):
                kept.append(cand)
            else:
                denied.append(cand.capability.capability_id)
        if denied:
            logger.info("iCourse MCP scope-gated: %s (conv=%s actor=%s)",
                        ",".join(denied), conv, actor)
        return tuple(kept)

    async def _phase_tool_chain(self, state: RuntimeState) -> RuntimeState:
        """真实工具链：规划 -> 校验 -> 执行 -> 结果校验 -> 动作归一化。

        文档 2.5.5：默认 4 步、全局硬上限 8；retry 计入步数；
        deadline 到达后不再开始新调用。
        """
        budget = state.budget
        max_steps = min(max(1, budget.max_tool_steps), _TOOL_HARD_CAP)
        permissions = self._permissions_of(state)
        candidates = state.capability_candidates or ()

        state = state.transition(RuntimePhase.TOOLS_PLANNED)
        if not candidates:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    tool_observations=(), errors=("No capabilities",),
                                    outcome=RunOutcome.DEGRADED)

        # 1) 规划：优先 Planner 模式；无集成或空计划时退化为直连 Top-K
        plan = self._plan(state, candidates, max_steps, permissions)
        if plan is None:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    tool_observations=(), errors=("No plan",),
                                    outcome=RunOutcome.DEGRADED)

        # 2) 计划校验：Schema / 预算 / 依赖（fail closed）
        validator = ToolPlanValidator(self._capability_registry)
        ok, plan_errors = validator.validate_plan(plan, budget)
        if not ok:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    tool_observations=(), errors=plan_errors,
                                    outcome=RunOutcome.DEGRADED)

        # 2.5) 按群/按人策略裁剪计划步骤（fail closed）：被拒能力不执行
        plan = self._scope_prune_plan(state, plan)
        if plan is None:
            return state.transition(
                RuntimePhase.VALIDATED_TOOLS, tool_observations=(),
                errors=("iCourse MCP not enabled for this scope",),
                outcome=RunOutcome.DEGRADED)

        # 3) 执行（每步重新授权 + deadline/预算检查）
        observations = await self._execute(state, plan, max_steps, permissions)

        # 3.5) 取消：迟到结果不推进状态（文档 2.4.12 / 2.5.5）
        if any(getattr(o, "cancelled", False) for o in observations):
            return state.transition(RuntimePhase.TOOLS_EXECUTED,
                                    tool_plan=plan, tool_observations=tuple(observations),
                                    outcome=RunOutcome.CANCELLED)

        # 4) 结果校验 -> 动作归一化
        verdict = validator.validate_results(tuple(observations))
        state = state.transition(RuntimePhase.TOOLS_EXECUTED,
                                 tool_plan=plan, tool_observations=tuple(observations))
        state = state.transition(RuntimePhase.VALIDATED_TOOLS)

        if verdict.action == ValidatorAction.ABORT:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    outcome=RunOutcome.FAILED,
                                    errors=verdict.error_details)
        if verdict.action == ValidatorAction.DEGRADE:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    outcome=RunOutcome.DEGRADED)
        if verdict.action == ValidatorAction.CLARIFY:
            return state.transition(RuntimePhase.VALIDATED_TOOLS,
                                    outcome=RunOutcome.SUCCEEDED,
                                    errors=verdict.error_details)
        return state.transition(RuntimePhase.VALIDATED_TOOLS, outcome=RunOutcome.SUCCEEDED)

    # ---- 工具链子步骤 ----

    def _plan(self, state: RuntimeState, candidates, max_steps, permissions):
        if self._tool_chain is not None:
            from ..planner.planner import PlanningContext
            try:
                plan = self._tool_chain.planner.plan(PlanningContext(
                    user_intent=self._intent_of(state),
                    available_capabilities=candidates,
                    max_steps=max_steps,
                    permissions=permissions,
                ))
                if plan is not None and getattr(plan, "steps", ()):
                    return plan
            except Exception:
                pass
        return self._fallback_plan(candidates, max_steps)

    def _scope_prune_plan(self, state: RuntimeState, plan):
        """按群/按人策略裁剪计划步骤（fail closed）。

        被拒能力的步骤直接删除；全部被拒返回 None（调用方降级）。
        """
        conv = self._conversation_id(state)
        actor = self._actor_id(state)
        steps = list(getattr(plan, "steps", ()) or ())
        kept = [s for s in steps
                if mcp_access.is_allowed(getattr(s, "capability_id", ""), conv, actor)]
        if not kept:
            return None
        if len(kept) == len(steps):
            return plan
        from ..planner.planner import GeneratedPlan
        return GeneratedPlan(
            goal=getattr(plan, "goal", "fallback"),
            steps=tuple(kept),
            rationale=getattr(plan, "rationale", "scope-pruned"),
        )

    def _fallback_plan(self, candidates, max_steps):
        """无 Planner 集成时的退路：按相关性取前 N 个能力直连。"""
        from ..planner.planner import GeneratedPlan, PlannedStep
        steps = []
        seen = set()
        for cand in candidates:
            cap = cand.capability
            if cap.capability_id in seen:
                continue
            if self._capability_registry.get_provider(cap.capability_id) is None:
                continue
            seen.add(cap.capability_id)
            steps.append(PlannedStep(
                step_id=f"f{len(steps) + 1}",
                capability_id=cap.capability_id,
                arguments={},
                purpose=cap.description,
            ))
            if len(steps) >= max_steps:
                break
        if not steps:
            return None
        return GeneratedPlan(goal="fallback", steps=tuple(steps), rationale="direct fallback")

    async def _execute(self, state: RuntimeState, plan, max_steps, permissions):
        budget = state.budget
        if self._tool_chain is not None and getattr(self._tool_chain, "executor", None) is not None:
            from ..planner.executor import ExecutionContext
            ctx = ExecutionContext(
                max_steps=max_steps,
                max_retries_per_step=budget.max_tool_retries,
                deadline_seconds=budget.deadline_seconds,
                permissions=permissions,
                actor=self._actor_id(state),
                conversation_scope=self._conversation_id(state),
                cancellation=self._pending_cancellation,
                confirmation_store=self._confirmation_store,
                confirmation_ids=state.confirmation_ids,
                run_id=state.run_id,
                trace_id=state.trace_id,
            )
            step_results = await self._tool_chain.executor.execute_plan(plan, ctx)
            by_id = {s.step_id: s for s in plan.steps}
            observations = []
            for sr in step_results:
                step = by_id.get(sr.step_id)
                observations.append(ToolObservation(
                    step_id=sr.step_id,
                    capability_id=step.capability_id if step else "",
                    success=sr.success,
                    data=sr.data if sr.success else None,
                    error=sr.error,
                    source=sr.source or "provider",
                    latency_ms=sr.latency_ms,
                    cached=sr.cached,
                    cancelled=sr.cancelled,
                ))
            return observations
        return await self._execute_direct(state, plan, max_steps)

    async def _execute_direct(self, state: RuntimeState, plan, max_steps):
        """无 Planner/Executor 集成时的直连退化路径。

        使用临时 ToolExecutor，保证与正式路径一致的
        每步重新授权 / 重试 / 去重 / 取消语义（文档 2.4.12）。
        """
        from ..planner.executor import ToolExecutor, ExecutionContext
        budget = state.budget
        permissions = self._permissions_of(state)
        executor = ToolExecutor(self._capability_registry)
        ctx = ExecutionContext(
            max_steps=max_steps,
            max_retries_per_step=budget.max_tool_retries,
            deadline_seconds=budget.deadline_seconds,
            permissions=permissions,
            actor=self._actor_id(state),
            conversation_scope=self._conversation_id(state),
            cancellation=self._pending_cancellation,
            confirmation_store=self._confirmation_store,
            confirmation_ids=state.confirmation_ids,
            run_id=state.run_id,
            trace_id=state.trace_id,
        )
        step_results = await executor.execute_plan(plan, ctx)
        by_id = {s.step_id: s for s in plan.steps}
        observations: list[ToolObservation] = []
        for sr in step_results:
            step = by_id.get(sr.step_id)
            observations.append(ToolObservation(
                step_id=sr.step_id,
                capability_id=step.capability_id if step else "",
                success=sr.success,
                data=sr.data if sr.success else None,
                error=sr.error,
                source=sr.source or "provider",
                latency_ms=sr.latency_ms,
                cached=sr.cached,
                cancelled=sr.cancelled,
            ))
        return observations

    def _should_use_tools(self, state: RuntimeState) -> bool:
        if not state.perception:
            return False
        if not state.capability_candidates:
            return False
        return (getattr(state.perception, "needs_tools", False)
                or state.social_decision == SocialAction.USE_TOOLS)

    def _phase_compose(self, state: RuntimeState) -> RuntimeState:
        draft_text = self._build_draft_text(state)
        draft = DraftResponse(
            text=draft_text,
            fact_anchors=self._extract_fact_anchors(state),
            target_users=((state.envelope.sender.actor_id,)
                          if state.envelope and state.envelope.sender else ()),
        )
        return state.transition(RuntimePhase.COMPOSED, draft_response=draft)

    async def _phase_render(self, state: RuntimeState) -> RuntimeState:
        if state.draft_response is None:
            return state.transition(RuntimePhase.RENDERED, final_response=FinalResponse(text=""))
        renderer = self._renderer
        if hasattr(renderer, "render_hybrid"):
            # 2.5.8 hybrid：LLM 风格转换 + 事实校验，失败回退确定性渲染
            final = await renderer.render_hybrid(
                state.draft_response, run_id=state.run_id,
                trace_id=state.trace_id)
        else:
            final = renderer.render(state.draft_response)
        return state.transition(RuntimePhase.RENDERED, final_response=final)

    def _build_draft_text(self, state: RuntimeState) -> str:
        if state.social_decision == SocialAction.ASK:
            return "能再说详细一点吗？"
        if state.social_decision == SocialAction.BLOCK:
            return "抱歉，我暂时不能回答这个问题。"
        obs = state.tool_observations
        if obs:
            ok_texts = [str(_REDACTOR.redact(o.data)[0])
                        for o in obs if o.success and o.data is not None]
            if ok_texts:
                return chr(10).join(ok_texts)
            if state.outcome == RunOutcome.DEGRADED:
                return "查到了部分信息，还有一部分暂时不可用，我再整理一下~"
            return "暂时没查到相关信息，换个说法再试试？"
        if state.perception and state.perception.is_question:
            return "让我想想……这个问题我还需要更多信息才能回答。"
        return "嗯嗯。"

    @staticmethod
    def _extract_fact_anchors(state: RuntimeState) -> tuple[FactAnchor, ...]:
        anchors: list[FactAnchor] = []
        for obs in state.tool_observations:
            if obs.success and obs.data is not None:
                anchors.append(FactAnchor(
                    field=obs.capability_id,
                    value=str(_REDACTOR.redact(obs.data)[0]),
                    source=obs.source))
        return tuple(anchors)

    # ---- 记忆候选（文档 2.3.16：只产生候选，投递确认后过 Write Gate） ----

    def _build_memory_candidates(self, state: RuntimeState) -> tuple[MemoryCandidate, ...]:
        candidates: list[MemoryCandidate] = []
        scope_tool = MemScope(
            memory_type=MemoryType.EPISODIC,
            platform=self._platform(state),
            bot_id="dududa",
            conversation_id=self._conversation_id(state),
            actor_id=self._actor_id(state),
        )
        for obs in state.tool_observations:
            if obs.success and obs.data is not None:
                candidates.append(MemoryCandidate(
                    proposed_record=MemoryRecord(
                        scope=scope_tool,
                        content=str(_REDACTOR.redact(obs.data)[0])[:2000],
                        source="tool",
                        sensitivity=SensitivityLevel.INTERNAL,
                        evidence=(f"tool:{obs.capability_id}",),
                    ),
                    requires_delivery_ack=False,
                    metadata={"run_id": state.run_id,
                              "trace_id": state.trace_id},
                ))
        if state.final_response and state.final_response.text:
            scope_bot = MemScope(
                memory_type=MemoryType.SHORT_TERM,
                platform=self._platform(state),
                bot_id="dududa",
                conversation_id=self._conversation_id(state),
                actor_id=self._actor_id(state),
            )
            candidates.append(MemoryCandidate(
                proposed_record=MemoryRecord(
                    scope=scope_bot,
                    content=f"[嘟嘟哒]: {state.final_response.text[:500]}",
                    source="bot",
                    sensitivity=SensitivityLevel.INTERNAL,
                    evidence=(f"run:{state.run_id}",),
                ),
                requires_delivery_ack=True,
                metadata={"run_id": state.run_id,
                          "trace_id": state.trace_id},
            ))
        return tuple(candidates)

    async def acknowledge_delivery(
        self, receipt: DeliveryReceipt, state: Optional[RuntimeState] = None,
    ) -> CompletionReceipt:
        """投递回执确认（文档 2.3.15-2.3.16）。

        校验回执对应等待确认的运行（run_id 唯一绑定）、时间带时区；
        重复回执幂等返回首次完成回执；
        投递未 SUCCEEDED 时跳过"已告知"类记忆。
        """
        state = state or getattr(self, "_last_state", None)
        if state is None:
            return CompletionReceipt(
                run_id=receipt.run_id, final_phase="unknown",
                delivery_status=receipt.status)
        if state.run_id != receipt.run_id:
            # 悬挂/错配回执：幂等拒绝，不做任何推进
            return CompletionReceipt(
                run_id=receipt.run_id, final_phase=state.phase.value,
                delivery_status=receipt.status, memory_write_receipts=())
        cached = self._completions.get(receipt.run_id)
        if cached is not None:
            return cached
        if receipt.acknowledged_at.tzinfo is None:
            # 时间必须带时区（文档 2.3.15）
            return CompletionReceipt(
                run_id=receipt.run_id, final_phase=state.phase.value,
                delivery_status=receipt.status, memory_write_receipts=())

        state = state.transition(RuntimePhase.DELIVERY_ACKNOWLEDGED,
                                 delivery_receipt=receipt)
        memory_receipts: list[str] = []
        for candidate in state.memory_candidates:
            if getattr(candidate, "requires_delivery_ack", False):
                if not receipt.is_ok:
                    continue
                candidate = replace(candidate, delivery_run_id=receipt.run_id)
            decision = self._write_gate.evaluate(candidate)
            if decision == WriteGateDecision.ALLOW:
                rid = self._memory_repo.write(candidate.proposed_record)
                memory_receipts.append(rid)
        state = state.transition(RuntimePhase.MEMORY_EVALUATED,
                                 memory_write_receipts=tuple(memory_receipts))
        final_state = state.transition(RuntimePhase.COMPLETED)
        self._last_state = final_state
        comp = CompletionReceipt(
            run_id=state.run_id, final_phase=final_state.phase.value,
            delivery_status=receipt.status,
            memory_write_receipts=tuple(memory_receipts))
        self._completions[receipt.run_id] = comp
        trace_recorder.record(
            event="run_complete", run_id=state.run_id,
            trace_id=state.trace_id, final_phase=comp.final_phase,
            delivery_status=(comp.delivery_status.value
                             if comp.delivery_status else None),
            memory_write_receipts=list(comp.memory_write_receipts))
        logger.info(
            "Run complete | run_id=%s final_phase=%s delivery=%s memory=%d",
            state.run_id, comp.final_phase,
            comp.delivery_status.value if comp.delivery_status else "-",
            len(comp.memory_write_receipts))
        return comp

    async def complete_without_delivery(
        self, state: Optional[RuntimeState] = None,
    ) -> CompletionReceipt:
        """无可视输出的运行（IGNORE/ABORTED/降级无回复）收尾（文档 2.3.16）。

        不伪造空回复或 Delivery Receipt；只评估不依赖投递确认的候选，
        依赖投递的"已告知"候选一律跳过。
        """
        state = state or getattr(self, "_last_state", None)
        if state is None:
            return CompletionReceipt(run_id="", final_phase="unknown")
        cached = self._completions.get(state.run_id)
        if cached is not None:
            return cached
        memory_receipts: list[str] = []
        for candidate in state.memory_candidates:
            if getattr(candidate, "requires_delivery_ack", False):
                continue
            decision = self._write_gate.evaluate(candidate)
            if decision == WriteGateDecision.ALLOW:
                memory_receipts.append(
                    self._memory_repo.write(candidate.proposed_record))
        if state.phase == RuntimePhase.READY_TO_EMIT:
            state = state.transition(
                RuntimePhase.MEMORY_EVALUATED,
                memory_write_receipts=tuple(memory_receipts))
            final_state = state.transition(RuntimePhase.COMPLETED)
            self._last_state = final_state
            final_phase = final_state.phase.value
        else:
            final_state = state
            final_phase = state.phase.value
        comp = CompletionReceipt(
            run_id=state.run_id, final_phase=final_phase,
            delivery_status=None,
            memory_write_receipts=tuple(memory_receipts))
        self._completions[state.run_id] = comp
        trace_recorder.record(
            event="run_complete", run_id=state.run_id,
            trace_id=state.trace_id, final_phase=comp.final_phase,
            delivery_status=None,
            memory_write_receipts=list(comp.memory_write_receipts))
        logger.info(
            "Run complete | run_id=%s final_phase=%s delivery=%s memory=%d",
            state.run_id, comp.final_phase, "-",
            len(comp.memory_write_receipts))
        return comp

    # ---- 小工具 ----

    @staticmethod
    def _with_updates(state: RuntimeState, **updates: Any) -> RuntimeState:
        return type(state)(**{**state.__dict__, **updates})

    @staticmethod
    def _permissions_of(state: RuntimeState) -> tuple[str, ...]:
        snapshot = state.context_snapshot
        if not snapshot:
            return ()
        return tuple(getattr(snapshot, "permissions", ()) or ())

    @staticmethod
    def _intent_of(state: RuntimeState) -> str:
        env = state.envelope
        if not env:
            return ""
        parts = list(env.mentions or ()) + [env.text]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _actor_id(state: RuntimeState) -> str:
        env = state.envelope
        return env.sender.actor_id if env and env.sender else "unknown"

    @staticmethod
    def _conversation_id(state: RuntimeState) -> str:
        env = state.envelope
        return env.conversation.conversation_id if env and env.conversation else "unknown"

    @staticmethod
    def _platform(state: RuntimeState) -> str:
        env = state.envelope
        plat = env.platform if env else None
        return getattr(plat, "value", "qq") if plat else "qq"

    def _record_profile(self, state: RuntimeState, perception) -> None:
        """SESSION_STATE / USER_PROFILE 学习（文档 2.4.6）。

        每条消息更新会话状态；engaged（@/命令/回复链）时学习画像信号，
        避免把群聊噪音写进长期偏好。
        """
        store = self._profile_store
        if store is None:
            return
        env = state.envelope
        if env is None or env.sender is None:
            return
        engaged = bool(
            getattr(env, "mentions", ()) or env.reply_to
            or bool(getattr(perception, "is_explicit_command", False)))
        try:
            store.record_message(
                platform=self._platform(state),
                bot_id="dududa",
                conversation_id=self._conversation_id(state),
                actor_id=self._actor_id(state),
                text=getattr(env, "text", "") or "",
                intents=tuple(getattr(perception, "candidate_intents", ()) or ()),
                topics=tuple(getattr(perception, "topics", ()) or ()),
                engaged=engaged,
            )
        except Exception as e:
            logger.warning("Profile record failed: %s", e)

    def _record_style(self, state: RuntimeState, perception,
                      persona_id: str = "dududa_default",
                      bot_id: str = "dududa") -> None:
        """用户 style 学习（文档 2.5.8）：与画像同语义，engaged 才写长期偏好。

        四维键 platform+bot+user+persona；保留来源会话与可见性；
        跨会话读取走 UserStyleStore.get() 具名 selector。
        """
        store = self._style_store
        if store is None:
            return
        env = state.envelope
        if env is None or env.sender is None:
            return
        engaged = bool(
            getattr(env, "mentions", ()) or env.reply_to
            or bool(getattr(perception, "is_explicit_command", False)))
        try:
            kind = getattr(env, "kind", None)
            kind_value = getattr(kind, "value", "") or ""
            store.record_message(
                platform=self._platform(state),
                bot_id=bot_id,
                conversation_id=self._conversation_id(state),
                user_id=self._actor_id(state),
                persona_id=persona_id,
                text=getattr(env, "text", "") or "",
                engaged=engaged,
                visibility="private" if kind_value == "private" else "public",
            )
        except Exception as e:
            logger.warning("Style record failed: %s", e)

    @staticmethod
    def _result_from_state(state: RuntimeState, outcome: Optional[RunOutcome]) -> RuntimeResult:
        _outcome = outcome or RunOutcome.IGNORED
        logger.info(
            "Run end | run_id=%s trace_id=%s outcome=%s final_phase=%s errors=%d",
            state.run_id, state.trace_id, _outcome.value, state.phase.value,
            len(state.errors),
        )
        if _outcome in (RunOutcome.FAILED, RunOutcome.ABORTED):
            logger.warning(
                "Run error | run_id=%s trace_id=%s errors=%s",
                state.run_id, state.trace_id, list(state.errors)[:5],
            )
            trace_recorder.record(
                event="run_error", run_id=state.run_id, trace_id=state.trace_id,
                errors=list(state.errors)[:5],
            )
        trace_recorder.record(
            event="run_end", run_id=state.run_id, trace_id=state.trace_id,
            outcome=_outcome.value, final_phase=state.phase.value,
            errors=len(state.errors), phases_visited=len(state.trace),
            tool_steps=len(state.tool_observations),
            decision_reason=state.decision_reason or "",
        )
        return RuntimeResult(
            run_id=state.run_id,
            trace_id=state.trace_id,
            outcome=_outcome,
            final_response=state.final_response,
            reason_codes=((state.decision_reason,) if state.decision_reason else ()),
            trace_summary={
                "phases_visited": len(state.trace),
                "tool_steps": len(state.tool_observations),
                "errors": state.errors,
            },
            requires_delivery_ack=(state.final_response is not None and bool(state.final_response.text)),
        )
