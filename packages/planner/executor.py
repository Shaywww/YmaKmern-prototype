"""Enhanced ToolExecutor - multi-step orchestration with retry and timeout."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4
import asyncio
import time as _time


class AuthorizationError(RuntimeError):
    """Doc 2.4.12: non-retryable rejection (permission/risk/confirmation/schema/idempotency)."""

@dataclass
class ExecutionContext:
    max_steps: int = 4
    max_retries_per_step: int = 2
    deadline_seconds: float = 30.0
    parallelism: int = 1      # Max concurrent steps
    created_at: float = field(default_factory=_time.time)
    step_count: int = 0
    retry_count: int = 0
    # ---- Doc 2.4.12: re-authorization inputs (checked before every step) ----
    permissions: tuple = ()
    actor: str = ""
    conversation_scope: str = ""
    policy_hint: str = ""
    confirmed_ids: tuple = ()
    max_risk: Optional[object] = None

    @property
    def is_expired(self) -> bool:
        return (_time.time() - self.created_at) > self.deadline_seconds

    @property
    def can_execute_step(self) -> bool:
        return self.step_count < self.max_steps and not self.is_expired

    @property
    def can_retry(self) -> bool:
        # max_retries_per_step 表示允许的重试次数（不含首次尝试）
        return self.retry_count <= self.max_retries_per_step and not self.is_expired

@dataclass
class StepResult:
    step_id: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    source: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    retries_used: int = 0
    completed: bool = False

class ToolExecutor:
    """Multi-step tool executor with retry, timeout, and budget tracking."""

    def __init__(self, capability_registry=None):
        self._registry = capability_registry
        self._step_results: dict[str, StepResult] = {}

    async def execute_plan(self, plan, context: Optional[ExecutionContext] = None) -> tuple[StepResult, ...]:
        ctx = context or ExecutionContext(max_steps=len(plan.steps))
        self._step_results.clear()

        from .dependency import DependencyResolver
        try:
            batches = DependencyResolver.execution_order(list(plan.steps))
        except Exception:
            batches = [[s] for s in plan.steps]

        results: list[StepResult] = []
        for batch in batches:
            if not ctx.can_execute_step:
                break
            remaining = ctx.max_steps - ctx.step_count
            if remaining <= 0:
                break
            batch = batch[:remaining]  # 硬上限按步生效，不因并行 batch 越界
            batch_results = await asyncio.gather(*[self._execute_step(s, ctx) for s in batch])
            results.extend(batch_results)
            for r in batch_results:
                self._step_results[r.step_id] = r

        return tuple(results)

    async def _execute_step(self, step, ctx: ExecutionContext) -> StepResult:
        start = _time.time()
        retries = 0
        cap = None
        while retries <= ctx.max_retries_per_step:
            ctx.step_count += 1
            try:
                # Doc 2.4.12: re-resolve Definition + re-authorize before EVERY execution
                cap = self._reauthorize(step, ctx)
                result = await self._call_provider(step, cap)
                ctx.retry_count = 0
                return StepResult(
                    step_id=step.step_id, success=True, data=result,
                    source="provider", latency_ms=(_time.time()-start)*1000,
                    retries_used=retries, completed=True,
                )
            except AuthorizationError as e:
                # Auth/permission/schema/security rejections are NOT fixed by retry
                return StepResult(
                    step_id=step.step_id, success=False, error=str(e),
                    latency_ms=(_time.time()-start)*1000,
                    retries_used=retries, completed=True,
                )
            except Exception as e:
                retries += 1
                ctx.retry_count += 1
                # Doc 2.4.12: non-idempotent capabilities do not auto-retry
                if cap is not None and not cap.idempotent:
                    return StepResult(
                        step_id=step.step_id, success=False, error=str(e),
                        latency_ms=(_time.time()-start)*1000,
                        retries_used=retries, completed=True,
                    )
                if not ctx.can_retry or ctx.is_expired:
                    return StepResult(
                        step_id=step.step_id, success=False, error=str(e),
                        latency_ms=(_time.time()-start)*1000,
                        retries_used=retries, completed=True,
                    )
                await asyncio.sleep(0.5 * retries)  # Exponential-ish backoff

        return StepResult(
            step_id=step.step_id, success=False, error="Max retries exceeded",
            latency_ms=(_time.time()-start)*1000, retries_used=retries, completed=True,
        )

    def _reauthorize(self, step, ctx: ExecutionContext):
        """Doc 2.4.12: re-resolve Definition/Provider and check latest permissions,
        risk, confirmation, argument schema and idempotency key before execution."""
        if self._registry is None:
            raise RuntimeError("No capability registry configured")
        cap = self._registry.get(step.capability_id)
        if cap is None:
            raise AuthorizationError(f"Capability {step.capability_id} not found")
        if not cap.enabled:
            raise AuthorizationError(f"Capability {step.capability_id} is disabled")
        # Latest permissions
        if cap.required_permissions and not all(
            p in ctx.permissions for p in cap.required_permissions
        ):
            raise AuthorizationError(
                f"Missing permissions for {step.capability_id}: "
                f"need {cap.required_permissions}, have {ctx.permissions}"
            )
        # Risk cap
        if ctx.max_risk is not None:
            risk_order = {"read_only": 0, "side_effect": 1, "dangerous": 2}
            cap_risk = getattr(cap.risk, "value", str(cap.risk))
            max_risk = getattr(ctx.max_risk, "value", str(ctx.max_risk))
            if risk_order.get(cap_risk, 9) > risk_order.get(max_risk, 1):
                raise AuthorizationError(
                    f"Risk {cap_risk} exceeds allowed {max_risk} for {step.capability_id}"
                )
        # Confirmation
        if cap.requires_confirmation and cap.capability_id not in ctx.confirmed_ids:
            raise AuthorizationError(
                f"Capability {step.capability_id} requires confirmation"
            )
        # Argument schema (basic: required present, no unknown keys)
        schema = cap.schema.input_schema if cap.schema else {}
        args = dict(step.arguments or {})
        if schema:
            for r in schema.get("required", ()):
                if r not in args:
                    raise AuthorizationError(
                        f"Missing required argument {r!r} for {step.capability_id}"
                    )
            props = schema.get("properties", {})
            if props:
                for k in args:
                    if k not in props:
                        raise AuthorizationError(
                            f"Unknown argument {k!r} for {step.capability_id}"
                        )
        # Idempotency key: dedup enforced only when a key is provided
        idem_key = getattr(step, "idempotency_key", "") or ""
        if idem_key and self._registry.record_call(
            step.capability_id, idem_key, window_seconds=60.0
        ):
            raise AuthorizationError(
                f"Duplicate call rejected for {step.capability_id} (key={idem_key!r})"
            )
        return cap

    async def _call_provider(self, step, cap) -> Any:
        if self._registry is None:
            raise RuntimeError("No capability registry configured")
        provider = self._registry.get_provider(step.capability_id)
        if provider is None:
            raise RuntimeError(f"No provider for {step.capability_id}")
        # Inject step arguments from previous results
        args = dict(step.arguments)
        for dep in step.depends_on:
            if dep in self._step_results and self._step_results[dep].success:
                args[f"_{dep}_result"] = self._step_results[dep].data
        obs = await provider.execute(cap, args)
        if not obs.success:
            raise RuntimeError(obs.error or "Tool execution failed")
        return obs.data

    def get_step_result(self, step_id: str) -> Optional[StepResult]:
        return self._step_results.get(step_id)
