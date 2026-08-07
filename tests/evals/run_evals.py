#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立运行五组件 Eval；任一阈值不过则退出码 1。

用法: cd /opt/dududa20-prototype && python3.12 -m tests.evals.run_evals
"""
import asyncio
import os
import sys
from pathlib import Path

# Eval 自身的 Trace 事件写入独立目录，不污染生产 data/traces（文档 2.5.10）
os.environ.setdefault(
    "DUDUDA_TRACE_DIR",
    str(Path(__file__).resolve().parents[2] / "data" / "traces-eval"),
)

sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")
sys.path.insert(0, "/root/data/plugins/dududa20")

from tests.evals import evals


def main() -> int:
    thresholds = evals.load_thresholds()
    versions = {name: evals.load_fixture(f"{name}_cases.json").get("version")
                for name in ("perception", "social_decision", "social_decision_policy",
                             "tool_runtime", "capability_retrieval",
                             "memory_writegate", "oc_render")}
    print("=" * 64)
    print("Dududa 2.0 Eval（文档 2.5.10 / Phase 9 前半）")
    print("fixture versions:", versions)
    print("=" * 64)
    all_ok = True
    for name, runner in (
        ("perception", evals.run_perception),
        ("social_decision", evals.run_social_decision),
        ("social_decision_policy", evals.run_social_decision_policy),
        ("tool_runtime", lambda: asyncio.run(evals.run_tool_runtime())),
        ("capability_retrieval", evals.run_capability_retrieval),
        ("memory_writegate", evals.run_memory_writegate),
        ("oc_render", evals.run_oc_render),
    ):
        metric = runner()
        ok, failures = evals.check(name, metric, thresholds)
        all_ok = all_ok and ok
        print(f"\n[{name}] {'PASS' if ok else 'FAIL'}")
        for k, v in metric.items():
            if k in ("details", "version"):
                continue
            print(f"  {k}: {v}")
        for f in failures:
            print(f"  !! {f}")
    print("\n" + ("ALL EVALS PASS" if all_ok else "EVALS FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
