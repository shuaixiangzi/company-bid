"""An isolated diagnostic unit uses the exact production request and executor."""
import copy
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    extraction_window_payload, extract_requirements, run_extraction_window,
)
from company_bid.runtime import Executor, NodeSpec
from company_bid.structured import JsonTaskGateway
from test_source_selected_citations import Context
from test_source_selector_wire import raw_for, sources


def test_full_extraction_and_isolated_window_share_request_validation_and_result(tmp_path):
    manifest, parsed, _, windows, _ = sources(tmp_path)
    config = {"window_max_chars": 14000, "window_max_blocks": 60}
    requests = []

    def call(ctx, task_type, unit_id, request, validate):
        requests.append(copy.deepcopy(request))
        return validate(raw_for(request["window"], ["B1", "B2", "B3"]))

    complete = extract_requirements(Context(), parsed, manifest, call, config)
    isolated_context = Context()
    isolated = run_extraction_window(isolated_context, windows[0], config, call)
    assert requests[0] == requests[1] == extraction_window_payload(windows[0], config)
    assert isolated == complete["window_results"][0]
    assert "coverage" not in isolated and "deferred_context_closure" not in isolated
    assert isolated_context.captures["n2-window-partition"]["scope"] == "one_window_not_whole_extraction_or_semantic_acceptance"


@pytest.mark.parametrize("fail_partition_capture", [False, True])
def test_isolated_window_keeps_real_executor_checkpoint_trace_and_resume_without_http(tmp_path, monkeypatch, fail_partition_capture):
    _, _, _, windows, _ = sources(tmp_path)
    config = {"window_max_chars": 14000, "window_max_blocks": 60}
    monkeypatch.setenv("TEST_ISOLATED_KEY", "synthetic-key")
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
               "auth_env": "TEST_ISOLATED_KEY", "max_attempts": 1, "max_output_tokens": 1000,
               "max_input_chars": 50000, "max_run_calls": 2, "max_run_usd": .1,
               "input_usd_per_million": 1, "output_usd_per_million": 1}
    sent = []

    def transport(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        request = json.loads(payload["messages"][-1]["content"])
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(raw_for(request["window"], ["B1", "B2", "B3"]))}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}

    gateway = JsonTaskGateway(options, transport=transport)

    def handler(ctx, value):
        result = run_extraction_window(ctx, value["window"], config, gateway)
        return {"diagnostic_scope": "single_window", "whole_N2_complete": False, "window_result": result}

    specs = [NodeSpec("N2_WINDOW_DIAGNOSTIC", "synthetic-window-v1", handler)]
    execution_config = {"structured_provider": options, "debug_capture": True}
    executor = Executor(tmp_path / "runtime", specs, execution_config)
    rid = executor.create_run({"window": windows[0], "synthetic": True})
    if fail_partition_capture:
        put_json = executor.store.put_json

        def fail_capture(run_id, kind, value):
            if kind == "capture" and value.get("rule") == "native_pdf_chapter_then_bounded_capacity_v1":
                raise OSError("Synthetic partition capture write failure")
            return put_json(run_id, kind, value)

        monkeypatch.setattr(executor.store, "put_json", fail_capture)
    state = executor.execute(rid)
    if fail_partition_capture:
        assert state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED" and sent == []
        assert state.get("call_budget", {}).get("reservation_count", 0) == 0
        report = inspect_diagnostics(tmp_path / "runtime", rid)
        assert report["actual_call_attempts"] == 0 and report["gaps"]
        return
    assert state["status"] == "succeeded" and len(sent) == 1
    assert executor.output(rid)["whole_N2_complete"] is False
    original_unit = copy.deepcopy(state["nodes"][0]["units"][windows[0]["window_id"]])
    restarted = Executor(tmp_path / "runtime", specs, execution_config)
    resumed = restarted.resume(rid)
    assert len(sent) == 1 and resumed["nodes"][0]["units"][windows[0]["window_id"]] == original_unit
    report = inspect_diagnostics(tmp_path / "runtime", rid)
    assert report["actual_call_attempts"] == 1 and not report["gaps"]
