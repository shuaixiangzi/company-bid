"""Cold-window admission stays closed until the stopped prefix is authenticated."""
from types import SimpleNamespace

import pytest

from company_bid import extraction
from company_bid.extraction_failed_scoring_replay import FailedScoringReplay
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def prefix(monkeypatch):
    ordered = ["W1", "W2", "W3", "W4", "W5", "W6", "W7"]
    bindings = {"extract_requirements:" + unit: {
        "alias": "history", "run_id": "1" * 32 if unit == "W1" else "2" * 32,
        "node_key": "N2", "unit_id": unit} for unit in ordered[:5]}
    config = {"structured_provider": {"mode": "diagnostic_mixed"},
              "extraction": {"failed_cell_wrap_replay": {"unit_id": "W1"},
                             "failed_scoring_replay": {"unit_id": "W5", "call_id": "3" * 32,
                                                       "source_state_sha256": "a" * 64}}}
    candidates = {unit: {"window_id": unit} for unit in ordered[:5]}
    replay = SimpleNamespace(config={"bindings": bindings}, _failed_cell_wrap={"unit_id": "W1"},
                             windows={unit: {"window_id": unit} for unit in ordered})
    replay._entries = {"extract_requirements:" + unit: {"origin": {"candidate_sha256": digest(raw)}}
                       for unit, raw in candidates.items()}
    gate = FailedScoringReplay(replay, config)
    # The lineage reader fills these after authenticating the historical runs.
    gate.prefix = tuple(candidates)
    gate.window = {"window_id": "W5", "source_proof_present": True}
    units = {unit: {"status": "succeeded", "data": {"raw_model_candidate": raw}} for unit, raw in candidates.items()}
    units["W6"] = {"status": "running"}
    reservations, checked = [], []
    ctx = SimpleNamespace(run_id="R", attempt_id="A", execution_config=config, _record={"units": units},
                          _state={"call_budget": {"reservations": reservations}},
                          executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay._ready = (ctx.run_id, ctx.attempt_id, digest(config))
    monkeypatch.setattr(extraction, "_validate_normalized_window_result",
                        lambda output, window: checked.append((output["raw_model_candidate"]["window_id"], window)))
    return gate, replay, ctx, units, reservations, checked


@pytest.mark.parametrize("fault", ["no_preflight", "missing_first", "failed_fifth", "out_of_order",
                                  "substituted_candidate", "reserved_prefix", "reserved_live_unit"])
def test_failed_scoring_cold_window_admission_is_closed(prefix, fault):
    gate, replay, ctx, units, reservations, _ = prefix
    unit = "W6"
    expected = "DIAGNOSTIC_REPLAY_FAILED_SCORING_CHECKPOINTS_REQUIRED"
    if fault == "no_preflight":
        replay._ready = None
        expected = "DIAGNOSTIC_REPLAY_PREFLIGHT_REQUIRED"
    elif fault == "missing_first":
        del units["W1"]
    elif fault == "failed_fifth":
        units["W5"]["status"] = "failed"
    elif fault == "out_of_order":
        unit = "W7"
    elif fault == "substituted_candidate":
        units["W5"]["data"] = {"raw_model_candidate": {"window_id": "forged-W5"}}
    elif fault == "reserved_prefix":
        reservations.append({"node_key": "N2", "unit_id": "W5"})
    else:
        reservations.append({"node_key": "N2", "unit_id": unit})
        expected = "DIAGNOSTIC_REPLAY_FAILED_SCORING_LIVE_WINDOW_ALREADY_RESERVED"
    with pytest.raises(ProviderFailure) as rejected:
        gate.allow_candidate(replay, ctx, "extract_requirements", unit)
    assert rejected.value.code == expected


def test_failed_scoring_gate_requires_entire_revalidated_prefix_and_serial_order(prefix):
    gate, replay, ctx, units, reservations, checked = prefix
    gate.allow_candidate(replay, ctx, "extract_requirements", "W6")
    assert [unit for unit, _ in checked] == list(gate.prefix)
    assert checked[-1][1] == gate.window
    units["W6"]["status"] = "succeeded"
    reservations.append({"node_key": "N2", "unit_id": "W6"})
    gate.allow_candidate(replay, ctx, "extract_requirements", "W7")
    assert [unit for unit, _ in checked] == list(gate.prefix)
