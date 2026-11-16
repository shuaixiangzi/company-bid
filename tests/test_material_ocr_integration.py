"""DeepSeek-only N3 wiring, visible failures and source-bound numeric checks."""
from __future__ import annotations

import copy
import json
import socket

import pytest

from company_bid import material_content_index, material_deepseek_ocr, material_ocr_index, material_ocr_replay
from company_bid.diagnostics import inspect_diagnostics
from company_bid.evidence import EvidenceSelection, prepare_evidence
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import digest, json_bytes
from test_material_ocr_index import _manifest, _pdf
from test_material_ocr_replay import original


SECRET = "synthetic-credential-integration-0123456789"
CANARY = "PRIVATE_OCR_INTEGRATION_FIELD"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Integration tests must not reach the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _payload(text=CANARY, finish="stop"):
    return {"id": "ocr-integration-001", "object": "ocr.result", "created": 1,
            "purpose": "rich_layout_recognition", "page_count": 1,
            "pages": [{"index": 0, "finish_reason": finish, "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}


def _block(text="", box=(10, 20, 200, 300)):
    return "<｜ref｜>text<｜/ref｜><｜box｜>" + json.dumps([box]) + "<｜/box｜> " + text + "\n"


def _review(identity, reference=None):
    reference = reference or {"image_sha256": identity["image_sha256"], "checks": [
        {"field_id": "area-001", "kind": "measure", "expected_text": "740 m²", "region_index": 0}]}
    return {**{k: identity[k] for k in ("source_id", "source_sha256", "selection_sha256", "image_sha256")},
            "review_method": "agent_source_image_review", "review_id": "synthetic-source-review-001",
            "reference": reference, "reference_sha256": digest(reference)}


def _options():
    result = {"engine": "deepseek_ocr", "max_run_calls": 8, "max_run_usd": ".012", "timeout_seconds": 5}
    result["expected_engine_fingerprint"] = material_deepseek_ocr.engine_fingerprint(result)
    return result


def _pipeline(tmp_path, monkeypatch, response=None, *, mode="live", with_review=False, credential=True,
              configure=None):
    """Real source rendering and N3 projection; only the network is stubbed."""
    root = tmp_path / "runtime"
    live_calls = []
    if credential:
        monkeypatch.setenv("COMPANY_BID_DEEPSEEK_API_KEY", SECRET)
    else:
        monkeypatch.delenv("COMPANY_BID_DEEPSEEK_API_KEY", raising=False)
    def transport(body, media, secret, timeout):
        live_calls.append(1)
        assert mode == "live", "A recorded failure must not fall back to live OCR"
        return 200, {"x-request-id": "integration-request-001"}, json_bytes(response or _payload())
    monkeypatch.setattr(material_deepseek_ocr, "_transport", transport)
    if mode.startswith("replay"):
        def forbidden_live(*args, **kwargs):
            pytest.fail("Recorded OCR may not fall back to the live adapter")
        monkeypatch.setattr(material_deepseek_ocr, "recognize", forbidden_live)
    if mode == "replay_quality":
        def recorded(ctx, **kwargs):
            return copy.deepcopy(response), {"origin_call_id": "ocrcompare-synthetic001", "origin_run_id": "1" * 32}
        monkeypatch.setattr(material_ocr_replay, "replay_material_ocr", recorded)

    def n0(ctx, value):
        manifest = copy.deepcopy(value["manifest"])
        proof = prepare_evidence(ctx, manifest["sources"], manifest["evidence_selections"], root)
        manifest["sources"].extend(proof["sources"])
        manifest["attachments"].extend(proof["attachments"])
        return {"manifest": manifest}

    def n3(ctx, value):
        manifest = value["manifest"]
        index = material_content_index.build_material_content_index(ctx, manifest, {"documents": []})
        options = _options()
        image = manifest["attachments"][0]
        selection = EvidenceSelection.model_validate(manifest["evidence_selections"][0])
        identity = {"source_id": selection.source_id, "source_sha256": selection.source_sha256,
                    "image_sha256": image["sha256"], "selection_sha256": digest(selection.model_dump(mode="json"))}
        if mode.startswith("replay"):
            options["recorded_sources"] = {image["sha256"]: {
                "run_id": "0" * 32, "node_key": "OCR_COMPARISON", "unit_id": "compare-synthetic001"}}
        if with_review:
            options["source_checks"] = {image["sha256"]: _review(identity)}
        if configure:
            options.update(configure(image["sha256"], identity))
        result = material_ocr_index.transcribe_selected_index(ctx, manifest, index, options)
        return {"index": result}

    executor = Executor(root, [NodeSpec("N0", "integration-proof.v1", n0),
                               NodeSpec("N3", "integration-material-ocr.v1", n3)], {"debug_capture": True})
    run = executor.create_run({"manifest": _manifest(_pdf(tmp_path), crop=(.1, .1, .9, .9), rotation=90)})
    state = executor.execute(run)
    return executor, run, state, live_calls


@pytest.mark.parametrize("engine", ["windows_ocr", "paddle_ocr", "tesseract", None, ""])
def test_settings_reject_every_non_deepseek_engine(engine):
    with pytest.raises(material_content_index.MaterialContentFailure, match="MATERIAL_OCR_DEEPSEEK_REQUIRED_NO_LOCAL_FALLBACK"):
        material_ocr_index._settings({"engine": engine})


def test_settings_routes_provider_replay_and_review_without_mutating_input():
    options = {"engine": "deepseek_ocr", "purpose": "rich_layout_recognition",
               "recorded_sources": {"a" * 64: {"run_id": "1" * 32, "node_key": "OCR_COMPARISON",
                                                "unit_id": "compare-synthetic001"}},
               "source_checks": {"a" * 64: {"review_id": "q1"}}}
    before = copy.deepcopy(options)
    provider, bindings, reviews = material_ocr_index._settings(options)
    assert provider == {"engine": "deepseek_ocr", "purpose": "rich_layout_recognition"}
    assert bindings == options["recorded_sources"] and reviews == options["source_checks"]
    assert options == before


@pytest.mark.parametrize("mapping", [
    {"recorded_sources": None},
    {"recorded_sources": {"a" * 64: None}},
    {"recorded_sources": {"a" * 64: {}}},
    {"recorded_sources": {"a" * 64: {"run_id": "1" * 32}}},
    {"recorded_sources": {"a" * 64: {"run_id": "1" * 32, "node_key": "N3", "unit_id": "u1"}}},
    {"source_checks": None},
    {"source_checks": {"a" * 64: None}},
    {"source_checks": {"a" * 64: {}}},
])
def test_settings_reject_explicit_invalid_bindings_instead_of_erasing_intent(mapping):
    with pytest.raises(material_content_index.MaterialContentFailure,
                       match="MATERIAL_OCR_REVIEW_OR_REPLAY_CONFIG_INVALID"):
        material_ocr_index._settings({"engine": "deepseek_ocr", **mapping})


@pytest.mark.parametrize("mapping,value", [
    ("recorded_sources", None), ("recorded_sources", {}),
    ("recorded_sources", {"run_id": "1" * 32}), ("source_checks", None),
])
def test_invalid_selected_image_binding_never_becomes_a_live_request(monkeypatch, tmp_path, mapping, value):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch,
        configure=lambda image_sha, identity: {mapping: {image_sha: value}})
    assert state["status"] == "failed"
    assert state["error_code"] == "MATERIAL_OCR_REVIEW_OR_REPLAY_CONFIG_INVALID"
    assert calls == []
    assert inspect_diagnostics(ex.store.root, run)["deepseek_ocr_actual_attempts"] == 0
    assert not state.get("call_budget", {}).get("reservations")


@pytest.mark.parametrize("mapping", ["recorded_sources", "source_checks"])
def test_unknown_image_scope_is_rejected_before_any_selected_image_is_called(monkeypatch, tmp_path, mapping):
    def configuration(image_sha, identity):
        assert image_sha != "f" * 64
        value = ({"run_id": "1" * 32, "node_key": "OCR_COMPARISON", "unit_id": "compare-other"}
                 if mapping == "recorded_sources" else _review(identity))
        return {mapping: {"f" * 64: value}}
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, configure=configuration)
    assert state["status"] == "failed"
    assert state["error_code"] == "MATERIAL_OCR_UNUSED_REPLAY_OR_REVIEW_SCOPE"
    assert calls == []
    assert inspect_diagnostics(ex.store.root, run)["deepseek_ocr_actual_attempts"] == 0
    assert not state.get("call_budget", {}).get("reservations")


@pytest.mark.parametrize("field", ["source_id", "source_sha256", "selection_sha256", "image_sha256"])
def test_review_rejects_changed_source_scope(field):
    identity = {"source_id": "SYNTHETIC", "source_sha256": "a" * 64,
                "selection_sha256": "b" * 64, "image_sha256": "c" * 64}
    review = _review(identity)
    review[field] = "changed" if field == "source_id" else "d" * 64
    with pytest.raises(material_content_index.MaterialContentFailure, match="MATERIAL_OCR_REVIEW_SOURCE_CHANGED"):
        material_ocr_index._reviewed_reference(review, identity)


@pytest.mark.parametrize("change,code", [
    (lambda r: r.update(review_method="model_guess"), "MATERIAL_OCR_REVIEW_PROVENANCE_REQUIRED"),
    (lambda r: r.update(review_id=""), "MATERIAL_OCR_REVIEW_PROVENANCE_REQUIRED"),
    (lambda r: r.update(reference_sha256="0" * 64), "MATERIAL_OCR_REVIEW_HASH_INVALID"),
    (lambda r: r["reference"]["checks"][0].update(expected_text="741 m²"), "MATERIAL_OCR_REVIEW_HASH_INVALID"),
    (lambda r: r.update(extra="unfrozen"), "MATERIAL_OCR_REVIEW_SCHEMA_INVALID"),
])
def test_review_requires_frozen_hash_and_review_provenance(change, code):
    identity = {"source_id": "SYNTHETIC", "source_sha256": "a" * 64,
                "selection_sha256": "b" * 64, "image_sha256": "c" * 64}
    review = _review(identity)
    change(review)
    with pytest.raises(material_content_index.MaterialContentFailure, match=code):
        material_ocr_index._reviewed_reference(review, identity)


def test_valid_review_returns_copy_not_mutable_configuration():
    identity = {"source_id": "SYNTHETIC", "source_sha256": "a" * 64,
                "selection_sha256": "b" * 64, "image_sha256": "c" * 64}
    review = _review(identity)
    actual = material_ocr_index._reviewed_reference(review, identity)
    actual["checks"][0]["expected_text"] = "changed"
    assert review["reference"]["checks"][0]["expected_text"] == "740 m²"


def test_live_deepseek_n3_projection_and_diagnostic_summary(monkeypatch, tmp_path):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, _payload(_block("面积740m²")))
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == [1]
    output = ex.output(run)["index"]
    assert output["coverage"]["fallback_enabled"] is False
    assert output["coverage"]["ocr_transcribed_unverified_count"] == 1
    assert output["coverage"]["ocr_numeric_pending_unit_count"] == 1
    assert output["units"][0]["numeric_validation"]["automatic_numeric_adoption"] is False
    source_unit = output["parsed"]["documents"][0]["units"][0]
    ocr_block = next(b for b in source_unit["blocks"] if b["origin"] == "ocr")
    assert ocr_block["bbox"] is not None
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == 1
    assert diagnosis["deepseek_ocr_failed_attempts"] == 0
    assert diagnosis["actual_local_ocr_attempts"] == 0
    assert diagnosis["material_ocr_local_fallback_enabled"] is False
    assert diagnosis["deepseek_ocr_replay_or_preflight_failures"] == []


@pytest.mark.parametrize("payload,review,code", [
    (_payload(_block("") * 5), False, "MATERIAL_OCR_REPETITIVE_OUTPUT"),
    (_payload(_block("partial"), "length"), False, "MATERIAL_OCR_OUTPUT_INCOMPLETE"),
    (_payload(_block("面积740m3")), True, "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"),
])
def test_live_quality_failures_stop_n3_and_persist_failed_unit(monkeypatch, tmp_path, payload, review, code):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, payload, with_review=review)
    assert state["status"] == "failed" and state["error_code"] == code
    node = state["nodes"][1]
    assert node["status"] == "failed"
    unit, = [u for key, u in node["units"].items() if key.startswith("material-ocr-")]
    assert unit["status"] == "failed" and len(unit["attempts"]) == 1
    assert calls == [1]
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == diagnosis["deepseek_ocr_failed_attempts"] == 1
    assert diagnosis["deepseek_ocr_calls"][0]["error_code"] == code
    assert diagnosis["actual_local_ocr_attempts"] == 0
    assert call_budget_summary(state["call_budget"])["effective_budget_charge_usd"] == "0.0015"


def test_missing_credential_preflight_failure_is_visible_without_any_call(monkeypatch, tmp_path):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, credential=False)
    assert state["status"] == "failed" and state["error_code"] == "MATERIAL_OCR_CREDENTIAL_MISSING"
    assert calls == []
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == 0
    failures = diagnosis["deepseek_ocr_replay_or_preflight_failures"]
    assert len(failures) == 1 and failures[0]["error_code"] == "MATERIAL_OCR_CREDENTIAL_MISSING"
    assert failures[0]["unit_id"].startswith("material-ocr-")


@pytest.mark.parametrize("payload,review,code", [
    (_payload(_block("") * 5), False, "MATERIAL_OCR_REPETITIVE_OUTPUT"),
    (_payload(_block("partial"), "length"), False, "MATERIAL_OCR_OUTPUT_INCOMPLETE"),
    (_payload(_block("面积740m3")), True, "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"),
])
def test_recorded_quality_rejection_is_visible_and_never_calls_live(monkeypatch, tmp_path, payload, review, code):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, payload, mode="replay_quality", with_review=review)
    assert state["status"] == "failed" and state["error_code"] == code
    assert calls == []
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == 0
    failures = diagnosis["deepseek_ocr_replay_or_preflight_failures"]
    assert len(failures) == 1 and failures[0]["error_code"] == code
    assert failures[0]["trace_id"] and failures[0]["span_id"]
    assert not state.get("call_budget", {}).get("reservations")


def test_recorded_authentication_failure_visible_without_live_fallback(monkeypatch, tmp_path):
    ex, run, state, calls = _pipeline(tmp_path, monkeypatch, mode="replay_authentication")
    assert state["status"] == "failed" and state["error_code"].startswith("MATERIAL_OCR_REPLAY_")
    assert calls == []
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == 0
    failures = diagnosis["deepseek_ocr_replay_or_preflight_failures"]
    assert len(failures) == 1 and failures[0]["error_code"] == state["error_code"]


def test_authenticated_recorded_response_runs_current_validator_and_is_diagnosable(original, monkeypatch):
    spec = original["specs"][0]
    def forbidden(*args, **kwargs):
        pytest.fail("Authenticated replay must not call live OCR")
    monkeypatch.setattr(material_deepseek_ocr, "recognize", forbidden)
    options = _options()
    def handler(ctx, initial):
        return ctx.run_unit("material-ocr-replayed", initial, lambda: material_ocr_index._recorded(
            ctx, {"run_id": original["rid"], "node_key": "OCR_COMPARISON", "unit_id": spec["unit_id"]},
            spec["identity"], spec["dimensions"], options, options["expected_engine_fingerprint"], None))
    ex = Executor(original["root"], [NodeSpec("N3", "current-replay-quality-v1", handler)], {"debug_capture": True})
    run = ex.create_run({"image_sha256": spec["identity"]["image_sha256"]})
    state = ex.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    value = ex.output(run)
    assert value["actual_new_api_calls"] == 0 and value["recorded_response"]["origin_run_id"] == original["rid"]
    assert value["numeric_validation"]["automatic_numeric_adoption"] is False
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["deepseek_ocr_actual_attempts"] == 0
    assert len(diagnosis["deepseek_ocr_replayed_sources"]) == 1
    assert diagnosis["deepseek_ocr_replayed_sources"][0]["origin_run_id"] == original["rid"]
    assert diagnosis["deepseek_ocr_replay_or_preflight_failures"] == []
    assert not state.get("call_budget", {}).get("reservations")
