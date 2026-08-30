"""测试 Model Router 与 Safeguards。"""
import sys
import pytest
from dududa.router.router import (
    ModelRole, ModelConfig, RouterConfig, ModelRouter,
)
from dududa.safeguards.safeguards import (
    IdentityValidator, IdentityCheck, PrivacyGuard, PrivacyLevel,
    PrivacyScope, BudgetTracker, Permission,
)
from dududa.core.envelope import Actor, Platform


class TestRouterConfig:
    def test_default_config(self):
        config = RouterConfig.default_config()
        for role in ModelRole:
            assert config.get(role) is not None

    def test_get_missing_role(self):
        config = RouterConfig()
        assert config.get(ModelRole.PERCEPTION) is None


class TestModelRouter:
    @pytest.mark.asyncio
    async def test_stub_response(self):
        router = ModelRouter()
        response = await router.route(ModelRole.PERCEPTION, [{"role": "user", "content": "hello"}])
        assert isinstance(response, str)

    @pytest.mark.asyncio
    async def test_stub_ignore_response(self):
        router = ModelRouter()
        response = await router.route(ModelRole.DECISION, [{"role": "user", "content": "test"}])
        assert "ignore" in response

    @pytest.mark.asyncio
    async def test_direct_chat_stub_never_echoes_user(self):
        router = ModelRouter()
        user_text = "把这句话原样复读给我"
        response = await router.route(
            ModelRole.DIRECT_CHAT, [{"role": "user", "content": user_text}])
        assert response != user_text
        assert "有点卡" in response


class TestIdentityValidator:
    def test_valid_actor(self):
        actor = Actor(actor_id="user_1", platform=Platform.QQ, display_name="小明")
        check = IdentityValidator.validate(actor)
        assert check.verified

    def test_invalid_actor_no_id(self):
        actor = Actor(actor_id="unknown", platform=Platform.QQ, display_name="")
        check = IdentityValidator.validate(actor)
        assert not check.verified

    def test_verify_consistency(self):
        actor = Actor(actor_id="user_1", platform=Platform.QQ, display_name="test")
        assert IdentityValidator.verify_consistency(actor, "user_1")
        assert not IdentityValidator.verify_consistency(actor, "different")


class TestPrivacyGuard:
    def test_public_access(self):
        guard = PrivacyGuard()
        guard.register_scope("public_data", PrivacyScope(level=PrivacyLevel.PUBLIC))
        assert guard.check_access("public_data", "anyone")

    def test_private_access(self):
        guard = PrivacyGuard()
        guard.register_scope("private_data", PrivacyScope(level=PrivacyLevel.PRIVATE, allowed_actors=("user_1",)))
        assert guard.check_access("private_data", "user_1")
        assert not guard.check_access("private_data", "user_2")

    def test_nonexistent_scope(self):
        guard = PrivacyGuard()
        assert not guard.check_access("nonexistent", "anyone")

    def test_redact(self):
        guard = PrivacyGuard()
        result = guard.redact("我的电话是13800138000", ("13800138000",))
        assert "[REDACTED]" in result
        assert "13800138000" not in result


class TestBudgetTracker:
    def test_initial_state(self):
        bt = BudgetTracker()
        assert bt.can_call_model()
        assert bt.can_use_tool()
        assert bt.can_use_tokens(100)

    def test_record_model_call(self):
        bt = BudgetTracker(max_model_calls=2)
        bt = bt.record_model_call()
        assert bt.model_calls_used == 1
        bt = bt.record_model_call()
        assert bt.model_calls_used == 2
        assert not bt.can_call_model()

    def test_record_tool_step(self):
        bt = BudgetTracker(max_tool_steps=2)
        bt = bt.record_tool_step()
        bt = bt.record_tool_step()
        assert not bt.can_use_tool()

    def test_token_tracking(self):
        bt = BudgetTracker(max_tokens=1000)
        assert bt.can_use_tokens(500)
        bt = bt.record_model_call(tokens=500)
        assert bt.tokens_used == 500
        assert not bt.can_use_tokens(600)
