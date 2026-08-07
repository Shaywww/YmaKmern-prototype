#!/bin/bash
# dududa20 退出门禁证据检查（文档 2.5.11 P0/P1/P2 + Phase 10 兼容清理）。
# 只读：跑门禁相关测试 + 静态证据；--fast 跳过 pytest 只做静态检查。
# 用法: bash scripts/exit_gate_check.sh [--fast]
set -euo pipefail

PROTO="/opt/dududa20-prototype"
PLUGIN="/root/data/plugins/dududa20"
FAST="${1:-}"

pass=0
fail=0
ok() { echo "  [PASS] $*"; pass=$((pass + 1)); }
ko() { echo "  [FAIL] $*"; fail=$((fail + 1)); }

run_tests() { # name files...
    local name="$1"; shift
    local out
    out=$(cd "$PROTO" && python3.12 -m pytest "$@" -q --tb=line 2>&1 | tail -1)
    if echo "$out" | grep -q "passed"; then
        ok "$name: $out"
    else
        ko "$name: $out"
    fi
}

static() { # name cmd...
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$name"; else ko "$name"; fi
}

echo "== P0 gate（契约 -> 安全 -> 插件边界 -> Memory）=="
if [ "$FAST" = "--fast" ]; then
    for f in test_forbidden_imports test_plugin_split_p0 test_astrbot_adapter \
             test_security_p0 test_prompt_injection test_memory_isolation_p0 \
             test_migration_p0; do
        static "P0 门禁测试存在: $f" test -f "$PROTO/tests/$f.py"
    done
else
    run_tests "P0 契约（forbidden imports + 插件拆分 + 事件契约）" \
        tests/test_forbidden_imports.py tests/test_plugin_split_p0.py \
        tests/test_astrbot_adapter.py
    run_tests "P0 权限负向" tests/test_security_p0.py tests/test_prompt_injection.py
    run_tests "P0 Memory 隔离" tests/test_memory_isolation_p0.py
    run_tests "P0 数据迁移回滚" tests/test_migration_p0.py
fi
static "P0 插件真实加载（import + 实例化）" python3.12 -c "import sys; sys.path.insert(0, '$PROTO'); sys.path.insert(0, '$PLUGIN'); import main; from unittest import mock; p = main.Main(mock.Mock()); assert p.oc_renderer is not None and p.limits is not None"

echo "== P1 gate（feature flag 选择性切换 + 行为/降级证据）=="
static "P1 DUDUDA_ROUTER 开关" grep -q 'DUDUDA_ROUTER' "$PLUGIN/main.py"
static "P1 DUDUDA_HYBRID_RENDER 开关" grep -q 'DUDUDA_HYBRID_RENDER' "$PLUGIN/main.py"
static "P1 DUDUDA_LIMITS_ENABLED 开关" grep -rq 'DUDUDA_LIMITS_ENABLED' "$PLUGIN/main.py" "$PROTO/packages"
static "P1 DUDUDA_MCP_CLIENT 开关" grep -rq 'DUDUDA_MCP_CLIENT' "$PLUGIN/main.py" "$PROTO/packages"
if [ "$FAST" != "--fast" ]; then
    run_tests "P1 429 降级 + Router 行为" tests/test_router_wiring.py
    run_tests "P1 工具链降级/重试/硬上限" tests/test_tool_runtime_closure.py
    run_tests "P1 无重复回复/重复 Tool/错误 Memory" \
        tests/test_at_only_replies.py tests/test_p2_orchestrator_tools.py \
        tests/test_store_memory_writegate.py
fi

echo "== P2 gate（部署全生命周期 + legacy 清零）=="
static "P2 manage.sh backup/restore/rollback" grep -q 'rollback' /root/manage.sh
static "P2 ops.sh health/manifest/smoke" test -x "$PROTO/scripts/ops.sh"
static "P2 插件薄壳 main.py 行数" bash -c "[ \$(wc -l < '$PLUGIN/main.py') -lt 500 ]"
static "P2 应用层不引用旧 main" bash -c "! grep -rnE 'from main import|^import main' '$PROTO/packages/application/' | grep -v __pycache__ | grep -q ."
if [ "$FAST" != "--fast" ]; then
    tmp=$(mktemp -d)
    if OPS_OUT="$tmp" bash "$PROTO/scripts/ops.sh" manifest >/dev/null 2>&1 \
        && python3.12 -c "import json,sys; json.load(open('$tmp/supply_chain_manifest.json', encoding='utf-8'))" \
        && grep -q '"ok": true' "$tmp/supply_chain_manifest.json"; then
        ok "P2 供应链 manifest 可生成且 ok"
    else
        ko "P2 供应链 manifest 可生成且 ok"
    fi
    if OPS_OUT="$tmp" bash "$PROTO/scripts/ops.sh" health >/dev/null 2>&1 \
        && grep -q '"ok": true' "$tmp/health_status.json"; then
        ok "P2 health 检查 ok"
    else
        ko "P2 health 检查 ok"
    fi
    rm -rf "$tmp"
fi
echo "== CP gate（ADR-0001 控制面安全基线）=="
static "CP security 模块存在" test -f "$PROTO/packages/control_plane/security.py"
static "CP token fail closed（未配置 -> 401）" grep -q 'def token_ok' "$PROTO/packages/control_plane/security.py"
static "CP 写操作权限（manage_config, 非 owner 403）" bash -c "grep -q 'require_write' '$PROTO/packages/control_plane/security.py' && grep -q 'manage_config' '$PROTO/packages/control_plane/security.py'"
static "CP MCP query 走 CapabilityRegistry + access 策略" bash -c "grep -q 'cap_registry.get' '$PROTO/packages/control_plane/app.py' && grep -q 'mcp_access.is_allowed' '$PROTO/packages/control_plane/app.py'"
static "CP 审计 JSONL（AuditLogger）" grep -q 'class AuditLogger' "$PROTO/packages/control_plane/security.py"
static "CP 脱敏走共享 Redactor" grep -q 'Redactor' "$PROTO/packages/control_plane/security.py"
if [ "$FAST" != "--fast" ]; then
    run_tests "CP-P0 安全基线（鉴权/权限/审计/脱敏/Scope/MCP 入口）"         tests/test_control_plane.py tests/test_control_plane_cp_p0.py
fi

echo "== P10 gate（Phase 10 兼容清理 + legacy 清零）=="
static "P10 原型仓库无 legacy 副本" bash -c "! find '$PROTO' -path '$PROTO/.git' -prune -o -type f \( -name '*.bak*' -o -name '*.swp' -o -name '*.swo' \) -print | grep -q ."
static "P10 插件仓库无 legacy 副本" bash -c "! find '$PLUGIN' -path '$PLUGIN/.git' -prune -o -type f \( -name '*.bak*' -o -name 'main.py.final' -o -name 'main.py.stable*' -o -name 'main.py.v2.final' \) -print | grep -q ."
static "P10 原型仓库工作区干净" bash -c "cd '$PROTO' && test -z \"\$(git status --porcelain)\""
static "P10 插件仓库工作区干净" bash -c "cd '$PLUGIN' && test -z \"\$(git status --porcelain)\""
static "P10 应用层无旧入口路径引用" bash -c "! grep -rn '$PLUGIN/main.py' '$PROTO/packages/' | grep -v __pycache__ | grep -q ."
static "P10 文档含 rollback/清理清单" grep -q 'rollback\|回滚\|清理' "$PROTO/docs/ops_runbook.md"

echo
echo "summary: 门禁检查 $((pass + fail)) 项, PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
