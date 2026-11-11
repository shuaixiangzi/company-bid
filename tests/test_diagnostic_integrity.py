"""A read-only doctor must expose corrupt evidence without rewriting state."""
import json
from pathlib import Path

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import StorageError


def completed(tmp_path, *, unit=False, binary=False):
    def handler(ctx, data):
        if binary:
            return {"file": ctx.store_bytes("example", b"immutable-file")}
        if unit:
            return ctx.run_unit("U1", {"source_hash": "a" * 64}, lambda: {"read": True})
        return {"read": True}
    runner = Executor(tmp_path, [NodeSpec("N1", "1", handler)], {})
    run = runner.create_run({"input": True});state = runner.execute(run)
    assert state["status"] == "succeeded"
    return runner, run, state


def state_bytes(root, run):
    return (root / "runs" / run / "state.json").read_bytes()


@pytest.mark.parametrize("field", ["output_hash", "input_hash", "version"])
def test_checkpoint_metadata_disagreement_is_not_reported_available(tmp_path, field):
    runner, run, state = completed(tmp_path)
    state["nodes"][0][field] = "0" * 64
    runner.store.write_state(state);before = state_bytes(tmp_path, run)
    report = inspect_diagnostics(tmp_path, run)
    assert report["evidence_status"] == "incomplete"
    assert "CHECKPOINT_METADATA_MISMATCH" in {gap["code"] for gap in report["gaps"]}
    assert state_bytes(tmp_path, run) == before


def test_final_embedded_file_is_checked_without_exposing_its_content(tmp_path):
    runner, run, state = completed(tmp_path, binary=True)
    artifact = runner.output(run)["file"]
    (tmp_path / artifact["path"]).write_bytes(b"corrupt-file")
    before = state_bytes(tmp_path, run);report = inspect_diagnostics(tmp_path, run)
    assert report["evidence_status"] == "incomplete"
    assert any(gap["code"] == "ARTIFACT_INTEGRITY_FAILED" and gap["artifact_id"] == artifact["artifact_id"] for gap in report["gaps"])
    assert "corrupt-file" not in json.dumps(report)
    assert state_bytes(tmp_path, run) == before


def test_unit_input_and_checkpoint_consistency_are_checked(tmp_path):
    runner, run, state = completed(tmp_path, unit=True)
    unit = state["nodes"][0]["units"]["U1"]
    unit["input_hash"] = "0" * 64;runner.store.write_state(state)
    before = state_bytes(tmp_path, run);report = inspect_diagnostics(tmp_path, run)
    codes = {gap["code"] for gap in report["gaps"]}
    assert {"UNIT_INPUT_HASH_MISMATCH", "CHECKPOINT_METADATA_MISMATCH"} <= codes
    assert state_bytes(tmp_path, run) == before


def test_missing_unit_input_reference_is_explicitly_incomplete(tmp_path):
    runner, run, state = completed(tmp_path, unit=True)
    state["nodes"][0]["units"]["U1"]["input_ref"] = None;runner.store.write_state(state)
    report = inspect_diagnostics(tmp_path, run)
    assert any(gap["code"] == "UNIT_INPUT_MISSING" for gap in report["gaps"])


def test_nonfinite_json_is_a_gap_instead_of_breaking_checkpoint_inspection(tmp_path):
    runner, run, state = completed(tmp_path)
    state["nodes"][0]["output_ref"] = runner.store.put_bytes(run, "broken.json", b'{"data": NaN}', "application/json")
    runner.store.write_state(state)
    report = inspect_diagnostics(tmp_path, run)
    assert any(gap["code"] == "ARTIFACT_JSON_INVALID" for gap in report["gaps"])


@pytest.mark.parametrize("filename", ["events.jsonl", "spans.jsonl"])
def test_invalid_utf8_is_a_gap_not_an_unhandled_exception(tmp_path, filename):
    _, run, _ = completed(tmp_path)
    (tmp_path / "runs" / run / filename).write_bytes(b"\xff\xfe\n")
    before = state_bytes(tmp_path, run);report = inspect_diagnostics(tmp_path, run)
    assert report["evidence_status"] == "incomplete"
    assert any(gap["code"] == "DIAGNOSTIC_ENCODING_INVALID" and gap["file"] == filename for gap in report["gaps"])
    assert state_bytes(tmp_path, run) == before


def test_unreadable_file_is_a_gap_and_other_evidence_is_still_indexed(tmp_path, monkeypatch):
    _, run, _ = completed(tmp_path)
    trace = tmp_path / "runs" / run / "spans.jsonl";original = Path.read_bytes
    def read(path):
        if path == trace:
            raise PermissionError("Sensitive path and details must not escape")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    before = state_bytes(tmp_path, run);report = inspect_diagnostics(tmp_path, run)
    assert report["event_count"] > 0 and report["artifacts_checked"] > 0
    assert any(gap["code"] == "DIAGNOSTIC_FILE_UNREADABLE" and gap["error_type"] == "PermissionError" for gap in report["gaps"])
    assert "Sensitive" not in json.dumps(report) and state_bytes(tmp_path, run) == before


def test_valid_json_without_a_trace_identity_is_not_a_valid_span(tmp_path):
    _, run, _ = completed(tmp_path)
    (tmp_path / "runs" / run / "spans.jsonl").write_text('{}\n', encoding="utf-8")
    report = inspect_diagnostics(tmp_path, run)
    assert {"DIAGNOSTIC_ROW_INVALID", "ATTEMPT_TRACE_MISSING"} <= {gap["code"] for gap in report["gaps"]}


def test_checkpoint_chain_and_cursor_corruption_are_visible(tmp_path):
    runner, run, state = completed(tmp_path)
    state["cursor"] = 0;state["initial_input_hash"] = "0" * 64;runner.store.write_state(state)
    report = inspect_diagnostics(tmp_path, run)
    assert {"INITIAL_INPUT_HASH_MISMATCH", "CHECKPOINT_CHAIN_MISMATCH", "CHECKPOINT_CURSOR_MISMATCH"} <= {gap["code"] for gap in report["gaps"]}


def test_missing_root_is_not_created_by_inspection(tmp_path):
    absent = tmp_path / "not-created"
    with pytest.raises(StorageError):inspect_diagnostics(absent, "a" * 32)
    assert not absent.exists()


def test_valid_inherited_units_remain_valid_and_read_only(tmp_path):
    def n1(ctx, data):return ctx.run_unit("U1", data, lambda: {"read": True})
    runner = Executor(tmp_path, [NodeSpec("N1", "1", n1), NodeSpec("N6", "1", lambda ctx, data: data)], {})
    parent = runner.create_run({});runner.execute(parent)
    child = runner.fork(parent, "N6");runner.execute(child)
    before = state_bytes(tmp_path, child);report = inspect_diagnostics(tmp_path, child)
    assert report["evidence_status"] == "available"
    assert state_bytes(tmp_path, child) == before
