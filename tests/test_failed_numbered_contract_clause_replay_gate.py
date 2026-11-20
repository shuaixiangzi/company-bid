"""Numbered contract replay admits a later request only after the sealed prefix."""

from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.contract_clause_resolution import RESOLUTION_KEY
from company_bid.extraction_numbered_clause_replay import FailedNumberedContractClauseReplay
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.provider import ProviderFailure
from company_bid.storage import digest
from test_contract_clause_window_resolution import _answer, _call, _scenario
from test_source_selected_citations import Context


@pytest.fixture
def stopped_clause(tmp_path):
    manifest, parsed, extraction_config = _scenario(tmp_path, 4)
    extraction_config = extraction.extraction_window_config(extraction_config)
    blocks, _, documents = extraction._index(parsed, manifest)
    legacy, _ = extraction._windows(documents, blocks, extraction_config, manifest=manifest)
    windows = {window["window_id"]: window for window in legacy}
    prev_id, target_id, next_id = windows
    results = extraction.extract_requirements(Context(), parsed, manifest, _call(), extraction_config)["window_results"]
    previous, recovered, _ = results
    raw = _answer(windows[target_id])
    with pytest.raises(extraction.PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED") as original_failure:
        extraction.validate_live_window(raw, windows[target_id])
    call = {"candidate": raw, "request": {"body": "captured"}, "response": {"body": "captured"},
            "terminal": {"trace_id": "failed-trace", "span_id": "failed-span"}}
    previous_call = {"run_id": "history", "trace_id": "previous-trace", "span_id": "previous-span"}
    version_record = {"version": "historical"}
    historical_unit = {"output_hash": digest(previous)}
    origin = {"calls": [previous_call], "candidate_sha256": digest(previous["raw_model_candidate"])}
    gate = FailedNumberedContractClauseReplay.__new__(FailedNumberedContractClauseReplay)
    gate.unit_id, gate.prefix = target_id, (prev_id, target_id)
    gate.call_id, gate.state_sha256 = "original-call", "original-state"
    gate.prior_checkpoint_hashes = {}
    gate.checkpoints_verified = False
    gate.predecessor = SimpleNamespace(enriched_windows=lambda: {})
    gate.n1 = {"output_hash": digest({"manifest": manifest, "parsed": parsed})}
    gate.run = SimpleNamespace(run_id="history", alias="source",
                               unit=lambda node, unit_id: (None, historical_unit, None, previous, None))
    gate.read_original_failure = lambda *args: (version_record, call, original_failure.value.diagnostic_context)
    gate.offline_proof = {"raw_candidate_sha256": digest(raw), "predecessor_result_sha256": digest(previous),
                          "result_sha256": digest(recovered),
                          "index_sha256": recovered[RESOLUTION_KEY]["items"][0]["index_sha256"]}
    bindings = {f"extract_requirements:{unit_id}": {"unit_id": unit_id} for unit_id in gate.prefix}
    current_requests = {f"extract_requirements:{unit_id}": digest({
        **extraction.extraction_window_payload(window, extraction_config), "task_type": "extract_requirements"
    }) for unit_id, window in windows.items()}
    replay = SimpleNamespace(current={"manifest": manifest, "parsed": parsed}, _target_extraction=extraction_config,
                             windows=windows, current_requests=current_requests, window_config=extraction_config,
                             config={"bindings": bindings})

    def load(binding, task, window):
        assert binding["unit_id"] == window["window_id"] == prev_id
        assert digest(task) == current_requests[f"extract_requirements:{prev_id}"]
        gate.prior_checkpoint_hashes[prev_id] = historical_unit["output_hash"]
        return {"origin": origin}

    replay._load_entry = load
    task = {**extraction.extraction_window_payload(windows[target_id], extraction_config), "task_type": "extract_requirements"}
    return SimpleNamespace(gate=gate, replay=replay, task=task, window=windows[target_id], raw=raw, previous=previous,
                           recovered=recovered, prev_id=prev_id, target_id=target_id, next_id=next_id,
                           previous_call=previous_call)


def _read(stopped):
    return stopped.gate.read_failure(stopped.replay, {"unit_id": stopped.target_id}, stopped.task, stopped.window)


def test_replay_recomputes_the_entire_result_and_links_both_actual_attempts(stopped_clause):
    stopped = stopped_clause
    entry = _read(stopped)
    assert entry["candidate"] == stopped.raw
    assert [call["trace_id"] for call in entry["origin"]["calls"]] == ["failed-trace", "previous-trace"]
    lineage, = entry["origin"]["lineage_notes"]
    assert lineage["historical_status"] == "failed"
    assert lineage["historical_candidate_modified"] is False
    assert lineage["offline_proof"] == stopped.gate.offline_proof
    assert lineage["authenticated_predecessor"]["checkpoint_sha256"] == digest(stopped.previous)


@pytest.mark.parametrize("proof_hash", ["raw_candidate_sha256", "predecessor_result_sha256", "result_sha256", "index_sha256"])
def test_changed_offline_proof_stops_before_a_candidate_is_available(stopped_clause, proof_hash):
    stopped_clause.gate.offline_proof[proof_hash] = "0" * 64
    with pytest.raises(ProviderFailure, match="PROOF_MISMATCH"):
        _read(stopped_clause)


def test_a_changed_later_request_or_predecessor_call_origin_stops_preflight(stopped_clause):
    stopped = stopped_clause
    later_key = f"extract_requirements:{stopped.next_id}"
    frozen_request = stopped.replay.current_requests[later_key]
    stopped.replay.current_requests[later_key] = "0" * 64
    with pytest.raises(ProviderFailure, match="REQUEST_MISMATCH"):
        _read(stopped)
    stopped.replay.current_requests[later_key] = frozen_request
    stopped.previous_call["run_id"] = "unrelated"
    with pytest.raises(ProviderFailure, match="SOURCE_INVALID"):
        _read(stopped)


def test_cold_window_requires_identical_replayed_result_and_actual_predecessor(stopped_clause):
    stopped = stopped_clause
    gate, replay = stopped.gate, stopped.replay
    target_entry = _read(stopped)
    replay._entries = {f"extract_requirements:{stopped.target_id}": target_entry,
                       f"extract_requirements:{stopped.prev_id}": {"origin": {
                           "candidate_sha256": digest(stopped.previous["raw_model_candidate"])}}}
    previous_row = {"status": "succeeded", "data": stopped.previous}
    target_row = {"status": "succeeded", "data": stopped.recovered}
    units = {stopped.prev_id: previous_row}
    ctx = SimpleNamespace(run_id="new", attempt_id="attempt", execution_config={}, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay._ready = (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config))
    gate.allow_candidate(replay, ctx, "extract_requirements", stopped.target_id)
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", stopped.next_id)

    units[stopped.target_id] = {**target_row, "data": {**stopped.recovered, "changed": True}}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", stopped.next_id)
    units[stopped.target_id] = target_row
    units[stopped.prev_id] = {**previous_row, "data": {**stopped.previous, "changed": True}}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", stopped.next_id)
    units[stopped.prev_id] = previous_row
    gate.allow_candidate(replay, ctx, "extract_requirements", stopped.next_id)
    assert gate.checkpoints_verified
    ctx._state["call_budget"]["reservations"].append({"node_key": "N2", "unit_id": stopped.next_id})
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", stopped.next_id)


def test_actual_windows_are_derived_from_the_ordered_stops(stopped_clause):
    stopped = stopped_clause
    gate = stopped.gate
    gate.predecessor.unit_id = stopped.prev_id
    gate.unit_id = stopped.next_id
    assert gate.actual_units(stopped.replay) == (stopped.target_id, stopped.next_id)
    gate.unit_id = stopped.target_id
    with pytest.raises(ProviderFailure, match="CONFIG_INVALID"):
        gate.actual_units(stopped.replay)


def test_numbered_replay_config_requires_its_historical_recovery_chain(tmp_path):
    with pytest.raises(PipelineFailure, match="FAILED_NUMBERED_CONTRACT_CLAUSE_CONFIG_INVALID"):
        build_executor(tmp_path, {"extraction": {FailedNumberedContractClauseReplay.replay_key: {}}})
