#!/bin/bash
# 安装/更新 dududa-cp（ADR-0001 CP-P1）：systemd 单元 + cp.env + 防火墙白名单。
# 幂等：重复执行会更新单元并重启服务；token 只在首次生成。
set -euo pipefail

PROTO="/opt/dududa20-prototype"
SRC="$PROTO/deploy/control_plane"
UNIT="/etc/systemd/system/dududa-cp.service"
ENV_FILE="/root/data/cp.env"

if [ ! -f "$ENV_FILE" ]; then
    mkdir -p /root/data
    umask 077
    cp "$SRC/cp.env.example" "$ENV_FILE"
    TOKEN=$(python3.12 -c 'import secrets; print(secrets.token_urlsafe(32))')
    sed -i "s|^DUDUDA_CP_TOKEN=.*|DUDUDA_CP_TOKEN=$TOKEN|" "$ENV_FILE"
    echo "created $ENV_FILE with random token"
fi

cp "$SRC/dududa-cp.service" "$UNIT"
systemctl daemon-reload
systemctl enable dududa-cp >/dev/null 2>&1 || true
systemctl restart dududa-cp
bash "$PROTO/ops/dududa-fw.sh"
echo "dududa-cp: $(systemctl is-active dududa-cp)"
