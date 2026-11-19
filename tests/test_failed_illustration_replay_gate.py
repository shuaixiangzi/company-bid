from types import SimpleNamespace

import pytest

import company_bid.extraction as extraction
from company_bid.extraction_failed_scoring_replay import FailedIllustrationReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def illustration_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    gate = FailedIllustrationReplay.__new__(FailedIllustrationReplay)
    gate.unit_id, gate.prefix, gate.window = "W15", order[:15], {**windows["W15"], "proof": True}
    gate.predecessor = SimpleNamespace(unit_id="W12", prefix=order[:12], enriched_windows=lambda: {})
    gate.checkpoints_verified = False
    units = {unit: {"status": "succeeded", "data": {"window_id": unit, "status": "complete",
                                                       "raw_model_candidate": {"window_id": unit}}}
             for unit in gate.prefix}
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in gate.prefix[:-1]}
    entries = {f"extract_requirements:{unit}": {"origin": {"candidate_sha256": digest(row["data"]["raw_model_candidate"])}}
               for unit, row in units.items()}
    context = SimpleNamespace(run_id="run", attempt_id="attempt", execution_config={}, _record={"units": units},
                              _state={"call_budget": {"reservations": []}},
                              executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay = SimpleNamespace(windows=windows, _entries=entries, _ready=("run", "attempt", digest({})))
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, context, checked


def test_live_window_requires_fourteen_unchanged_outputs_and_reviewed_w15(illustration_gate):
    gate, replay, ctx, checked = illustration_gate
    assert gate.actual_units(replay) == ("W13", "W14", "W15")
    units = ctx._record["units"]
    repaired = units.pop("W15")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W15")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W16")
    units["W15"] = repaired
    units["W14"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W16")
    del units["W14"]["data"]["validated"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W16")
    assert tuple(checked) == gate.prefix and checked["W15"] == gate.window
    ctx._state["call_budget"]["reservations"].append({"node_key": "N2", "unit_id": "W16"})
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W16")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W17")


@pytest.mark.parametrize("wrong_unit", ["W13", "W14", "W15", "W16"])
def test_illustration_history_requires_exact_three_actual_call_bindings(illustration_gate, wrong_unit):
    gate, replay, _, _ = illustration_gate
    gate.run = None
    gate.predecessor.verify_history = lambda replay: None
    gate.source_binding = {"alias": "history", "run_id": "parent", "node_key": "N2", "unit_id": "W15"}
    bindings = {f"extract_requirements:{unit}": {**gate.source_binding, "unit_id": unit} for unit in gate.prefix}
    if wrong_unit == "W16":
        bindings["extract_requirements:W16"] = {**gate.source_binding, "unit_id": "W16"}
    else:
        bindings[f"extract_requirements:{wrong_unit}"]["run_id"] = "other"
    replay.config = {"bindings": bindings}
    replay._run = lambda *args: pytest.fail("Read attempted for invalid bindings")
    with pytest.raises(ProviderFailure, match=gate.config_error):
        gate.verify_history(replay)


def test_illustration_resolution_requires_exact_error_and_w14_source_coverage(illustration_gate, monkeypatch):
    gate, _, _, _ = illustration_gate
    error = {"candidate_type": "requirement", "candidate_index": 0, "block_id": "instruction",
             "block_ids": ["instruction", "example", "interface"]}
    proof = {"original_error": {"error_code": gate.historical_failure, "diagnostic_context": error},
             "proof": {"predecessor": {"window_id": "W14"}}}
    reads, checked = [], []

    def historical_unit(node, unit):
        reads.append((node, unit))
        return None, None, None, {"window_id": unit}, None

    gate.run = SimpleNamespace(unit=historical_unit)
    monkeypatch.setattr(extraction, "verify_illustration_predecessor", lambda data, proof: checked.append((data, proof)))
    gate.verify_source_resolution(proof, error)
    assert reads == [("N2", "W14")] and checked == [({"window_id": "W14"}, proof)]
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.verify_source_resolution(proof, {**error, "block_ids": ["instruction"]})
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.verify_source_resolution({**proof, "proof": {"predecessor": {"window_id": "W13"}}}, error)
    monkeypatch.setattr(extraction, "verify_illustration_predecessor", _reject_missing_native_clause)
    with pytest.raises(ProviderFailure, match=gate.proof_error):
        gate.verify_source_resolution(proof, error)


def _reject_missing_native_clause(*_):
    raise extraction.PlanningError("ILLUSTRATION_PREDECESSOR_MISMATCH")
