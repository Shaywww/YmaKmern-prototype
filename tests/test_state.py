"""测试 RuntimeState 状态机。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from packages.core.state import (
    RuntimeState, RuntimePhase, RunOutcome, SocialAction,
    RuntimeBudget, ToolStep, ToolPlan, ToolPlanStatus,
)


class TestRuntimeBudget:
    def test_default_budget(self):
        b = RuntimeBudget()
        assert b.max_model_calls == 6
        assert b.max_tool_steps == 4
        assert not b.is_expired()

    def test_deadline(self):
        b = RuntimeBudget(deadline_seconds=0.001)
        import time
        time.sleep(0.01)
        assert b.is_expired()


class TestToolPlan:
    def test_pending_steps(self):
        steps = (
            ToolStep(step_id="s1", capability_id="c1", arguments={}, purpose="test"),
            ToolStep(step_id="s2", capability_id="c2", arguments={}, purpose="test2"),
        )
        plan = ToolPlan(goal="test", steps=steps)
        assert len(plan.pending_steps) == 2

    def test_all_completed(self):
        steps = (
            ToolStep(
                step_id="s1", capability_id="c1", arguments={}, purpose="test",
                status=ToolPlanStatus.SUCCEEDED,
            ),
        )
        plan = ToolPlan(goal="test", steps=steps)
        assert plan.all_completed


class TestRuntimeState:
    def test_initial_state(self):
        state = RuntimeState()
        assert state.phase == RuntimePhase.RECEIVED
        assert not state.is_terminal

    def test_transition(self):
        state = RuntimeState()
        next_state = state.transition(
            RuntimePhase.VALIDATED,
            social_decision=SocialAction.IGNORE,
        )
        assert next_state.phase == RuntimePhase.VALIDATED
        assert next_state.social_decision == SocialAction.IGNORE
        assert len(next_state.trace) == 1
        # Original unchanged
        assert state.phase == RuntimePhase.RECEIVED

    def test_with_error(self):
        state = RuntimeState()
        err = state.with_error("test error")
        assert "test error" in err.errors
        assert len(err.errors) == 1

    def test_terminal_phases(self):
        for phase in (RuntimePhase.COMPLETED, RuntimePhase.ABORTED, RuntimePhase.CANCELLED):
            state = RuntimeState(phase=phase)
            assert state.is_terminal

    def test_non_terminal_phases(self):
        for phase in (RuntimePhase.RECEIVED, RuntimePhase.DECIDED, RuntimePhase.COMPOSED):
            state = RuntimeState(phase=phase)
            assert not state.is_terminal
