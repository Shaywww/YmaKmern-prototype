#!/bin/bash
# YmaKmern lifecycle manager.  All machine paths can be overridden so the
# script is both versioned/testable in CI and usable from the production host.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROTO="${DUDUDA_PROTO_DIR:-$(dirname "$SCRIPT_DIR")}"
PLUGIN_DIR="${DUDUDA_PLUGIN_DIR:-${HOME}/data/plugins/dududa20}"
BACKUP_DIR="${DUDUDA_BACKUP_DIR:-${HOME}/backups/dududa20}"
DATA_DIR="${DUDUDA_DATA_DIR:-${HOME}/data}"
SERVICE="${DUDUDA_SERVICE:-astrbot}"
PY="${DUDUDA_PYTHON:-python3.12}"

backup() {
    local dst="$BACKUP_DIR/dududa20_$(date +%Y%m%d_%H%M%S).tar.gz"
    local targets=("$PLUGIN_DIR" "$PROTO") optional
    for optional in \
        /etc/systemd/system/astrbot.service \
        /etc/systemd/system/dududa-cp.service \
        "$DATA_DIR/cp.env" "$DATA_DIR/ops"; do
        [ ! -e "$optional" ] || targets+=("$optional")
    done
    mkdir -p "$BACKUP_DIR"
    tar -czf "$dst" "${targets[@]}" 2>/dev/null
    echo "backup -> $dst"
    # Retain the newest five archives, constrained to this backup directory.
    mapfile -t archives < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name 'dududa20_*.tar.gz' -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
    local i
    for ((i=5; i<${#archives[@]}; i++)); do
        rm -f -- "${archives[$i]}"
    done
}

restore() {
    local archive="${1:-}"
    [ -f "$archive" ] || { echo "not found: $archive" >&2; return 1; }
    if tar -tzf "$archive" | grep -Eq '(^|/)\.\.(/|$)'; then
        echo "unsafe archive path: $archive" >&2
        return 1
    fi
    systemctl stop "$SERVICE"
    tar -xzf "$archive" -C /
    systemctl start "$SERVICE"
    echo "restored from $archive"
}

rollback() {
    local latest
    latest=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name 'dududa20_*.tar.gz' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | head -1 | cut -d' ' -f2-)
    [ -n "$latest" ] || { echo "no backup" >&2; return 1; }
    restore "$latest"
}

status() {
    systemctl is-active "$SERVICE"
    journalctl -u "$SERVICE" --no-pager -n 3 \
        | grep -iE 'memory=OK|YmaKmern|Dududa|error' || true
}

restart() {
    systemctl restart "$SERVICE"
    sleep 3
    status
}

logs() {
    journalctl -u "$SERVICE" --no-pager -n "${1:-30}"
}

health_ok() {
    local out="${OPS_OUT:-$DATA_DIR/ops}"
    OPS_OUT="$out" bash "$PROTO/ops/ops.sh" health >/dev/null 2>&1 \
        || return 1
    grep -q '"ok": true' "$out/health_status.json"
}

tests_ok() {
    (cd "$PROTO" && DUDUDA_PLUGIN_DIR="$PLUGIN_DIR" \
        "$PY" -m pytest tests/ -q --tb=line 2>&1 \
        | tail -1 | grep -q 'passed')
}

# upgrade [tarball]: backup -> health gate -> update -> tests -> restart.
upgrade() {
    local src="${1:-}" latest
    echo "[upgrade] 1/6 backup"
    backup
    latest=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name 'dududa20_*.tar.gz' -printf '%T@ %p\n' \
        | sort -rn | head -1 | cut -d' ' -f2-)
    echo "[upgrade] backup = $latest"
    echo "[upgrade] 2/6 health gate"
    health_ok || { echo "[upgrade] FAIL: health gate"; return 1; }
    echo "[upgrade] 3/6 update"
    if [ -n "$src" ] && [ -f "$src" ]; then
        if tar -tzf "$src" | grep -q "${PROTO#/}/"; then
            systemctl stop "$SERVICE"
            tar -xzf "$src" -C /
        else
            echo "[upgrade] FAIL: archive does not contain $PROTO"
            return 1
        fi
    else
        local repo
        for repo in "$PROTO" "$PLUGIN_DIR"; do
            [ -z "$(git -C "$repo" status --porcelain 2>/dev/null)" ] \
                || { echo "[upgrade] FAIL: dirty worktree $repo"; return 1; }
            git -C "$repo" pull --ff-only 2>/dev/null \
                || echo "[upgrade] $repo has no usable remote; keeping HEAD"
        done
    fi
    echo "[upgrade] 4/6 tests"
    if ! tests_ok; then
        echo "[upgrade] FAIL: tests; rolling back $latest"
        restore "$latest"
        return 1
    fi
    echo "[upgrade] 5/6 restart"
    systemctl restart "$SERVICE"
    sleep 5
    echo "[upgrade] 6/6 verify"
    systemctl is-active "$SERVICE"
    journalctl -u "$SERVICE" --no-pager -n 30 \
        | grep -E 'YmaKmern|Dududa 2.0' | tail -1
    echo "[upgrade] OK (backup: $latest)"
}

# bootstrap [tarball]: restore source if necessary, install CP unit, then start.
bootstrap() {
    local src="${1:-}"
    command -v "$PY" >/dev/null \
        || { echo "FAIL: missing $PY" >&2; return 1; }
    command -v astrbot >/dev/null 2>&1 \
        || [ -x /usr/local/python3.12/bin/astrbot ] \
        || { echo "FAIL: missing astrbot CLI" >&2; return 1; }
    if [ ! -d "$PROTO/.git" ]; then
        [ -n "$src" ] && [ -f "$src" ] \
            || { echo "FAIL: missing $PROTO and backup archive" >&2; return 1; }
        tar -xzf "$src" -C /
    fi
    [ -d "$PLUGIN_DIR/.git" ] \
        || { echo "FAIL: missing $PLUGIN_DIR" >&2; return 1; }
    if [ -f "$PROTO/deploy/control_plane/install_cp.sh" ]; then
        bash "$PROTO/deploy/control_plane/install_cp.sh"
    fi
    [ -f /etc/systemd/system/astrbot.service ] \
        || { echo "FAIL: missing astrbot.service" >&2; return 1; }
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE"
    sleep 5
    systemctl is-active "$SERVICE"
    health_ok && echo "[bootstrap] OK" \
        || echo "[bootstrap] WARN: health check not ready"
}

case "${1:-}" in
    backup) backup ;;
    restore) restore "${2:-}" ;;
    rollback) rollback ;;
    upgrade) upgrade "${2:-}" ;;
    bootstrap) bootstrap "${2:-}" ;;
    status) status ;;
    restart) restart ;;
    logs) logs "${2:-30}" ;;
    *)
        echo "usage: $0 {backup|restore <file>|rollback|upgrade [tarball]|bootstrap [tarball]|status|restart|logs [n]}" >&2
        exit 2
        ;;
esac
