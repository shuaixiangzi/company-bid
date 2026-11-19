from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedContractInterestedPartiesReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def interested_parties_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    gate = FailedContractInterestedPartiesReplay.__new__(FailedContractInterestedPartiesReplay)
    gate.unit_id, gate.prefix = "W26", order[:26]
    gate.window = {**windows["W26"], gate.proof_key: {"proof": True}}
    gate.predecessor = SimpleNamespace(unit_id="W24", prefix=order[:24], enriched_windows=lambda: {})
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


def test_interested_parties_gate_requires_original_w25_and_completed_w26_before_w27(interested_parties_gate):
    gate, replay, ctx, checked = interested_parties_gate
    units = ctx._record["units"]
    w26 = units.pop("W26")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W26")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W27")
    units["W26"] = w26
    units["W25"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W27")
    del units["W25"]["data"]["validated"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W27")
    assert tuple(checked) == gate.prefix and checked["W26"] == gate.window


def test_interested_parties_gate_stops_at_first_failure_and_rejects_a_second_reservation(interested_parties_gate):
    gate, replay, ctx, _ = interested_parties_gate
    units, reservations = ctx._record["units"], ctx._state["call_budget"]["reservations"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W27")
    reservations.append({"node_key": "N2", "unit_id": "W27", "call_attempt": 1, "call_id": "a" * 32,
                         "unit_attempt_id": "b" * 32, "input_hash": replay.current_wires["extract_requirements:W27"]})
    units["W27"] = {"status": "failed", "attempts": [{"status": "failed", "attempt_id": "b" * 32}]}
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W27")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W28")
    units["W27"]["status"] = units["W27"]["attempts"][0]["status"] = "succeeded"
    gate.allow_candidate(replay, ctx, "extract_requirements", "W28")
    reservations.append(dict(reservations[0]))
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W28")


@pytest.mark.parametrize("wrong_unit", ["W25", "W26", "W27"])
def test_interested_parties_history_requires_both_actual_bindings_and_no_cold_binding(interested_parties_gate, wrong_unit):
    gate, replay, _, _ = interested_parties_gate
    assert gate.actual_units(replay) == ("W25", "W26")
    gate.run = None
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W26"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    if wrong_unit == "W27":
        bindings["extract_requirements:W27"] = {**gate.source_binding, "unit_id": wrong_unit}
    else:
        bindings[f"extract_requirements:{wrong_unit}"]["run_id"] = "different"
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Unexpected historical read for malformed bindings")
    with pytest.raises(ProviderFailure, match=gate.config_error):
        gate.verify_history(replay)


def test_interested_parties_proof_uses_exact_unresolved_block_and_the_v88_w25_checkpoint(interested_parties_gate, monkeypatch):
    gate, _, _, _ = interested_parties_gate
    context = {"field": "unresolved_block_ids", "block_ids": ["continuation"]}
    proof = {"predecessor": {"window_id": "W25"}}
    resolution = {"original_error": {"error_code": gate.historical_failure, "diagnostic_context": context},
                  "resolved_block_ids": ["continuation"], "proof": proof}
    data = {"window_id": "W25"}
    calls = []
    gate.run = SimpleNamespace(unit=lambda node, unit: (None, None, None, data, None) if (node, unit) == ("N2", "W25")
                               else pytest.fail("Unexpected predecessor lookup"))
    monkeypatch.setattr(extraction, "verify_contract_interested_parties_predecessor",
                        lambda predecessor, proof: calls.append((predecessor, proof)))
    gate.verify_source_resolution(resolution, context)
    assert calls == [(data, proof)]
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.verify_source_resolution({**resolution, "resolved_block_ids": ["other"]}, context)
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.verify_source_resolution({**resolution, "proof": {"predecessor": {"window_id": "W24"}}}, context)
