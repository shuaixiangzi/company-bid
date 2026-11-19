"""Known review-table evidence must be replayed before W11 can be bought."""
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedResponseReviewTableReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def gate_state(monkeypatch):
    order = tuple(f"W{i:02d}" for i in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    prefix = order[:10]
    enriched = {unit: {**windows[unit], "proof": True} for unit in ("W05", "W06", "W09", "W10")}
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
    gate = FailedResponseReviewTableReplay.__new__(FailedResponseReviewTableReplay)
    gate.predecessor = SimpleNamespace(unit_id="W09", enriched_windows=lambda: {k: v for k, v in enriched.items() if k != "W10"})
    gate.unit_id, gate.window, gate.prefix = "W10", enriched["W10"], prefix
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in prefix[:-1]}
    gate.checkpoints_verified = False
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, ctx, checked


def test_review_table_keeps_w10_in_replay_and_serializes_first_calls(gate_state):
    gate, replay, ctx, checked = gate_state
    assert gate.actual_units(replay) == ("W10",)
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    del units["W10"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W10")
    for unit in ("W11", "W12"):
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_CHECKPOINTS_REQUIRED"):
            gate.allow_candidate(replay, ctx, "extract_requirements", unit)
    units["W10"] = {"status": "succeeded", "data": {"window_id": "W10", "status": "complete",
                                                     "raw_model_candidate": {"window_id": "W10"}}}
    gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
    assert tuple(checked) == gate.prefix
    assert gate.enriched_windows().items() <= checked.items()
    reservations.append({"node_key": "N2", "unit_id": "W11", "call_attempt": 1, "call_id": "a" * 32,
                         "unit_attempt_id": "b" * 32, "input_hash": replay.current_wires["extract_requirements:W11"]})
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
    units["W11"] = {"status": "failed", "attempts": [{"status": "failed", "attempt_id": "b" * 32}]}
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    units["W11"]["status"] = units["W11"]["attempts"][0]["status"] = "succeeded"
    gate.allow_candidate(replay, ctx, "extract_requirements", "W12")


@pytest.mark.parametrize("fault", ["replacement_raw", "altered_parent_checkpoint", "historical_reservation", "wrong_bound_position"])
def test_review_table_cold_gate_rejects_changed_prefix(gate_state, fault):
    gate, replay, ctx, _ = gate_state
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    reason = "CHECKPOINTS_REQUIRED"
    if fault == "replacement_raw":
        units["W10"]["data"]["raw_model_candidate"]["window_id"] = "changed"
    elif fault == "altered_parent_checkpoint":
        units["W09"]["data"]["validated"] = {"changed": True}
    elif fault == "historical_reservation":
        reservations.append({"node_key": "N2", "unit_id": "W10", "call_attempt": 1})
    else:
        gate.unit_id = "W11"
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_CONFIG_INVALID"):
            gate.actual_units(replay)
        return
    with pytest.raises(ProviderFailure, match=f"FAILED_RESPONSE_REVIEW_TABLE_{reason}"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W11")


def test_review_table_proof_must_cover_the_original_failed_selection():
    gate = FailedResponseReviewTableReplay.__new__(FailedResponseReviewTableReplay)
    selected = ["heading", "table-first", "table-middle", "table-final", "signature", "date"]
    proof = {"proof": {"content_block_ids": selected}, "whole_form_requirement_index": 1}
    context = {"candidate_type": "requirement", "candidate_index": 1, "block_ids": selected}
    gate.check_proof_context(proof, context)
    for change in ({"candidate_type": "format_chapter"}, {"candidate_index": 2}, {"block_ids": selected[:-1]},
                   {"block_ids": [selected[0], selected[2], selected[1], *selected[3:]]}):
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_PROOF_MISMATCH"):
            gate.check_proof_context(proof, {**context, **change})


@pytest.mark.parametrize("fault", ["missing_w10", "prebound_w11", "rebound_w10_unit"])
def test_review_table_preflight_rejects_the_wrong_binding_set_before_reading_history(gate_state, fault):
    gate, replay, _, _ = gate_state
    gate.run = None
    gate.predecessor.prefix = gate.prefix[:-1]
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W10"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Malformed bindings reached the history reader")
    if fault == "missing_w10":
        del bindings["extract_requirements:W10"]
    elif fault == "prebound_w11":
        bindings["extract_requirements:W11"] = {**gate.source_binding, "unit_id": "W11"}
    else:
        bindings["extract_requirements:W10"] = {**gate.source_binding, "unit_id": "W09"}
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_REVIEW_TABLE_CONFIG_INVALID"):
        gate.verify_history(replay)
