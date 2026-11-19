"""Authenticate an actual N1 OCR response in the same controlled runtime."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path

from . import material_deepseek_ocr as official
from .material_ocr_replay import _OCROrigin, _request_body
from .observability import redact_payload
from .provider import ProviderFailure
from .runtime import call_budget_summary
from .storage import digest


VERSION = "tender-ocr-recorded.v1"
REVIEWABLE_FAILURE = "OCR_IMAGE_REGION_REVIEW_REQUIRED"
_CALL_EVENTS = {"call.started", "call.completed", "call.failed"}


def _require(valid, reason):
    if not valid:
        raise ProviderFailure("OCR_REPLAY_" + reason)


def _one(values, reason):
    found = list(values)
    _require(len(found) == 1, reason)
    return found[0]


def _same(row, expected):
    return all(row.get(key) == value for key, value in expected.items())


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class _TenderOrigin(_OCROrigin):
    def __init__(self, store, run_id):
        from .tender_deepseek_ocr import validate_options

        self.store, self.root, self.run_id = store, store.root, run_id
        self.evidence = {}
        self.source_refs = []
        self.state_raw = self._file("state.json")
        self.state = json.loads(self.state_raw)
        _require(_same(self.state, {"schema_version": "m1.local.v1", "run_id": run_id,
                                  "artifact_scope": "diagnostic", "capture_policy": True})
                 and self.state["status"] in {"succeeded", "failed", "paused_debug"}, "SOURCE_NOT_SEALED")
        config = self.state["execution_config"]
        runs = self.state["execution_attempts"]
        _require(self.state["config_hash"] == digest(config)
                 and _same(config, {"pipeline": "automatic", "run_purpose": "diagnostic_replay"})
                 and _same(self.state["observability"], {"trace_status": "captured", "capture_status": "captured"})
                 and runs and all(r.get("ended_at") and r.get("trace_status") == "captured" and
                                  r.get("status") in {"succeeded", "failed", "paused_debug"} for r in runs),
                 "SOURCE_DIAGNOSTICS_INVALID")
        self.config, self.options = validate_options(config["ocr"])
        self.fingerprint = official.engine_fingerprint(self.options)
        _require(self.config.recorded_targets is None, "ACTUAL_CALL_REQUIRED")
        self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")

    def _file(self, name):
        directory = self.store.run_dir(self.run_id)
        path = directory / name
        _require(path.resolve(strict=True).parent == directory and path.stat().st_size <= 30_000_000, "ORIGIN_FILE_LIMIT")
        return super()._file(name)

    def target(self, unit_id, allowed_failures):
        node, unit, inputs, _, _ = self.unit("N1", unit_id, successful=False)
        manifest = _one((item for item in self.state["node_manifest"] if item["key"] == "N1"), "NODE_VERSION_INVALID")
        _require(manifest["version"] == node["version"] and node["status"] in {"succeeded", "failed"}, "NODE_VERSION_INVALID")
        state = unit["status"]
        expected_failed = state == "failed"
        attempt = _one(unit["attempts"], "SINGLE_ATTEMPT_REQUIRED")
        _require(state in {"failed", "succeeded"} and attempt["status"] == state and attempt.get("ended_at") and
                 attempt["input_hash"] == unit["input_hash"], "SOURCE_UNIT_INVALID")
        parent = _one((a for a in node["attempts"] if a["attempt_id"] == attempt["node_attempt_id"]), "NODE_ATTEMPT_INVALID")
        _require(parent.get("ended_at") and parent["status"] in {"succeeded", "failed"} and
                 parent["trace_id"] == attempt["trace_id"], "NODE_ATTEMPT_INVALID")
        span = self.span(attempt["trace_id"], attempt["span_id"])
        _require(span["name"] == "unit.N1" and (span["status"] == "ERROR") == expected_failed and
                 _same(span["attributes"], {"unit_id": unit_id, "unit_attempt_id": attempt["attempt_id"],
                     "node_attempt_id": parent["attempt_id"], "input_hash": unit["input_hash"]}), "SOURCE_UNIT_TRACE_INVALID")
        if expected_failed:
            _require(attempt.get("error_code") in allowed_failures and not unit.get("output_ref"), "SOURCE_UNIT_INVALID")
            output = None
        else:
            checkpoint = self.checkpoint(unit, "N1", unit_id)
            _require(checkpoint["node_attempt_id"] == parent["attempt_id"] and
                     attempt["output_hash"] == span["attributes"].get("output_hash") == unit["output_hash"],
                     "SOURCE_UNIT_INVALID")
            output = checkpoint["data"]
        seen = set()
        while span["parent_span_id"] != parent["span_id"]:
            span_id = span["parent_span_id"]
            _require(span_id not in seen and len(seen) < 8, "SOURCE_UNIT_TRACE_INVALID")
            seen.add(span_id)
            span = self.span(attempt["trace_id"], span_id)
            outer = _one((a for u in node["units"].values() for a in u["attempts"] if a.get("span_id") == span_id and
                          a.get("trace_id") == attempt["trace_id"]), "SOURCE_UNIT_TRACE_INVALID")
            _require(span["name"] == "unit.N1" and outer.get("ended_at") and outer["status"] in {"failed", "succeeded"}
                     and span["attributes"].get("unit_attempt_id") == outer["attempt_id"] and
                     outer["node_attempt_id"] == parent["attempt_id"], "SOURCE_UNIT_TRACE_INVALID")
        node_span = self.span(attempt["trace_id"], parent["span_id"])
        _require(node_span["name"] == "node.N1" and node_span["attributes"].get("node_attempt_id") == parent["attempt_id"],
                 "SOURCE_UNIT_TRACE_INVALID")
        return node, inputs, output, attempt, expected_failed

    def source(self, identity, source_path):
        node = self.node("N0")
        _require(node["status"] == "succeeded", "SOURCE_FREEZE_INVALID")
        frozen = self.checkpoint(node, "N0")["data"]
        source = _one((item for item in frozen["manifest"]["sources"] if item["source_id"] == identity["source_id"]),
                      "SOURCE_FREEZE_INVALID")
        ref = _one((ref for ref in frozen["source_artifacts"] if ref["sha256"] == identity["source_sha256"]),
                   "SOURCE_FREEZE_INVALID")
        _require(source["sha256"] == ref["sha256"] and source["path"] == source_path and
                 Path(source_path).resolve() == self.store.resolve(ref["path"]), "SOURCE_FREEZE_INVALID")
        self.store.read_bytes(ref)
        self.source_refs.append(ref)
        return ref

    def unchanged(self):
        super().unchanged()
        for ref in self.source_refs:
            self.store.read_bytes(ref)


def _authenticate(origin, binding, image, preparation, purpose, *, allowed_failures=(REVIEWABLE_FAILURE,)):
    identity, raster, purchase_key = (preparation[key] for key in ("identity", "renderer", "purchase_key"))
    unit_id, call_id = binding["unit_id"], binding["call_id"]
    node, inputs, output, attempt, expected_failed = origin.target(unit_id, allowed_failures)
    provider_error = attempt.get("error_code") if expected_failed else None
    if provider_error == "OCR_OUTPUT_INCOMPLETE":
        provider_error = "MATERIAL_OCR_OUTPUT_INCOMPLETE"
    _require(unit_id == "tender-ocr-" + digest(identity) and
             _same(inputs, {"identity": identity, "renderer": raster, "options": origin.options,
                 "engine_fingerprint": origin.fingerprint, "purchase_key": purchase_key}) and
             origin.fingerprint == preparation["engine_fingerprint"], "TARGET_MISMATCH")
    source_ref = origin.source(identity, inputs["source_path"])
    captures = origin.captures(node, unit_id, attempt)
    prep = origin.capture(captures, "ocr-preparation")
    _require(prep == {**{key: value for key, value in inputs.items() if key != "options"}, "image": prep["image"]}
             and origin.binary(prep["image"]) == image, "TARGET_RASTER_MISMATCH")
    image_sha = identity["image_sha256"]
    frozen_target = origin.config.live_targets
    if frozen_target is not None:
        actual_target = {"source_sha256": identity["source_sha256"], "locator": identity["locator"],
            "page": identity["page"], "object_kind": identity["object_kind"], "bbox": identity["bbox"],
            "image_sha256": identity["embedded_image_sha256"], "package_part": identity["package_part"],
            "rendered_sha256": image_sha, "required_text": inputs["required_text"]}
        _require(sum(entry.model_dump(mode="json") == actual_target for entry in frozen_target) == 1,
                 "ORIGIN_TARGET_NOT_APPROVED")
    preflight_name = _one((c["name"] for c in captures if c["name"].startswith("ocr-preflight-") and
                           not c["name"].startswith("ocr-preflight-error-")), "PREFLIGHT_INVALID")
    preflight = origin.capture(captures, preflight_name)
    _require(preflight == redact_payload({"identity": identity, "options": origin.options, "image_sha256": image_sha,
                                         "numeric_reference": None}), "PREFLIGHT_INVALID")
    request = origin.capture(captures, "request-" + call_id)
    dimensions = [raster["width"], raster["height"]]
    _require(_same(request, {"call_id": call_id, "operation": "tender_ocr", "purchase_key": purchase_key,
        "endpoint": official.ENDPOINT, "purpose": purpose, "identity": identity, "image_sha256": image_sha,
        "expected_pages": 1, "engine_fingerprint": origin.fingerprint, "image_dimensions": dimensions,
        "automatic_retry": False, "local_fallback": False}), "REQUEST_INVALID")
    body = origin.binary(request["request_body_ref"])
    body_sha = _sha(body)
    _require(body == _request_body(image, image_sha, purpose) and request["body_sha256"] == body_sha, "REQUEST_BYTES_CHANGED")
    response = origin.capture(captures, "response-" + call_id)
    raw = origin.binary(response["raw_response_ref"])
    raw_sha = _sha(raw)
    _require(len(raw) <= official.MAX_RESPONSE_BYTES and raw_sha == binding["raw_response_sha256"] and
             _same(response, {"call_id": call_id, "http_status": 200, "raw_capture_status": "captured",
                              "raw_received_sha256": raw_sha}), "RESPONSE_CAPTURE_INVALID")
    payload = json.loads(raw, object_pairs_hook=official._unique_json_object)
    try:
        official.validate_provider_response(payload, purpose)
    except official.MaterialDeepSeekOCRFailure as exc:
        # Only a source-image review can cite a truncated response as a historical failure.
        # Ordinary OCR replay still requires a completed, schema-valid response.
        _require(provider_error == exc.code == "MATERIAL_OCR_OUTPUT_INCOMPLETE" and
                 official.returned_billable_pages(200, payload, purpose) == 1 and
                 payload["pages"][0]["finish_reason"] == "length", "FAILED_RESPONSE_NOT_REVIEWABLE")
    _require(payload == response["response"], "RESPONSE_BYTES_CHANGED")

    related = [e for e in origin.events if e.get("unit_id") == unit_id and e["event"] in _CALL_EVENTS]
    _require(len(related) == 2 and related == [e for e in origin.events if e.get("call_id") == call_id and
                                             e["event"] in _CALL_EVENTS], "SINGLE_CALL_REQUIRED")
    start = _one((e for e in related if e["event"] == "call.started"), "SINGLE_CALL_REQUIRED")
    terminal = _one((e for e in related if e["event"] == ("call.failed" if expected_failed else "call.completed")),
                    "CALL_OUTCOME_INVALID")
    ids = {"call_id": call_id, "call_attempt": 1, "node_key": "N1", "node_attempt_id": attempt["node_attempt_id"],
           "unit_id": unit_id, "unit_attempt_id": attempt["attempt_id"], "input_hash": body_sha}
    call_span = {"trace_id": binding["trace_id"], "span_id": binding["span_id"]}
    provider_call = {**ids, "provider": "deepseek_official_ocr", "operation": "tender_ocr"}
    _require(all(_same(e, {**provider_call, **call_span, "run_id": origin.run_id}) for e in (start, terminal)) and
             attempt["trace_id"] == binding["trace_id"], "CALL_TRACE_INVALID")
    outcome = {"output_hash": raw_sha, "http_status": 200, "provider_request_id": payload["id"],
               "validation_status": "failed" if expected_failed else "passed",
               "error_code": provider_error, "usage_status": "not_token_billed"}
    span = origin.span(binding["trace_id"], binding["span_id"])
    _require(_same(terminal, outcome) and span["name"] == "provider.call" and
             span["parent_span_id"] == attempt["span_id"] and (span["status"] == "ERROR") == expected_failed and
             _same(span["attributes"], {**provider_call, **outcome}), "CALL_TRACE_INVALID")

    validation = origin.capture(captures, "validation-" + call_id)
    _require(_same(validation, {"call_id": call_id, "validation_status": outcome["validation_status"]}),
             "VALIDATION_CAPTURE_INVALID")
    if expected_failed:
        _require(_same(validation, {"error_code": provider_error, "http_status": 200, "raw_response_available": True,
                                "fee_pages": 1, "server_completion": "response_received", "automatic_retry": False,
                                "local_fallback": False}), "FAILED_RESPONSE_NOT_REVIEWABLE")
    else:
        recorded = validation["validated_output"]
        _require(validation["validated_output_hash"] == digest(recorded) and _same(recorded, {"actual_external_call": True,
            "response_reused": False, "local_fallback": False, "engine_fingerprint": origin.fingerprint,
            "response_raw_sha256": raw_sha, "response_raw_ref": response["raw_response_ref"], "call_id": call_id,
            "provider_request_id": payload["id"], "image_sha256": image_sha, "purpose": purpose,
            "provider_finish_reason": "stop", "billing": response["billing"]}) and
            _same(output, {"identity": identity, "image": prep["image"], "reply": recorded["tender_ocr_response"],
                            "call_id": call_id, "provider_request_id": payload["id"],
                            "response_raw_sha256": raw_sha, "response_raw_ref": response["raw_response_ref"]}),
            "VALIDATION_CAPTURE_INVALID")

    call_budget_summary(origin.state["call_budget"])
    reservation = _one((row for row in origin.state["call_budget"]["reservations"] if row.get("unit_id") == unit_id or
                        row.get("call_id") == call_id), "RESERVATION_INVALID")
    _require(_same(reservation, {**ids, "settlement_status": "reserved_unknown_actual_cost",
        "input_usd_per_million": None, "output_usd_per_million": None}) and
        Decimal(reservation["estimated_upper_usd"]) == Decimal(origin.options["reserve_usd_per_page"]) and
        _same(response["billing"], {"provider_returned_pages": 1, "reserve_usd_retained": origin.options["reserve_usd_per_page"],
                                    "provider_invoice": False}), "RESERVATION_INVALID")
    pointer_path = "ocr-purchases/" + purchase_key + ".json"
    pointer_raw = origin.store.resolve(pointer_path).read_bytes()
    pointer = json.loads(pointer_raw)
    _require(pointer == {"schema_version": "tender.ocr.purchase-intent.v1", "purchase_key": purchase_key,
                        "run_id": origin.run_id, "call_id": call_id, "input_hash": body_sha}, "PURCHASE_POINTER_INVALID")
    origin.evidence[pointer_path] = pointer_raw
    return payload, raw, body, source_ref, reservation, response["billing"], outcome["error_code"], inputs["required_text"]


def authenticate_tender_ocr(ctx, *, binding, image, preparation, purpose):
    """Return raw candidate and provenance without consulting credentials or HTTP."""
    try:
        _require(ctx.node_key == "N1" and ctx.artifact_scope == "diagnostic" and ctx._state["capture_policy"] is True and
                 ctx.execution_config.get("run_purpose") == "diagnostic_replay" and binding["run_id"] != ctx.run_id,
                 "DIAGNOSTIC_SCOPE_REQUIRED")
        origin = _TenderOrigin(ctx.executor.store, binding["run_id"])
        payload, raw, body, source, reservation, billing, prior_error, historical_required_text = _authenticate(
            origin, binding, image, preparation, purpose)
        origin.unchanged()
        raw_ref = ctx.store_bytes("tender-ocr-recorded-response.bin", raw)
        body_ref = ctx.store_bytes("tender-ocr-recorded-request.bin", body)
        _require(raw_ref["sha256"] == binding["raw_response_sha256"] and body_ref["sha256"] == reservation["input_hash"],
                 "SNAPSHOT_HASH_CHANGED")
        evidence = [{"origin_path": path, "sha256": _sha(data)} for path, data in sorted(origin.evidence.items())]
        provenance = {"version": VERSION, "origin": binding, "origin_source_ref": source, "purpose": purpose,
            "image_sha256": preparation["identity"]["image_sha256"], "historical_error_code": prior_error,
            "historical_required_text": historical_required_text,
            "historical_reservation": reservation, "historical_billing": billing, "evidence_hashes": evidence,
            "raw_response_ref": raw_ref, "request_body_ref": body_ref, "actual_external_call": False,
            "response_reused": True, "new_fee_reservations": 0}
        origin.unchanged()
        return payload, provenance
    except ProviderFailure as exc:
        if exc.code.startswith("OCR_REPLAY_"):
            raise
        raise ProviderFailure("OCR_REPLAY_SOURCE_INVALID") from None
    except Exception:
        raise ProviderFailure("OCR_REPLAY_SOURCE_INVALID") from None
