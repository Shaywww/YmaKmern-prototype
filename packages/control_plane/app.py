"""嘟嘟哒 2.0 控制台 - Web Dashboard & API Server."""
from __future__ import annotations
import json
import os
import time
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
from .security import (
    AuditLogger, cp_auth_middleware, get_operator, redact_value,
    require_write, scope_filter_events,
)
from ..observability.observability import Tracer, InMemoryTraceSink, TraceEvent

_registry = PersonaRegistry()
_services = create_all_services()
_trace_sink = InMemoryTraceSink()
_tracer = Tracer(sink=_trace_sink)


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
        emoji_style=EmojiStyle(d.get("emoji_style", "moderate")),
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    _trace_sink.write(TraceEvent(level="phase",phase="control_plane_startup"))
    yield
    _trace_sink.write(TraceEvent(level="phase",phase="control_plane_shutdown"))

def create_app() -> FastAPI:
    app = FastAPI(title='嘟嘟哒 2.0 控制台',version='0.1.0',lifespan=lifespan)
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
    app.state.eval_dir = Path(
        os.environ.get("DUDUDA_EVAL_DIR") or str(
            Path(__file__).resolve().parents[2] / "data" / "traces-eval"))
    app.middleware('http')(cp_auth_middleware)
    _register_routes(app)
    return app

def run_server(host: str = '127.0.0.1', port: int = 8000, reload: bool = False):
    import uvicorn
    uvicorn.run('packages.control_plane.app:create_app', host=host, port=port, reload=reload, factory=True)

def _register_routes(app: FastAPI):
    @app.get('/health')
    async def health():
        svc_health = {sid: svc.check_health().value for sid, svc in app.state.services.items()}
        return {'status':'ok','version':'0.1.0','timestamp':time.time(),'services':svc_health,'active_persona':app.state.registry.active_id}

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

    @app.get('/runtime/state')
    async def runtime_state():
        return {'active_persona':app.state.registry.active_id,'persona_count':len(app.state.registry.list_all()),'group_overrides':len(app.state.registry._group_overrides),'user_overrides':len(app.state.registry._user_overrides),'mcp_services':len(app.state.services),'trace_events':len(app.state.trace_sink.events)}

    @app.get('/')
    async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dududa 2.0 - Control Plane</title>
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
<h1>嘟嘟哒 2.0 控制台</h1>
<div id="indicator"><span class="status-dot ok"></span> 系统正常</div>
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
<div class="card" style="grid-column:span 2">
<h2>MCP 查询</h2>
<div class="fr"><div><label>服务</label><select id="qs"></select></div><div><label>操作</label><select id="qa"><option>search</option></select></div><div><label>关键词</label><input id="qk"></div></div>
<button class="btn btn-p" onclick="queryM()" style="margin-top:.5rem">查询</button>
<pre id="qr" style="margin-top:.5rem;display:none"></pre>
</div>
</div>
<footer>Dududa 2.0 Agent 运行状态 - v0.1.0</footer>
<script>
const A="";async function api(u,o){const r=await fetch(A+u,o);if(!r.ok)throw new Error((await r.json()).detail||r.statusText);return r.json()}
async function rf(){try{const h=await api("/health");document.getElementById("ap").textContent=h.active_persona;document.getElementById("mc").textContent=Object.keys(h.services).length;const dot=document.querySelector(".status-dot");dot.className="status-dot "+(h.status==="ok"?"ok":"degraded");const p=await api("/personas");document.getElementById("pc").textContent=p.count;let ph="";for(const[id,d]of Object.entries(p.personas)){let cls=id.startsWith("dududa_")?id.replace("dududa_",""):"default";ph+='<div class="row"><span>'+escapeHtml(d.display_name||id)+'</span><span class="tag tag-'+cls+'">'+escapeHtml(id)+'</span></div>'}document.getElementById("pl").innerHTML=ph;const s=await api("/mcp/services");let mh="";for(const[id,sd]of Object.entries(s.services)){mh+='<div class="row"><span>'+escapeHtml(sd.name)+'</span><span class="badge badge-'+sd.health+'">'+sd.health+'</span>';if(sd.mock_mode)mh+='<span class="badge badge-mock">mock</span>';mh+="</div>"}document.getElementById("ml").innerHTML=mh;document.getElementById("qs").innerHTML=Object.keys(s.services).map(id=>'<option value="'+id+'">'+id+'</option>').join("");const t=await api("/traces?limit=6");document.getElementById("tc").textContent=t.count;let th=t.events.length?"":"<em>暂无追踪记录</em>";for(const e of t.events){th+='<div style="font-size:.7rem;margin-bottom:3px"><span class="badge badge-'+(e.level==="error"?"unavailable":"healthy")+'">'+e.level+'</span> '+escapeHtml(e.phase||"")+' <span style="color:#64748b">'+new Date(e.timestamp).toLocaleTimeString()+"</span></div>"}document.getElementById("tl").innerHTML=th}catch(e){console.error(e);document.querySelector(".status-dot").className="status-dot unavailable"}}
function showForm(){document.getElementById("pf").style.display="block"}
function hideForm(){document.getElementById("pf").style.display="none"}
async function createP(){const id=document.getElementById("nid").value,nm=document.getElementById("nnm").value,dc=document.getElementById("ndc").value;if(!id)return alert("Need persona_id");try{await api("/personas",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({persona_id:id,name:nm,display_name:nm,description:dc})});hideForm();rf()}catch(e){alert(e.message)}}
async function queryM(){const sv=document.getElementById("qs").value,ac=document.getElementById("qa").value,kw=document.getElementById("qk").value;try{const r=await api("/mcp/services/"+sv+"/query",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:ac,keyword:kw})});const pr=document.getElementById("qr");pr.style.display="block";pr.textContent=JSON.stringify(r,null,2)}catch(e){alert(e.message)}}
function escapeHtml(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}
rf();setInterval(rf,10000);
</script>
</body>
</html>"""

