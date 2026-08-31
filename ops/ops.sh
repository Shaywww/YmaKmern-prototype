#!/bin/bash
# dududa20 ops 运维收口（文档 2.5.9 / 2.5.10）：
#   health   - 生成 health_status.json（服务/banner/MCP/错误/备份新鲜度）
#   manifest - 生成供应链 manifest v2（仓库 commit + 工作区干净 + 镜像 digest + 最小挂载）
#   smoke    - 一次性 bootstrap smoke（语法 + 插件 import [+ 关键 pytest] + 服务状态）
#   smoke-net - 真实网络 smoke（网关可达性 + 生产 LLM 往返，与阻塞 CI 分开）
#   backup   - 先写 health 快照，再委托可移植的 ops/manage.sh backup
#   cp       - Control Plane 状态/启停（ADR-0001 CP-P0，鉴权访问）
# 所有命令只读（backup 除外），输出目录用 OPS_OUT 覆盖（默认 /root/data/ops）。
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROTO="${DUDUDA_PROTO_DIR:-$(dirname "$SCRIPT_DIR")}"
PLUGIN="${DUDUDA_PLUGIN_DIR:-${HOME}/data/plugins/dududa20}"
AGENT_SRC="${DUDUDA_AGENT_SRC:-$PROTO/packages/dududa-agent/src}"
SERVICE="astrbot"
OUT="${OPS_OUT:-/root/data/ops}"
MANAGE="${DUDUDA_MANAGE_SCRIPT:-$SCRIPT_DIR/manage.sh}"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

json_ok() { python3.12 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$1"; }

# ---------- health ----------
cmd_health() {
    mkdir -p "$OUT"
    local f="$OUT/health_status.json"
    local active banner mcp errs backup_latest backup_age
    active="false"
    systemctl is-active "$SERVICE" >/dev/null 2>&1 && active="true"
    banner=$(journalctl -u "$SERVICE" --no-pager -n 200 2>/dev/null | grep "Dududa 2.0" | tail -1 | sed 's/.*\(Dududa 2.0.*\)/\1/' || true)
    mcp=$(journalctl -u "$SERVICE" --no-pager -n 200 2>/dev/null | grep -c "MCP capabilities registered" || true)
    errs=$(journalctl -u "$SERVICE" --no-pager -n 300 2>/dev/null | grep -cE "Traceback|\[ERRO\]" || true)
    backup_latest=$(ls -t /root/backups/dududa20/dududa20_*.tar.gz 2>/dev/null | head -1 || true)
    backup_age=-1
    if [ -n "$backup_latest" ]; then
        backup_age=$(( $(date +%s) - $(stat -c %Y "$backup_latest") ))
    fi
    python3.12 - "$f" "$active" "$banner" "$mcp" "$errs" "$backup_latest" "$backup_age" <<'PY'
import json, sys, time
f, active, banner, mcp, errs, bl, age = sys.argv[1:]
def i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d
data = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "service": {"name": "astrbot", "active": active == "true"},
    "banner": banner,
    "mcp_capabilities": i(mcp),
    "recent_errors": i(errs),
    "backup": {"latest": bl or None, "age_seconds": i(age, -1)},
    "backup_ok": bool(i(age, -1) != -1 and i(age, -1) < 7 * 24 * 3600),
    "ok": bool(active == "true" and banner
               and i(age, -1) != -1 and i(age, -1) < 7 * 24 * 3600),
}
json.dump(data, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    json_ok "$f"
    echo "health -> $f"
}

# ---------- supply chain manifest v2 ----------
cmd_manifest() {
    mkdir -p "$OUT"
    local f="$OUT/supply_chain_manifest.json"
    local pc ppc pclean pclean2 napdigest napup mounts
    pc=$(git -C "$PROTO" rev-parse HEAD 2>/dev/null || echo "")
    ppc=$(git -C "$PLUGIN" rev-parse HEAD 2>/dev/null || echo "")
    pclean=$(git -C "$PROTO" status --porcelain 2>/dev/null | wc -l | tr -d " ")
    pclean2=$(git -C "$PLUGIN" status --porcelain 2>/dev/null | wc -l | tr -d " ")
    napdigest=$(podman inspect napcat --format "{{.Image}}" 2>/dev/null || echo "")
    napup=$(podman ps --filter name=napcat --format "{{.Names}}" 2>/dev/null | head -1 || true)
    mounts=$(podman inspect napcat --format "{{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}" 2>/dev/null || true)
    python3.12 - "$f" "$pc" "$ppc" "$pclean" "$pclean2" "$napdigest" "$napup" "$mounts" <<'PY'
import json, sys, time
f, pc, ppc, pclean, pclean2, napdigest, napup, mounts = sys.argv[1:]
data = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "schema_version": 2,
    "repos": {
        "prototype": {"commit": pc or None, "working_tree_clean": pclean == "0"},
        "plugin": {"commit": ppc or None, "working_tree_clean": pclean2 == "0"},
    },
    "runtime": {"python": sys.version.split()[0]},
    "containers": {
        "napcat": {
            "running": bool(napup),
            "image_digest": napdigest or None,
            "mounts": (mounts or "").split(),
        }
    },
    "ok": bool(pc and ppc and napdigest),
}
json.dump(data, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    json_ok "$f"
    echo "manifest -> $f"
}

# ---------- bootstrap smoke ----------
cmd_smoke() {
    local fast="${1:-}"
    local pass=0 fail=0
    check() {
        if "$@" >/dev/null 2>&1; then
            echo "  PASS: $*"
            pass=$((pass + 1))
        else
            echo "  FAIL: $*"
            fail=$((fail + 1))
        fi
    }
    check bash -n "$PROTO/ops/ops.sh"
    check python3.12 -m py_compile \
        "$PROTO/packages/dududa-agent/src/dududa/safeguards/limits.py" \
        "$PROTO/packages/dududa-agent/src/dududa/application/dududa_prod.py" \
        "$PROTO/packages/dududa-agent/src/dududa/application/dududa_core.py" \
        "$PROTO/packages/dududa-agent/src/dududa/core/renderer.py"
    check env DUDUDA_AGENT_SRC="$AGENT_SRC" \
        PYTHONPATH="$PLUGIN${PYTHONPATH:+:$PYTHONPATH}" \
        python3.12 -c "import main"
    if [ "$fast" != "--fast" ]; then
        check python3.12 -m pytest "$PROTO/tests/unit/test_p6_mcp_client.py" -q --tb=line
        check python3.12 -m pytest \
            "$PROTO/tests/unit/test_runtime_limits.py" \
            "$PROTO/tests/unit/test_renderer_delivery.py" -q --tb=line
    fi
    check systemctl is-active "$SERVICE"
    echo "smoke: $pass passed, $fail failed"
    [ "$fail" -eq 0 ]
}

# ---------- real-network smoke（Phase 9：与阻塞 CI 分开） ----------
cmd_smoke_net() {
    bash "$PROTO/ops/smoke_net.sh"
}

# ---------- Control Plane（ADR-0001 CP-P0） ----------
cmd_cp() {
    local action="${1:-status}"
    case "$action" in
        status)
            # token 在 dududa-cp 的 EnvironmentFile（/root/data/cp.env），不打印明文
            if [ -f /root/data/cp.env ] && grep -qE '^DUDUDA_CP_TOKEN=.+' /root/data/cp.env 2>/dev/null; then
                echo "cp token configured: yes"
            else
                echo "cp token configured: no"
            fi
            if command -v ss >/dev/null 2>&1; then
                ss -ltn 2>/dev/null | grep -q ':8000 '                     && echo "cp listener: 0.0.0.0:8000 (listening)"                     || echo "cp listener: not listening"
            fi
            local audit="$PROTO/data/cp_audit.jsonl"
            echo "cp audit: $([ -f "$audit" ] && wc -l < "$audit" || echo 0) lines"
            if [ -f /etc/systemd/system/dududa-cp.service ]; then
                echo "cp service: $(systemctl is-active dududa-cp 2>/dev/null || echo inactive)"
            else
                echo "cp service: unit not installed"
            fi
            ;;
        start|stop|restart)
            if [ ! -f /etc/systemd/system/dududa-cp.service ]; then
                echo "dududa-cp unit not installed; run: bash $PROTO/deploy/control_plane/install_cp.sh" >&2
                return 2
            fi
            systemctl "$action" dududa-cp
            ;;
        *)
            echo "usage: ops.sh cp {status|start|stop|restart}" >&2
            return 2
            ;;
    esac
}

# ---------- backup（先写 health 快照，再委托 manage.sh） ----------
cmd_backup() {
    OPS_OUT=/tmp cmd_health >/dev/null
    [ -f "$MANAGE" ] || { echo "manage script not found: $MANAGE" >&2; return 1; }
    bash "$MANAGE" backup
}

case "${1:-}" in
    health) cmd_health ;;
    manifest) cmd_manifest ;;
    smoke) cmd_smoke "${2:-}" ;;
    smoke-net) cmd_smoke_net ;;
    cp) cmd_cp "${2:-status}" ;;
    backup) cmd_backup ;;
    *) echo "usage: $0 {health|manifest|smoke [--fast]|backup}" >&2; exit 1 ;;
esac
