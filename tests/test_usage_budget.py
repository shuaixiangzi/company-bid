"""Synthetic-only durable usage estimates; no transport performs network IO."""
from __future__ import annotations

import copy
from dataclasses import asdict
from decimal import Decimal
import io
import json
from urllib.error import HTTPError

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure, digest
from company_bid.runtime import (
    Executor, NodeSpec, ProviderBudgetError, ProviderUsagePersistenceError,
    ProviderUsageSettlementError, call_budget_summary, usage_cost_estimate,
)


def response(*, usage=None, finish="stop", content='{"ok":true}'):
    result = {"id": "synthetic-request", "choices": [{"finish_reason": finish, "message": {"content": content}}]}
    if usage is not None:
        result["usage"] = usage
    return result


VALID_USAGE = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}


def setup_run(tmp_path, monkeypatch, *, transport=None, validate=None, units=("W1",), **options):
    monkeypatch.setenv("TEST_USAGE_KEY", "synthetic-credential-never-log")
    settings = dict(endpoint="https://example.invalid/chat", model="synthetic", auth_env="TEST_USAGE_KEY",
                    max_attempts=1, max_output_tokens=1000, max_run_usd=.90, max_run_calls=10,
                    input_usd_per_million=1, output_usd_per_million=1)
    settings.update(options)
    config = ChatConfig(**settings)
    sent, validated = [], []
    def send(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        return transport(payload, len(sent)) if transport else response(usage=VALID_USAGE)
    def check(value):
        validated.append(copy.deepcopy(value))
        return validate(value, len(validated)) if validate else value
    provider = ChatProposalProvider(config, transport=send, sleep=lambda _: None)
    def handler(ctx, data):
        return {"results": [ctx.run_unit(unit, {"window": unit}, lambda unit=unit: provider.generate_json(
            ctx, {"window": unit}, unit_id=unit, prompt_version="usage-v1", system_prompt="Return synthetic JSON.",
            task_type="extract", validate=check)) for unit in units]}
    nodes, execution_config = [NodeSpec("N2", "usage-v1", handler)], {"provider": asdict(config)}
    executor = Executor(tmp_path, nodes, execution_config)
    run = executor.create_run({"synthetic": True})
    return executor, run, config, sent, validated, nodes, execution_config


def records(executor, run):
    path = executor.store.run_dir(run) / "events.jsonl"
    return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def rewrite_events(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_effective_charge_admits_windows_without_erasing_original_upper_or_counts(tmp_path, monkeypatch):
    executor, run, _, sent, _, _, _ = setup_run(tmp_path, monkeypatch, units=("W1", "W2", "W3"), max_run_usd=.004)
    state = executor.execute(run)
    summary = call_budget_summary(state["call_budget"])
    assert state["status"] == "succeeded" and len(sent) == 3
    assert Decimal(summary["original_reserved_upper_usd"]) > Decimal(".004")
    assert summary["effective_budget_charge_usd"] == "0.000036"
    assert summary["known_usage_estimate_count"] == summary["reservation_count"] == 3
    assert summary["unknown_reservation_count"] == 0 and summary["cost_is_provider_invoice"] is False
    report = inspect_diagnostics(tmp_path, run)
    assert report["actual_call_attempts"] == 3 and report["budget_summary"] == summary
    assert not report["gaps"] and report["known_estimated_cost_usd"] == pytest.approx(.000036)


def test_restart_keeps_failed_usage_and_reuses_successful_window(tmp_path, monkeypatch):
    def validate(value, count):
        if count == 2:
            raise ValueError("synthetic-invalid-candidate")
        return value
    executor, run, _, sent, _, nodes, execution_config = setup_run(tmp_path, monkeypatch,
        units=("W1", "W2"), max_run_usd=.004, validate=validate)
    first = executor.execute(run)
    assert first["status"] == "failed" and len(sent) == 2
    successful = copy.deepcopy(first["nodes"][0]["units"]["W1"]["output_ref"])
    assert call_budget_summary(first["call_budget"])["known_usage_estimate_count"] == 2
    restarted = Executor(tmp_path, nodes, execution_config)
    final = restarted.resume(run)
    assert final["status"] == "succeeded" and len(sent) == 3
    assert final["nodes"][0]["units"]["W1"]["output_ref"] == successful
    assert len(final["nodes"][0]["units"]["W1"]["attempts"]) == 1
    assert len(final["nodes"][0]["units"]["W2"]["attempts"]) == 2
    assert call_budget_summary(final["call_budget"])["effective_budget_charge_usd"] == "0.000036"
    assert inspect_diagnostics(tmp_path, run)["actual_call_attempts"] == 3


@pytest.mark.parametrize("failure,code,validations", [
    ("length", "PROVIDER_TRUNCATED_OR_INCOMPLETE", 0),
    ("invalid_json", "PROVIDER_SCHEMA_INVALID", 0),
    ("validation", "PROVIDER_SCHEMA_INVALID", 1),
])
def test_known_response_is_charged_before_all_candidate_failure_paths(tmp_path, monkeypatch, failure, code, validations):
    def transport(payload, count):
        return response(usage=VALID_USAGE, finish="length" if failure == "length" else "stop",
                        content="" if failure == "length" else "bad-json" if failure == "invalid_json" else '{"ok":true}')
    def validate(value, count):
        raise ValueError("synthetic-validation-failed")
    executor, run, _, sent, checked, _, _ = setup_run(tmp_path, monkeypatch, transport=transport,
        validate=validate if failure == "validation" else None, max_attempts=3)
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == code
    assert len(sent) == 1 and len(checked) == validations
    item = state["call_budget"]["reservations"][0]
    assert item["settlement_status"] == "known_usage_cost_estimate"
    assert item["known_usage_cost_estimate_usd"] == "0.000012"
    _, rows = records(executor, run)
    names = [row["event"] for row in rows]
    assert names.index("call.usage_recorded") < names.index("call.failed")
    assert names.count("call.started") == names.count("call.usage_recorded") == names.count("call.failed") == 1


@pytest.mark.parametrize("usage", [
    None, {}, [], "unavailable",
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 1.0, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": "1", "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 1, "completion_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
])
def test_missing_or_invalid_usage_retains_full_unknown_reservation(tmp_path, monkeypatch, usage):
    executor, run, _, sent, _, _, _ = setup_run(tmp_path, monkeypatch,
        transport=lambda payload, count: response(usage=usage))
    state = executor.execute(run)
    summary = call_budget_summary(state["call_budget"])
    assert state["status"] == "succeeded" and len(sent) == 1
    assert summary["known_usage_estimate_count"] == 0 and summary["unknown_reservation_count"] == 1
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]
    report = inspect_diagnostics(tmp_path, run)
    assert report["unknown_usage_attempts"] == 1
    diff = executor.diff(run, run)["calls"]["after"]
    assert diff["known_usage_attempts"] == 0 and diff["unknown_usage_attempts"] == 1
    assert all(value is None for value in diff["known_token_counts"].values())
    assert diff["estimated_cost_usd"] is None


def test_reported_zero_usage_is_distinct_from_unknown(tmp_path, monkeypatch):
    executor, run, _, _, _, _, _ = setup_run(tmp_path, monkeypatch,
        transport=lambda payload, count: response(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
    state = executor.execute(run)
    summary = call_budget_summary(state["call_budget"])
    assert summary["known_usage_estimate_count"] == 1 and summary["effective_budget_charge_usd"] == "0"
    assert summary["unknown_reservation_count"] == 0


@pytest.mark.parametrize("kind", ["timeout", "http"])
def test_no_complete_response_keeps_unknown_full_charge(tmp_path, monkeypatch, kind):
    def transport(payload, count):
        if kind == "timeout":
            raise TimeoutError("synthetic timeout")
        raise HTTPError("https://example.invalid", 503, "synthetic", {}, io.BytesIO(b"synthetic error"))
    executor, run, _, sent, checked, _, _ = setup_run(tmp_path, monkeypatch, transport=transport)
    state = executor.execute(run)
    assert state["status"] == "failed" and len(sent) == 1 and not checked
    summary = call_budget_summary(state["call_budget"])
    assert summary["known_usage_estimate_count"] == 0
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]


def test_unknown_retry_and_known_retry_both_retain_their_attempts(tmp_path, monkeypatch):
    def transport(payload, count):
        if count == 1:
            raise TimeoutError("synthetic timeout")
        return response(usage=VALID_USAGE)
    executor, run, _, sent, _, _, _ = setup_run(tmp_path, monkeypatch, transport=transport, max_attempts=2)
    state = executor.execute(run)
    summary = call_budget_summary(state["call_budget"])
    assert state["status"] == "succeeded" and len(sent) == 2
    assert summary["reservation_count"] == 2 and summary["unknown_reservation_count"] == summary["known_usage_estimate_count"] == 1
    assert Decimal(summary["effective_budget_charge_usd"]) == Decimal(summary["unknown_reserved_upper_usd"]) + Decimal(".000012")
    assert inspect_diagnostics(tmp_path, run)["actual_call_attempts"] == 2


def test_attempt_count_is_never_released_by_settlement(tmp_path, monkeypatch):
    executor, run, _, sent, _, _, _ = setup_run(tmp_path, monkeypatch, units=("W1", "W2"), max_run_calls=1)
    state = executor.execute(run)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1
    summary = call_budget_summary(state["call_budget"])
    assert summary["reservation_count"] == summary["known_usage_estimate_count"] == 1
    assert inspect_diagnostics(tmp_path, run)["actual_call_attempts"] == 1


def test_unknown_charge_blocks_next_request_before_transport(tmp_path, monkeypatch):
    executor, run, _, sent, _, _, _ = setup_run(tmp_path, monkeypatch, units=("W1", "W2"), max_run_usd=.004,
        transport=lambda payload, count: response())
    state = executor.execute(run)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1
    assert state["call_budget"]["reservation_count"] == 1


def test_settlement_identity_idempotence_conflicts_and_legacy_rates(tmp_path):
    def handler(ctx, data):
        def unit():
            ctx.reserve_call("original", 1, Decimal(".1"), .90, 10,
                input_usd_per_million=".3", output_usd_per_million="1.2", input_hash="a" * 64)
            expected = ctx.settle_call_usage("original", 1, 7, 5, 12, response_sha256="b" * 64)
            revision = ctx._state["revision"]
            assert ctx.settle_call_usage("original", 1, 7, 5, 12, response_sha256="b" * 64) == expected
            assert ctx._state["revision"] == revision
            for usage, sha in [((8, 5, 13), "b" * 64), ((7, 5, 12), "c" * 64)]:
                with pytest.raises(ProviderUsageSettlementError):
                    ctx.settle_call_usage("original", 1, *usage, response_sha256=sha)
            for attribute, changed in [("attempt_id", "foreign-attempt"), ("node_key", "N3"),
                                       ("_active_unit", {"unit_id": "W2", "unit_attempt_id": "foreign-unit"})]:
                foreign = copy.copy(ctx)
                setattr(foreign, attribute, changed)
                with pytest.raises(ProviderUsageSettlementError):
                    foreign.settle_call_usage("original", 1, 7, 5, 12, response_sha256="b" * 64)
            ctx.reserve_call("legacy", 1, ".1", .90, 10)
            with pytest.raises(ProviderUsageSettlementError):
                ctx.settle_call_usage("legacy", 1, 7, 5, 12, response_sha256="b" * 64)
            return {"ok": True}
        return ctx.run_unit("W1", data, unit)
    executor = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "succeeded"
    summary = call_budget_summary(state["call_budget"])
    assert summary["reservation_count"] == 2 and summary["known_usage_estimate_count"] == summary["unknown_reservation_count"] == 1
    assert summary["effective_budget_charge_usd"] == "0.1000081"
    _, rows = records(executor, run)
    assert sum(row["event"] == "call.usage_recorded" for row in rows) == 1


@pytest.mark.parametrize("write_then_raise", [False, True])
def test_settlement_persist_failure_keeps_full_charge_through_capture_and_restart(tmp_path, monkeypatch, write_then_raise):
    executor, run, _, sent, checked, nodes, execution_config = setup_run(tmp_path, monkeypatch, max_attempts=3)
    write_state, failed = executor.store.write_state, []
    def failing_write(state):
        if not failed and any(item.get("settlement_status") == "known_usage_cost_estimate"
                              for item in state.get("call_budget", {}).get("reservations", [])):
            failed.append(True)
            if write_then_raise:
                write_state(state)
            raise OSError("synthetic state persistence failure")
        return write_state(state)
    monkeypatch.setattr(executor.store, "write_state", failing_write)
    first = executor.execute(run)
    assert first["error_code"] == "PROVIDER_USAGE_PERSIST_FAILED" and len(sent) == 1 and not checked
    disk = executor.store.read_state(run)
    summary = call_budget_summary(disk["call_budget"])
    assert summary["budget_stopped"] and summary["stop_reason"] == "usage_persistence_failed"
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]
    item = disk["call_budget"]["reservations"][0]
    assert item["settlement_status"] == "reserved_unknown_actual_cost"
    assert item["uncommitted_usage_observation"]["usage"] == {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}
    final = Executor(tmp_path, nodes, execution_config).resume(run)
    assert final["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1
    assert inspect_diagnostics(tmp_path, run)["actual_call_attempts"] == 1


def test_failed_settlement_blocks_same_process_even_after_another_capture(tmp_path, monkeypatch):
    observed = []
    def handler(ctx, data):
        ctx.reserve_call("original", 1, ".1", .90, 10, input_usd_per_million=1, output_usd_per_million=1)
        write_state = ctx.executor.store.write_state
        def fail(state):
            raise OSError("synthetic failure")
        monkeypatch.setattr(ctx.executor.store, "write_state", fail)
        with pytest.raises(ProviderUsagePersistenceError):
            ctx.settle_call_usage("original", 1, 7, 5, 12, response_sha256="b" * 64)
        monkeypatch.setattr(ctx.executor.store, "write_state", write_state)
        ctx.capture("later-safe-capture", {"synthetic": True})
        with pytest.raises(ProviderBudgetError):
            ctx.reserve_call("later", 1, ".1", .90, 10, input_usd_per_million=1, output_usd_per_million=1)
        observed.append(call_budget_summary(ctx._state["call_budget"]))
        return {"ok": True}
    executor = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    run = executor.create_run({})
    executor.execute(run)
    assert observed[0]["effective_budget_charge_usd"] == "0.1" and observed[0]["budget_stopped"]
    assert call_budget_summary(executor.store.read_state(run)["call_budget"])["effective_budget_charge_usd"] == "0.1"


def test_usage_exceeding_upper_is_saved_then_stops_future_calls(tmp_path, monkeypatch):
    usage = {"prompt_tokens": 100_000, "completion_tokens": 5, "total_tokens": 100_005}
    executor, run, _, sent, checked, nodes, execution_config = setup_run(tmp_path, monkeypatch,
        max_attempts=3, transport=lambda payload, count: response(usage=usage))
    state = executor.execute(run)
    assert state["error_code"] == "PROVIDER_USAGE_EXCEEDS_RESERVATION" and len(sent) == 1 and not checked
    summary = call_budget_summary(state["call_budget"])
    assert Decimal(summary["effective_budget_charge_usd"]) < Decimal(".90")
    assert summary["effective_budget_charge_usd"] == "0.100005"
    assert summary["stop_reason"] == "usage_exceeds_reservation" and summary["budget_stopped"]
    assert state["call_budget"]["reservations"][0]["exceeds_reserved_upper"] is True
    final = Executor(tmp_path, nodes, execution_config).resume(run)
    assert final["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(sent) == 1


def test_decimal_upper_exact_usage_boundary_and_16000_output_cap(tmp_path, monkeypatch):
    def transport(payload, count):
        bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048
        return response(usage={"prompt_tokens": bound, "completion_tokens": payload["max_tokens"],
                               "total_tokens": bound + payload["max_tokens"]})
    executor, run, config, sent, _, _, _ = setup_run(tmp_path, monkeypatch, transport=transport,
        max_output_tokens=16000, input_usd_per_million=.3, output_usd_per_million=1.2)
    state = executor.execute(run)
    item = state["call_budget"]["reservations"][0]
    assert state["status"] == "succeeded" and sent[0]["max_tokens"] == config.max_output_tokens == 16000
    assert Decimal(item["estimated_upper_usd"]) == Decimal(item["known_usage_cost_estimate_usd"])
    assert item["exceeds_reserved_upper"] is False
    assert ChatConfig(endpoint="https://example.invalid", model="synthetic").max_output_tokens == 2500


@pytest.mark.parametrize("capacity", [0, 16001, True, 16000.0, "16000"])
def test_invalid_output_capacity_is_rejected_before_transport(capacity):
    with pytest.raises(ProviderFailure, match="PROVIDER_BUDGET_INVALID"):
        ChatConfig(endpoint="https://example.invalid", model="synthetic", max_output_tokens=capacity)


def test_legacy_reservations_retain_full_charge_and_invalid_ledgers_are_reported(tmp_path, monkeypatch):
    executor, run, _, _, _, _, _ = setup_run(tmp_path, monkeypatch)
    state = executor.execute(run)
    item = state["call_budget"]["reservations"][0]
    for key in ("settlement_status", "input_usd_per_million", "output_usd_per_million", "usage", "response_sha256",
                "known_usage_cost_estimate_usd", "usage_recorded_at", "exceeds_reserved_upper", "cost_is_provider_invoice"):
        item.pop(key, None)
    executor.store.write_state(state)
    report = inspect_diagnostics(tmp_path, run)
    summary = report["budget_summary"]
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]
    assert summary["known_usage_estimate_count"] == 0 and summary["unknown_reservation_count"] == 1
    before = executor.store.read_state(run)
    inspect_diagnostics(tmp_path, run)
    assert executor.store.read_state(run) == before
    del item["node_attempt_id"]
    executor.store.write_state(state)
    invalid = inspect_diagnostics(tmp_path, run)
    assert invalid["budget_summary"]["ledger_status"] == "invalid"
    assert "CALL_BUDGET_LEDGER_INVALID" in {gap["code"] for gap in invalid["gaps"]}


@pytest.mark.parametrize("corruption,gap", [
    ("duplicate", "CALL_USAGE_EVENT_DUPLICATE"), ("wrong_span", "CALL_USAGE_TRACE_MISMATCH"),
    ("conflict", "CALL_USAGE_LEDGER_MISMATCH"), ("missing", "CALL_USAGE_EVENT_MISSING"),
    ("orphan_call", "CALL_USAGE_WITHOUT_ATTEMPT"), ("orphan_node", "CALL_USAGE_WITHOUT_ATTEMPT"),
])
def test_usage_events_cannot_change_attempt_identity_count_or_durable_cost(tmp_path, monkeypatch, corruption, gap):
    executor, run, _, _, _, _, _ = setup_run(tmp_path, monkeypatch)
    executor.execute(run)
    original = inspect_diagnostics(tmp_path, run)
    path, rows = records(executor, run)
    usage = next(row for row in rows if row["event"] == "call.usage_recorded")
    if corruption == "duplicate":
        rows.append(copy.deepcopy(usage))
    elif corruption == "wrong_span":
        usage["span_id"] = "f" * 16
    elif corruption == "conflict":
        usage.update(input_tokens=999, output_tokens=999, total_tokens=1998, cost=999)
    elif corruption == "missing":
        rows.remove(usage)
    else:
        orphan = copy.deepcopy(usage)
        orphan["call_id" if corruption == "orphan_call" else "node_attempt_id"] = "orphan"
        rows.append(orphan)
    rewrite_events(path, rows)
    report = inspect_diagnostics(tmp_path, run)
    assert gap in {item["code"] for item in report["gaps"]}
    assert report["actual_call_attempts"] == 1 and report["budget_summary"] == original["budget_summary"]
    assert report["known_estimated_cost_usd"] == original["known_estimated_cost_usd"]
    assert report["calls"][0]["span_id"] == original["calls"][0]["span_id"]
    assert report["calls"][0]["input_tokens"] == 7 and report["calls"][0]["provider_request_id"] == "synthetic-request"
