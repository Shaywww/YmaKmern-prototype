"""Phase 3 —— typed config 与稳定错误类型测试。"""
import pytest
from pydantic import ValidationError

from packages.core.config import BotConfig, ModelConfig, ModelRole
from packages.core.errors import (
    CapabilityError, ConfigError, DeliveryError, DududaError,
    MemoryError, ModelError, UnauthorizedError,
)


class TestTypedConfig:
    def test_defaults(self):
        cfg = BotConfig()
        assert cfg.default_role == "normal"
        assert cfg.tool_max_steps == 4
        assert cfg.memory_ttl_seconds == 7 * 86400
        assert cfg.mcp_enabled is True

    def test_valid_values(self):
        cfg = BotConfig(
            owner_ids=("u1",),
            tool_max_steps=8,
            model_roles={ModelRole.DIRECT_CHAT: ModelConfig(model_id="gpt-5.5", temperature=0.3)},
        )
        assert cfg.owner_ids == ("u1",)
        assert cfg.model_roles[ModelRole.DIRECT_CHAT].model_id == "gpt-5.5"

    def test_reject_invalid_role(self):
        with pytest.raises(ValidationError):
            BotConfig(default_role="superuser")

    def test_reject_negative_memory_ttl(self):
        with pytest.raises(ValidationError):
            BotConfig(memory_ttl_seconds=-1)

    def test_reject_tool_steps_out_of_range(self):
        with pytest.raises(ValidationError):
            BotConfig(tool_max_steps=9)

    def test_reject_empty_model_id(self):
        with pytest.raises(ValidationError):
            ModelConfig(model_id="")

    def test_reject_invalid_temperature(self):
        with pytest.raises(ValidationError):
            ModelConfig(temperature=2.5)

    def test_reject_invalid_log_level(self):
        with pytest.raises(ValidationError):
            BotConfig(log_level="TRACE")


class TestErrors:
    def test_base_error_fields(self):
        e = DududaError("坏了", reason="boom", recoverable=True)
        assert e.reason == "boom"
        assert e.recoverable is True
        assert e.message == "坏了"

    def test_hierarchy(self):
        assert issubclass(UnauthorizedError, DududaError)
        assert issubclass(ConfigError, DududaError)
        assert issubclass(ModelError, DududaError)
        assert issubclass(CapabilityError, DududaError)
        assert issubclass(MemoryError, DududaError)
        assert issubclass(DeliveryError, DududaError)

    def test_default_reasons(self):
        assert UnauthorizedError().reason == "denied"
        assert ModelError().recoverable is True
        assert ConfigError("x", field="owner_ids").field == "owner_ids"

    def test_raise_catch(self):
        with pytest.raises(DududaError) as ei:
            raise UnauthorizedError("非 owner 禁止", reason="role_too_low")
        assert ei.value.reason == "role_too_low"
        assert ei.value.recoverable is False
