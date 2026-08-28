"""YmaKmern 控制台 - Web Dashboard & API Server."""
from __future__ import annotations
import json
import os
import time
from collections import Counter
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from ..core.persona.templates import PersonaTemplate, PersonaTraits, ToneConfig, FormalityLevel, PlayfulnessLevel, EmojiStyle
from ..core.persona.registry import PersonaRegistry
from ..mcp.registry import create_all_services, register_all_mcp_services
from ..core.capability import CapabilityRegistry, CapabilityRisk
from ..safeguards.security import PermissionEngine, Redactor
from ..mcp import access as mcp_access
from ..core.memory import (
    JSONMemoryRepository, ScopeSelector, SensitivityLevel, MemoryType,
)
from ..evolution import ShadowEvolution
from .security import (
    AuditLogger, cp_auth_middleware, get_operator, redact_value,
    require_write, scope_filter_events,
)
from ..observability.observability import Tracer, InMemoryTraceSink, TraceEvent

_registry = PersonaRegistry()
_services = create_all_services()
_trace_sink = InMemoryTraceSink()
_tracer = Tracer(sink=_trace_sink)

# ---- P3 trace 可视化：成本估算（trace 无 token 计数，按角色估算 token/调用 × 模型单价） ----
# 元 / 1K tokens（估算单价；中转网关实际价格不同，仅作量级参考）
_MODEL_PRICE_YUAN = {
    "deepseek-chat": (0.001, 0.002),
    "claude-haiku-4-5-20251001": (0.006, 0.03),
    "gpt-5.5": (0.02, 0.08),
}
_DEFAULT_PRICE_YUAN = (0.01, 0.02)
# 每角色单次调用估算 token（输入, 输出）
_ROLE_TOKEN_EST = {
    "perception": (512, 256), "social_decision": (512, 128),
    "tool_planning": (1024, 512), "direct_chat": (1024, 512),
    "response_composition": (1024, 512), "memory_summary": (512, 256),
    "image_understanding": (800, 400), "image_generation": (800, 400),
}
_DEFAULT_TOKEN_EST = (512, 256)


def _persona_to_dict(p: PersonaTemplate) -> dict:
    return {
        "persona_id": p.persona_id, "name": p.name,
        "display_name": p.display_name, "description": p.description,
        "traits": p.traits.to_dict(), "tone": p.tone.to_dict(),
        "speaking_style": p.speaking_style, "first_person": p.first_person,
        "response_length": p.response_length,
    }

def _dict_to_traits(d: dict) -> PersonaTraits:
    defaults = PersonaTraits().to_dict()
    merged = {**defaults, **d}
    return PersonaTraits(**{k: merged[k] for k in defaults})

def _dict_to_tone(d: dict) -> ToneConfig:
    return ToneConfig(
        formality=FormalityLevel(d.get("formality", "casual")),
        playfulness=PlayfulnessLevel(d.get("playfulness", "playful")),
        emoji_style=EmojiStyle(d.get("emoji_style", "text_only")),
        max_emojis_per_message=int(d.get("max_emojis_per_message", 3)),
        use_kaomoji=bool(d.get("use_kaomoji", True)),
        use_stickers=bool(d.get("use_stickers", False)),
    )

def _event_to_dict(e: TraceEvent) -> dict:
    return {
        "event_id": e.event_id, "trace_id": e.trace_id,
        "run_id": e.run_id, "level": e.level if isinstance(e.level, str) else e.level.value,
        "phase": e.phase, "duration_ms": e.duration_ms,
        "timestamp": e.timestamp.isoformat(), "metadata": e.metadata,
    }

class PersonaCreate(BaseModel):
    persona_id: str
    name: str = ''
    display_name: str = ''
    description: str = ''
    traits: dict[str, float] = {}
    tone: dict[str, Any] = {}
    speaking_style: str = ''
    first_person: str = 'I'
    response_length: str = 'medium'

class OverrideSet(BaseModel):
    persona_id: str

class MCPQuery(BaseModel):
    action: str
    keyword: str = ''
    course_id: str = ''
    department: str = ''
    semester: str = ''
    student_id: str = ''
    major_id: str = ''
    category: str = ''
    source: str = ''
    days: int | None = None
    token: str = ''


class PlaygroundRun(BaseModel):
    message: str
    actor_id: str = "playground_user"


class EvolutionExperienceCreate(BaseModel):
    summary: str
    signal_type: str = "correction"
    category: str = ""
    severity: str = "medium"
    run_id: str = ""
    trace_id: str = ""


class EvolutionDecision(BaseModel):
    decision: str
    note: str = ""



@asynccontextmanager
async def lifespan(app: FastAPI):
    _trace_sink.write(TraceEvent(level="phase",phase="control_plane_startup"))
    yield
    _trace_sink.write(TraceEvent(level="phase",phase="control_plane_shutdown"))

def create_app() -> FastAPI:
    app = FastAPI(title='YmaKmern 控制台',version='0.7.0',lifespan=lifespan)
    app.state.registry = _registry
    app.state.services = _services
    app.state.tracer = _tracer
    app.state.trace_sink = _trace_sink
    # CP-P0 安全基线（ADR-0001）：权限 / 脱敏 / 审计 / Capability 入口 / access 策略
    app.state.permission_engine = PermissionEngine()
    app.state.redactor = Redactor()
    app.state.audit_logger = AuditLogger()
    cap_registry = CapabilityRegistry()
    register_all_mcp_services(cap_registry)
    app.state.cap_registry = cap_registry
    app.state.mcp_access = mcp_access.MCPAccessPolicy()
    # CP-P1 只读面板（ADR-0001）：Memory Explorer 经 JSONMemoryRepository；Eval 报告只读
    app.state.memory_repo = JSONMemoryRepository(
        path=os.environ.get("DUDUDA_MEMORY_FILE") or str(
            Path(__file__).resolve().parents[2] / "data" / "memory.json"))
    app.state.trace_dir = Path(
        os.environ.get("DUDUDA_CP_TRACE_DIR") or str(
            Path(__file__).resolve().parents[2] / "data" / "traces"))
    app.state.evolution = ShadowEvolution(
        os.environ.get("DUDUDA_EVOLUTION_DIR"), app.state.redactor)
    app.state.playground = _PlaygroundSandbox()
    app.state.eval_dir = Path(
        os.environ.get("DUDUDA_EVAL_DIR") or str(
            Path(__file__).resolve().parents[2] / "data" / "traces-eval"))
    app.middleware('http')(cp_auth_middleware)
    _register_routes(app)
    return app

def _load_trace_events(trace_dir, files: int = 5) -> list:
    """读取最近的 trace JSONL 事件（生产 TraceRecorder 格式，CP-P2 只读聚合）。"""
    events: list = []
    if not trace_dir.is_dir():
        return events
    for p in sorted(trace_dir.glob('*.jsonl'))[-files:]:
        try:
            for line in p.open(encoding='utf-8'):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            continue
    return events


def _estimate_call_cost(role: str, model_id: str) -> float:
    """单次模型调用的估算成本（元）：估算 token × 模型单价。"""
    in_tok, out_tok = _ROLE_TOKEN_EST.get(role, _DEFAULT_TOKEN_EST)
    in_p, out_p = _MODEL_PRICE_YUAN.get(model_id, _DEFAULT_PRICE_YUAN)
    return (in_tok / 1000.0) * in_p + (out_tok / 1000.0) * out_p


def _estimate_cost(events: list) -> float:
    return round(sum(
        _estimate_call_cost(e.get('role', ''), e.get('model_id', ''))
        for e in events), 4)


def _weekly_cost_report(events: list, weeks: int = 8) -> list:
    """按 ISO 周聚合 model_response：调用量 / 降级 / 错误 / 估算成本（最新在前）。"""
    import datetime as _dt
    buckets: dict = {}
    for e in events:
        try:
            ts = float(e.get('ts_ms', 0)) / 1000.0
            d = _dt.datetime.fromtimestamp(ts)
        except (TypeError, ValueError, OSError):
            continue
        iso = "%d-W%02d" % (d.isocalendar()[0], d.isocalendar()[1])
        monday = (d - _dt.timedelta(days=d.weekday())).strftime('%Y-%m-%d')
        b = buckets.setdefault(iso, {
            'week': iso, 'start': monday, 'calls': 0, 'degraded': 0,
            'errors': 0, 'est_cost_yuan': 0.0,
            'by_model': Counter(), 'by_role': Counter()})
        b['calls'] += 1
        b['degraded'] += 1 if e.get('degraded') else 0
        b['errors'] += 1 if e.get('error_kind') else 0
        b['est_cost_yuan'] = round(b['est_cost_yuan'] + _estimate_call_cost(
            e.get('role', ''), e.get('model_id', '')), 4)
        b['by_model'][e.get('model_id', '?')] += 1
        b['by_role'][e.get('role', '?')] += 1
    out = [{'week': b['week'], 'start': b['start'], 'calls': b['calls'],
            'degraded': b['degraded'], 'errors': b['errors'],
            'est_cost_yuan': b['est_cost_yuan'],
            'by_model': dict(b['by_model']), 'by_role': dict(b['by_role'])}
           for b in buckets.values()]
    out.sort(key=lambda x: x['start'], reverse=True)
    return out[:weeks]


def _playground_llm_cb():
    """Playground LLM 回调：有 key 走 OpenAI 兼容接口；无 key 离线占位（不调真实模型）。"""
    api_key = (os.environ.get("DUDUDA_CP_LLM_KEY")
               or os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "")
    model = os.environ.get("DUDUDA_CP_LLM_MODEL", "deepseek-chat")
    base = os.environ.get("DUDUDA_CP_LLM_BASE", "https://api.deepseek.com/v1")
    if not api_key:
        return None

    from ..router.openai_provider import OpenAIProvider
    from ..router.router import ModelConfig, ModelRole

    provider = OpenAIProvider(api_key=api_key, base_url=base)

    async def cb(prompt: str, run_id: str = "", trace_id: str = "", **kw) -> str:
        config = ModelConfig(
            role=ModelRole.DIRECT_CHAT, model_id=model,
            max_tokens=int(kw.get("max_tokens", 1024)),
            temperature=float(kw.get("temperature", 0.7)),
        )
        return await provider.complete(model, [
            {"role": "system", "content": "你是 YmaKmern，请直接回答问题。"},
            {"role": "user", "content": prompt},
        ], config)

    return cb


class _PlaygroundSandbox:
    """Agent Playground 沙箱（ADR-0001 CP-P2）：
    - 独立 InMemoryRepository / TraceSink / NoOp 投递：不写生产 Memory、不投递消息；
    - DANGEROUS 风险能力从沙箱注册表移除：沙箱内不得执行危险工具；
    - LLM 未配置时离线占位（rule-based 决策 + 确定性渲染）。
    """

    def __init__(self):
        from ..core.memory import InMemoryRepository
        from ..core.renderer import OCRenderer, Persona as OCPersona
        from ..core.delivery import DeliveryManager, NoOpOutputAdapter
        from ..runtime.orchestrator import RuntimeOrchestrator

        self.memory = InMemoryRepository()
        self.trace_sink = InMemoryTraceSink()
        self.tracer = Tracer(sink=self.trace_sink)
        cap_registry = CapabilityRegistry()
        register_all_mcp_services(cap_registry)
        for cap in list(cap_registry.list_enabled()):
            if cap.risk == CapabilityRisk.DANGEROUS:
                cap_registry.unregister(cap.capability_id)
        self.cap_registry = cap_registry
        persona = OCPersona(persona_id="playground", version="1.0", name="YmaKmern")
        self.orchestrator = RuntimeOrchestrator(
            memory_repo=self.memory,
            capability_registry=cap_registry,
            renderer=OCRenderer(persona=persona, llm=_playground_llm_cb()),
            delivery_manager=DeliveryManager(NoOpOutputAdapter()),
        )

    async def run(self, text: str, actor_id: str = "playground_user"):
        from ..core.envelope import (
            Actor, ConversationRef, MessageEnvelope, MessageKind, Platform,
        )
        env = MessageEnvelope(
            text=text,
            sender=Actor(actor_id=actor_id, platform=Platform.QQ,
                         display_name="playground"),
            conversation=ConversationRef(
                conversation_id="playground", platform=Platform.QQ,
                kind=MessageKind.PRIVATE),
        )
        return await self.orchestrator.run(env)


def run_server(host: str = '127.0.0.1', port: int = 8000, reload: bool = False):
    import uvicorn
    uvicorn.run('dududa.control_plane.app:create_app', host=host, port=port, reload=reload, factory=True)

def _register_routes(app: FastAPI):
    @app.get('/health')
    async def health():
        svc_health = {sid: svc.check_health().value for sid, svc in app.state.services.items()}
        status = ('ok' if all(value == 'healthy' for value in svc_health.values())
                  else 'degraded')
        return {'status':status,'version':'0.1.0','timestamp':time.time(),'services':svc_health,'active_persona':app.state.registry.active_id}

    @app.get('/personas')
    async def list_personas():
        ids = app.state.registry.list_all()
        result = {pid: _persona_to_dict(app.state.registry.get(pid)) for pid in ids if app.state.registry.get(pid)}
        return {'personas': result, 'count': len(result)}

    @app.get('/personas/overrides')
    async def list_overrides():
        return {'groups': dict(app.state.registry._group_overrides), 'users': dict(app.state.registry._user_overrides)}

    @app.put('/personas/overrides/groups/{group_id}')
    async def set_group_override(group_id: str, body: OverrideSet, request: Request):
        require_write(request, app)
        app.state.registry.set_group_override(group_id, body.persona_id)
        return {'group': group_id, 'persona': body.persona_id}

    @app.delete('/personas/overrides/groups/{group_id}')
    async def clear_group_override(group_id: str, request: Request):
        require_write(request, app)
        app.state.registry.set_group_override(group_id, None)
        return {'group': group_id, 'cleared': True}

    @app.put('/personas/overrides/users/{user_id}')
    async def set_user_override(user_id: str, body: OverrideSet, request: Request):
        require_write(request, app)
        app.state.registry.set_user_override(user_id, body.persona_id)
        return {'user': user_id, 'persona': body.persona_id}

    @app.delete('/personas/overrides/users/{user_id}')
    async def clear_user_override(user_id: str, request: Request):
        require_write(request, app)
        app.state.registry.set_user_override(user_id, None)
        return {'user': user_id, 'cleared': True}

    @app.get('/personas/{persona_id}')
    async def get_persona(persona_id: str):
        p = app.state.registry.get(persona_id)
        if not p: raise HTTPException(404, f'Persona {persona_id!r} not found')
        return _persona_to_dict(p)

    @app.post('/personas/{persona_id}/activate')
    async def activate_persona(persona_id: str, request: Request):
        require_write(request, app)
        if not app.state.registry.switch(persona_id):
            raise HTTPException(404, f'Persona {persona_id!r} not found')
        return {'active': persona_id}

    @app.put('/personas')
    async def create_persona(body: PersonaCreate, request: Request):
        require_write(request, app)
        if body.persona_id in app.state.registry.list_all():
            raise HTTPException(409, f'Persona {body.persona_id!r} already exists')
        p = PersonaTemplate(
            persona_id=body.persona_id, name=body.name or body.persona_id,
            display_name=body.display_name or body.name, description=body.description,
            traits=_dict_to_traits(body.traits), tone=_dict_to_tone(body.tone),
            speaking_style=body.speaking_style, first_person=body.first_person,
            response_length=body.response_length,
        )
        app.state.registry.register(p)
        return {'created': body.persona_id}

    @app.delete('/personas/{persona_id}')
    async def delete_persona(persona_id: str, request: Request):
        require_write(request, app)
        if not app.state.registry.unregister(persona_id):
            raise HTTPException(400, f'Cannot delete {persona_id!r} (protected or not found)')
        return {'deleted': persona_id}

    @app.get('/mcp/services')
    async def list_mcp_services():
        result = {}
        for sid, svc in app.state.services.items():
            result[sid] = {'name':svc.config.service_name,'description':svc.config.description,'mock_mode':svc.config.mock_mode,'health':svc.check_health().value,'cache_policy':svc.config.cache_policy.value}
        return {'services': result, 'count': len(result)}

    @app.get('/mcp/services/{service_id}/health')
    async def mcp_service_health(service_id: str):
        svc = app.state.services.get(service_id)
        if not svc: raise HTTPException(404, f'MCP service {service_id!r} not found')
        return {'service': service_id, 'health': svc.check_health().value}

    @app.post('/mcp/services/{service_id}/query')
    async def query_mcp_service(service_id: str, body: MCPQuery, request: Request):
        # CP-P0（ADR-0001）：不直连 service，经 CapabilityRegistry + access 策略 + 熔断
        op = get_operator(request)
        cap_id = f"mcp.{service_id}"
        cap = app.state.cap_registry.get(cap_id)
        if cap is None:
            raise HTTPException(404, f'MCP service {service_id!r} not found')
        if cap.risk == CapabilityRisk.DANGEROUS:
            raise HTTPException(403, f'dangerous capability not allowed via CP: {cap_id}')
        if not app.state.mcp_access.is_allowed(cap_id, "", op.actor_id):
            raise HTTPException(403, f'access policy denied: {cap_id}')
        provider = app.state.cap_registry.get_provider(cap_id)
        if provider is None:
            raise HTTPException(500, f'no provider for {cap_id}')
        args = {k: v for k, v in body.model_dump().items()
                if v is not None and (not isinstance(v, str) or v)}
        try:
            obs = await provider.execute(cap, args)
            if (not obs.success) and obs.error and obs.error.startswith("Unknown action"):
                raise HTTPException(400, obs.error)
            data = redact_value(app.state.redactor, obs.data) if obs.success else None
            return {'success': obs.success, 'data': data,
                    'error': obs.error, 'source': obs.source, 'cached': obs.cached}
        except HTTPException: raise
        except Exception as e: raise HTTPException(500, str(e))

    def _visible_events(events, op, limit=None):
        """Scope 过滤 + Redactor 脱敏（CP-P0）。"""
        events = scope_filter_events(events, op)
        if limit is not None:
            events = events[-limit:]
        return [redact_value(app.state.redactor, _event_to_dict(e))
                for e in events]

    @app.get('/traces')
    async def list_traces(request: Request, limit: int = Query(50, ge=1, le=500)):
        op = get_operator(request)
        events = _visible_events(app.state.trace_sink.events, op, limit)
        return {'events': events, 'count': len(events)}

    @app.get('/traces/{trace_id}')
    async def get_trace(trace_id: str, request: Request):
        op = get_operator(request)
        events = _visible_events(app.state.trace_sink.by_trace(trace_id), op)
        if not events: raise HTTPException(404, f'Trace {trace_id!r} not found')
        return {'trace_id': trace_id, 'events': events}

    @app.get('/traces/runs/{run_id}')
    async def get_run_traces(run_id: str, request: Request):
        op = get_operator(request)
        events = _visible_events(app.state.trace_sink.by_run(run_id), op)
        return {'run_id': run_id, 'events': events, 'count': len(events)}

    # ---------- CP-P1 只读面板（ADR-0001）：Memory Explorer / Eval 报告 ----------
    def _memory_visible(records, op):
        """与 query_visible 语义一致：RESTRICTED 永不召回；PRIVATE 仅本人可见。"""
        out = []
        for r in records:
            if r.sensitivity == SensitivityLevel.RESTRICTED:
                continue
            if (r.sensitivity == SensitivityLevel.PRIVATE
                    and r.scope.actor_id != op.actor_id):
                continue
            out.append(r)
        return out

    def _memory_to_dict(record):
        s = record.scope
        return {
            'record_id': record.record_id,
            'scope': {
                'memory_type': s.memory_type.value,
                'platform': s.platform,
                'bot_id': s.bot_id,
                'conversation_id': s.conversation_id,
                'actor_id': s.actor_id,
                'persona_id': s.persona_id,
            },
            'content': redact_value(app.state.redactor, record.content),
            'source': record.source,
            'sensitivity': record.sensitivity.value,
            'visibility': record.visibility.value,
            'evidence': [redact_value(app.state.redactor, e)
                         for e in record.evidence],
            'created_at': str(record.created_at),
        }

    @app.get('/memory')
    async def memory_explore(request: Request, limit: int = Query(50, ge=1, le=200),
                             actor_id: str = '', conversation_id: str = '',
                             memory_type: str = ''):
        op = get_operator(request)
        if op.role != 'owner':
            if actor_id and actor_id != op.actor_id:
                raise HTTPException(403, 'non-owner cannot scope memory to other actors')
            actor_id = op.actor_id
        mtype = None
        if memory_type:
            try:
                mtype = MemoryType(memory_type)
            except ValueError:
                raise HTTPException(400, f'invalid memory_type: {memory_type}')
        records = app.state.memory_repo.query_selector(ScopeSelector(
            actor_id=actor_id or None,
            conversation_id=conversation_id or None,
            memory_type=mtype,
        ), limit=limit * 4)
        records = _memory_visible(records, op)[:limit]
        return {'records': [_memory_to_dict(r) for r in records],
                'count': len(records)}

    @app.get('/memory/{record_id}')
    async def memory_record(record_id: str, request: Request):
        op = get_operator(request)
        record = app.state.memory_repo._records.get(record_id)
        if record is None or not _memory_visible([record], op):
            raise HTTPException(404, f'Memory record {record_id!r} not found')
        return _memory_to_dict(record)

    @app.get('/eval/reports')
    async def eval_reports():
        out = []
        for p in sorted(app.state.eval_dir.glob('*')):
            if not p.is_file() or p.suffix not in ('.jsonl', '.json'):
                continue
            lines = -1
            if p.suffix == '.jsonl':
                try:
                    lines = sum(1 for _ in p.open(encoding='utf-8'))
                except OSError:
                    lines = -1
            out.append({'name': p.name, 'size': p.stat().st_size,
                        'mtime': p.stat().st_mtime, 'lines': lines})
        return {'reports': out, 'count': len(out)}

    @app.get('/eval/reports/{name}')
    async def eval_report(name: str):
        if Path(name).name != name or not name.endswith(('.jsonl', '.json')):
            raise HTTPException(400, f'invalid report name: {name}')
        p = app.state.eval_dir / name
        if not p.is_file():
            raise HTTPException(404, f'Eval report {name!r} not found')
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError as exc:
            raise HTTPException(500, str(exc))
        entries = []
        for line in lines[:500]:
            if not line.strip():
                continue
            try:
                entries.append(redact_value(app.state.redactor, json.loads(line)))
            except ValueError:
                entries.append({'raw': redact_value(
                    app.state.redactor, line[:2000])})
        return {'report': name, 'entries': entries, 'count': len(entries),
                'truncated': len(lines) > 500}

    # ---------- CP-P2 高级能力（ADR-0001）：Playground / 成本性能 / 告警 / 日志检索 ----------
    @app.post('/playground/run')
    async def playground_run(body: PlaygroundRun, request: Request):
        require_write(request, app)  # 高级能力仅 owner；沙箱内运行，写操作边界见 ADR 第 2.7 条
        if not body.message.strip():
            raise HTTPException(400, 'message is empty')
        if len(body.message) > 4000:
            raise HTTPException(400, 'message too long (max 4000 chars)')
        result = await app.state.playground.run(body.message, body.actor_id)
        reply = ""
        if result.final_response is not None:
            reply = result.final_response.text or ""
        elif result.reaction:
            reply = result.reaction
        return {
            'run_id': result.run_id,
            'trace_id': result.trace_id,
            'outcome': result.outcome.value,
            'reply': redact_value(app.state.redactor, reply),
            'reason_codes': list(result.reason_codes),
            'tool_steps': int(result.trace_summary.get('tool_steps', 0)),
            'phases_visited': int(result.trace_summary.get('phases_visited', 0)),
            'sandboxed': True,
        }

    @app.get('/metrics/costs')
    async def metrics_costs():
        events = _load_trace_events(app.state.trace_dir)
        calls = [e for e in events if e.get('event') == 'model_response']
        return {
            'window_events': len(calls),
            'calls_by_role': dict(Counter(e.get('role', '?') for e in calls)),
            'calls_by_model': dict(Counter(e.get('model_id', '?') for e in calls)),
            'degraded': sum(1 for e in calls if e.get('degraded')),
            'errors': sum(1 for e in calls if e.get('error_kind')),
            'estimate': True,
            'est_cost_yuan': _estimate_cost(calls),
            'weekly': _weekly_cost_report(calls),
        }

    @app.get('/metrics/performance')
    async def metrics_performance():
        events = _load_trace_events(app.state.trace_dir)
        calls = [e for e in events if e.get('event') == 'model_response']
        latencies = [float(e['latency_ms']) for e in calls
                     if isinstance(e.get('latency_ms'), (int, float))]
        latencies.sort()
        n = len(latencies)
        p50 = latencies[n // 2] if n else 0.0
        p95 = latencies[int(n * 0.95) - 1] if n else 0.0
        errors = sum(1 for e in calls if e.get('error_kind'))
        return {
            'calls': n,
            'latency_ms_avg': (sum(latencies) / n) if n else 0.0,
            'latency_ms_p50': p50,
            'latency_ms_p95': p95,
            'error_rate': (errors / n) if n else 0.0,
        }

    @app.get('/metrics/tools')
    async def metrics_tools():
        """P3 trace 可视化：工具使用率 / 失败率（按工具 + 按天，只读）。"""
        events = _load_trace_events(app.state.trace_dir)
        results = [e for e in events if e.get('event') == 'tool_result']
        by_tool: dict = {}
        by_day: dict = {}
        for e in results:
            cid = str(e.get('capability_id', '?'))
            ok = bool(e.get('success'))
            t = by_tool.setdefault(cid, {'calls': 0, 'failures': 0,
                                         'lat_sum': 0.0, 'retries_used': 0})
            t['calls'] += 1
            t['failures'] += 0 if ok else 1
            t['lat_sum'] += float(e.get('latency_ms', 0) or 0)
            t['retries_used'] += int(e.get('retries_used', 0) or 0)
            day = str(e.get('ts', ''))[:10] or '?'
            d = by_day.setdefault(day, {'calls': 0, 'failures': 0})
            d['calls'] += 1
            d['failures'] += 0 if ok else 1
        tools = []
        for cid, t in by_tool.items():
            tools.append({
                'capability_id': cid,
                'calls': t['calls'],
                'failures': t['failures'],
                'fail_rate': round(t['failures'] / t['calls'], 4) if t['calls'] else 0.0,
                'avg_latency_ms': round(t['lat_sum'] / t['calls'], 1) if t['calls'] else 0.0,
                'retries_used': t['retries_used'],
            })
        tools.sort(key=lambda x: -x['calls'])
        days = [{'day': day, 'calls': d['calls'], 'failures': d['failures'],
                 'fail_rate': round(d['failures'] / d['calls'], 4) if d['calls'] else 0.0}
                for day, d in sorted(by_day.items(), key=lambda x: x[0])]
        total_calls = sum(t['calls'] for t in tools)
        total_fail = sum(t['failures'] for t in tools)
        return {
            'window_calls': total_calls,
            'window_failures': total_fail,
            'window_fail_rate': round(total_fail / total_calls, 4) if total_calls else 0.0,
            'by_tool': tools,
            'by_day': days,
        }

    @app.get('/alerts')
    async def alerts():
        events = _load_trace_events(app.state.trace_dir)
        now_ms = time.time() * 1000
        recent = [e for e in events
                  if now_ms - float(e.get('ts_ms', 0)) <= 600000]
        out = []
        resp = [e for e in recent if e.get('event') == 'model_response']
        if resp:
            degraded = sum(1 for e in resp if e.get('degraded'))
            if degraded / len(resp) > 0.5:
                out.append({'severity': 'warn', 'rule': 'model_degraded_ratio',
                            'detail': f'{degraded}/{len(resp)} degraded'})
            errs = [e for e in resp if e.get('error_kind')]
            if len(errs) >= 3:
                out.append({'severity': 'critical', 'rule': 'model_errors',
                            'detail': f'{len(errs)} errors in window'})
        for sid, svc in app.state.services.items():
            if svc.check_health().value != 'healthy':
                out.append({'severity': 'warn', 'rule': 'mcp_unhealthy',
                            'detail': sid})
        gates = [e for e in recent if e.get('event') == 'memory_gate']
        rejects = [e for e in gates if e.get('decision') in (
            'reject', 'defer_for_conflict_resolution')]
        if len(rejects) >= 5:
            out.append({'severity': 'info', 'rule': 'memory_gate_pressure',
                        'detail': f'{len(rejects)} non-allow decisions'})
        return {'alerts': out, 'count': len(out), 'window_seconds': 600}

    @app.get('/logs')
    async def logs(request: Request, level: str = '', query: str = '',
                   source: str = 'traces',
                   limit: int = Query(100, ge=1, le=500)):
        op = get_operator(request)
        rows = []
        if source in ('traces', 'all'):
            events = _load_trace_events(app.state.trace_dir)
            rows = [dict(e, source='trace') for e in events]
        if source in ('audit', 'all'):
            for line in app.state.audit_logger.lines():
                rows.append(dict(line, source='audit'))
        if level:
            rows = [r for r in rows if str(r.get('level', '')) == level
                    or r.get('event') == level]
        if query:
            rows = [r for r in rows
                    if query in json.dumps(r, ensure_ascii=False)]
        rows = rows[-limit:]
        return {'logs': [redact_value(app.state.redactor, r) for r in rows],
                'count': len(rows), 'source': source}

    @app.get('/runtime/state')
    async def runtime_state():
        return {'active_persona':app.state.registry.active_id,'persona_count':len(app.state.registry.list_all()),'group_overrides':len(app.state.registry._group_overrides),'user_overrides':len(app.state.registry._user_overrides),'mcp_services':len(app.state.services),'trace_events':len(app.state.trace_sink.events),'evolution':app.state.evolution.status()}

    # ---------- 影子进化：收集 / 聚类 / 审批，刻意没有激活和部署端点 ----------
    @app.get('/evolution/status')
    async def evolution_status():
        return app.state.evolution.status()

    @app.get('/evolution/experiences')
    async def evolution_experiences(request: Request,
                                    limit: int = Query(50, ge=1, le=200)):
        require_write(request, app)
        items = app.state.evolution.list_experiences(limit)
        return {'experiences': items, 'count': len(items)}

    @app.post('/evolution/experiences')
    async def evolution_add(body: EvolutionExperienceCreate, request: Request):
        require_write(request, app)
        try:
            item = app.state.evolution.add_experience(
                body.summary, source='operator', signal_type=body.signal_type,
                category=body.category, severity=body.severity,
                run_id=body.run_id, trace_id=body.trace_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return item

    @app.post('/evolution/analyze')
    async def evolution_analyze(request: Request):
        require_write(request, app)
        ingested = app.state.evolution.scan_trace_directory(app.state.trace_dir)
        result = app.state.evolution.analyze()
        return {'trace_failures_ingested': ingested, **result}

    @app.get('/evolution/candidates')
    async def evolution_candidates(request: Request):
        require_write(request, app)
        items = app.state.evolution.list_candidates()
        return {'candidates': items, 'count': len(items),
                'activation': 'disabled', 'deployment': 'disabled'}

    @app.get('/evolution/candidates/{candidate_id}')
    async def evolution_candidate(candidate_id: str, request: Request):
        require_write(request, app)
        item = app.state.evolution.get_candidate(candidate_id)
        if item is None:
            raise HTTPException(404, 'candidate not found')
        return item

    @app.post('/evolution/candidates/{candidate_id}/decision')
    async def evolution_decide(candidate_id: str, body: EvolutionDecision,
                               request: Request):
        require_write(request, app)
        try:
            return app.state.evolution.decide(candidate_id, body.decision, body.note)
        except KeyError:
            raise HTTPException(404, 'candidate not found')
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get('/')
    async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YmaKmern - Control Plane</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #334155}
h1{font-size:1.25rem;color:#38bdf8}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:6px}
.ok{background:#22c55e}
.degraded{background:#eab308}
.unavailable{background:#ef4444}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem;padding:1rem 2rem}
.card{background:#1e293b;border-radius:10px;padding:1.2rem;border:1px solid #334155}
.card h2{font-size:.9rem;color:#94a3b8;margin-bottom:.8rem;text-transform:uppercase;letter-spacing:.05em}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;margin:2px}
.tag-default{background:#1e40af;color:#93c5fd}
.tag-serious{background:#374151;color:#d1d5db}
.tag-tsundere{background:#831843;color:#f9a8d4}
.tag-mentor{background:#14532d;color:#86efac}
.row{display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem;font-size:.85rem}
.btn{display:inline-block;padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:.8rem;font-weight:600;transition:all .15s}
.btn-p{background:#0284c7;color:#fff}
.btn-p:hover{background:#0369a1}
.btn-s{background:#334155;color:#94a3b8}
.badge{font-size:.65rem;padding:1px 6px;border-radius:3px;margin-left:2px}
.badge-healthy{background:#166534;color:#86efac}
.badge-unavailable{background:#7f1d1d;color:#fca5a5}
.badge-mock{background:#3b0764;color:#d8b4fe}
input,select{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:5px 8px;border-radius:6px;font-size:.8rem;width:100%;margin-bottom:4px}
label{font-size:.75rem;color:#94a3b8;display:block;margin-bottom:2px}
.fr{display:flex;gap:.5rem}
.fr>*{flex:1}
pre{background:#0f172a;padding:.6rem;border-radius:6px;font-size:.7rem;max-height:200px;overflow:auto;white-space:pre-wrap}
footer{text-align:center;padding:1rem;color:#475569;font-size:.8rem}
</style>
</head>
<body>
<header>
<h1>YmaKmern 控制台</h1>
<div id="indicator" style="display:flex;align-items:center;gap:.6rem"><span class="status-dot ok"></span> 系统正常<button class="btn btn-s" onclick="showLogin()">Token</button></div>
</header>
<div class="grid">
<div class="card">
<h2>运行状态</h2>
<div class="row"><span>当前人格</span><span id="ap" class="tag tag-default">-</span></div>
<div class="row"><span>人格管理</span><strong id="pc">-</strong></div>
<div class="row"><span>MCP 服务</span><strong id="mc">-</strong></div>
<div class="row"><span>追踪</span><strong id="tc">-</strong></div>
</div>
<div class="card">
<h2>人格管理</h2><div id="pl">加载中...</div>
<div style="margin-top:.6rem"><button class="btn btn-p" onclick="showForm()">+ 新建</button></div>
<div id="pf" style="display:none;margin-top:.6rem">
<input id="nid" placeholder="persona_id"><input id="nnm" placeholder="显示名"><input id="ndc" placeholder="描述">
<div class="fr"><button class="btn btn-p" onclick="createP()">保存</button><button class="btn btn-s" onclick="hideForm()">取消</button></div>
</div>
</div>
<div class="card">
<h2>MCP 服务</h2><div id="ml">加载中...</div>
</div>
<div class="card">
<h2>最近追踪</h2><div id="tl">加载中...</div>
</div>
<div class="card">
<h2>工具使用率</h2><div id="tool">加载中...</div>
</div>
<div class="card">
<h2>成本周报</h2><div id="cost">加载中...</div>
</div>
<div class="card">
<h2>影子进化</h2><div id="evo">加载中...</div>
<button class="btn btn-p" onclick="analyzeE()" style="margin-top:.5rem">扫描并生成候选</button>
</div>
<div class="card" style="grid-column:span 2">
<h2>MCP 查询</h2>
<div class="fr"><div><label>服务</label><select id="qs"></select></div><div><label>操作</label><select id="qa"><option>search</option></select></div><div><label>关键词</label><input id="qk"></div></div>
<button class="btn btn-p" onclick="queryM()" style="margin-top:.5rem">查询</button>
<pre id="qr" style="margin-top:.5rem;display:none"></pre>
</div>
</div>
<div id="lg" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.94);z-index:50;align-items:center;justify-content:center"><div class="card" style="width:340px"><h2>控制台认证</h2><input id="lgt" placeholder="DUDUDA_CP_TOKEN" style="margin-bottom:.6rem"><div class="fr"><button class="btn btn-p" onclick="login()">进入</button></div></div></div>
<footer>YmaKmern Agent 运行状态 - v0.7.0</footer>
<script>
const A="";let TK=localStorage.getItem("cp_token")||"";function showLogin(){document.getElementById("lg").style.display="flex"}function hideLogin(){document.getElementById("lg").style.display="none"}async function login(){const t=document.getElementById("lgt").value.trim();if(!t)return alert("请输入 Token");TK=t;localStorage.setItem("cp_token",t);hideLogin();rf()}async function api(u,o){o=o||{};o.headers=Object.assign({},o.headers||{});if(TK)o.headers["Authorization"]="Bearer "+TK;const r=await fetch(A+u,o);if(r.status===401){showLogin();throw new Error("需要 Token")}if(!r.ok)throw new Error((await r.json()).detail||r.statusText);return r.json()}
async function rf(){try{const h=await api("/health");document.getElementById("ap").textContent=h.active_persona;document.getElementById("mc").textContent=Object.keys(h.services).length;const dot=document.querySelector(".status-dot");dot.className="status-dot "+(h.status==="ok"?"ok":"degraded");const p=await api("/personas");document.getElementById("pc").textContent=p.count;let ph="";for(const[id,d]of Object.entries(p.personas)){let cls=id.startsWith("dududa_")?id.replace("dududa_",""):"default";ph+='<div class="row"><span>'+escapeHtml(d.display_name||id)+'</span><span class="tag tag-'+cls+'">'+escapeHtml(id)+'</span></div>'}document.getElementById("pl").innerHTML=ph;const s=await api("/mcp/services");let mh="";for(const[id,sd]of Object.entries(s.services)){mh+='<div class="row"><span>'+escapeHtml(sd.name)+'</span><span class="badge badge-'+sd.health+'">'+sd.health+'</span>';if(sd.mock_mode)mh+='<span class="badge badge-mock">mock</span>';mh+="</div>"}document.getElementById("ml").innerHTML=mh;document.getElementById("qs").innerHTML=Object.keys(s.services).map(id=>'<option value="'+id+'">'+id+'</option>').join("");const t=await api("/traces?limit=6");document.getElementById("tc").textContent=t.count;let th=t.events.length?"":"<em>暂无追踪记录</em>";for(const e of t.events){th+='<div style="font-size:.7rem;margin-bottom:3px"><span class="badge badge-'+(e.level==="error"?"unavailable":"healthy")+'">'+e.level+'</span> '+escapeHtml(e.phase||"")+' <span style="color:#64748b">'+new Date(e.timestamp).toLocaleTimeString()+"</span></div>"}document.getElementById("tl").innerHTML=th;const tw=await api("/metrics/tools");let toh='<div class="row"><span>窗口调用</span><strong>'+tw.window_calls+'</strong></div><div class="row"><span>失败率</span><strong>'+(tw.window_fail_rate*100).toFixed(1)+'%</strong></div>';if(!tw.by_tool.length)toh="<em>暂无工具调用</em>";for(const t of tw.by_tool.slice(0,6)){toh+='<div class="row"><span>'+escapeHtml(t.capability_id)+'</span><span>'+t.calls+' 次 · 失败 '+(t.fail_rate*100).toFixed(1)+'%</span></div>'}document.getElementById("tool").innerHTML=toh;const cw=await api("/metrics/costs");let coh='<div class="row"><span>窗口调用</span><strong>'+cw.window_events+'</strong></div><div class="row"><span>估算成本</span><strong>¥'+cw.est_cost_yuan.toFixed(4)+'</strong></div>';if(!cw.weekly.length)coh+="<em>暂无模型调用</em>";for(const w of cw.weekly.slice(0,6)){coh+='<div class="row"><span>'+escapeHtml(w.week)+'</span><span>'+w.calls+' 次 · ¥'+w.est_cost_yuan.toFixed(4)+'</span></div>'}document.getElementById("cost").innerHTML=coh;const ev=await api("/evolution/status");document.getElementById("evo").innerHTML='<div class="row"><span>模式</span><strong>'+ev.mode+'</strong></div><div class="row"><span>脱敏经验</span><strong>'+ev.experience_count+'</strong></div><div class="row"><span>待审候选</span><strong>'+ev.candidate_count+'</strong></div><div class="row"><span>自动生效 / 部署</span><strong>关闭 / 关闭</strong></div>'}catch(e){console.error(e);document.querySelector(".status-dot").className="status-dot unavailable"}}
async function analyzeE(){try{const r=await api("/evolution/analyze",{method:"POST"});alert("新增失败经验 "+r.trace_failures_ingested+" 条，候选更新 "+r.created_or_updated+" 个。候选不会自动生效或部署。");rf()}catch(e){alert(e.message)}}
function showForm(){document.getElementById("pf").style.display="block"}
function hideForm(){document.getElementById("pf").style.display="none"}
async function createP(){const id=document.getElementById("nid").value,nm=document.getElementById("nnm").value,dc=document.getElementById("ndc").value;if(!id)return alert("Need persona_id");try{await api("/personas",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({persona_id:id,name:nm,display_name:nm,description:dc})});hideForm();rf()}catch(e){alert(e.message)}}
async function queryM(){const sv=document.getElementById("qs").value,ac=document.getElementById("qa").value,kw=document.getElementById("qk").value;try{const r=await api("/mcp/services/"+sv+"/query",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:ac,keyword:kw})});const pr=document.getElementById("qr");pr.style.display="block";pr.textContent=JSON.stringify(r,null,2)}catch(e){alert(e.message)}}
function escapeHtml(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}
if(!TK)showLogin();rf();setInterval(rf,10000);
</script>
</body>
</html>"""

