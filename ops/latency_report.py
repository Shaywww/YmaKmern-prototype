#!/usr/bin/env python3
"""Summarise user-visible latency traces without reading message content.

Usage:
    python ops/latency_report.py
    python ops/latency_report.py --trace-dir data/traces --days 7

The report separates chat and tool runs and prints P50/P95 end-to-end latency,
time to the first complete model output, and model calls per completed run.
Providers are currently non-streaming, so ``model_first_output`` means the
first complete provider response; it is deliberately not presented as TTFT.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return float(ordered[index])


def load_events(directory: Path, days: int) -> list[dict]:
    events: list[dict] = []
    for offset in range(max(1, int(days))):
        path = directory / f"{date.today() - timedelta(days=offset)}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    events.append(value)
    return events


def summarise(events: list[dict]) -> dict[str, dict]:
    runs: dict[str, dict] = defaultdict(
        lambda: {"stages": {}, "model_calls": 0, "received_ts_ms": None,
                 "first_model_output_ms": None, "flow_kind": "unknown"})
    for event in events:
        trace_id = str(event.get("trace_id", "") or "")
        if not trace_id:
            continue
        run = runs[trace_id]
        kind = str(event.get("flow_kind", "") or "")
        if kind in {"chat", "tool"}:
            run["flow_kind"] = kind
        name = event.get("event")
        if name == "interaction_timing":
            stage = str(event.get("stage", "") or "")
            run["stages"][stage] = float(event.get("elapsed_ms", 0) or 0)
            if stage == "received":
                run["received_ts_ms"] = float(event.get("ts_ms", 0) or 0)
        elif name == "model_request":
            run["model_calls"] += 1
        elif (name == "model_response"
              and run["first_model_output_ms"] is None):
            received = run.get("received_ts_ms")
            if received is not None:
                run["first_model_output_ms"] = max(
                    0.0, float(event.get("ts_ms", 0) or 0) - received)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for run in runs.values():
        stages = run["stages"]
        completed = stages.get("send_success", stages.get("validation_end"))
        if completed is None:
            continue
        run["completed_ms"] = completed
        grouped[run["flow_kind"]].append(run)

    result = {}
    for kind, values in sorted(grouped.items()):
        completed = [item["completed_ms"] for item in values]
        first_output = [item["first_model_output_ms"] for item in values
                        if item["first_model_output_ms"] is not None]
        result[kind] = {
            "runs": len(values),
            "latency_p50_ms": percentile(completed, 0.50),
            "latency_p95_ms": percentile(completed, 0.95),
            "first_model_output_p50_ms": percentile(first_output, 0.50),
            "first_model_output_p95_ms": percentile(first_output, 0.95),
            "model_calls_per_run": round(
                sum(item["model_calls"] for item in values) / len(values), 2),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trace-dir",
        default=os.environ.get("DUDUDA_TRACE_DIR", "data/traces"))
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()
    summary = summarise(load_events(Path(args.trace_dir), args.days))
    if not summary:
        print("No completed chat/tool traces found.")
        return 0
    for kind, row in summary.items():
        print(
            f"{kind}: runs={row['runs']} "
            f"latency_p50={row['latency_p50_ms']:.0f}ms "
            f"latency_p95={row['latency_p95_ms']:.0f}ms "
            f"first_output_p50={row['first_model_output_p50_ms']}ms "
            f"first_output_p95={row['first_model_output_p95_ms']}ms "
            f"model_calls/run={row['model_calls_per_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
