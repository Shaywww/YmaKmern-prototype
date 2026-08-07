#!/bin/bash
# 真实网络 smoke（文档 2.5.10 Phase 9）：LLM 网关可达性 + 生产 Router 真实往返。
# 与阻塞 CI 分开：exit_gate_check.sh / eval_gate.sh 均不调用本脚本；
# 默认 pytest 通过 -m "not net" 排除 tests/smoke。
set -uo pipefail
SERVICE="astrbot"
PROTO="/opt/dududa20-prototype"

ENV_LINE=$(systemctl show "$SERVICE" -p Environment | sed 's/^Environment=//')
if [ -z "$ENV_LINE" ]; then
    echo "!! 无法从 $SERVICE 读取 Environment" >&2
    exit 2
fi
eval "export $ENV_LINE"

cd "$PROTO"
echo "== real-network smoke: 网关可达性 + 生产 LLM 往返 =="
python3.12 -m pytest tests/smoke -m net -q --tb=short
rc=$?
echo "== smoke_net: $([ $rc -eq 0 ] && echo PASS || echo FAIL) =="
exit $rc