from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid import extraction_window_lineage as lineage
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


def _proof(unit_id, call, state):
    raw = {"candidate_for": unit_id}
    result = {"window_id": unit_id, "status": "complete", "raw_model_candidate": raw}
    return {"unit_id": unit_id, "call_id": f"{call:032x}",
            "source_state_sha256": hashlib.sha256(state).hexdigest(),
            "original_error": {"error_code": "INVALID_CITATION", "diagnostic_context": {"window": unit_id}},
            "raw_candidate_sha256": digest(raw), "result_sha256": digest(result)}


@pytest.fixture
def stopped_lineage(monkeypatch):
    order = tuple(f"W{i}" for i in range(38, 45))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    bindings = {lineage._TASK + unit: {"alias": "history", "run_id": f"{i:032x}", "node_key": "N2", "unit_id": unit}
                for unit, i in zip(order[:5], (1, 2, 2, 3, 3), strict=True)}
    replay_config = {"schema_version": "diagnostic-replay-v1", "sources": {"history": "history-root"}, "bindings": bindings}
    proofs = [_proof("W40", 40, b"first"), _proof("W42", 42, b"second")]
    settings = {"window_concurrency": 1, "window_max_blocks": 60}
    w40 = {"window_id": "W40", "frozen": "first-review"}
    w42 = {"window_id": "W42", "frozen": "second-review"}
    target = {**settings, lineage.CONFIG_KEY: proofs, lineage.SOURCE_CONFIG_KEY: [w40, w42]}
    provider = {"mode": "diagnostic_mixed", "max_attempts": 1, "validation_repair_attempts": 0, "max_run_calls": 2}
    frozen = {"pipeline": "automatic", "run_purpose": "diagnostic_replay", "structured_provider": provider}
    current = {"manifest": "original"}
    n1 = {"key": "N1", "output_hash": digest(current), "output_ref": {"sha256": "frozen"}}
    base = SimpleNamespace(unit_id="W38", prefix=("W38",), run=SimpleNamespace(run_id=f"{1:032x}"),
                           n1=n1, verify_history=lambda *_: None, source_binding=bindings[lineage._TASK + "W38"])
    ctx = SimpleNamespace(execution_config={**frozen, "extraction": target},
                          _state={"nodes": [n1], "parent_run_id": f"{3:032x}", "fork_from_node": "N2"})

    def source_run(number, state_raw, prior_settings, end):
        prefix = order[:end]
        inherited, actual = prefix[:-2], prefix[-2:]
        historical = {**frozen, "extraction": prior_settings, "diagnostic_replay": {
            **replay_config, "bindings": {lineage._TASK + unit: bindings[lineage._TASK + unit] for unit in inherited}}}
        units = {unit: {"attempts": [{}], "status": "failed" if unit == prefix[-1] else "succeeded"} for unit in prefix}
        events = [{"event": "unit.started", "node_key": "N2", "unit_id": unit} for unit in prefix]
        calls = [{"event": "call.started", "operation": "extract_requirements", "node_key": "N2", "unit_id": unit,
                  "call_id": f"{int(unit[1:]):032x}", "call_attempt": 1} for unit in actual]
        fees = [{**call, "settlement_status": "known_usage_cost_estimate"} for call in calls]
        state = {"status": "failed", "error_code": "INVALID_CITATION", "execution_config": historical,
                 "fork_from_node": "N2", "parent_run_id": f"{number-1:032x}", "call_budget": {"reservations": fees}}
        preflight = {"binding_count": len(inherited), "verified_binding_count": len(inherited), "origins": list(inherited)}
        node = {"units": units, "attempts": [{"captures": []}]}
        return SimpleNamespace(run_id=f"{number:032x}", alias="history", mixed_source=True, state_raw=state_raw,
                               state=state, events=events + calls, node=lambda *_: node, capture=lambda *_: preflight)

    first = source_run(2, b"first", settings, 3)
    second = source_run(3, b"second", {**settings, lineage.CONFIG_KEY: proofs[:1], lineage.SOURCE_CONFIG_KEY: w40}, 5)
    runs = {run.run_id: run for run in (first, second)}
    replay = SimpleNamespace(config=replay_config, _target_extraction=target, windows=windows,
                             provider_config=SimpleNamespace(max_attempts=1), _failed_contract_heading_only_annex=base,
                             current=current, _run=lambda _, run_id: runs[run_id], _n1=lambda _: (current, n1))
    monkeypatch.setattr(lineage, "rebuild_frozen_contract_windows", lambda *args, **kwargs: (windows, settings))
    return SimpleNamespace(gate=lineage.FailedWindowLineage(replay, ctx), replay=replay, ctx=ctx,
                           first=first, second=second, proofs=proofs, settings=settings)


def test_ordered_success_and_failure_are_bound_across_two_generations(stopped_lineage):
    gate, replay = stopped_lineage.gate, stopped_lineage.replay
    gate.verify_history(replay)
    assert gate.prefix == ("W38", "W39", "W40", "W41", "W42")
    assert [stage.actual for stage in gate.stages] == [("W39", "W40"), ("W41", "W42")]
    assert [stage.inherited for stage in gate.stages] == [("W38",), ("W38", "W39", "W40")]


@pytest.mark.parametrize("change", ["foreign_parent", "repaid_inherited", "missing_binding", "changed_prior_proof"])
def test_rejects_sources_that_break_the_frozen_ancestry(stopped_lineage, change):
    setup = stopped_lineage
    if change == "foreign_parent":
        setup.second.state["parent_run_id"] = setup.first.state["parent_run_id"]
    elif change == "repaid_inherited":
        setup.second.state["call_budget"]["reservations"][0]["unit_id"] = "W40"
    elif change == "missing_binding":
        del setup.replay.config["bindings"][lineage._TASK + "W41"]
    else:
        historical = setup.second.state["execution_config"]
        historical["extraction"][lineage.CONFIG_KEY] = [{**setup.proofs[0], "result_sha256": "0" * 64}]
    with pytest.raises(ProviderFailure, match="FAILED_WINDOW_LINEAGE_.*INVALID"):
        setup.gate.verify_history(setup.replay)


def test_original_candidate_and_new_checkpoint_require_exact_original_failure(stopped_lineage, monkeypatch):
    gate, replay = stopped_lineage.gate, stopped_lineage.replay
    gate.verify_history(replay)
    stage, unit_id = gate.stages[0], "W40"
    proof = stage.proof
    historical = proof["original_error"]
    raw = {"candidate_for": unit_id}
    prior = {"window_id": "W39"}
    stage.run.unit = lambda *_: (None, None, None, prior, None)
    call = {"candidate": raw, "request": {"wire": True}, "response": {"response": True},
            "terminal": {"trace_id": "original-trace", "span_id": "original-call"},
            "validation": {"diagnostic_context": copy.deepcopy(historical["diagnostic_context"])}}
    monkeypatch.setattr(lineage, "_read_failed_call", lambda *args: ({"version": "original"}, call))

    def original_failure(candidate, window, *, predecessor_result):
        assert candidate == raw and predecessor_result == prior
        raise extraction.PlanningError(historical["error_code"], call["validation"]["diagnostic_context"])

    def revalidate(ctx, window, window_config, callback, *, predecessor_result):
        assert ctx is None and predecessor_result == prior
        return {"window_id": unit_id, "status": "complete",
                "raw_model_candidate": callback(ctx, None, None, None, lambda candidate: candidate)}

    monkeypatch.setattr(extraction, "validate_live_window", original_failure)
    monkeypatch.setattr(extraction, "run_extraction_window", revalidate)
    monkeypatch.setattr(extraction, "_validate_normalized_window_result", lambda *args, **kwargs: None)
    entry = gate._read_failure(replay, stage, {"task_type": "extract_requirements"})
    assert entry["candidate"] == raw and entry["candidate"] is not raw
    assert entry["origin"]["calls"][0]["trace_id"] == "original-trace"
    assert entry["origin"]["lineage_notes"][0]["revalidated_checkpoint_sha256"] == proof["result_sha256"]

    original_result_sha256 = proof["result_sha256"]
    proof["result_sha256"] = "0" * 64
    with pytest.raises(ProviderFailure, match="PROOF_MISMATCH"):
        gate._read_failure(replay, stage, {})
    proof["result_sha256"] = original_result_sha256
    call["validation"]["diagnostic_context"] = {"window": "elsewhere"}
    with pytest.raises(ProviderFailure, match="PROOF_MISMATCH"):
        gate._read_failure(replay, stage, {})


def test_cold_request_requires_completed_prefix_and_settled_first_call(stopped_lineage, monkeypatch):
    gate, replay, ctx = stopped_lineage.gate, stopped_lineage.replay, stopped_lineage.ctx
    gate.verify_history(replay)
    units = {unit: {"status": "succeeded", "data": {"window_id": unit, "status": "complete",
              "raw_model_candidate": {"candidate_for": unit}}} for unit in gate.prefix}
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in gate.prefix[:-1]}
    replay._entries = {lineage._TASK + unit: {"origin": {"candidate_sha256": digest(row["data"]["raw_model_candidate"])}}
                       for unit, row in units.items()}
    replay.current_wires = {lineage._TASK + "W43": "frozen-wire"}
    ctx._record = {"units": units}
    ctx.run_id, ctx.attempt_id = "current", "node-attempt"
    ctx.executor = SimpleNamespace(_checkpoint_data=lambda unit: unit["data"])
    replay._ready = ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)
    monkeypatch.setattr(extraction, "_validate_normalized_window_result", lambda *args, **kwargs: None)

    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W44")
    units["W39"]["data"]["changed"] = True
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W43")
    del units["W39"]["data"]["changed"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W43")
    assert gate.checkpoints_verified
    units["W43"] = {"status": "succeeded", "attempts": [{"status": "succeeded", "attempt_id": "a" * 32}]}
    reservation = {"node_key": "N2", "unit_id": "W43", "call_id": "b" * 32, "unit_attempt_id": "a" * 32,
                   "call_attempt": 1, "input_hash": replay.current_wires[lineage._TASK + "W43"],
                   "settlement_status": "outcome_unknown", "exceeds_reserved_upper": False}
    ctx._state["call_budget"] = {"reservations": [reservation]}
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W43")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W44")
    reservation["settlement_status"] = "known_usage_cost_estimate"
    gate.allow_candidate(replay, ctx, "extract_requirements", "W44")


def test_requires_legacy_replay_and_diagnostic_mode(tmp_path):
    with pytest.raises(PipelineFailure, match="FAILED_WINDOW_LINEAGE_CONFIG_INVALID"):
        build_executor(tmp_path, {"extraction": {lineage.CONFIG_KEY: []}})
