"""P1: Phase 6 Agent Runtime + Phase 7 Capability — canonical actions, decision
engine, router eight-role contract, capability Top-K retrieval, executor
re-authorization. These tests encode the document contracts (2.4.8/2.4.10/
2.4.11/2.4.12/2.5.7)."""
import sys

import pytest

from dududa.core.state import SocialAction
from dududa.core.decision import SocialDecision, SocialDecisionEngine
from dududa.core.perception import PerceptionResult, SpeechAct
from dududa.core.capability import (
    Capability, CapabilityQuery, CapabilityRegistry, CapabilityRisk,
    CapabilitySchema, ProviderType,
)
from dududa.router.router import (
    ModelRole, ModelDataClass, ModelError, ModelErrorKind, ModelRequest,
    ModelConfig, RouterConfig, ModelRouter, CredentialResolver,
)
from dududa.planner.executor import ToolExecutor, ExecutionContext, AuthorizationError
from dududa.planner.planner import PlannedStep, GeneratedPlan
from dududa.router import openai_provider as op_module


# ---------------------------------------------------------------- SocialAction
class TestSocialActionCanonical:
    """Doc 2.4.8: six canonical actions + BLOCK extension; aliases keep old code working."""

    def test_canonical_values(self):
        values = {a.value for a in SocialAction}
        assert values == {
            "ignore", "react", "direct_reply", "ask_clarification",
            "use_tools", "defer", "block",
        }

    def test_aliases_same_member(self):
        assert SocialAction.ANSWER is SocialAction.DIRECT_REPLY
        assert SocialAction.ASK is SocialAction.ASK_CLARIFICATION
        assert SocialAction.ANSWER.value == "direct_reply"
        assert SocialAction.ASK.value == "ask_clarification"

    def test_legacy_properties(self):
        assert SocialDecision(action=SocialAction.ANSWER).should_reply
        assert not SocialDecision(action=SocialAction.ANSWER).should_ask
        assert SocialDecision(action=SocialAction.ASK).should_ask
        assert SocialDecision(action=SocialAction.BLOCK).is_blocked
        assert not SocialDecision(action=SocialAction.IGNORE).should_reply


# ------------------------------------------------------------ Decision engine
class TestDecisionEngine:
    def _perceive(self, **kw):
        base = dict(schema_version="1.0", confidence=0.8)
        base.update(kw)
        return PerceptionResult(**base)

    def test_command_with_tools(self):
        p = self._perceive(is_explicit_command=True, needs_tools=True)
        d = SocialDecisionEngine().decide(p)
        assert d.action == SocialAction.USE_TOOLS
        assert d.should_use_tools

    def test_command_without_tools(self):
        p = self._perceive(is_explicit_command=True, needs_tools=False)
        d = SocialDecisionEngine().decide(p)
        assert d.action == SocialAction.DIRECT_REPLY

    def test_mention_with_tools(self):
        p = self._perceive(has_explicit_mention=True, needs_tools=True)
        d = SocialDecisionEngine().decide(p)
        assert d.action == SocialAction.USE_TOOLS

    def test_ambiguity_question_asks_clarification(self):
        p = self._perceive(
            speech_acts=(SpeechAct(act_type="question", confidence=0.9),),
            ambiguities=("你说的是哪个课程？",),
        )
        d = SocialDecisionEngine().decide(p)
        assert d.action == SocialAction.ASK_CLARIFICATION
        assert d.clarification_question and "课程" in d.clarification_question

    def test_ambiguity_statement_not_ask(self):
        p = self._perceive(
            speech_acts=(SpeechAct(act_type="statement", confidence=0.9),),
            ambiguities=("有点歧义",),
        )
        d = SocialDecisionEngine().decide(p)
        assert d.action != SocialAction.ASK_CLARIFICATION

    def test_default_ignore_when_no_triggers(self):
        p = self._perceive()
        engine = SocialDecisionEngine(reply_probability=0.0)
        d = engine.decide(p)
        assert d.action == SocialAction.IGNORE


# ------------------------------------------------------------------- Router
class TestRouterEightRoles:
    """Doc 2.5.7: eight canonical roles; aliases are not canonical."""

    CANONICAL = {
        "perception", "social_decision", "tool_planning", "direct_chat",
        "response_composition", "memory_summary", "image_understanding",
        "image_generation",
    }

    def test_eight_canonical_roles(self):
        values = {r.value for r in ModelRole}
        assert values == self.CANONICAL
        assert len(values) == 8

    def test_default_config_covers_all_roles(self):
        config = RouterConfig.default_config()
        for role in ModelRole:
            assert config.get(role) is not None

    def test_legacy_aliases(self):
        assert ModelRole.COMPOSER.value == "response_composition"
        assert ModelRole.RENDERER == ModelRole.RESPONSE_COMPOSITION
        assert ModelRole.DECISION == ModelRole.SOCIAL_DECISION

    @pytest.mark.asyncio
    async def test_route_returns_stub_string(self):
        router = ModelRouter()
        out = await router.route(ModelRole.DIRECT_CHAT, [{"role": "user", "content": "hi"}])
        assert isinstance(out, str)

    @pytest.mark.asyncio
    async def test_route_request_no_route(self):
        router = ModelRouter(config=RouterConfig())
        with pytest.raises(ModelError) as ei:
            await router.route_request(ModelRequest(role=ModelRole.DIRECT_CHAT, messages=[{"role": "user", "content": "hi"}]))
        assert ei.value.kind == ModelErrorKind.NO_ROUTE

    @pytest.mark.asyncio
    async def test_sensitive_data_rejected(self):
        router = ModelRouter()
        req = ModelRequest(
            role=ModelRole.DIRECT_CHAT,
            messages=[{"role": "user", "content": "my phone is 13800138000"}],
            data_class=ModelDataClass.SENSITIVE,
        )
        with pytest.raises(ModelError) as ei:
            await router.route_request(req)
        assert ei.value.kind == ModelErrorKind.SAFETY_REJECTED

    @pytest.mark.asyncio
    async def test_route_hint_not_honored_by_default(self):
        router = ModelRouter()
        req = ModelRequest(
            role=ModelRole.DIRECT_CHAT,
            messages=[{"role": "user", "content": "hi"}],
            route_hint="evil-model",
        )
        resp = await router.route_request(req)  # degraded stub path
        assert resp.model_id == RouterConfig.default_config().get(ModelRole.DIRECT_CHAT).model_id

    @pytest.mark.asyncio
    async def test_route_hint_honored_when_allowed(self):
        cfg = RouterConfig.default_config()
        cfg.get(ModelRole.DIRECT_CHAT)
        config = RouterConfig(
            roles={
                ModelRole.DIRECT_CHAT: ModelConfig(
                    role=ModelRole.DIRECT_CHAT, model_id="base-model",
                    route_hint_allowed=True,
                )
            }
        )
        router = ModelRouter(config=config)
        req = ModelRequest(
            role=ModelRole.DIRECT_CHAT,
            messages=[{"role": "user", "content": "hi"}],
            route_hint="hinted-model",
        )
        resp = await router.route_request(req)
        assert resp.model_id == "hinted-model"
        assert resp.degraded  # no provider -> stub


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "status error", request=None, response=None
            )

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, status=200, payload=None, exc=None):
        self.status = status
        self.payload = payload
        self.exc = exc

    async def post(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.status, self.payload)


class TestOpenAIProviderErrors:
    @pytest.fixture(autouse=True)
    def _patch_client(self, monkeypatch):
        monkeypatch.setattr(
            op_module.httpx, "AsyncClient",
            lambda *a, **kw: _FakeClient(),
        )

    async def _complete(self, client):
        provider = op_module.OpenAIProvider(api_key="k", base_url="https://x")
        cfg = ModelConfig(role=ModelRole.DIRECT_CHAT, model_id="m")
        return await provider.complete("m", [{"role": "user", "content": "hi"}], cfg)

    @pytest.mark.asyncio
    async def test_success(self):
        op_module.httpx.AsyncClient = lambda *a, **kw: _FakeClient(
            200, {"choices": [{"message": {"content": "ok"}}]}
        )
        assert await self._complete(None) == "ok"

    @pytest.mark.asyncio
    async def test_rate_limited(self):
        op_module.httpx.AsyncClient = lambda *a, **kw: _FakeClient(429)
        with pytest.raises(ModelError) as ei:
            await self._complete(None)
        assert ei.value.kind == ModelErrorKind.RATE_LIMITED
        assert ei.value.retryable

    @pytest.mark.asyncio
    async def test_auth_failed(self):
        op_module.httpx.AsyncClient = lambda *a, **kw: _FakeClient(401)
        with pytest.raises(ModelError) as ei:
            await self._complete(None)
        assert ei.value.kind == ModelErrorKind.AUTH_FAILED
        assert not ei.value.retryable

    @pytest.mark.asyncio
    async def test_timeout(self):
        import httpx
        op_module.httpx.AsyncClient = lambda *a, **kw: _FakeClient(exc=httpx.TimeoutException("slow"))
        with pytest.raises(ModelError) as ei:
            await self._complete(None)
        assert ei.value.kind == ModelErrorKind.TIMEOUT
        assert ei.value.retryable


# ------------------------------------------------------- Capability Top-K
class TestCapabilityRetrieve:
    def _registry(self):
        reg = CapabilityRegistry()
        for cid, cat, risk, perms, latency in [
            ("mcp.course", "course", CapabilityRisk.READ_ONLY, (), 10.0),
            ("mcp.send", "send", CapabilityRisk.SIDE_EFFECT, ("admin",), 5.0),
            ("mcp.delete", "admin", CapabilityRisk.DANGEROUS, ("admin",), 1.0),
        ]:
            reg.register(
                Capability(
                    capability_id=cid, name=cid, description="cap " + cid,
                    provider=ProviderType.MCP, risk=risk, category=cat,
                    required_permissions=perms, latency_hint_ms=latency,
                ),
                None,
            )
        return reg

    def test_top_k(self):
        reg = self._registry()
        out = reg.retrieve(CapabilityQuery(top_k=2), permissions=("admin",))
        assert len(out) == 2
        assert [c.rank for c in out] == [1, 2]

    def test_permission_hides_capability(self):
        reg = self._registry()
        out = reg.retrieve(CapabilityQuery(), permissions=())
        ids = {c.capability.capability_id for c in out}
        assert "mcp.send" not in ids
        assert "mcp.delete" not in ids
        assert "mcp.course" in ids

    def test_risk_cap(self):
        reg = self._registry()
        out = reg.retrieve(
            CapabilityQuery(max_risk=CapabilityRisk.READ_ONLY),
            permissions=("admin",),
        )
        ids = {c.capability.capability_id for c in out}
        assert "mcp.delete" not in ids

    def test_forbidden_side_effects(self):
        reg = CapabilityRegistry()
        reg.register(
            Capability(
                capability_id="c1", name="c1", description="send msg",
                provider=ProviderType.BUILTIN, side_effects=("send",),
            ),
            None,
        )
        reg.register(
            Capability(capability_id="c2", name="c2", description="read"),
            None,
        )
        out = reg.retrieve(CapabilityQuery(forbidden_side_effects=("send",)))
        assert [c.capability.capability_id for c in out] == ["c2"]

    def test_record_call_dedup(self):
        reg = CapabilityRegistry()
        assert not reg.record_call("c1", "key-1")
        assert reg.record_call("c1", "key-1")      # duplicate
        assert not reg.record_call("c1", "key-2")  # different key


# ------------------------------------------------- Executor re-authorization
class TestExecutorReauthorization:
    """Doc 2.4.12: re-resolve + re-check before every step; rejections not retried."""

    def _registry(self):
        reg = CapabilityRegistry()

        class P:
            async def execute(self, cap, args):
                return type("O", (), {"success": True, "data": "ok", "error": None})()

        reg.register(
            Capability(
                capability_id="need.admin", name="n", description="needs admin",
                required_permissions=("admin",), provider=ProviderType.BUILTIN,
            ),
            P(),
        )
        reg.register(
            Capability(
                capability_id="need.confirm", name="n", description="confirm",
                requires_confirmation=True, provider=ProviderType.BUILTIN,
            ),
            P(),
        )
        reg.register(
            Capability(
                capability_id="need.schema", name="n", description="schema",
                schema=CapabilitySchema(
                    input_schema={"type": "object", "required": ["q"],
                                  "properties": {"q": {"type": "string"}}}
                ),
                provider=ProviderType.BUILTIN,
            ),
            P(),
        )
        reg.register(
            Capability(
                capability_id="idem", name="n", description="idempotent",
                idempotent=True, provider=ProviderType.BUILTIN,
            ),
            P(),
        )
        return reg

    @pytest.mark.asyncio
    async def test_missing_permission_fails_without_retry(self):
        reg = self._registry()
        ex = ToolExecutor(reg)
        step = PlannedStep("s1", "need.admin", {}, "test")
        ctx = ExecutionContext(permissions=())
        res = await ex._execute_step(step, ctx)
        assert not res.success
        assert "Missing permissions" in res.error

    @pytest.mark.asyncio
    async def test_confirmation_required(self):
        reg = self._registry()
        ex = ToolExecutor(reg)
        step = PlannedStep("s1", "need.confirm", {}, "test")
        res = await ex._execute_step(step, ExecutionContext(permissions=()))
        assert not res.success
        assert "requires confirmation" in res.error

    @pytest.mark.asyncio
    async def test_confirmation_granted(self):
        reg = self._registry()
        ex = ToolExecutor(reg)
        step = PlannedStep("s1", "need.confirm", {}, "test")
        res = await ex._execute_step(
            step, ExecutionContext(permissions=(), confirmed_ids=("need.confirm",))
        )
        assert res.success

    @pytest.mark.asyncio
    async def test_schema_missing_arg(self):
        reg = self._registry()
        ex = ToolExecutor(reg)
        step = PlannedStep("s1", "need.schema", {}, "test")
        res = await ex._execute_step(step, ExecutionContext(permissions=()))
        assert not res.success
        assert "Missing required argument" in res.error

    @pytest.mark.asyncio
    async def test_idempotency_key_dedup(self):
        reg = self._registry()
        ex = ToolExecutor(reg)

        class Step:
            def __init__(self, sid, key):
                self.step_id = sid
                self.capability_id = "idem"
                self.arguments = {}
                self.depends_on = ()
                self.idempotency_key = key

        ctx = ExecutionContext(permissions=())
        r1 = await ex._execute_step(Step("s1", "k1"), ctx)
        r2 = await ex._execute_step(Step("s2", "k1"), ctx)
        assert r1.success
        assert not r2.success
        assert "Duplicate call" in r2.error

    @pytest.mark.asyncio
    async def test_execute_plan_rejects(self):
        reg = self._registry()
        ex = ToolExecutor(reg)
        plan = GeneratedPlan(
            goal="test",
            steps=(PlannedStep("s1", "need.admin", {}, "test"),),
        )
        results = await ex.execute_plan(plan, ExecutionContext(permissions=()))
        assert not results[0].success
