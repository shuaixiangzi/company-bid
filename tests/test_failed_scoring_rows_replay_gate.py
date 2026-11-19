"""A scoring-row replay opens the next cold window only after six successes."""
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedScoringRowsReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def prefix(monkeypatch):
    order = tuple(f"W{i}" for i in range(1, 9))
    windows = {unit: {"window_id": unit} for unit in order}
    entries = {}
    units = {}
    for unit in order[:6]:
        raw = {"window_id": unit}
        entries["extract_requirements:" + unit] = {"origin": {"candidate_sha256": digest(raw)}}
        units[unit] = {"status": "succeeded", "data": {"raw_model_candidate": raw}}
    config = {"extraction": {}}
    ctx = SimpleNamespace(run_id="R", attempt_id="A", execution_config=config, _record={"units": units},
                          _state={"call_budget": {"reservations": []}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay = SimpleNamespace(windows=windows, _entries=entries, _ready=("R", "A", digest(config)))
    gate = FailedScoringRowsReplay.__new__(FailedScoringRowsReplay)
    gate.predecessor = SimpleNamespace(unit_id="W5", window={"window_id": "W5", "price_proof": True})
    gate.unit_id = "W6"
    gate.window = {"window_id": "W6", "row_proof": True}
    gate.prefix = order[:6]
    gate.prior_checkpoint_hashes = {unit: digest(units[unit]["data"]) for unit in order[:5]}
    gate.checkpoints_verified = False
    checked = []
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda result, window: checked.append((result["raw_model_candidate"]["window_id"], window)))
    return gate, replay, ctx, units, ctx._state["call_budget"]["reservations"], checked


@pytest.mark.parametrize("fault", ["missing_sixth", "changed_sixth_raw", "changed_prior_result",
                                  "prefix_was_charged", "cold_was_charged", "out_of_order"])
def test_scoring_rows_cold_window_admission_is_closed(prefix, fault):
    gate, replay, ctx, units, reservations, _ = prefix
    unit = "W7"
    reason = "CHECKPOINTS_REQUIRED"
    if fault == "missing_sixth":
        del units["W6"]
    elif fault == "changed_sixth_raw":
        units["W6"]["data"]["raw_model_candidate"]["window_id"] = "forged"
    elif fault == "changed_prior_result":
        units["W5"]["data"]["validated"] = {"changed": True}
    elif fault == "prefix_was_charged":
        reservations.append({"node_key": "N2", "unit_id": "W6", "call_attempt": 1})
    elif fault == "cold_was_charged":
        reservations.append({"node_key": "N2", "unit_id": unit, "call_attempt": 1})
        reason = "LIVE_WINDOW_ALREADY_RESERVED"
    else:
        unit = "W8"
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", unit)
    assert rejected.value.code == "DIAGNOSTIC_REPLAY_FAILED_SCORING_ROWS_" + reason


def test_scoring_rows_gate_revalidates_full_prefix_and_cold_debit_order(prefix):
    gate, replay, ctx, units, reservations, checked = prefix
    gate.allow_candidate(replay, ctx, "extract_requirements", "W7")
    assert [unit for unit, _ in checked] == list(gate.prefix)
    assert [window for _, window in checked[-2:]] == [gate.predecessor.window, gate.window]
    units["W7"] = {"status": "succeeded"}
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", "W8")
    assert rejected.value.code == "DIAGNOSTIC_REPLAY_FAILED_SCORING_ROWS_CHECKPOINTS_REQUIRED"
    reservations.append({"node_key": "N2", "unit_id": "W7", "call_attempt": 1})
    gate.allow_candidate(replay, ctx, "extract_requirements", "W8")
    assert len(checked) == 6
