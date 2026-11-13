"""Real Executor/provider accounting with synthetic HTTP and strict validators."""
from __future__ import annotations

import copy
import io
import json
from decimal import Decimal
from urllib.error import HTTPError, URLError

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import PlanningError
from company_bid.provider import ProviderFailure
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.structured import JsonTaskGateway, PROMPT_VERSION, SYSTEM_PROMPT


SOURCE = "SYNTHETIC-SOURCE-CANARY"
RAW = {"ok": False, "raw": "SYNTHETIC-RAW-CANARY"}
CONTEXT = {"candidate_type": "requirement", "candidate_index": 2, "field": "citation.block_ids", "label_index": 1,
           "locator": SOURCE, "block_id": SOURCE, "echo": SOURCE, "exception": "PRIVATE-EXCEPTION-CANARY"}


def response(value, *, finish="stop"):
    return {"id": "synthetic-http", "choices": [{"finish_reason": finish, "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}


def setup_run(tmp_path, monkeypatch, *, repair=1, mode="chat_completions", validate=None, transport=None,
              capture=True, **settings):
    monkeypatch.setenv("TEST_REPAIR_KEY", "synthetic-repair-credential")
    options = {"mode": mode, "endpoint": "https://example.invalid/chat", "model": "synthetic",
               "auth_env": "TEST_REPAIR_KEY", "max_attempts": 1, "max_output_tokens": 64,
               "max_input_chars": 10000, "max_run_calls": 5, "max_run_usd": .10,
               "input_usd_per_million": 1, "output_usd_per_million": 1}
    if repair is not None:
        options["validation_repair_attempts"] = repair
    options.update(settings)
    sent, validated, replies = [], [], []
    def send(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        item = transport(payload, len(sent)) if transport else response(RAW if len(sent) == 1 else {"ok": True})
        replies.append(copy.deepcopy(item))
        return item
    def check(value):
        validated.append(copy.deepcopy(value))
        if validate:
            return validate(value, len(validated))
        if not value.get("ok"):
            value["raw"] = "VALIDATOR-MUTATION-CANARY"
            raise PlanningError("EXTRACTION_LABEL_NOT_GROUNDED", CONTEXT)
        return value
    gateway = JsonTaskGateway(options, transport=send)
    original = {"window": {"window_id": "W", "blocks": [{"source_id": "S", "block_id": "B", "locator": "test/block[B]", "text": SOURCE}]},
                "task_instructions": "Return all original requirements.", "output_contract": {"type": "object"}}
    payload = copy.deepcopy(original)
    def handler(ctx, value):
        return ctx.run_unit("W", value, lambda: gateway(ctx, "extract_requirements", "W", payload, check))
    executor = Executor(tmp_path, [NodeSpec("N2", "synthetic-repair-v1", handler)],
                        {"structured_provider": options, "debug_capture": capture})
    rid = executor.create_run({"synthetic": True})
    return executor, rid, options, payload, original, sent, validated, replies


def artifacts(executor, state):
    records = state["nodes"][0]["attempts"][-1].get("captures", [])
    return [(record, json.loads(executor.store.read_bytes(record["ref"]))) for record in records if record.get("ref")]


def events(executor, rid):
    return [json.loads(line) for line in (executor.store.run_dir(rid) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("code", ["EXTRACTION_SCHEMA_INVALID", "EXTRACTION_LABEL_NOT_GROUNDED"])
def test_one_repair_keeps_raw_request_and_records_two_budgeted_calls_and_real_spans(tmp_path, monkeypatch, code):
    def validate(value, count):
        if count == 1:
            value["raw"] = "VALIDATOR-MUTATION-CANARY"
            raise PlanningError(code, CONTEXT)
        return value
    executor, rid, _, payload, original, sent, validated, replies = setup_run(tmp_path, monkeypatch, validate=validate)
    state = executor.execute(rid)
    assert state["status"] == "succeeded" and len(sent) == 2 and len(validated) == 2
    assert payload == original and json.loads(replies[0]["choices"][0]["message"]["content"]) == RAW
    first, second = (json.loads(item["messages"][1]["content"]) for item in sent)
    assert first == {**original, "task_type": "extract_requirements"}
    assert {key: value for key, value in second.items() if key not in {"validation_feedback", "task_instructions"}} == {
        key: value for key, value in first.items() if key != "task_instructions"}
    assert second["task_instructions"].startswith(first["task_instructions"] + "\n\n")
    assert "required_coverage.counts/anchors" in second["task_instructions"]
    feedback = second["validation_feedback"]
    assert feedback["code"] == code and feedback["repair_attempt"] == 1
    assert feedback["context"] == {key: CONTEXT[key] for key in ("candidate_type", "candidate_index", "field", "label_index")}
    assert "SYNTHETIC-RAW-CANARY" not in json.dumps(second) and "PRIVATE-EXCEPTION-CANARY" not in json.dumps(second)
    assert "不得删除要求" in feedback["instructions"]
    summary = call_budget_summary(state["call_budget"])
    assert summary["reservation_count"] == summary["known_usage_estimate_count"] == 2
    assert summary["effective_budget_charge_usd"] == "0.000024" and summary["unknown_reservation_count"] == 0
    assert len(state["nodes"][0]["units"]["W"]["attempts"]) == 1
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and not report["gaps"]
    assert [call["outcome"] for call in report["calls"]] == ["failed", "completed"]
    assert len({call["call_id"] for call in report["calls"]}) == 2
    records = events(executor, rid)
    retry = next(row for row in records if row["event"] == "call.retry_scheduled")
    first_call = report["calls"][0]
    assert (retry["call_id"], retry["call_attempt"], retry["span_id"]) == (first_call["call_id"], 1, first_call["span_id"])
    assert retry["retry_reason"] == code
    spans = {row["span_id"]: row for row in map(json.loads, (executor.store.run_dir(rid) / "spans.jsonl").read_text().splitlines())}
    assert all(spans[call["span_id"]]["name"] == "provider.call" for call in report["calls"])
    captured = artifacts(executor, state)
    first_raw = next(value for record, value in captured if record["name"].startswith("response-"))
    assert json.loads(first_raw["choices"][0]["message"]["content"]) == RAW
    relation = next(value for record, value in captured if record["name"].startswith("validation-repair-result-"))
    assert relation["status"] == "completed" and relation["first_request_hash"] != relation["repair_request_hash"]
    assert relation["first_call_id"] == feedback["first_call_id"] and relation["second_call_id"] == report["calls"][1]["call_id"]
    ordinary = "".join(path.read_text(encoding="utf-8") for path in executor.store.run_dir(rid).glob("*.jsonl"))
    assert SOURCE not in ordinary and "SYNTHETIC-RAW-CANARY" not in ordinary
    assert executor.resume(rid)["status"] == "succeeded" and len(sent) == 2


@pytest.mark.parametrize("repair", [None, 0])
def test_default_and_explicit_zero_preserve_original_request_prompt_and_single_attempt(tmp_path, monkeypatch, repair):
    executor, rid, options, _, original, sent, _, _ = setup_run(tmp_path, monkeypatch, repair=repair)
    state = executor.execute(rid)
    assert state["error_code"] == "EXTRACTION_LABEL_NOT_GROUNDED" and len(sent) == 1
    expected = {"model": options["model"], "messages": [{"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({**original, "task_type": "extract_requirements"}, ensure_ascii=False)}],
        "temperature": .2, "max_tokens": options["max_output_tokens"], "response_format": {"type": "json_object"}}
    assert json.dumps(sent[0], ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))
    assert PROMPT_VERSION == "chain-json-candidates-v1"


@pytest.mark.parametrize("options", [
    {"validation_repair_attempts": True}, {"validation_repair_attempts": "1"}, {"validation_repair_attempts": -1},
    {"validation_repair_attempts": 2}, {"validation_repair_attempts": 1, "mode": "recorded"},
    {"validation_repair_attempts": 1, "mode": "chat_completions", "max_attempts": 2},
    {"validation_repair_attempts": 1, "mode": "chat_completions"},
])
def test_invalid_config_is_rejected_before_context_auth_or_transport(options):
    gateway = JsonTaskGateway(options, transport=lambda *args: pytest.fail("Invalid config cannot call HTTP"))
    with pytest.raises(ProviderFailure, match="STRUCTURED_VALIDATION_REPAIR_CONFIG_INVALID"):
        gateway(None, "extract_requirements", "W", {}, lambda value: value)


@pytest.mark.parametrize("failure", ["critical_unresolved", "source_integrity", "provider_impostor", "untyped_impostor", "schema_with_critical_unresolved"])
def test_nonrepairable_validation_failures_never_request_another_candidate(tmp_path, monkeypatch, failure):
    def validate(value, count):
        if failure == "critical_unresolved":
            raise PlanningError("LIVE_REQUIRED_BLOCK_UNRESOLVED")
        if failure == "source_integrity":
            raise PlanningError("CITATION_SPAN_NOT_CONTIGUOUS")
        if failure == "provider_impostor":
            raise ProviderFailure("EXTRACTION_SCHEMA_INVALID")
        if failure == "schema_with_critical_unresolved":
            raise PlanningError("EXTRACTION_SCHEMA_INVALID", CONTEXT)
        class Other(ValueError):
            code = "EXTRACTION_SCHEMA_INVALID"
        raise Other()
    transport = (lambda *_: response({**RAW, "unresolved_block_ids": ["B"]})) if failure == "schema_with_critical_unresolved" else None
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, validate=validate, transport=transport)
    assert executor.execute(rid)["status"] == "failed" and len(sent) == 1
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


@pytest.mark.parametrize("failure", ["network", "http", "auth", "truncated", "malformed_response"])
def test_transport_auth_and_incomplete_response_failures_are_not_repaired(tmp_path, monkeypatch, failure):
    def transport(payload, count):
        if failure == "network":
            raise URLError("synthetic unavailable")
        if failure in {"http", "auth"}:
            raise HTTPError("https://example.invalid/chat", 503 if failure == "http" else 401, "synthetic", {}, io.BytesIO(b"{}"))
        if failure == "truncated":
            return response(RAW, finish="length")
        return b"invalid-json"
    executor, rid, _, _, _, sent, validated, _ = setup_run(tmp_path, monkeypatch, transport=transport)
    assert executor.execute(rid)["status"] == "failed" and len(sent) == 1 and not validated
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_second_invalid_candidate_stops_and_preserves_both_failed_charges(tmp_path, monkeypatch):
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, transport=lambda *_: response(RAW))
    state = executor.execute(rid)
    assert state["error_code"] == "EXTRACTION_LABEL_NOT_GROUNDED" and len(sent) == 2
    assert call_budget_summary(state["call_budget"])["known_usage_estimate_count"] == 2
    assert state["nodes"][0]["units"]["W"]["status"] == "failed"
    assert "output_ref" not in state["nodes"][0]["units"]["W"]
    assert [call["outcome"] for call in inspect_diagnostics(tmp_path, rid)["calls"]] == ["failed", "failed"]
    relation = next(value for record, value in artifacts(executor, state) if record["name"].startswith("validation-repair-result-"))
    assert relation["status"] == "failed" and relation["second_call_started"] is True


def test_existing_call_cap_blocks_second_http_without_erasing_first_settlement(tmp_path, monkeypatch):
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, max_run_calls=1)
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1
    assert inspect_diagnostics(tmp_path, rid)["actual_call_attempts"] == 1
    relation = next(value for record, value in artifacts(executor, state) if record["name"].startswith("validation-repair-result-"))
    assert relation["second_call_started"] is False and relation["second_call_id"] is None


def test_money_cap_is_unchanged_and_can_refuse_the_larger_repair_request(tmp_path, monkeypatch):
    executor, rid, options, _, original, sent, _, _ = setup_run(tmp_path, monkeypatch)
    expected = {"model": options["model"], "messages": [{"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({**original, "task_type": "extract_requirements"}, ensure_ascii=False)}],
        "temperature": .2, "max_tokens": options["max_output_tokens"], "response_format": {"type": "json_object"}}
    first_peak = Decimal(len(json.dumps(expected, ensure_ascii=False).encode()) + 2048 + options["max_output_tokens"]) / Decimal(1000000)
    options["max_run_usd"] = float(first_peak + Decimal("0.00005"))
    # Freeze a fresh synthetic run with this limit, before any mock HTTP call.
    executor = Executor(tmp_path, executor.nodes, {"structured_provider": options})
    rid = executor.create_run({"synthetic": True})
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1
    summary = call_budget_summary(state["call_budget"])
    assert summary["effective_budget_charge_usd"] == "0.000012" and summary["reservation_count"] == 1
    assert Decimal(state["call_budget"]["max_run_usd"]) == Decimal(str(options["max_run_usd"]))


@pytest.mark.parametrize("failure", ["timeout", "truncated"])
def test_second_incomplete_call_stops_without_a_third_request(tmp_path, monkeypatch, failure):
    def transport(payload, count):
        if count == 1:
            return response(RAW)
        if failure == "timeout":
            raise TimeoutError("synthetic repair timeout")
        return response({"ok": True}, finish="length")
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, transport=transport)
    state = executor.execute(rid)
    assert state["status"] == "failed" and len(sent) == 2
    assert inspect_diagnostics(tmp_path, rid)["actual_call_attempts"] == 2
    assert sum(row["event"] == "call.retry_scheduled" for row in events(executor, rid)) == 1


@pytest.mark.parametrize("point", ["request", "response", "validation", "failure", "scheduled", "result"])
def test_capture_write_failure_blocks_repair_or_success_checkpoint(tmp_path, monkeypatch, point):
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch)
    original = executor.store.put_json
    failed = []
    def put_json(run_id, name, value):
        target = isinstance(value, dict) and name == "capture" and (
            point == "request" and "messages" in value or
            point == "response" and "choices" in value or
            point == "validation" and value.get("status") == "failed" and "diagnostic_context" in value and "schema_version" not in value or
            point == "failure" and value.get("error_code") == "EXTRACTION_LABEL_NOT_GROUNDED" and "diagnostic_context" not in value or
            point in {"scheduled", "result"} and value.get("schema_version") == "validation-repair-v6" and
            value.get("status") == ("scheduled" if point == "scheduled" else "completed"))
        if target and not failed:
            failed.append(True)
            raise OSError("Synthetic diagnostic write failed")
        return original(run_id, name, value)
    monkeypatch.setattr(executor.store, "put_json", put_json)
    state = executor.execute(rid)
    assert failed and state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED"
    assert len(sent) == (0 if point == "request" else 2 if point == "result" else 1)
    assert state["nodes"][0]["units"]["W"]["status"] == "failed"
    assert "output_ref" not in state["nodes"][0]["units"]["W"]


def test_capture_disabled_explicitly_records_no_repair_and_keeps_only_real_call_trace(tmp_path, monkeypatch):
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, capture=False)
    state = executor.execute(rid)
    assert state["error_code"] == "EXTRACTION_LABEL_NOT_GROUNDED" and len(sent) == 1
    captures = state["nodes"][0]["attempts"][-1]["captures"]
    skipped = next(row for row in captures if row["name"].startswith("validation-repair-skipped-"))
    assert skipped["status"] == "disabled_by_policy" and "ref" not in skipped
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "metadata_only_capture_disabled" and report["actual_call_attempts"] == 1
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_failed_terminal_event_write_cannot_schedule_a_repair(tmp_path, monkeypatch):
    from company_bid.observability import Observer, ObservabilityWriteError
    original = Observer.event
    def event(observer, name, metadata=None):
        if name == "call.failed":
            raise ObservabilityWriteError("Synthetic failed event persistence")
        return original(observer, name, metadata)
    monkeypatch.setattr(Observer, "event", event)
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch)
    state = executor.execute(rid)
    assert state["error_code"] == "OBSERVABILITY_WRITE_FAILED" and len(sent) == 1
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_first_call_span_export_failure_prevents_repair_even_after_terminal_event(tmp_path, monkeypatch):
    import company_bid.observability as observability
    original = observability.append_json
    failed = []
    def append(path, value):
        if path.name == "spans.jsonl" and value.get("name") == "provider.call" and not failed:
            failed.append(True)
            raise observability.ObservabilityWriteError("Synthetic span persistence failure")
        return original(path, value)
    monkeypatch.setattr(observability, "append_json", append)
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch)
    state = executor.execute(rid)
    assert failed and state["error_code"] == "OBSERVABILITY_WRITE_FAILED" and len(sent) == 1
    assert any(row["event"] == "call.failed" for row in events(executor, rid))
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_feedback_drops_unbounded_or_unknown_paths_instead_of_copying_diagnostics(tmp_path, monkeypatch):
    def validate(value, count):
        if count == 1:
            raise PlanningError("EXTRACTION_SCHEMA_INVALID", {"candidate_type": {"source": SOURCE},
                "candidate_index": True, "label_index": 999999999999, "field": "citation." + SOURCE,
                "raw": RAW, "loc": [SOURCE], "message": SOURCE})
        return value
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, validate=validate)
    assert executor.execute(rid)["status"] == "succeeded"
    assert json.loads(sent[1]["messages"][1]["content"])["validation_feedback"]["context"] == {}


@pytest.mark.parametrize("damage", ["drop_count", "drop_anchor", "drop_format", "drop_clause_id", "drop_table_row_id", "drop_condition_block"])
def test_repair_cannot_pass_by_deleting_candidates_anchors_or_previously_selected_conditions(tmp_path, monkeypatch, damage):
    citation = {"source_id": "S", "block_id": "B", "locator": "test/block[B]", "block_ids": ["B", "TAIL"]}
    key = "clause_id" if damage == "drop_clause_id" else "table_row_id" if damage == "drop_table_row_id" else None
    if key:
        citation[key] = "CLAUSE-1" if key == "clause_id" else "TROW-1"
    first = {"requirements": [{"citation": citation, "private_raw": "OLD-CANDIDATE-ONLY-CANARY"}],
             "format_chapters": [{"citation": {"source_id": "S", "block_id": "B", "locator": "test/block[B]"}}]}
    second = copy.deepcopy(first)
    if damage == "drop_count":
        second["requirements"] = []
    elif damage == "drop_anchor":
        second["requirements"][0]["citation"]["block_id"] = "OTHER"
    elif damage == "drop_format":
        second["format_chapters"] = []
    elif damage == "drop_condition_block":
        second["requirements"][0]["citation"]["block_ids"] = ["B"]
    else:
        second["requirements"][0]["citation"].pop(key)
    def validate(value, count):
        if count == 1:
            raise PlanningError("EXTRACTION_SCHEMA_INVALID", CONTEXT)
        return value
    executor, rid, _, payload, _, sent, validated, _ = setup_run(tmp_path, monkeypatch,
        validate=validate, transport=lambda payload, count: response(first if count == 1 else second))
    payload["window"]["adjacent_context"] = [{"source_id": "S", "block_id": "TAIL", "text": "原文否定与完整条件"}]
    if key:
        payload["window"]["source_clause_catalog" if key == "clause_id" else "source_table_row_catalog"] = [citation]
    state = executor.execute(rid)
    assert state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST" and len(sent) == 2 and len(validated) == 1
    feedback = json.loads(sent[1]["messages"][1]["content"])["validation_feedback"]
    assert feedback["required_coverage"]["counts"] == {"requirements": 1, "format_chapters": 1}
    assert feedback["required_coverage"]["anchors"]["requirements"][0]["block_ids"] == ["B", "TAIL"]
    assert "OLD-CANDIDATE-ONLY-CANARY" not in json.dumps(feedback)


@pytest.mark.parametrize("damage", ["too_many", "unknown_source", "bad_item", "too_many_refs", "unbounded_ref"])
def test_unbounded_or_ambiguous_first_inventory_disables_automatic_repair(tmp_path, monkeypatch, damage):
    first = {"requirements": [{"citation": {"source_id": "S", "block_id": "B", "locator": "test/block[B]"}}], "format_chapters": []}
    if damage == "too_many":
        first["requirements"] *= 65
    elif damage == "unknown_source":
        first["requirements"][0]["citation"]["source_id"] = "UNKNOWN"
    elif damage == "bad_item":
        first["requirements"] = ["not-a-candidate"]
    elif damage == "too_many_refs":
        first["requirements"] *= 64
        for item in first["requirements"]:
            item["citation"]["block_ids"] = ["B", "T1", "T2", "T3", "T4"]
    else:
        first["requirements"][0]["citation"]["block_ids"] = ["B", "X" * 121]
    executor, rid, _, payload, _, sent, _, _ = setup_run(tmp_path, monkeypatch,
        validate=lambda *_: (_ for _ in ()).throw(PlanningError("EXTRACTION_SCHEMA_INVALID")),
        transport=lambda *_: response(first))
    payload["window"]["adjacent_context"] = [{"source_id": "S", "block_id": f"T{i}"} for i in range(1, 5)]
    if damage == "unbounded_ref":
        payload["window"]["adjacent_context"].append({"source_id": "S", "block_id": "X" * 121})
    state = executor.execute(rid)
    assert state["error_code"] == "EXTRACTION_SCHEMA_INVALID" and len(sent) == 1
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


def test_feedback_size_still_uses_original_provider_input_limit(tmp_path, monkeypatch):
    executor, _, options, _, original, sent, _, _ = setup_run(tmp_path, monkeypatch)
    options["max_input_chars"] = len(json.dumps({**original, "task_type": "extract_requirements"}, ensure_ascii=False)) + 8
    executor = Executor(tmp_path, executor.nodes, {"structured_provider": options})
    rid = executor.create_run({"synthetic": True})
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_INPUT_BUDGET_EXCEEDED" and len(sent) == 1
    assert call_budget_summary(state["call_budget"])["reservation_count"] == 1


def test_first_budget_refusal_never_calls_http_or_schedules_repair(tmp_path, monkeypatch):
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch, max_run_usd=.000001)
    assert executor.execute(rid)["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and not sent
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))


@pytest.mark.parametrize("point", ["validation", "failure"])
def test_capture_exception_is_not_mistaken_for_a_repairable_validation(tmp_path, monkeypatch, point):
    from company_bid.runtime import NodeContext
    original = NodeContext.capture
    failed = []
    def capture(ctx, name, value, **kwargs):
        if name.startswith(point + "-") and not failed:
            failed.append(True)
            raise OSError("Synthetic capture state persistence failure")
        return original(ctx, name, value, **kwargs)
    monkeypatch.setattr(NodeContext, "capture", capture)
    executor, rid, _, _, _, sent, _, _ = setup_run(tmp_path, monkeypatch)
    state = executor.execute(rid)
    assert failed and state["status"] == "failed" and len(sent) == 1
    assert not any(row["event"] == "call.retry_scheduled" for row in events(executor, rid))
