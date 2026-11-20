from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import (FailedContractInsuranceAcceptanceReplay,
                                                           _StoppedResponseReplay)
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def insurance_acceptance_gate(monkeypatch):
    order = tuple(f"W{number:02d}" for number in range(1, 60))
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in order}
    gate = FailedContractInsuranceAcceptanceReplay.__new__(FailedContractInsuranceAcceptanceReplay)
    gate.unit_id, gate.prefix = "W30", order[:30]
    gate.window = {**windows["W30"], gate.proof_key: {"proof": True}}
    gate.predecessor = SimpleNamespace(unit_id="W26", prefix=order[:26], enriched_windows=lambda: {})
    gate.checkpoints_verified = False
    units = {unit: {"status": "succeeded", "data": {"window_id": unit, "status": "complete",
                                                       "raw_model_candidate": {"window_id": unit}}}
             for unit in gate.prefix}
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in gate.prefix[:-1]}
    entries = {f"extract_requirements:{unit}": {"origin": {"candidate_sha256": digest(row["data"]["raw_model_candidate"])}}
               for unit, row in units.items()}
    bindings = {f"extract_requirements:{unit}": {"alias": "history", "run_id": "parent", "node_key": "N2",
                                               "unit_id": unit} for unit in gate.prefix}
    wires = {f"extract_requirements:{unit}": digest(window) for unit, window in windows.items()}
    ctx = SimpleNamespace(run_id="run", attempt_id="attempt", execution_config={}, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay = SimpleNamespace(windows=windows, window_config={}, current_wires=wires, _entries=entries,
                             _ready=(ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)),
                             config={"bindings": bindings})
    checked = {}
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.update({result["window_id"]: window}))
    return gate, replay, ctx, checked


def test_insurance_acceptance_gate_requires_unchanged_w29_and_complete_w30_before_w31(insurance_acceptance_gate):
    gate, replay, ctx, checked = insurance_acceptance_gate
    units = ctx._record["units"]
    w30 = units.pop("W30")
    gate.allow_candidate(replay, ctx, "extract_requirements", "W30")
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W31")
    units["W30"] = w30
    units["W29"]["data"]["validated"] = {"changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        gate.allow_candidate(replay, ctx, "extract_requirements", "W31")
    del units["W29"]["data"]["validated"]
    gate.allow_candidate(replay, ctx, "extract_requirements", "W31")
    assert tuple(checked) == gate.prefix and checked["W30"] == gate.window


@pytest.mark.parametrize("wrong_unit", ["W27", "W28", "W29", "W30", "W31"])
def test_insurance_acceptance_history_only_admits_the_four_actual_bindings(insurance_acceptance_gate, wrong_unit):
    gate, replay, _, _ = insurance_acceptance_gate
    assert gate.actual_units(replay) == ("W27", "W28", "W29", "W30")
    gate.run = None
    gate.predecessor.verify_history = lambda replay: None
    bindings = replay.config["bindings"]
    gate.source_binding = dict(bindings["extract_requirements:W30"])
    if wrong_unit == "W31":
        bindings["extract_requirements:W31"] = {**gate.source_binding, "unit_id": wrong_unit}
    else:
        bindings[f"extract_requirements:{wrong_unit}"]["run_id"] = "different"
    replay._run = lambda *args: pytest.fail("Unexpected historical read for malformed bindings")
    with pytest.raises(ProviderFailure, match=gate.config_error):
        gate.verify_history(replay)


def test_insurance_acceptance_uses_all_and_only_the_failed_target_ids(insurance_acceptance_gate):
    gate, _, _, _ = insurance_acceptance_gate
    context = {"field": "unresolved_block_ids", "block_ids": ["insurance-1", "insurance-2", "acceptance-heading"]}
    resolution = {"original_error": {"error_code": gate.historical_failure, "diagnostic_context": context},
                  "resolved_block_ids": context["block_ids"]}
    gate.check_proof_context(resolution, context)
    for bad_ids in (context["block_ids"][:-1], [*context["block_ids"], "adjacent-only"],
                    [*context["block_ids"][:2], "wrong-heading"]):
        with pytest.raises(ProviderFailure, match=gate.proof_error):
            gate.check_proof_context({**resolution, "resolved_block_ids": bad_ids}, context)


def test_w30_source_proof_loads_and_links_w29_actual_call_before_using_the_checkpoint(
        insurance_acceptance_gate, monkeypatch):
    gate, replay, ctx, _ = insurance_acceptance_gate
    raw = {"unresolved_block_ids": ["insurance-1", "insurance-2", "acceptance-heading", "deferred"]}
    original_call = {"run_id": "parent", "call_id": "w30"}
    preceding_call = {"run_id": "parent", "call_id": "w29"}
    entry = {"candidate": raw, "origin": {"calls": [original_call], "lineage_notes": [{}]}}
    monkeypatch.setattr(_StoppedResponseReplay, "read_failure", lambda *_: entry)
    preceding_data = ctx._record["units"]["W29"]["data"]
    preceding_hash = gate.prior_checkpoint_hashes.pop("W29")
    proof = {"predecessor": {"window_id": "W29"}}
    monkeypatch.setattr(extraction, "_verified_contract_insurance_acceptance_resolution", lambda *_: {"proof": proof})
    monkeypatch.setattr(extraction, "extraction_window_payload", lambda window, _: {"id": window["window_id"]})

    def load(binding, task, window):
        assert binding == replay.config["bindings"]["extract_requirements:W29"]
        assert task == {"id": "W29", "task_type": "extract_requirements"} and window == replay.windows["W29"]
        gate.prior_checkpoint_hashes["W29"] = preceding_hash
        return {"origin": {"calls": [preceding_call], "candidate_sha256": digest(preceding_data["raw_model_candidate"])}}

    def checkpoint(node, unit):
        assert (node, unit) == ("N2", "W29") and gate.prior_checkpoint_hashes[unit] == preceding_hash
        return None, {"output_hash": preceding_hash}, None, preceding_data, None

    used = []
    replay._load_entry = load
    gate.run = SimpleNamespace(run_id="parent", unit=checkpoint)
    monkeypatch.setattr(extraction, "verify_contract_insurance_acceptance_predecessor",
                        lambda data, proof: used.append((data, proof)))
    recovered = gate.read_failure(replay, replay.config["bindings"]["extract_requirements:W30"], {}, gate.window)
    assert used == [(preceding_data, proof)]
    assert recovered["candidate"] == raw and recovered["origin"]["calls"] == [original_call, preceding_call]
    assert recovered["origin"]["lineage_notes"][0]["authenticated_predecessor"]["checkpoint_sha256"] == preceding_hash
    preceding_call["run_id"] = "different"
    with pytest.raises(ProviderFailure, match=gate.source_error):
        gate.read_failure(replay, {}, {}, gate.window)
