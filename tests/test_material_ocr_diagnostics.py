"""Flat local OCR evidence is searchable, authenticated and separate from billing."""
from __future__ import annotations

import copy
import hashlib
import json

from PIL import Image
import pytest

import legacy_local_ocr_events as ocr
from company_bid.diagnostics import inspect_diagnostics
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes


CANARY = "PRIVATE_OCR_TEXT_证书原文"
PATH_CANARY = "PRIVATE_SOURCE_PATH"


def _fingerprint():
    return {"schema_version": ocr.FINGERPRINT_SCHEMA, "adapter_version": ocr.ADAPTER_VERSION,
            "engine": "windows_ocr", "language": "zh-Hans-CN", "available_languages": ["en-US", "zh-Hans-CN"],
            "max_image_dimension": 10000, "os": {"version": "10.0.26200.0", "architecture": "x64"},
            "powershell": {"version": "5.1.26100.9818", "exe_sha256": "a" * 64},
            "ocr_dll": {"file_version": "10.0.26100.9278", "sha256": "b" * 64},
            "script_sha256": hashlib.sha256(ocr._SCRIPT.read_bytes()).hexdigest()}


def _runner(tmp_path, monkeypatch, *, units=1, fail_once=False, preflight=False, capture=True, empty=False):
    image = tmp_path / (PATH_CANARY + ".png")
    Image.new("RGB", (128, 64), "white").save(image)
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    fingerprint = _fingerprint()
    calls = []

    def process(mode, request, timeout):
        if mode == "fingerprint":
            value = {"status": "succeeded", "engine_fingerprint": fingerprint}
        else:
            calls.append(copy.deepcopy(request))
            if fail_once and len(calls) == 2:
                raise ocr.MaterialLocalOCRFailure("MATERIAL_OCR_TIMEOUT", details={"stderr": CANARY})
            text = "" if empty else CANARY
            value = {"schema_version": ocr.RESPONSE_SCHEMA, "status": "succeeded", "text": text,
                "lines": [{"text": text, "words": [{"text": text, "box": [1, 2, 100, 30]}]}] if text else [],
                "width": 128, "height": 64, "image_sha256": image_hash, "engine_fingerprint": fingerprint}
        return {"returncode": 0, "stdout": json_bytes(value), "stderr": b""}

    monkeypatch.setattr(ocr, "_run_powershell", process)
    options = {"expected_engine_fingerprint": fingerprint}

    def recognize(ctx, value):
        results = []
        for number in range(units):
            identity = {"source_id": "SOURCE", "source_sha256": "c" * 64, "selection_sha256": "d" * 64,
                        "target": number + 1, "image_sha256": image_hash}
            results.append(ctx.run_unit("material-ocr-" + str(number), {"identity": identity},
                lambda identity=identity: ocr.recognize(ctx, image_path=str(image),
                    image_sha256="f" * 64 if preflight else image_hash, identity=identity, options=options)))
        return {"results": results}

    root = tmp_path / "runtime"
    executor = Executor(root, [NodeSpec("N3", "ocr-diagnostic-test-v1", recognize),
                               NodeSpec("N6", "finish-v1", lambda ctx, data: data)], {"debug_capture": capture})
    run_id = executor.create_run({})
    state = executor.execute(run_id)
    return executor, run_id, state, calls


def _codes(report):
    return {row["code"] for row in report["gaps"]}


def _capture(executor, state, prefix):
    attempt = state["nodes"][0]["attempts"][0]
    return next(row for row in attempt["captures"] if row["name"].startswith(prefix))


def _replace_capture(executor, run_id, state, prefix, mutate):
    record = _capture(executor, state, prefix)
    value = executor.store.read_json(record["ref"])
    mutate(value)
    record["ref"] = executor.store.put_json(run_id, "changed-capture", value)
    record["hash"] = record["ref"]["sha256"]
    executor.store.write_state(state)


def test_one_success_has_flat_timing_hash_trace_and_artifact_index_without_content(tmp_path, monkeypatch):
    executor, run_id, state, calls = _runner(tmp_path, monkeypatch)
    assert state["status"] == "succeeded"
    before = {str(path): path.read_bytes() for path in executor.store.run_dir(run_id).rglob("*") if path.is_file()}
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["evidence_status"] == report["local_ocr_evidence_status"] == "available", report["gaps"]
    assert report["local_ocr_schema_version"] == "material.local-ocr.diagnostic-index.v1"
    assert report["actual_local_ocr_attempts"] == report["local_ocr_completed_attempts"] == len(calls) == 1
    assert report["local_ocr_failed_attempts"] == report["actual_call_attempts"] == 0
    assert report["calls"] == [] and state.get("call_budget") is None
    row, = report["local_ocr_calls"]
    assert row["outcome"] == "completed" and row["elapsed_ms"] >= 0 and row["span_duration_ms"] >= 0
    assert row["source_sha256"] == "c" * 64 and row["selection_sha256"] == "d" * 64
    assert row["selected_image_sha256"] == calls[0]["image_sha256"]
    assert all(len(row[key]) == 64 for key in ("request_sha256", "raw_response_sha256", "validated_output_sha256"))
    assert row["transcription_status"] == "ocr_transcribed_unverified"
    assert row["trace_id"] and row["span_id"] and row["unit_attempt_id"]
    assert row["captures"]["request"]["integrity"] == "valid"
    assert row["captures"]["response"]["integrity"] == "valid"
    assert row["captures"]["validation"]["integrity"] == "valid"
    flat = json.dumps(report, ensure_ascii=False)
    assert CANARY not in flat and PATH_CANARY not in flat
    after = {str(path): path.read_bytes() for path in executor.store.run_dir(run_id).rglob("*") if path.is_file()}
    assert before == after


def test_empty_success_is_indexed_as_unresolved_transcription(tmp_path, monkeypatch):
    executor, run_id, _, _ = _runner(tmp_path, monkeypatch, empty=True)
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["local_ocr_calls"][0]["transcription_status"] == "ocr_no_text_unresolved"
    assert report["local_ocr_evidence_status"] == "available"


def test_timeout_failure_and_resume_keep_each_real_attempt_and_reuse_success(tmp_path, monkeypatch):
    executor, run_id, state, calls = _runner(tmp_path, monkeypatch, units=2, fail_once=True)
    assert state["status"] == "failed" and len(calls) == 2
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["actual_local_ocr_attempts"] == 2
    assert report["local_ocr_completed_attempts"] == report["local_ocr_failed_attempts"] == 1
    assert report["local_ocr_evidence_status"] == "available", report["gaps"]
    failure, = [row for row in report["local_ocr_calls"] if row["outcome"] == "failed"]
    assert failure["error_code"] == "MATERIAL_OCR_TIMEOUT"
    assert failure["captures"]["error"]["integrity"] == "valid" and failure["captures"]["response"] is None
    assert CANARY not in json.dumps(report, ensure_ascii=False)
    assert executor.resume(run_id)["status"] == "succeeded"
    resumed = inspect_diagnostics(executor.store.root, run_id)
    assert len(calls) == resumed["actual_local_ocr_attempts"] == 3
    assert resumed["local_ocr_completed_attempts"] == 2 and resumed["local_ocr_failed_attempts"] == 1
    assert resumed["local_ocr_evidence_status"] == "available", resumed["gaps"]
    assert len({row["unit_attempt_id"] for row in resumed["local_ocr_calls"]}) == 3
    assert resumed["actual_call_attempts"] == resumed["known_estimated_cost_usd"] == 0


def test_preflight_failure_is_visible_but_not_counted_as_recognition(tmp_path, monkeypatch):
    executor, run_id, state, calls = _runner(tmp_path, monkeypatch, preflight=True)
    assert state["status"] == "failed" and calls == []
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["actual_local_ocr_attempts"] == report["actual_call_attempts"] == 0
    failure, = report["local_ocr_preflight_failures"]
    assert failure["outcome"] == "preflight_failed_no_recognition_attempt"
    assert failure["error_code"] == "MATERIAL_OCR_IMAGE_HASH_MISMATCH"
    assert failure["evidence_status"] == "available", report["gaps"]
    assert failure["error_capture"]["integrity"] == "valid"


def test_disabled_capture_explicitly_blocks_local_call_and_records_limitation(tmp_path, monkeypatch):
    executor, run_id, state, calls = _runner(tmp_path, monkeypatch, capture=False)
    assert state["status"] == "failed" and calls == []
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["actual_local_ocr_attempts"] == 0
    assert report["local_ocr_evidence_status"] == "incomplete"
    assert "LOCAL_OCR_CAPTURE_UNAVAILABLE" in _codes(report)
    assert any(gap.get("capture_status") == "disabled_by_policy" for gap in report["gaps"])


@pytest.mark.parametrize("status", ["partial", "write_failed", "disabled_by_policy"])
def test_noncomplete_capture_status_is_not_reported_as_adequate_evidence(tmp_path, monkeypatch, status):
    executor, run_id, state, _ = _runner(tmp_path, monkeypatch)
    _capture(executor, state, "ocr-request-")["status"] = status
    executor.store.write_state(state)
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["local_ocr_calls"][0]["evidence_status"] == "incomplete"
    assert any(gap.get("code") == "LOCAL_OCR_CAPTURE_UNAVAILABLE" and gap.get("capture_status") == status
               for gap in report["gaps"])


@pytest.mark.parametrize("kind,code", [
    ("request", "LOCAL_OCR_REQUEST_HASH_MISMATCH"),
    ("response", "LOCAL_OCR_RESPONSE_HASH_MISMATCH"),
    ("validation", "LOCAL_OCR_VALIDATION_HASH_MISMATCH"),
])
def test_capture_content_with_new_valid_artifact_hash_still_must_match_call_evidence(tmp_path, monkeypatch, kind, code):
    executor, run_id, state, _ = _runner(tmp_path, monkeypatch)
    if kind == "request":
        mutate = lambda value: value["request"].update(image_sha256="f" * 64)
    elif kind == "response":
        mutate = lambda value: value.update(stdout=value["stdout"] + " ")
    else:
        mutate = lambda value: value["validated_output"].update(text="CHANGED")
    _replace_capture(executor, run_id, state, "ocr-" + kind + "-", mutate)
    report = inspect_diagnostics(executor.store.root, run_id)
    assert code in _codes(report) and report["local_ocr_evidence_status"] == "incomplete"


def test_capture_from_another_unit_attempt_cannot_be_borrowed(tmp_path, monkeypatch):
    executor, run_id, state, _ = _runner(tmp_path, monkeypatch)
    _capture(executor, state, "ocr-request-")["unit_attempt_id"] = "f" * 32
    executor.store.write_state(state)
    report = inspect_diagnostics(executor.store.root, run_id)
    assert "LOCAL_OCR_CAPTURE_MISSING_OR_DUPLICATE" in _codes(report)


@pytest.mark.parametrize("change,code", [
    ("missing", "LOCAL_OCR_TRACE_MISSING_OR_DUPLICATE"),
    ("hash", "LOCAL_OCR_TRACE_IDENTITY_MISMATCH"),
    ("parent", "LOCAL_OCR_TRACE_IDENTITY_MISMATCH"),
    ("identity", "LOCAL_OCR_TRACE_IDENTITY_MISMATCH"),
])
def test_call_span_missing_or_changed_is_not_reported_healthy(tmp_path, monkeypatch, change, code):
    executor, run_id, _, _ = _runner(tmp_path, monkeypatch)
    path = executor.store.run_dir(run_id) / "spans.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    span = next(row for row in rows if row["name"] == "ocr.local.call")
    if change == "missing":
        rows.remove(span)
    elif change == "hash":
        span["attributes"]["output_hash"] = "f" * 64
    elif change == "parent":
        span["parent_span_id"] = "0" * 16
    else:
        # Keep the grouping identity and mutate its node attempt relationship.
        span["attributes"]["node_attempt_id"] = "f" * 32
    path.write_bytes(b"\n".join(json_bytes(row) for row in rows) + b"\n")
    report = inspect_diagnostics(executor.store.root, run_id)
    assert code in _codes(report) or (change == "identity" and "LOCAL_OCR_TRACE_MISSING_OR_DUPLICATE" in _codes(report))
    assert report["local_ocr_evidence_status"] == "incomplete"
    assert report["actual_local_ocr_attempts"] == 1


def test_missing_terminal_event_and_orphan_span_remain_visible(tmp_path, monkeypatch):
    executor, run_id, _, _ = _runner(tmp_path, monkeypatch)
    path = executor.store.run_dir(run_id) / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_bytes(b"\n".join(json_bytes(row) for row in rows if not row["event"].startswith("ocr.local.")) + b"\n")
    report = inspect_diagnostics(executor.store.root, run_id)
    assert report["actual_local_ocr_attempts"] == report["local_ocr_unknown_attempts"] == 1
    assert "LOCAL_OCR_ATTEMPT_INCOMPLETE" in _codes(report)


def test_n3_inheritance_links_origin_without_recounting_local_calls(tmp_path, monkeypatch):
    executor, run_id, state, calls = _runner(tmp_path, monkeypatch)
    child = executor.fork(run_id, "N6")
    assert executor.execute(child)["status"] == "succeeded"
    report = inspect_diagnostics(executor.store.root, child)
    assert len(calls) == 1 and report["actual_local_ocr_attempts"] == report["actual_call_attempts"] == 0
    assert report["local_ocr_calls"] == []
    inherited, = report["inherited_local_ocr_sources"]
    assert inherited["node_key"] == "N3" and inherited["origin_run_id"] == run_id
    assert inherited["origin_output"]["sha256"] == state["nodes"][0]["output_ref"]["sha256"]
    assert inherited["new_local_ocr_attempts"] == 0
