from ops.latency_report import percentile, summarise


def test_percentile_uses_nearest_rank():
    assert percentile([10, 20, 30, 40], 0.5) == 20
    assert percentile([10, 20, 30, 40], 0.95) == 40


def test_latency_summary_separates_chat_and_tool_and_counts_models():
    events = [
        {"event": "interaction_timing", "trace_id": "chat-1",
         "stage": "received", "elapsed_ms": 0, "ts_ms": 1000,
         "flow_kind": "unknown"},
        {"event": "model_request", "trace_id": "chat-1", "ts_ms": 1010},
        {"event": "model_response", "trace_id": "chat-1", "ts_ms": 1200},
        {"event": "interaction_timing", "trace_id": "chat-1",
         "stage": "send_success", "elapsed_ms": 260,
         "flow_kind": "chat"},
        {"event": "interaction_timing", "trace_id": "tool-1",
         "stage": "received", "elapsed_ms": 0, "ts_ms": 2000,
         "flow_kind": "unknown"},
        {"event": "model_request", "trace_id": "tool-1", "ts_ms": 2010},
        {"event": "model_request", "trace_id": "tool-1", "ts_ms": 2030},
        {"event": "model_response", "trace_id": "tool-1", "ts_ms": 2300},
        {"event": "interaction_timing", "trace_id": "tool-1",
         "stage": "validation_end", "elapsed_ms": 900,
         "flow_kind": "tool"},
    ]

    summary = summarise(events)

    assert summary["chat"]["latency_p50_ms"] == 260
    assert summary["chat"]["first_model_output_p50_ms"] == 200
    assert summary["chat"]["model_calls_per_run"] == 1
    assert summary["tool"]["latency_p95_ms"] == 900
    assert summary["tool"]["model_calls_per_run"] == 2
