from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedContractAnnexReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def annex_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    gate = FailedContractAnnexReplay.__new__(FailedContractAnnexReplay)
    gate.unit_id, gate.prefix = "W24", order[:24]
    gate.window = {**windows["W24"], gate.proof_key: {"proof": True}}
    gate.predecessor = SimpleNamespace(unit_id="W15", prefix=order[:15], enriched_windows=lambda: {})
    gate.checkpoints_verified = False
    units = {unit: {"status": "succeeded", "data": {"window_id": unit, "status": "complete",
                                                       "raw_model_candidate": {"window_id": unit}}}
             for unit in gate.prefix}
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in gate.prefix[:-1]}
    entries = {f"extract_requirements:{unit}": {"origin": {"candidate_sha256": digest(row["data"]["raw_model_candidate"])}}
               for unit, row in units.items()}
    wires = {f"extract_requirements:{unit}": digest(window) for unit, window in windows.items()}
    ctx = SimpleNamespace(run_id="run", attempt_id="attempt", execution_config={}, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay = SimpleNamespace(windows=windows, current_wires=wires, _entries=entries,
                             _ready=(ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)))
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, ctx, checked


def test_annex_gate_checks_23_unchanged_checkpoints_and_validates_w24_before_w25(annex_gate):
    gate, replay, ctx, checked = annex_gate
    units = ctx._record["units"]
    recovered = units.pop("W24")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W24")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W25")
    units["W24"] = recovered
    units["W23"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W25")
    del units["W23"]["data"]["validated"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W25")
    assert tuple(checked) == gate.prefix and checked["W24"] == gate.window


def test_annex_gate_releases_later_windows_in_order_after_one_matching_call(annex_gate):
    gate, replay, ctx, _ = annex_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W26")
    reservations.append({"node_key": "N2", "unit_id": "W25", "call_attempt": 1, "call_id": "a" * 32,
                         "unit_attempt_id": "b" * 32, "input_hash": replay.current_wires["extract_requirements:W25"]})
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W25")
    units["W25"] = {"status": "succeeded", "attempts": [{"status": "succeeded", "attempt_id": "b" * 32}]}
    gate.allow_candidate(replay, ctx, "extract_requirements", "W26")
    reservations[0]["input_hash"] = replay.current_wires["extract_requirements:W26"]
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W26")


@pytest.mark.parametrize("wrong_unit", ["W16", "W23", "W24", "W25"])
def test_annex_history_requires_all_nine_actual_bindings_and_no_cold_binding(annex_gate, wrong_unit):
    gate, replay, _, _ = annex_gate
    assert gate.actual_units(replay) == tuple(replay.windows)[15:24]
    gate.run = None
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W24"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    if wrong_unit == "W25":
        bindings["extract_requirements:W25"] = {**gate.source_binding, "unit_id": wrong_unit}
    else:
        bindings[f"extract_requirements:{wrong_unit}"]["run_id"] = "different"
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Unexpected historical read for malformed bindings")
    with pytest.raises(ProviderFailure, match=gate.config_error):
        gate.verify_history(replay)


def test_annex_proof_only_resolves_the_single_saved_unresolved_block(annex_gate):
    gate, _, _, _ = annex_gate
    context = {"field": "unresolved_block_ids", "block_ids": ["annex-reference"]}
    proof = {"original_error": {"error_code": gate.historical_failure, "diagnostic_context": context},
             "resolved_block_ids": ["annex-reference"]}
    gate.check_proof_context(proof, context)
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.check_proof_context({**proof, "resolved_block_ids": ["other-block"]}, context)
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.check_proof_context(proof, {**context, "block_ids": ["annex-reference", "other-block"]})
