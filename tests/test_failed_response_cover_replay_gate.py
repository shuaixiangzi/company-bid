"""The recovered cover prefix admits only the next never-reserved window."""
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedResponseCoverReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def prefix(monkeypatch):
    order = tuple(f"W{i}" for i in range(1, 13))
    windows = {unit: {"window_id": unit} for unit in order}
    entries = {}
    units = {}
    for unit in order[:9]:
        raw = {"window_id": unit}
        entries["extract_requirements:" + unit] = {"origin": {"candidate_sha256": digest(raw)}}
        units[unit] = {"status": "succeeded", "data": {"window_id": unit, "status": "complete", "raw_model_candidate": raw}}
    config = {"extraction": {}}
    ctx = SimpleNamespace(run_id="R", attempt_id="A", execution_config=config, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    wires = {"extract_requirements:" + unit: digest(window) for unit, window in windows.items()}
    replay = SimpleNamespace(windows=windows, current_wires=wires, _entries=entries, _ready=("R", "A", digest(config)))
    gate = FailedResponseCoverReplay.__new__(FailedResponseCoverReplay)
    price = SimpleNamespace(unit_id="W5", window={"window_id": "W5", "price_proof": True})
    gate.predecessor = SimpleNamespace(unit_id="W6", window={"window_id": "W6", "row_proof": True}, predecessor=price)
    gate.unit_id = "W9"
    gate.window = {"window_id": "W9", "cover_proof": True}
    gate.prefix = order[:9]
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in order[:8]}
    gate.checkpoints_verified = False
    checked = []
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.append((result["raw_model_candidate"]["window_id"], window)))
    return gate, replay, ctx, units, ctx._state["call_budget"]["reservations"], checked


@pytest.mark.parametrize("fault", ["missing_cover", "changed_cover_raw", "changed_historical_actual",
                                  "prefix_was_charged", "reserved_after_restart", "out_of_order"])
def test_response_cover_never_dispatches_without_the_exact_prefix(prefix, fault):
    gate, replay, ctx, units, reservations, _ = prefix
    unit = "W10"
    reason = "CHECKPOINTS_REQUIRED"
    if fault == "missing_cover":
        del units["W9"]
    elif fault == "changed_cover_raw":
        units["W9"]["data"]["raw_model_candidate"]["window_id"] = "changed"
    elif fault == "changed_historical_actual":
        units["W8"]["data"]["validated"] = {"changed": True}
    elif fault == "prefix_was_charged":
        reservations.append({"node_key": "N2", "unit_id": "W9", "call_attempt": 1})
    elif fault == "reserved_after_restart":
        reservations.append({"node_key": "N2", "unit_id": unit, "call_attempt": 1,
                             "settlement_status": "reserved_unknown_actual_cost"})
        reason = "LIVE_WINDOW_ALREADY_RESERVED"
    else:
        unit = "W11"
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", unit)
    assert rejected.value.code == "DIAGNOSTIC_REPLAY_FAILED_RESPONSE_COVER_" + reason


def test_response_cover_authenticates_all_checkpoints_and_preceding_cold_charge(prefix):
    gate, replay, ctx, units, reservations, checked = prefix
    gate.allow_candidate(replay, ctx, "extract_requirements", "W10")
    assert [unit for unit, _ in checked] == list(gate.prefix)
    assert {unit: window for unit, window in checked}.items() >= gate.enriched_windows().items()
    units["W10"] = {"status": "succeeded", "attempts": [{"status": "succeeded", "attempt_id": "a" * 32}]}
    reservations.append({"node_key": "N2", "unit_id": "W10", "call_attempt": 1, "call_id": "b" * 32,
                         "input_hash": replay.current_wires["extract_requirements:W10"],
                         "unit_attempt_id": "0" * 32, "settlement_status": "known_usage_cost_estimate"})
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
    assert rejected.value.code == "DIAGNOSTIC_REPLAY_FAILED_RESPONSE_COVER_CHECKPOINTS_REQUIRED"
    reservations[0]["unit_attempt_id"] = "a" * 32
    reservations[0]["input_hash"] = "f" * 64
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
    assert rejected.value.code == "DIAGNOSTIC_REPLAY_FAILED_RESPONSE_COVER_CHECKPOINTS_REQUIRED"
    reservations[0]["input_hash"] = replay.current_wires["extract_requirements:W10"]
    reservations[0]["settlement_status"] = "reserved_unknown_actual_cost"
    gate.allow_candidate(replay, ctx, "extract_requirements", "W11")
