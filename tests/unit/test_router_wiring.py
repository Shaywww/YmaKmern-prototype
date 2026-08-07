# -*- coding: utf-8 -*-
"""Model Router 生产接线（文档 2.5.7）。

- per-call max_tokens/temperature 预算覆盖生效
- route_request 支持 provider 临时注入（测试/会话级切换）
- OpenAIProvider 按模型选择 base + key（多网关降级）
- main.py 装配：8 类角色、主模型/降级/视觉角色、ROUTER_ENABLED 开关
"""
import os, sys, types
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dududa_main_wiring", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

import pytest
from dududa.router.router import (
    ModelRole, ModelError, ModelErrorKind,
    ModelRequest, RouterConfig, ModelRouter,
)
from dududa.router.openai_provider import OpenAIProvider


def _make_context():
    try:
        return main.star.Context()
    except TypeError:
        from unittest import mock
        return mock.Mock()


class _RecProvider:
    """记录 model/msgs/config 的假 Provider。"""

    def __init__(self, text="ok", err=None):
        self.calls = []
        self.text = text
        self.err = err

    async def complete(self, model_id, msgs, config=None):
        self.calls.append((model_id, msgs, config))
        if self.err:
            raise self.err
        return self.text

    def health(self, model_id):
        return True


class TestPerCallBudget:
    @pytest.mark.asyncio
    async def test_route_request_honors_per_call_budget(self):
        prov = _RecProvider()
        router = ModelRouter(provider=prov)
        resp = await router.route_request(ModelRequest(
            role=ModelRole.RESPONSE_COMPOSITION,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=777, temperature=0.1))
        assert resp.text == "ok"
        _m, _msgs, cfg = prov.calls[0]
        assert cfg.max_tokens == 777
        assert cfg.temperature == 0.1

    @pytest.mark.asyncio
    async def test_route_request_default_budget_when_unset(self):
        prov = _RecProvider()
        router = ModelRouter(provider=prov)
        await router.route_request(ModelRequest(
            role=ModelRole.DIRECT_CHAT,
            messages=[{"role": "user", "content": "hi"}]))
        cfg = prov.calls[0][2]
        expected = RouterConfig.default_config().get(ModelRole.DIRECT_CHAT)
        assert cfg.max_tokens == expected.max_tokens
        assert cfg.temperature == expected.temperature


class TestProviderOverride:
    @pytest.mark.asyncio
    async def test_route_request_provider_override_wins(self):
        bad = _RecProvider(err=ModelError(
            ModelErrorKind.PROVIDER_UNAVAILABLE, retryable=True))
        good = _RecProvider(text="B-ok")
        router = ModelRouter(provider=bad)
        resp = await router.route_request(
            ModelRequest(role=ModelRole.DIRECT_CHAT,
                         messages=[{"role": "user", "content": "hi"}]),
            provider=good)
        assert resp.text == "B-ok"
        assert bad.calls == []

    @pytest.mark.asyncio
    async def test_route_request_fallback_uses_effective_provider(self):
        class Flaky(_RecProvider):
            def __init__(self):
                super().__init__(text="fb-ok")
                self.failed = False

            async def complete(self, model_id, msgs, config=None):
                self.calls.append((model_id, msgs, config))
                if not self.failed and config.fallback_model_id:
                    self.failed = True
                    raise ModelError(ModelErrorKind.RATE_LIMITED,
                                     retryable=True)
                return self.text

        cfg = RouterConfig.default_config()
        role = ModelRole.DIRECT_CHAT
        base = cfg.get(role)
        import dataclasses
        cfg = RouterConfig(roles={
            role: dataclasses.replace(base, fallback_model_id="fb-model")})
        prov = Flaky()
        router = ModelRouter(config=cfg, provider=prov)
        resp = await router.route_request(ModelRequest(
            role=role, messages=[{"role": "user", "content": "hi"}]))
        assert resp.text == "fb-ok"
        assert resp.degraded is True
        assert prov.calls[-1][0] == "fb-model"


class TestOpenAIProviderMultiGateway:
    def test_base_and_key_selected_by_model(self):
        prov = OpenAIProvider(
            api_key="main-key", base_url="https://api.deepseek.com/v1",
            base_urls={"gpt-5.5": "https://fallback.example/v1",
                       "claude-x": "https://vision.example/v1"},
            api_keys={"gpt-5.5": "fb-key", "claude-x": "vis-key"})
        assert prov._base_for("deepseek-chat") == "https://api.deepseek.com/v1"
        assert prov._base_for("gpt-5.5") == "https://fallback.example/v1"
        assert prov._base_for("claude-x") == "https://vision.example/v1"
        assert prov._key_for("deepseek-chat") == "main-key"
        assert prov._key_for("gpt-5.5") == "fb-key"
        assert prov._key_for("claude-x") == "vis-key"


class TestMainWiring:
    def test_router_config_has_8_roles(self):
        cfg = main.router_config
        assert len(cfg.roles) == 8
        for role in ModelRole:
            m = cfg.get(role).model_id
            if role in (ModelRole.IMAGE_UNDERSTANDING, ModelRole.IMAGE_GENERATION):
                assert m == main.VISION_MODEL
            else:
                assert m == main.MODEL
        comp = cfg.get(ModelRole.RESPONSE_COMPOSITION)
        assert comp.fallback_model_id == main.FALLBACK_MODEL
        assert comp.allow_sensitive is False
        assert comp.route_hint_allowed is False

    @pytest.mark.asyncio
    async def test_call_llm_uses_router_with_injected_provider(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "MEMORY_FILE", str(tmp_path / "memory.json"))
        monkeypatch.setattr(main, "CONFIRM_FILE", str(tmp_path / "confirmations.json"))
        plugin = main.Main(_make_context())
        prov = _RecProvider(text="嗨嗨~ 测试回复 (≧▽≦)")
        plugin._core._llm_provider = prov
        assert plugin._core._model_router is not None
        reply = await plugin._core._call_llm("你是嘟嘟哒。", "你好")
        assert prov.calls, "router should call injected provider"
        assert prov.calls[0][0] == main.MODEL
        assert "你好" in prov.calls[0][1][-1]["content"]
        assert reply
