#!/usr/bin/env bash
# Eval 门禁（Phase 9）：语法 + 全量测试 + 版本化 Eval，任一失败即非零退出。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/3 py_compile =="
python3.12 -m py_compile $(find packages -name '*.py') $(find tests -name '*.py' -not -path '*/__pycache__/*')

echo "== 2/3 pytest =="
python3.12 -m pytest tests/ -q --tb=line

echo "== 3/3 evals =="
python3.12 -m tests.evals.run_evals

echo "ALL GATES PASS"
