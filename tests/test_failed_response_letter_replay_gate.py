"""Authenticate the stopped letter response before admitting W12 onward."""
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedResponseLetterReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def letter_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    prefix = order[:11]
    enriched = {unit: {**windows[unit], "proof": True} for unit in ("W05", "W06", "W09", "W10", "W11")}
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
    previous_windows = {unit: window for unit, window in enriched.items() if unit != "W11"}
    gate = FailedResponseLetterReplay.__new__(FailedResponseLetterReplay)
    gate.predecessor = SimpleNamespace(unit_id="W10", enriched_windows=lambda: previous_windows)
    gate.unit_id, gate.window, gate.prefix = "W11", enriched["W11"], prefix
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in prefix[:-1]}
    gate.checkpoints_verified = False
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, ctx, checked


def test_letter_replays_eleven_and_gates_first_calls_in_order(letter_gate):
    gate, replay, ctx, checked = letter_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    assert gate.actual_units(replay) == ("W11",)
    repaired = units.pop("W11")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    units["W11"] = repaired
    gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    assert tuple(checked) == gate.prefix
    assert gate.enriched_windows().items() <= checked.items()
    reservations.append({"node_key": "N2", "unit_id": "W12", "call_attempt": 1, "call_id": "a" * 32,
                         "unit_attempt_id": "b" * 32, "input_hash": replay.current_wires["extract_requirements:W12"]})
    units["W12"] = {"status": "failed", "attempts": [{"status": "failed", "attempt_id": "b" * 32}]}
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    units["W12"]["status"] = units["W12"]["attempts"][0]["status"] = "succeeded"
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W14")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W13")
    reservations[0]["input_hash"] = "changed"
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W13")


def test_letter_gate_rejects_changed_historical_checkpoint_or_reservation(letter_gate):
    gate, replay, ctx, _ = letter_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    units["W10"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_LETTER_CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W12")
    del units["W10"]["data"]["validated"]
    reservations.append({"node_key": "N2", "unit_id": "W11", "call_attempt": 1})
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_LETTER_CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W12")


def test_letter_proof_is_bound_to_original_heading_failure_and_owner(letter_gate):
    gate, _, _, _ = letter_gate
    selected = ["first", "second", "third", "seal", "signature", "date"]
    proof = {"proof": {"selected_block_ids": selected}, "format_index": 0, "owner_window_id": "W10"}
    context = {"candidate_type": "format_chapter", "field": "title", "candidate_index": 0,
               "block_id": selected[0], "block_ids": selected}
    gate.check_proof_context(proof, context)
    for change in ({"candidate_type": "requirement"}, {"field": "format_title"}, {"candidate_index": 1},
                   {"block_ids": selected[:-1]}, {"block_id": selected[1]}, {"block_ids": selected[::-1]}):
        with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_LETTER_PROOF_MISMATCH"):
            gate.check_proof_context(proof, {**context, **change})
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_LETTER_PROOF_MISMATCH"):
        gate.check_proof_context({**proof, "owner_window_id": "W09"}, context)


@pytest.mark.parametrize("fault", ["missing_w11", "prebound_w12", "wrong_position"])
def test_letter_gate_rejects_extra_or_misplaced_bindings(letter_gate, fault):
    gate, replay, _, _ = letter_gate
    gate.run = None
    gate.predecessor.prefix = gate.prefix[:-1]
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W11"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Unexpected read for a malformed prefix")
    if fault == "missing_w11":
        del bindings["extract_requirements:W11"]
    elif fault == "prebound_w12":
        bindings["extract_requirements:W12"] = {**gate.source_binding, "unit_id": "W12"}
    else:
        gate.unit_id = "W12"
    with pytest.raises(ProviderFailure, match="FAILED_RESPONSE_LETTER_CONFIG_INVALID"):
        gate.verify_history(replay)
