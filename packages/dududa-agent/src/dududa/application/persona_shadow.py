"""Low-frequency, non-blocking online persona quality evaluation.

Only numeric scores and deterministic violation names are persisted.  Raw
messages/replies are evaluated in memory and never written to the quality
store.  Sensitive requests reuse the official-only MEMORY_SUMMARY route so
they cannot fall through to the third-party fallback provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from dududa.core.quality_eval import (
    LLMPersonaJudge,
    PersonaQualityRecord,
    PersonaQualityStore,
    persona_contract_violations,
)
from dududa.core.trace_recorder import trace_recorder
from dududa.router.router import ModelDataClass, ModelRequest, ModelRole

from .dududa_utils import _redact_text


logger = logging.getLogger("dududa20.persona_shadow")


class PersonaShadowEvaluator:
    """Persistent daily sampler plus official-provider persona judge."""

    def __init__(self, *, store: PersonaQualityStore, state_path,
                 sample_rate: float = 0.05, daily_limit: int = 12,
                 enabled: bool = True):
        self.store = store
        self.state_path = Path(state_path)
        self.sample_rate = min(1.0, max(0.0, float(sample_rate)))
        self.daily_limit = max(0, int(daily_limit))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    def _load_state(self, day: str) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("day") == day:
                return {
                    "day": day,
                    "seen": max(0, int(value.get("seen", 0))),
                    "sampled": max(0, int(value.get("sampled", 0))),
                }
        except (OSError, TypeError, ValueError):
            pass
        return {"day": day, "seen": 0, "sampled": 0}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def reserve(self, run_id: str, now: float | None = None) -> bool:
        """Reserve a daily sample deterministically; first delivery warms up."""
        if not self.enabled or self.daily_limit <= 0 or self.sample_rate <= 0:
            return False
        stamp = float(now or time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(stamp))
        digest = hashlib.sha256(f"{day}:{run_id}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") / float(2 ** 64)
        with self._lock:
            state = self._load_state(day)
            state["seen"] += 1
            selected = (
                state["sampled"] < self.daily_limit
                and (state["sampled"] == 0 or bucket < self.sample_rate)
            )
            if selected:
                state["sampled"] += 1
            try:
                self._save_state(state)
            except OSError:
                logger.warning("Persona shadow sampler state was not saved",
                               exc_info=True)
                return False
        return selected

    async def evaluate(self, plugin, event, *, user_message: str,
                       response: str, run_id: str, trace_id: str) -> None:
        router = getattr(plugin, "_model_router", None)
        provider = getattr(getattr(plugin, "_core", None),
                           "_llm_provider", None)
        if router is None or provider is None:
            return
        clean_user = _redact_text(str(user_message or ""))[:1000]
        clean_response = _redact_text(str(response or ""))[:2000]
        if not clean_response:
            return

        async def complete(system: str, user: str) -> str:
            result = await router.route_request(
                ModelRequest(
                    role=ModelRole.MEMORY_SUMMARY,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    data_class=ModelDataClass.SENSITIVE,
                    max_tokens=300,
                    temperature=0.0,
                    metadata={
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "task": "persona_shadow",
                    },
                ),
                provider=provider,
            )
            return result.text or ""

        try:
            score = await LLMPersonaJudge(complete).evaluate(
                clean_user, clean_response)
            violations = persona_contract_violations(
                clean_response, casual_chat=False)
            scope_hash, is_group = _scope_identity(event)
            record = PersonaQualityRecord(
                run_id=run_id,
                trace_id=trace_id,
                scope_hash=scope_hash,
                is_group=is_group,
                persona_consistency=score.persona_consistency,
                conversationality=score.conversationality,
                non_customer_tone=score.non_customer_tone,
                overall=score.overall,
                violations=violations,
                observed_at=time.time(),
            )
            self.store.append(record)
            trace_recorder.record(
                event="persona_shadow_score",
                run_id=run_id,
                trace_id=trace_id,
                scope_hash=scope_hash,
                is_group=is_group,
                persona_consistency=score.persona_consistency,
                conversationality=score.conversationality,
                non_customer_tone=score.non_customer_tone,
                overall=score.overall,
                violations=list(violations),
            )
        except Exception as exc:
            logger.warning("Persona shadow evaluation failed | run_id=%s: %s",
                           run_id, exc)
            trace_recorder.record(
                event="persona_shadow_error",
                run_id=run_id,
                trace_id=trace_id,
                error=type(exc).__name__,
            )


def _scope_identity(event) -> tuple[str, bool]:
    try:
        session = str(event.get_session_id() or "")
    except Exception:
        session = ""
    try:
        bot_id = str(event.get_self_id() or "")
    except Exception:
        bot_id = ""
    try:
        is_group = bool(getattr(event.message_obj, "group", None))
    except Exception:
        is_group = False
    opaque = hashlib.sha256(
        f"{bot_id}|{session}|{'group' if is_group else 'private'}".encode(
            "utf-8")
    ).hexdigest()[:16]
    return opaque, is_group


def create_persona_shadow(plugin_data_dir: str) -> PersonaShadowEvaluator:
    root = Path(os.environ.get(
        "DUDUDA_PERSONA_SHADOW_DIR",
        os.path.join(plugin_data_dir, "data", "persona_quality"),
    ))
    state_path = os.environ.get(
        "DUDUDA_PERSONA_SHADOW_STATE",
        os.path.join(plugin_data_dir, "data", "persona_shadow_state.json"),
    )
    return PersonaShadowEvaluator(
        store=PersonaQualityStore(root),
        state_path=state_path,
        sample_rate=float(os.environ.get(
            "DUDUDA_PERSONA_SHADOW_RATE", "0.05")),
        daily_limit=int(os.environ.get(
            "DUDUDA_PERSONA_SHADOW_DAILY_LIMIT", "12")),
        enabled=os.environ.get("DUDUDA_PERSONA_SHADOW", "1") == "1",
    )


def schedule_persona_shadow(plugin, event, *, user_message: str,
                            response: str, run_id: str,
                            trace_id: str) -> bool:
    """Schedule a sampled evaluation without delaying the sent reply."""
    evaluator = getattr(plugin, "persona_shadow", None)
    if evaluator is None or not evaluator.reserve(run_id):
        return False
    task = asyncio.create_task(evaluator.evaluate(
        plugin,
        event,
        user_message=user_message,
        response=response,
        run_id=run_id,
        trace_id=trace_id,
    ))
    tasks = getattr(plugin, "_persona_shadow_tasks", None)
    if tasks is None:
        tasks = plugin._persona_shadow_tasks = set()
    tasks.add(task)

    def finished(done):
        tasks.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(finished)
    return True
