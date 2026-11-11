"""Library preparation uses persisted units and the same CLI/executor."""
import json

from company_bid.cli import main
from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import file_sha256
from company_bid.pipeline import build_executor


def test_library_cli_terminal_step_and_immutable_child(tmp_path, capsys):
    source = tmp_path / "source.txt"
    source.write_text("Synthetic company table source", encoding="utf-8")
    registered = {"source_id": "S1", "title": "虚构资料", "path": str(source),
                  "sha256": file_sha256(source), "locator": "document", "kind": "company_material",
                  "provenance": "synthetic", "synthetic": True, "role": "company_material", "material_type": "company_profile"}
    document = {"source_id": "S1", "role": "company_material", "sha256": registered["sha256"], "format": "docx", "units": [
        {"unit_id": "U1", "source_id": "S1", "locator": "body", "page": None, "coverage": {"status": "read"}, "blocks": [
            {"block_id": "B1", "source_id": "S1", "locator": "body/table[0]", "kind": "table", "text": "", "origin": "native",
             "rows": [["公司名称", "虚构资料测试公司"]], "merges": [], "column_widths": [1, 1]}]}]}
    config = {"pipeline": "library", "debug_capture": True, "library": {"table_scopes": {"S1": ["body/table[0]"]}}}
    input_file, config_file, root = tmp_path / "input.json", tmp_path / "config.json", tmp_path / "runtime"
    input_file.write_text(json.dumps({"registered_sources": [registered], "parsed": {"documents": [document]}}), encoding="utf-8")
    config_file.write_text(json.dumps(config), encoding="utf-8")
    base = ["--root", str(root), "--config", str(config_file)]
    assert main([*base, "create", "--input", str(input_file), "--stop-after", "LIBRARY"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "succeeded"
    executor = build_executor(root, config)
    output = executor.output(state["run_id"])
    snapshot_file = root / output["snapshot_artifact"]["path"]
    original_hash = file_sha256(snapshot_file)
    assert output["counts"]["usable_facts"] == 1
    assert main([*base, "result", state["run_id"]]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert "facts" not in summary and summary["snapshot_id"] == output["snapshot_id"]
    child = executor.fork(state["run_id"], "LIBRARY")
    assert executor.resume(child)["status"] == "succeeded"
    assert file_sha256(snapshot_file) == original_hash
    report = inspect_diagnostics(root, state["run_id"])
    assert report["evidence_status"] == "available" and report["actual_call_attempts"] == 0


def test_authorized_derived_bid_snapshot_survives_real_executor_redaction(tmp_path):
    # Synthetic bytes exercise the real derived/provisional provenance branch.
    source = tmp_path / "bid-source.txt"
    source.write_text("Synthetic prior bid with an explicitly selected native table", encoding="utf-8")
    original_hash = file_sha256(source)
    registered = {"source_id": "BID", "title": "虚构人工标书", "path": str(source), "sha256": original_hash,
                  "locator": "document", "kind": "company_material", "provenance": "derived_from_bid",
                  "synthetic": True, "role": "reference_bid", "material_type": "reference_bid"}
    document = {"source_id": "BID", "role": "reference_bid", "sha256": original_hash, "format": "docx", "units": [
        {"unit_id": "U1", "source_id": "BID", "locator": "body/table[0]", "page": None, "coverage": {"status": "read"}, "blocks": [
            {"block_id": "B1", "source_id": "BID", "locator": "body/table[0]", "kind": "table", "text": "", "origin": "native",
             "rows": [["公司名称", "虚构测试公司"]], "merges": [], "column_widths": [1, 1]}]}]}
    config = {"pipeline": "library", "debug_capture": True, "library": {
        "authorized_bid_fact_sources": {"BID": original_hash}, "table_scopes": {"BID": ["body/table[0]"]}}}
    root = tmp_path / "runtime"
    executor = build_executor(root, config)
    run_id = executor.create_run({"library_input": {"registered_sources": [registered], "parsed": {"documents": [document]}}})
    state = executor.execute(run_id)
    assert state["status"] == "succeeded"
    snapshot = executor.output(run_id)
    assert snapshot["fact_candidates"][0]["fact_adoption_source_sha256"] == original_hash
    assert snapshot["source_artifacts"][0]["sha256"] == original_hash
    assert all(ref["sha256"] == original_hash and ref["provenance"] == "derived_from_bid" for ref in snapshot["sources"])
    artifact_path = root / snapshot["snapshot_artifact"]["path"]
    assert file_sha256(artifact_path) == snapshot["snapshot_artifact"]["sha256"]
    assert "[REDACTED]" not in artifact_path.read_text(encoding="utf-8")
    assert snapshot["fact_candidates"][0]["status"] == "usable_provisional"
