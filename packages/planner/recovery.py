"""Error recovery strategies for tool execution failures."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

class RecoveryAction(str, Enum):
    RETRY = "retry"           # Same step, same args
    RETRY_WITH_FALLBACK = "retry_with_fallback"  # Try alternative provider
    SKIP = "skip"             # Skip this step, continue plan
    DEGRADE = "degrade"       # Use partial results
    CLARIFY = "clarify"       # Ask user for clarification
    ABORT = "abort"           # Stop execution entirely
    USE_CACHE = "use_cache"   # Use cached/stale data

@dataclass
class RecoveryDecision:
    action: RecoveryAction
    reason: str = ""
    modified_args: Optional[dict] = None
    fallback_capability_id: Optional[str] = None
    user_question: Optional[str] = None

@dataclass
class ErrorContext:
    step_id: str
    capability_id: str
    error_message: str
    error_type: str = "unknown"     # timeout, permission, schema, provider, network
    retries_used: int = 0
    max_retries: int = 2
    has_partial_data: bool = False
    partial_data: Any = None
    has_cache: bool = False
    critical_step: bool = False
    depends_on_results: dict[str, Any] = None

    def __post_init__(self):
        if self.depends_on_results is None:
            self.depends_on_results = {}

class ErrorRecovery:
    """Determines the best recovery action based on error context."""

    def decide(self, ctx: ErrorContext) -> RecoveryDecision:
        # 1. Timeout -> retry once, then degrade
        if ctx.error_type == "timeout":
            if ctx.retries_used < ctx.max_retries:
                return RecoveryDecision(RecoveryAction.RETRY, "Timeout, retrying")
            if ctx.has_partial_data:
                return RecoveryDecision(RecoveryAction.DEGRADE, "Using partial results after timeout")
            return RecoveryDecision(RecoveryAction.SKIP, "Skipping after repeated timeouts")

        # 2. Permission denied -> abort (cannot fix)
        if ctx.error_type == "permission":
            if ctx.critical_step:
                return RecoveryDecision(RecoveryAction.ABORT, "Critical step denied permission")
            return RecoveryDecision(RecoveryAction.SKIP, "Skipping unauthorized step")

        # 3. Schema/validation error -> clarify
        if ctx.error_type == "schema":
            return RecoveryDecision(
                RecoveryAction.CLARIFY,
                reason="Schema validation failed",
                user_question=f"I had trouble processing the request for this step. Could you rephrase what you need?",
            )

        # 4. Provider error -> retry with fallback or degrade
        if ctx.error_type == "provider":
            if ctx.retries_used < ctx.max_retries:
                return RecoveryDecision(RecoveryAction.RETRY, "Provider error, retrying")
            if ctx.has_cache:
                return RecoveryDecision(RecoveryAction.USE_CACHE, "Using cached data after provider failure")
            if ctx.has_partial_data:
                return RecoveryDecision(RecoveryAction.DEGRADE, "Using partial results")
            return RecoveryDecision(RecoveryAction.SKIP, "Skipping failed step")

        # 5. Network error -> retry
        if ctx.error_type == "network":
            if ctx.retries_used < ctx.max_retries:
                return RecoveryDecision(RecoveryAction.RETRY, "Network error, retrying")
            return RecoveryDecision(RecoveryAction.ABORT, "Network unavailable")

        # 6. Default: retry if possible, otherwise degrade
        if ctx.retries_used < ctx.max_retries:
            return RecoveryDecision(RecoveryAction.RETRY, "Retrying...")
        if ctx.has_partial_data:
            return RecoveryDecision(RecoveryAction.DEGRADE, "Using partial results")
        if not ctx.critical_step:
            return RecoveryDecision(RecoveryAction.SKIP, "Skipping non-critical step")
        return RecoveryDecision(RecoveryAction.ABORT, "Cannot recover critical step")

    def classify_error(self, error_message: str) -> str:
        msg = error_message.lower()
        if any(w in msg for w in ("timeout", "timed out", "deadline")):
            return "timeout"
        if any(w in msg for w in ("permission", "denied", "unauthorized", "forbidden")):
            return "permission"
        if any(w in msg for w in ("schema", "validation", "invalid", "malformed")):
            return "schema"
        if any(w in msg for w in ("provider", "service unavailable", "internal error")):
            return "provider"
        if any(w in msg for w in ("network", "connection", "dns", "unreachable")):
            return "network"
        return "unknown"
