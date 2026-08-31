import json
from types import SimpleNamespace

import pytest

from dududa.application.persona_shadow import PersonaShadowEvaluator
from dududa.core.quality_eval import PersonaQualityRecord, PersonaQualityStore
from dududa.router.router import ModelDataClass, ModelRole


def test_quality_store_persists_scores_but_no_conversation_text(tmp_path):
    store = PersonaQualityStore(tmp_path / "quality")
    store.append(PersonaQualityRecord(
        run_id="run-1",
        trace_id="trace-1",
        scope_hash="opaque",
        is_group=True,
        persona_consistency=0.9,
        conversationality=0.8,
        non_customer_tone=1.0,
        overall=0.9,
        violations=("customer_template",),
        observed_at=1_788_105_600.0,
    ))
    path = next((tmp_path / "quality").glob("*.jsonl"))
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert "user_message" not in payload
    assert "response" not in payload
    assert "rationale" not in payload
    summary = store.summary(days=1, now=1_788_105_600.0)
    assert summary["sample_count"] == 1
    assert summary["overall"] == 0.9
    assert summary["violations"] == {"customer_template": 1}


def test_sampler_forces_first_daily_sample_and_honours_cap(tmp_path):
    evaluator = PersonaShadowEvaluator(
        store=PersonaQualityStore(tmp_path / "quality"),
        state_path=tmp_path / "state.json",
        sample_rate=1.0,
        daily_limit=1,
    )
    now = 1_788_105_600.0
    assert evaluator.reserve("run-1", now=now) is True
    assert evaluator.reserve("run-2", now=now) is False
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["seen"] == 2
    assert state["sampled"] == 1


@pytest.mark.asyncio
async def test_online_judge_uses_sensitive_official_only_role_and_stores_no_raw(
        tmp_path):
    calls = []

    class Router:
        async def route_request(self, request, provider=None):
            calls.append((request, provider))
            return SimpleNamespace(text=(
                '{"persona_consistency":0.9,"conversationality":0.8,'
                '"non_customer_tone":1.0,"rationale":"自然且没有客服腔"}'
            ))

    class Event:
        message_obj = SimpleNamespace(group="g1")
        def get_session_id(self): return "group-481757927"
        def get_self_id(self): return "bot-1"

    store = PersonaQualityStore(tmp_path / "quality")
    evaluator = PersonaShadowEvaluator(
        store=store,
        state_path=tmp_path / "state.json",
        sample_rate=1.0,
        daily_limit=2,
    )
    provider = object()
    plugin = SimpleNamespace(
        _model_router=Router(),
        _core=SimpleNamespace(_llm_provider=provider),
    )
    await evaluator.evaluate(
        plugin,
        Event(),
        user_message="这是绝不能落盘的用户原话",
        response="行吧，勉强陪你聊两句 (≧▽≦)",
        run_id="r1",
        trace_id="t1",
    )
    assert len(calls) == 1
    request, used_provider = calls[0]
    assert request.role == ModelRole.MEMORY_SUMMARY
    assert request.data_class == ModelDataClass.SENSITIVE
    assert request.metadata["task"] == "persona_shadow"
    assert used_provider is provider
    path = next((tmp_path / "quality").glob("*.jsonl"))
    raw = path.read_text(encoding="utf-8")
    assert "绝不能落盘" not in raw
    assert "勉强陪你" not in raw
    assert "自然且没有客服腔" not in raw
    payload = json.loads(raw)
    assert payload["overall"] == 0.9
    assert payload["scope_hash"] != "group-481757927"
