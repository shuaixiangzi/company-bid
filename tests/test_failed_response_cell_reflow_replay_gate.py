from types import SimpleNamespace

import pytest

import company_bid.extraction as extraction
from company_bid.extraction_failed_scoring_replay import FailedResponseCellReflowReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def cell_reflow_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    prefix = order[:12]
    enriched = {unit: {**windows[unit], "proof": True} for unit in ("W05", "W06", "W09", "W10", "W11", "W12")}
    units = {unit: {"status": "succeeded", "data": {"window_id": unit, "status": "complete",
                                                     "raw_model_candidate": {"window_id": unit}}} for unit in prefix}
    entries = {f"extract_requirements:{unit}": {"origin": {"candidate_sha256": digest(row["data"]["raw_model_candidate"])}}
               for unit, row in units.items()}
    wires = {f"extract_requirements:{unit}": digest(window) for unit, window in windows.items()}
    ctx = SimpleNamespace(run_id="run", attempt_id="attempt", execution_config={}, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay = SimpleNamespace(windows=windows, current_wires=wires, _entries=entries,
                             _ready=(ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)))
    previous_windows = {unit: window for unit, window in enriched.items() if unit != "W12"}
    gate = FailedResponseCellReflowReplay.__new__(FailedResponseCellReflowReplay)
    gate.predecessor = SimpleNamespace(unit_id="W11", enriched_windows=lambda: previous_windows)
    gate.unit_id, gate.window, gate.prefix = "W12", enriched["W12"], prefix
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in prefix[:-1]}
    gate.checkpoints_verified = False
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, ctx, checked


def test_cell_reflow_requires_twelve_checkpoints_and_one_call_per_cold_window(cell_reflow_gate):
    gate, replay, ctx, checked = cell_reflow_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    assert gate.actual_units(replay) == ("W12",)
    repaired = units.pop("W12")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    units["W12"] = repaired
    gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    assert tuple(checked) == gate.prefix
    assert gate.enriched_windows().items() <= checked.items()
    reservations.append({"node_key": "N2", "unit_id": "W13", "call_attempt": 1, "call_id": "a" * 32,
                         "unit_attempt_id": "b" * 32, "input_hash": replay.current_wires["extract_requirements:W13"]})
    units["W13"] = {"status": "failed", "attempts": [{"status": "failed", "attempt_id": "b" * 32}]}
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W14")
    units["W13"]["status"] = units["W13"]["attempts"][0]["status"] = "succeeded"
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W15")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W14")
    reservations[0]["input_hash"] = "changed"
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W14")


def test_cell_reflow_gate_rejects_changed_historical_checkpoint_or_reservation(cell_reflow_gate):
    gate, replay, ctx, _ = cell_reflow_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    units["W11"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_CELL_REFLOW_CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    del units["W11"]["data"]["validated"]
    reservations.append({"node_key": "N2", "unit_id": "W12", "call_attempt": 1})
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_CELL_REFLOW_CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")


def test_cell_reflow_proof_requires_exact_original_attachment_error(cell_reflow_gate):
    gate, _, _, _ = cell_reflow_gate
    context = {"candidate_type": "requirement", "field": "attachment_keywords", "candidate_index": 5,
               "label_index": 0, "block_id": "table", "block_ids": ["table"],
               "locator": "pdf/page[30]/table[0]", "source_id": "source"}
    proof = {"original_error": {"error_code": gate.historical_failure, "diagnostic_context": context}}
    gate.check_proof_context(proof, context)
    for change in ({"candidate_index": 6}, {"label_index": 1}, {"label_index": False}, {"block_id": "other-table"},
                   {"block_ids": ["table", "other-table"]}, {"locator": "pdf/page[29]/table[0]"},
                   {"source_id": "other-source"}):
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_CELL_REFLOW_PROOF_MISMATCH"):
            gate.check_proof_context(proof, {**context, **change})
    for error in ({"error_code": "FORMAT_HEADING_NOT_GROUNDED"},
                  {"diagnostic_context": {**context, "field": "business_label"}},
                  {"diagnostic_context": {**context, "candidate_type": "format_chapter"}}):
        conflicting = {**proof["original_error"], **error}
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_CELL_REFLOW_PROOF_MISMATCH"):
            gate.check_proof_context({"original_error": conflicting}, conflicting["diagnostic_context"])


@pytest.mark.parametrize("fault", ["missing_w12", "prebound_w13", "wrong_position"])
def test_cell_reflow_gate_rejects_extra_or_misplaced_bindings(cell_reflow_gate, fault):
    gate, replay, _, _ = cell_reflow_gate
    gate.run = None
    gate.predecessor.prefix = gate.prefix[:-1]
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W12"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Unexpected read for a malformed prefix")
    if fault == "missing_w12":
        del bindings["extract_requirements:W12"]
    elif fault == "prebound_w13":
        bindings["extract_requirements:W13"] = {**gate.source_binding, "unit_id": "W13"}
    else:
        gate.unit_id = "W13"
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_CELL_REFLOW_CONFIG_INVALID"):
        gate.verify_history(replay)
