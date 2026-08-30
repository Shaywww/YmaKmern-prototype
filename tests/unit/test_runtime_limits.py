from tests.path_config import PLUGIN_DIR, PLUGIN_MAIN
# -*- coding: utf-8 -*-
"""2.5.9 Runtime 限流与预算：RateLimiter / TokenBudget / RuntimeLimits / 生产接线。"""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PLUGIN_DIR))

import pytest

from dududa.safeguards.limits import (
    RateLimiter, TokenBudget, RuntimeLimits, make_runtime_limits_from_env,
)
from dududa.core.trace_recorder import trace_recorder
from dududa.core.perception import PerceptionResult
from tests.unit.test_p4_production_runtime import (
    main, _FakeEvent, _make_envelope, _make_orchestrator,
)


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class TestRateLimiter:
    def test_allows_under_limit(self):
        rl = RateLimiter(max_events=3, window_seconds=60)
        for _ in range(3):
            res = rl.check("k1")
            assert res.allowed
            assert res.remaining >= 0

    def test_denies_over_limit(self):
        rl = RateLimiter(max_events=2, window_seconds=60)
        assert rl.check("k1").allowed
        assert rl.check("k1").allowed
        res = rl.check("k1")
        assert not res.allowed
        assert res.reason == "rate_limited"
        assert res.retry_after_seconds > 0

    def test_window_slides(self):
        now = [100.0]
        rl = RateLimiter(max_events=1, window_seconds=10, now_fn=lambda: now[0])
        assert rl.check("k1").allowed
        assert not rl.check("k1").allowed
        now[0] += 11
        assert rl.check("k1").allowed

    def test_keys_isolated(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        assert rl.check("a").allowed
        assert rl.check("b").allowed

    def test_reset(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        rl.check("k1")
        rl.reset("k1")
        assert rl.check("k1").allowed

    def test_batch_cost(self):
        rl = RateLimiter(max_events=3, window_seconds=60)
        assert rl.check("k1", cost=3).allowed
        assert not rl.check("k1").allowed


class TestTokenBudget:
    def test_spend_and_remaining(self):
        b = TokenBudget(daily_limit=100)
        assert b.check("u1", tokens=40).allowed
        res = b.spend("u1", tokens=40)
        assert res.allowed
        assert res.remaining_tokens == 60
        assert b.check("u1", tokens=70).remaining_tokens == 60

    def test_over_limit_denied(self):
        b = TokenBudget(daily_limit=10)
        assert b.spend("u1", 10).allowed
        res = b.spend("u1", 1)
        assert not res.allowed
        assert res.reason == "budget_exhausted"
        assert res.remaining_tokens == 0

    def test_daily_rollover(self):
        day = ["2026-08-06"]
        b = TokenBudget(daily_limit=10, today_fn=lambda: day[0])
        b.spend("u1", 10)
        assert not b.check("u1", 1).allowed
        day[0] = "2026-08-07"
        assert b.check("u1", 1).allowed

    def test_persistence_round_trip(self, tmp_path):
        f = str(tmp_path / "budget.json")
        b1 = TokenBudget(daily_limit=100, state_file=f)
        b1.spend("u1", 30)
        b2 = TokenBudget(daily_limit=100, state_file=f)
        assert b2.check("u1", 30).remaining_tokens == 70

    def test_corrupt_file_tolerated(self, tmp_path):
        f = tmp_path / "budget.json"
        f.write_text("{not json", encoding="utf-8")
        b = TokenBudget(daily_limit=100, state_file=str(f))
        assert b.check("u1", 5).allowed

    def test_keys_isolated(self):
        b = TokenBudget(daily_limit=10)
        b.spend("a", 10)
        assert b.check("b", 10).allowed


class TestRuntimeLimitsFacade:
    def test_check_message_records_trace(self):
        lim = RuntimeLimits(
            RateLimiter(max_events=1, window_seconds=60),
            TokenBudget(daily_limit=1000))
        res = lim.check_message("user-1", run_id="rl-259-1", trace_id="tr-259-1")
        assert res.allowed
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "rate_limit"
                 and x.get("run_id") == "rl-259-1"]
        assert len(lines) == 1
        assert lines[0]["allowed"] is True
        assert lines[0]["actor"] != "user-1"     # trace 中不落原始 UID
        assert len(lines[0]["actor"]) == 12

    def test_budget_trace_event(self):
        lim = RuntimeLimits(
            RateLimiter(max_events=10, window_seconds=60),
            TokenBudget(daily_limit=100))
        res = lim.spend_tokens("u1", 40, run_id="bd-259-1", trace_id="tr-259-1")
        assert res.allowed
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "budget"
                 and x.get("run_id") == "bd-259-1"]
        assert len(lines) == 1
        assert lines[0]["allowed"] is True
        assert lines[0]["remaining_tokens"] == 60

    def test_hints_are_set(self):
        lim = RuntimeLimits(
            RateLimiter(max_events=1, window_seconds=60),
            TokenBudget(daily_limit=10))
        assert lim.RATE_LIMIT_HINT
        assert lim.BUDGET_HINT


class TestEnvFactory:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("DUDUDA_LIMITS_ENABLED", raising=False)
        monkeypatch.delenv("DUDUDA_RATE_LIMIT_PER_MIN", raising=False)
        monkeypatch.delenv("DUDUDA_TOKEN_BUDGET_DAILY", raising=False)
        monkeypatch.delenv("DUDUDA_BUDGET_FILE", raising=False)
        lim = make_runtime_limits_from_env()
        assert lim is not None
        assert lim._rate._max == 60
        assert lim._budget._limit == 200_000
        assert lim._budget._file is None

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("DUDUDA_LIMITS_ENABLED", "0")
        assert make_runtime_limits_from_env() is None

    def test_custom(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DUDUDA_RATE_LIMIT_PER_MIN", "5")
        monkeypatch.setenv("DUDUDA_TOKEN_BUDGET_DAILY", "999")
        monkeypatch.setenv("DUDUDA_BUDGET_FILE", str(tmp_path / "b.json"))
        lim = make_runtime_limits_from_env()
        assert lim._rate._max == 5
        assert lim._budget._limit == 999
        assert lim._budget._file == str(tmp_path / "b.json")

    def test_budget_file_default_override(self, tmp_path):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("DUDUDA_BUDGET_FILE", raising=False)
        d = str(tmp_path / "x" / "budget.json")
        lim = make_runtime_limits_from_env(budget_file_default=d)
        assert lim._budget._file == d
        monkeypatch.undo()


class TestRealPluginAssembly:
    def test_real_plugin_wires_limits(self):
        plugin = main.Main(_make_context())
        assert plugin.limits is not None
        assert plugin.limits._rate._max == 60
        assert plugin.limits._budget._limit == 200_000


class TestOrchestratorRateGate:
    @pytest.mark.asyncio
    async def test_rate_limited_returns_hint_without_llm(self):
        orch, plugin, memory, reg = _make_orchestrator()
        plugin.limits = RuntimeLimits(
            RateLimiter(max_events=1, window_seconds=60),
            TokenBudget(daily_limit=10 ** 9))
        plugin.limits.check_message("user_1")   # 消耗唯一配额
        res = await orch.run(
            _make_envelope("你好"), perception=PerceptionResult(),
            event=_FakeEvent("你好"))
        assert res.has_visible_output
        assert RuntimeLimits.RATE_LIMIT_HINT in res.final_response.text
        assert plugin.last_user_msg == ""        # 未触发 LLM 合成

    @pytest.mark.asyncio
    async def test_without_limits_unaffected(self):
        orch, plugin, memory, reg = _make_orchestrator()
        res = await orch.run(
            _make_envelope("你好"), perception=PerceptionResult(),
            event=_FakeEvent("你好"))
        assert res.final_response.text == plugin.llm_reply

    @pytest.mark.asyncio
    async def test_budget_exhausted_replaces_draft(self):
        orch, plugin, memory, reg = _make_orchestrator()
        plugin.limits = RuntimeLimits(
            RateLimiter(max_events=10 ** 6, window_seconds=60),
            TokenBudget(daily_limit=1))
        res = await orch.run(
            _make_envelope("你好"), perception=PerceptionResult(),
            event=_FakeEvent("你好"))
        assert RuntimeLimits.BUDGET_HINT in res.final_response.text
