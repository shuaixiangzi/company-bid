import json

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest
from company_bid.structured import JsonTaskGateway


def test_retry_keeps_actual_attempt_trace_identity(tmp_path, monkeypatch):
    import io
    from urllib.error import HTTPError
    monkeypatch.setenv("TEST_CHAIN_KEY", "synthetic-retry-key")
    count = 0
    def transport(*args):
        nonlocal count
        count += 1
        if count == 1:
            raise HTTPError("https://example.invalid/chat", 503, "busy", {}, io.BytesIO(b'{}'))
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_CHAIN_KEY",
                                    max_attempts=2), transport=transport, sleep=lambda _: None)
    def run(ctx, value):
        return ctx.run_unit("W1", value, lambda: provider.generate_json(ctx, value, unit_id="W1", prompt_version="1",
                            system_prompt="extract JSON", task_type="extract", validate=lambda x: x))
    executor = Executor(tmp_path, [NodeSpec("N2", "1", run)], {})
    rid = executor.create_run({})
    assert executor.execute(rid)["status"] == "succeeded"
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "available" and report["actual_call_attempts"] == 2
    spans = {row["span_id"]: row for row in map(json.loads, (tmp_path / "runs" / rid / "spans.jsonl").read_text().splitlines())}
    for call in report["calls"]:
        span = spans[call["span_id"]]
        assert span["name"] == "provider.call"
        assert span["attributes"]["call_attempt"] == call["call_attempt"]
        assert span["attributes"]["call_id"] == call["call_id"]
    assert report["calls"][0]["outcome"] == "failed"
    # An older/wrong retry event is reported, without replacing actual identity.
    events = tmp_path / "runs" / rid / "events.jsonl"
    rows = list(map(json.loads, events.read_text().splitlines()))
    for row in rows:
        if row["event"] == "call.retry_scheduled":
            row["span_id"] = "0" * 16
    events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    changed = inspect_diagnostics(tmp_path, rid)
    assert changed["calls"][0]["span_id"] == report["calls"][0]["span_id"]
    assert any(gap["code"] == "CALL_RETRY_TRACE_MISMATCH" for gap in changed["gaps"])


def test_provider_redacts_credential_keys_and_preserves_domain_validation_code(tmp_path, monkeypatch):
    secret = "opaque-test-active-credential-97"
    monkeypatch.setenv("TEST_CHAIN_KEY", secret)
    from company_bid.planning import PlanningError

    def transport(*args):
        return {secret: {"echo": secret}, "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({secret: secret, "quote": "wrong"})}}]}

    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_CHAIN_KEY",
                                    max_attempts=1), transport=transport)
    def reject(candidate):
        raise PlanningError("CITATION_QUOTE_NOT_UNIQUE_EXACT")
    def run(ctx, value):
        return provider.generate_json(ctx, value, unit_id="W", prompt_version="1", system_prompt="extract JSON",
                                      task_type="extract", validate=reject)
    executor = Executor(tmp_path, [NodeSpec("N2", "1", run)], {})
    rid = executor.create_run({})
    state = executor.execute(rid)
    assert state["error_code"] == "CITATION_QUOTE_NOT_UNIQUE_EXACT"
    assert inspect_diagnostics(tmp_path, rid)["calls"][0]["error_code"] == state["error_code"]
    for path in (tmp_path / "runs" / rid).rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_unexpected_transport_exception_records_terminal_call_and_safe_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CHAIN_KEY", "transport-credential-canary")
    def transport(*args):
        raise AssertionError("transport-credential-canary and source-content-canary")
    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_CHAIN_KEY",
                                    max_attempts=1), transport=transport)
    def run(ctx, value):
        return provider.generate_json(ctx, value, unit_id="W", prompt_version="1", system_prompt="extract JSON",
                                      task_type="extract", validate=lambda x: x)
    executor = Executor(tmp_path, [NodeSpec("N2", "1", run)], {})
    rid = executor.create_run({})
    assert executor.execute(rid)["error_code"] == "PROVIDER_EXECUTION_ERROR"
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 1 and report["calls"][0]["outcome"] == "failed"
    assert not any(gap["code"] == "CALL_ATTEMPT_INCOMPLETE" for gap in report["gaps"])
    captures = report["nodes"][0]["attempts"][0]["captures"]
    evidence = next(c["artifact"] for c in captures if c["name"].startswith("adapter-error-"))
    private = json.loads((tmp_path / evidence["path"]).read_text())
    assert any(frame["function"] == "transport" for frame in private["frames"])
    for path in (tmp_path / "runs" / rid).rglob("*"):
        if path.is_file():
            assert b"transport-credential-canary" not in path.read_bytes()
            assert b"source-content-canary" not in path.read_bytes()


def test_window_failure_resume_has_complete_call_trace_and_private_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CHAIN_KEY", "credential-canary-123")
    calls = []

    def transport(endpoint, payload, secret, timeout):
        content = json.loads(payload["messages"][1]["content"])
        unit = content["window"]
        calls.append(unit)
        answer = {"quote": "WRONG" if len(calls) == 2 else "SOURCE-CANARY"}
        result = {"id": "test-request", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        if len(calls) != 2:
            result["usage"] = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        return result

    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_CHAIN_KEY",
                                   max_attempts=1, max_run_calls=10, max_run_usd=1), transport=transport)

    def validate(candidate):
        if candidate["quote"] != "SOURCE-CANARY":
            raise ProviderFailure("QUOTATION_NOT_IN_SOURCE")
        return candidate

    def extract(ctx, value):
        outputs = []
        for unit in ("W1", "W2"):
            outputs.append(ctx.run_unit(unit, {"source": "SOURCE-CANARY", "unit": unit},
                lambda unit=unit: provider.generate_json(ctx, {"window": unit, "source": "SOURCE-CANARY"}, unit_id=unit,
                    task_type="requirements", prompt_version="test-v1", system_prompt="extract", validate=validate)))
        return {"windows": outputs}

    executor = Executor(tmp_path, [NodeSpec("N2", "1", extract), NodeSpec("N6", "1", lambda ctx, value: value)], {})
    rid = executor.create_run({})
    assert executor.execute(rid)["status"] == "failed"
    first = inspect_diagnostics(tmp_path, rid)
    assert first["evidence_status"] == "available"
    assert first["calls"][1]["error_code"] == "QUOTATION_NOT_IN_SOURCE"
    assert first["unknown_usage_attempts"] == 1
    assert executor.resume(rid)["status"] == "succeeded"
    assert calls == ["W1", "W2", "W2"]
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "available" and report["actual_call_attempts"] == 3
    assert report["nodes"][0]["unit_counts"] == {"succeeded": 2}
    spans = [json.loads(line) for line in (tmp_path / "runs" / rid / "spans.jsonl").read_text().splitlines()]
    for call in report["calls"]:
        span = next(s for s in spans if s["span_id"] == call["span_id"])
        assert span["name"] == "provider.call" and span["attributes"]["unit_id"] == call["unit_id"]
        assert span["attributes"]["validation_status"] == call["validation_status"]
    captures = [capture for item in report["nodes"][0]["attempts"] for capture in item["captures"]]
    assert any(c["unit_id"] == "W2" and c["name"].startswith("failure-") for c in captures)
    assert "SOURCE-CANARY" not in json.dumps(report)
    ordinary = "".join(p.read_text() for p in (tmp_path / "runs" / rid).glob("*.jsonl"))
    assert "SOURCE-CANARY" not in ordinary and "credential-canary" not in ordinary
    child = executor.fork(rid, "N6")
    assert executor.execute(child)["status"] == "succeeded"
    child_report = inspect_diagnostics(tmp_path, child)
    assert child_report["evidence_status"] == "available" and child_report["actual_call_attempts"] == 0
    child_spans = [json.loads(line) for line in (tmp_path / "runs" / child / "spans.jsonl").read_text().splitlines()]
    assert any(span["links"] for span in child_spans if span["name"] == "job.execute")


def test_diagnostics_detects_corrupt_artifact_and_missing_trace_without_changing_state(tmp_path):
    executor = Executor(tmp_path, [NodeSpec("N1", "1", lambda ctx, data: {"parsed": True})], {})
    rid = executor.create_run({})
    state = executor.execute(rid)
    directory = tmp_path / "runs" / rid
    state_bytes = (directory / "state.json").read_bytes()
    (tmp_path / state["nodes"][0]["output_ref"]["path"]).write_bytes(b"corrupt")
    (directory / "spans.jsonl").write_bytes(b"")
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "incomplete"
    assert {gap["code"] for gap in report["gaps"]} >= {"ARTIFACT_INTEGRITY_FAILED", "ATTEMPT_TRACE_MISSING"}
    assert (directory / "state.json").read_bytes() == state_bytes


def test_disabled_capture_never_reports_full_diagnostic_content(tmp_path):
    executor = Executor(tmp_path, [NodeSpec("N1", "1", lambda ctx, data: data)], {"debug_capture": False})
    rid = executor.create_run({})
    executor.execute(rid)
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "metadata_only_capture_disabled"
    assert report["actual_call_attempts"] == 0


def test_recorded_candidate_is_bound_to_exact_request_and_never_an_http_call(tmp_path):
    request = {"task_type": "extract", "source": "ONE"}
    gateway = JsonTaskGateway({"mode": "recorded"}, {"extract:W1": {"request_hash": digest(request), "response": {"ok": True}}})
    def run(ctx, value):
        return ctx.run_unit("W1", value, lambda: gateway(ctx, "extract", "W1", value, lambda x: x))
    executor = Executor(tmp_path, [NodeSpec("N2", "1", run)], {})
    rid = executor.create_run({"source": "ONE"})
    assert executor.execute(rid)["status"] == "succeeded"
    assert inspect_diagnostics(tmp_path, rid)["actual_call_attempts"] == 0
    changed = executor.create_run({"source": "TWO"})
    state = executor.execute(changed)
    assert state["error_code"] == "RECORDED_CANDIDATE_MISSING_OR_STALE"


def test_nested_reused_unit_event_names_the_child_not_its_running_parent(tmp_path):
    attempts = []
    def handler(ctx, value):
        def assemble():
            mapped = ctx.run_unit("form-map", {}, lambda: {"mapped": True})
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderFailure("STOP_AFTER_MAPPING")
            return mapped
        return ctx.run_unit("plan-assemble", {}, assemble)
    executor = Executor(tmp_path, [NodeSpec("N3", "1", handler)], {})
    rid = executor.create_run({})
    assert executor.execute(rid)["status"] == "failed"
    assert executor.resume(rid)["status"] == "succeeded"
    rows = [json.loads(line) for line in (tmp_path / "runs" / rid / "events.jsonl").read_text().splitlines()]
    reuse = next(row for row in rows if row["event"] == "unit.reused")
    assert reuse["unit_id"] == "form-map"
    assert reuse["unit_attempt_id"] == executor.inspect(rid)["nodes"][0]["units"]["form-map"]["attempts"][0]["attempt_id"]
