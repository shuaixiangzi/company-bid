"""Native optional-field recovery preserves each checkpoint in the stopped prefix."""

from types import SimpleNamespace

import pytest

from company_bid import extraction, extraction_optional_fields_replay as replay_module
from company_bid.extraction_optional_fields_replay import FailedContractOptionalFieldsReplay
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.provider import ProviderFailure
from company_bid.storage import digest


@pytest.fixture
def stopped_optional_fields(monkeypatch):
    from company_bid.contract_optional_fields import RESOLUTION_KEY, WINDOW_CONTEXT_KEY

    prefix = ("earlier", "numbered", "actual", "failed")
    windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in (*prefix, "cold")}
    window = {**windows["failed"], WINDOW_CONTEXT_KEY: {"source_sha256": "original"}}
    raw = {"unresolved_block_ids": ["field-a", "field-b"]}
    context = {"field": "unresolved_block_ids", "block_ids": list(raw["unresolved_block_ids"])}
    resolution = {"raw_candidate_sha256": digest(raw), "resolved_block_ids": context["block_ids"],
                  "proof": window[WINDOW_CONTEXT_KEY]}
    recovered = {"window_id": "failed", "status": "complete", "raw_model_candidate": raw, RESOLUTION_KEY: resolution}
    checkpoints = {unit: {"window_id": unit, "status": "complete", "raw_model_candidate": {"unit": unit}}
                   for unit in prefix[:-1]}
    call = {"candidate": raw, "request": {"body": "saved"}, "response": {"body": "saved"},
            "terminal": {"trace_id": "original-trace", "span_id": "original-span"}}
    unit = {"version": "historical"}
    reads = []
    validated = []
    extraction_config = {"window_max_chars": 2000}

    def checkpoint(_node, unit_id):
        data = checkpoints[unit_id]
        return None, {"output_hash": digest(data)}, None, data, None

    def verify(_replay, _invalid):
        return {**windows, "failed": window}, extraction_config

    def payload(value, config):
        assert config is extraction_config
        return {"window_id": value["window_id"]}

    def revalidate(ctx, value, config, caller, *, predecessor_result):
        assert ctx is None and value == window and config is extraction_config
        assert predecessor_result is checkpoints["actual"]
        assert caller(ctx, "extract_requirements", "failed", None, lambda candidate: candidate) == raw
        return recovered

    def check_result(data, value, *, predecessor_result):
        assert value["window_id"] == data.get("window_id", "failed")
        if value["window_id"] == "numbered":
            assert predecessor_result == checkpoints["earlier"]
        if value["window_id"] == "failed":
            assert predecessor_result == checkpoints["actual"]
        validated.append(value["window_id"])

    def load(binding, task, value):
        reads.append((binding["unit_id"], task["window_id"], value["window_id"]))

    monkeypatch.setattr(replay_module, "rebuild_frozen_contract_windows", verify)
    monkeypatch.setattr(extraction, "extraction_window_payload", payload)
    monkeypatch.setattr(extraction, "run_extraction_window", revalidate)
    monkeypatch.setattr(extraction, "_validate_normalized_window_result", check_result)
    gate = FailedContractOptionalFieldsReplay.__new__(FailedContractOptionalFieldsReplay)
    gate.unit_id, gate.prefix = "failed", prefix
    gate.call_id, gate.state_sha256 = "original-call", "original-state"
    gate.predecessor = SimpleNamespace(unit_id="numbered", prefix=prefix[:2], window=windows["numbered"],
                                       enriched_windows=lambda: {"numbered": windows["numbered"]})
    gate.prior_checkpoint_hashes = {unit: digest(data) for unit, data in checkpoints.items()}
    gate.checkpoints_verified = False
    gate.run = SimpleNamespace(run_id="history", alias="source", unit=checkpoint)
    gate.n1 = {"output_hash": "frozen-N1-hash"}
    gate.read_original_failure = lambda *args: (unit, call, context)
    gate.offline_proof = {"raw_candidate_sha256": digest(raw), "result_sha256": digest(recovered),
                          "source_resolution_sha256": digest(resolution)}
    bindings = {f"extract_requirements:{unit}": {"unit_id": unit} for unit in prefix}
    replay = SimpleNamespace(windows=windows, window_config=extraction_config, config={"bindings": bindings},
                             _load_entry=load)
    return SimpleNamespace(gate=gate, replay=replay, checkpoints=checkpoints, recovered=recovered,
                           raw=raw, resolution=resolution, context=context, reads=reads, validated=validated)


def _read(stopped):
    task = {"window_id": "failed", "task_type": "extract_requirements"}
    return stopped.gate.read_failure(stopped.replay, {"unit_id": "failed"}, task, stopped.replay.windows["failed"])


def test_failed_response_and_exact_resolution_are_sealed(stopped_optional_fields):
    stopped = stopped_optional_fields
    entry = _read(stopped)
    assert entry["candidate"] == stopped.raw
    assert stopped.reads == [("actual", "actual", "actual")]
    assert stopped.validated == ["failed"]
    link, = entry["origin"]["calls"]
    assert link["trace_id"] == "original-trace"
    lineage, = entry["origin"]["lineage_notes"]
    assert lineage["historical_diagnostic_context"] == stopped.context
    assert lineage["historical_candidate_modified"] is False
    assert lineage["source_resolution_sha256"] == digest(stopped.resolution)
    assert lineage["offline_proof"] == stopped.gate.offline_proof


@pytest.mark.parametrize("key", ["raw_candidate_sha256", "result_sha256", "source_resolution_sha256"])
def test_rejects_any_changed_frozen_hash(stopped_optional_fields, key):
    stopped_optional_fields.gate.offline_proof[key] = "0" * 64
    with pytest.raises(ProviderFailure, match="PROOF_MISMATCH"):
        _read(stopped_optional_fields)


def test_rejects_any_mismatch_in_original_failure_context(stopped_optional_fields):
    stopped_optional_fields.context["block_ids"] = ["field-a", "operative-clause"]
    with pytest.raises(ProviderFailure, match="PROOF_MISMATCH"):
        _read(stopped_optional_fields)


def test_prior_numbered_resolution_reuses_verified_actual_predecessor(stopped_optional_fields, monkeypatch):
    stopped = stopped_optional_fields

    def authenticate(gate, replay, unit_id, entry, window, invalid, *, predecessor_result):
        assert predecessor_result is stopped.checkpoints["earlier"]
        assert unit_id == window["window_id"] == "numbered"
        return digest(stopped.checkpoints["numbered"])

    monkeypatch.setattr(replay_module, "_verify_replayed_checkpoint", authenticate)
    stopped.gate.verify_success(stopped.replay, {"unit_id": "numbered"}, {}, stopped.replay.windows["numbered"])
    stopped.checkpoints["earlier"]["changed"] = True
    with pytest.raises(ProviderFailure, match="SOURCE_INVALID"):
        stopped.gate.verify_success(stopped.replay, {"unit_id": "numbered"}, {}, stopped.replay.windows["numbered"])


def test_cold_request_requires_every_unchanged_checkpoint(stopped_optional_fields):
    stopped = stopped_optional_fields
    _read(stopped)
    results = {**stopped.checkpoints, "failed": {**stopped.recovered}}
    replay = stopped.replay
    replay._entries = {f"extract_requirements:{unit}": {"origin": {
        "candidate_sha256": digest(data["raw_model_candidate"])}} for unit, data in results.items()}
    units = {unit: {"status": "succeeded", "data": data} for unit, data in results.items()}
    ctx = SimpleNamespace(run_id="new", attempt_id="attempt", execution_config={}, _record={"units": units},
                          _state={}, executor=SimpleNamespace(_checkpoint_data=lambda unit: unit["data"]))
    replay._ready = ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)
    assert stopped.gate.actual_units(replay) == ("actual", "failed")
    # A matching candidate digest is insufficient when any historical output changed.
    units["actual"]["data"] = {**results["actual"], "changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        stopped.gate.allow_candidate(replay, ctx, "extract_requirements", "cold")
    units["actual"]["data"] = results["actual"]
    units["failed"]["data"] = {**results["failed"], "changed": True}
    with pytest.raises(ProviderFailure, match="CHECKPOINTS_REQUIRED"):
        stopped.gate.allow_candidate(replay, ctx, "extract_requirements", "cold")
    units["failed"]["data"] = results["failed"]
    stopped.gate.allow_candidate(replay, ctx, "extract_requirements", "cold")
    assert stopped.gate.checkpoints_verified
    ctx._state["call_budget"] = {"reservations": [{"node_key": "N2", "unit_id": "cold"}]}
    with pytest.raises(ProviderFailure, match="LIVE_WINDOW_ALREADY_RESERVED"):
        stopped.gate.allow_candidate(replay, ctx, "extract_requirements", "cold")


def test_optional_fields_gate_requires_the_historical_numbered_chain(tmp_path):
    with pytest.raises(PipelineFailure, match="FAILED_CONTRACT_OPTIONAL_FIELDS_CONFIG_INVALID"):
        build_executor(tmp_path, {"extraction": {FailedContractOptionalFieldsReplay.replay_key: {}}})
