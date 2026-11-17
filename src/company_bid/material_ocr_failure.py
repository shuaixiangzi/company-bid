"""Authenticate a previously failed company-proof OCR attempt, never its text.

This explicit disposition preserves the original failure and charge. The caller
owns company-material scope/attachment checks and projects a visible read error;
it must not use this result as OCR success, a missing-material or fact conclusion.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from io import BytesIO
import json
import re

from PIL import Image

from .diagnostic_replay import _OriginRun
from .material_ocr_replay import _N3OCROrigin, _request_body, _sha
from .material_source_review import validate_material_source_review
from .observability import redact_payload
from .storage import digest


MATERIAL_OCR_FAILURE_VERSION = "material-ocr-failure.v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")
_HTTP = {408, 429, 500, 503, 504}
_TRANSPORT = {"MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN", "MATERIAL_OCR_TRANSPORT_OUTCOME_UNKNOWN"}
ALLOWED_FAILURE_CODES = frozenset(_TRANSPORT | {
    "MATERIAL_OCR_OUTPUT_INCOMPLETE", "MATERIAL_OCR_REPETITIVE_OUTPUT", "MATERIAL_OCR_REGION_LIMIT",
    "MATERIAL_OCR_TEXT_LIMIT", "MATERIAL_OCR_CONTROL_CHARACTER_INVALID", "MATERIAL_OCR_COORDINATES_INVALID",
    "MATERIAL_OCR_PAGE_COVERAGE_INVALID", "MATERIAL_OCR_RESPONSE_SCHEMA_INVALID", "MATERIAL_OCR_USAGE_INVALID",
    "MATERIAL_OCR_RESPONSE_NOT_JSON", "MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED",
    "MATERIAL_OCR_HTTP_ERROR", "MATERIAL_OCR_HTML_UNSAFE", "MATERIAL_OCR_MARKUP_INVALID",
    "MATERIAL_OCR_TABLE_INVALID", "MATERIAL_OCR_TABLE_LIMIT", "MATERIAL_OCR_TABLE_SPAN_INVALID",
    "MATERIAL_OCR_TABLE_SPAN_OVERLAP", "MATERIAL_OCR_TABLE_GRID_INCOMPLETE", "MATERIAL_OCR_NUMERIC_LIMIT",
    "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH",
})


class MaterialOCRFailureAuthenticationError(RuntimeError):
    def __init__(self, code):
        self.code = "MATERIAL_OCR_FAILURE_" + code
        super().__init__(self.code)


def _require(ok, code):
    if not ok:
        raise MaterialOCRFailureAuthenticationError(code)


def _one(items, code):
    values = list(items)
    _require(len(values) == 1, code)
    return values[0]


def _spec(specification):
    required = {"binding", "expected_error_code"}
    optional = {"source_review", "source_review_evidence"}
    _require(isinstance(specification, dict) and required <= set(specification)
             and not set(specification) - required - optional, "CONFIG_INVALID")
    binding = specification["binding"]
    _require(isinstance(binding, dict) and set(binding) == {"run_id", "node_key", "unit_id"}
             and isinstance(binding["run_id"], str) and _ID.fullmatch(binding["run_id"])
             and binding["node_key"] == "N3" and isinstance(binding["unit_id"], str)
             and re.fullmatch(r"material-ocr-[0-9a-f]{24}", binding["unit_id"]), "BINDING_INVALID")
    code = specification["expected_error_code"]
    _require(isinstance(code, str) and code in ALLOWED_FAILURE_CODES, "ERROR_NOT_ALLOWLISTED")
    _require(("source_review" in specification) == ("source_review_evidence" in specification), "REVIEW_PAIR_REQUIRED")
    if "source_review" in specification:
        validate_material_source_review(specification["source_review"])
        ref = specification["source_review_evidence"]
        _require(isinstance(ref, dict) and isinstance(ref.get("artifact_id"), str)
                 and _ID.fullmatch(ref["artifact_id"]) and isinstance(ref.get("path"), str)
                 and re.fullmatch(r"runs/[0-9a-f]{32}/artifacts/[^/\\]+", ref["path"])
                 and isinstance(ref.get("sha256"), str) and _SHA.fullmatch(ref["sha256"]), "REVIEW_EVIDENCE_INVALID")
    _require(code != "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH" or "source_review" in specification,
             "NUMERIC_CONFLICT_REVIEW_REQUIRED")
    return binding, code


def validate_failed_sources(config):
    """Reject unsafe dispositions before another selected page can incur cost."""
    try:
        _require(isinstance(config, dict) and len(config) <= 2000, "CONFIG_INVALID")
        for image_hash, spec in config.items():
            _require(isinstance(image_hash, str) and _SHA.fullmatch(image_hash), "CONFIG_INVALID")
            _spec(spec)
        return copy.deepcopy(config)
    except MaterialOCRFailureAuthenticationError:
        raise
    except Exception:
        raise MaterialOCRFailureAuthenticationError("CONFIG_INVALID") from None


def _failed_page(origin, uid, expected):
    node, unit, inputs, _, _ = _OriginRun.unit(origin, "N3", uid, successful=False)
    _require(unit.get("status") == "failed" and len(unit.get("attempts", [])) == 1
             and not unit.get("output_ref") and not unit.get("output_hash"), "UNIT_NOT_FAILED")
    attempt = unit["attempts"][0]
    _require(attempt.get("status") == "failed" and attempt.get("ended_at")
             and attempt.get("error_code") == expected and attempt.get("input_hash") == unit["input_hash"],
             "UNIT_FAILURE_CHANGED")
    parent = _one((a for a in node["attempts"] if a["attempt_id"] == attempt["node_attempt_id"]), "NODE_ATTEMPT_INVALID")
    _require(parent.get("status") in {"failed", "succeeded"} and parent.get("ended_at")
             and parent.get("trace_id") == attempt.get("trace_id"), "NODE_ATTEMPT_INVALID")
    span = origin.span(attempt["trace_id"], attempt["span_id"])
    attrs = span.get("attributes", {})
    _require(span.get("name") == "unit.N3" and span.get("status") == "ERROR"
             and all(attrs.get(k) == v for k, v in {"unit_id": uid, "unit_attempt_id": attempt["attempt_id"],
                 "node_attempt_id": parent["attempt_id"], "input_hash": unit["input_hash"]}.items()), "UNIT_TRACE_INVALID")
    seen = {attempt["span_id"]}
    while span.get("parent_span_id") != parent["span_id"]:
        sid = span.get("parent_span_id")
        _require(sid not in seen and len(seen) <= 8, "UNIT_TRACE_INVALID")
        seen.add(sid)
        span = origin.span(attempt["trace_id"], sid)
        outer = _one((a for u in node["units"].values() for a in u.get("attempts", [])
                      if a.get("span_id") == sid and a.get("trace_id") == attempt["trace_id"]), "UNIT_TRACE_INVALID")
        _require(span.get("name") == "unit.N3" and outer.get("ended_at")
                 and outer.get("node_attempt_id") == parent["attempt_id"]
                 and outer.get("status") in {"failed", "succeeded"}
                 and span.get("attributes", {}).get("unit_attempt_id") == outer.get("attempt_id"), "UNIT_TRACE_INVALID")
    node_span = origin.span(attempt["trace_id"], parent["span_id"])
    _require(node_span.get("name") == "node.N3"
             and node_span.get("attributes", {}).get("node_attempt_id") == parent["attempt_id"], "UNIT_TRACE_INVALID")
    return node, unit, inputs, attempt


def _response(origin, captures, call_id, validation, terminal, expected, identity, dimensions):
    from .material_deepseek_ocr import MAX_RESPONSE_BYTES, parse_response, returned_billable_pages

    responses = [c for c in captures if c["name"] == "response-" + call_id]
    if expected in _TRANSPORT:
        _require(not responses and validation.get("raw_response_available") is False
                 and validation.get("http_status") is None and validation.get("fee_pages") is None
                 and validation.get("server_completion") == "unknown" and terminal.get("output_hash") is None
                 and terminal.get("http_status") is None and terminal.get("usage_status") == "unknown",
                 "TRANSPORT_FAILURE_INVALID")
        return None, None, None
    _require(len(responses) == 1, "RESPONSE_CAPTURE_REQUIRED")
    response = origin.capture(captures, "response-" + call_id)
    raw = origin.binary(response["raw_response_ref"])
    _require(response.get("call_id") == call_id and type(response.get("http_status")) is int
             and response.get("raw_capture_status") == ("truncated" if len(raw) > MAX_RESPONSE_BYTES else "captured")
             and response.get("raw_received_sha256") == terminal.get("output_hash") == _sha(raw)
             and validation.get("raw_response_available") is True
             and validation.get("server_completion") == "response_received"
             and validation.get("http_status") == terminal.get("http_status") == response["http_status"],
             "RESPONSE_CAPTURE_INVALID")
    payload = None
    if len(raw) <= MAX_RESPONSE_BYTES:
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError):
            pass
    _require(response.get("response") == payload, "RESPONSE_BYTES_CHANGED")
    fee = returned_billable_pages(response["http_status"], payload, origin.options["purpose"])
    billing = response.get("billing", {})
    _require(validation.get("fee_pages") == billing.get("provider_returned_pages") == fee
             and billing.get("reserve_usd_retained") == origin.options["reserve_usd_per_page"]
             and billing.get("token_usage_is_billing") is False and billing.get("usd_is_fx_conversion") is False
             and billing.get("provider_invoice") is False
             and terminal.get("usage_status") == ("unknown" if fee is None else "not_token_billed"), "BILLING_INVALID")
    if expected == "MATERIAL_OCR_HTTP_ERROR":
        _require(response["http_status"] in _HTTP, "HTTP_STATUS_NOT_ALLOWLISTED")
    elif expected == "MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED":
        _require(len(raw) > MAX_RESPONSE_BYTES, "FAILURE_NOT_REPRODUCED")
    elif expected == "MATERIAL_OCR_RESPONSE_NOT_JSON":
        _require(response["http_status"] == 200 and payload is None, "FAILURE_NOT_REPRODUCED")
    else:
        _require(response["http_status"] == 200 and payload is not None, "FAILURE_NOT_REPRODUCED")
        observed = None
        try:
            parse_response(payload, width=dimensions[0], height=dimensions[1], image_sha256=identity["image_sha256"],
                identity=identity, options=origin.options, call_id=call_id,
                numeric_reference=origin.settings.get("source_checks", {}).get(identity["image_sha256"], {}).get("reference"))
        except Exception as error:
            observed = getattr(error, "code", None)
        _require(observed == expected, "FAILURE_NOT_REPRODUCED")
    return response["raw_response_ref"], _sha(raw), billing


def _review(origin, specification, identity, binding, expected, response_sha):
    if "source_review" not in specification:
        return None
    record = validate_material_source_review(specification["source_review"])
    _require(response_sha is not None and record["identity"] == {k: identity[k] for k in
                 ("source_id", "source_sha256", "selection_sha256", "image_sha256")}
             and record["ocr"] == {**binding, "response_sha256": response_sha, "status": "failed", "error_code": expected},
             "REVIEW_SOURCE_CHANGED")
    ref = specification["source_review_evidence"]
    raw = origin.binary(ref)
    _require(0 < len(raw) <= 1_000_000 and _sha(raw) == record["reviewer"]["evidence_sha256"], "REVIEW_EVIDENCE_CHANGED")
    evidence = json.loads(raw)
    _require(isinstance(evidence, dict), "REVIEW_EVIDENCE_INVALID")
    digest(evidence)
    if expected == "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH":
        _require(record["field"]["disposition"] == "machine_error", "NUMERIC_CONFLICT_REVIEW_REQUIRED")
    return record


def authenticate_failed_material_ocr(ctx, *, specification, image_sha256, identity, purpose):
    """Return only authenticated failure disposition; never calls a provider."""
    try:
        binding, expected = _spec(specification)
        _require(ctx.node_key == "N3" and isinstance(ctx._active_unit, dict) and ctx._active_unit.get("unit_id")
                 and ctx.execution_config.get("debug_capture", True) is True and ctx._state.get("capture_policy") is True,
                 "CAPTURED_N3_UNIT_REQUIRED")
        _require(binding["run_id"] != ctx.run_id and isinstance(identity, dict)
                 and isinstance(image_sha256, str) and _SHA.fullmatch(image_sha256)
                 and identity.get("image_sha256") == image_sha256
                 and binding["unit_id"] == "material-ocr-" + digest(identity)[:24], "IDENTITY_CHANGED")
        origin = _N3OCROrigin(ctx.executor.store, binding["run_id"])
        node, unit, inputs, attempt = _failed_page(origin, binding["unit_id"], expected)
        _require(inputs.get("identity") == identity and inputs.get("options") == origin.provider
                 and inputs.get("engine_fingerprint") == origin.fingerprint and inputs.get("recorded_source") is None
                 and image_sha256 not in origin.settings.get("recorded_sources", {})
                 and image_sha256 not in origin.settings.get("failed_sources", {})
                 and inputs.get("reviewed_numeric_reference") == origin.settings.get("source_checks", {}).get(image_sha256)
                 and purpose == origin.options["purpose"], "INPUT_CHANGED")
        captures = origin.captures(node, binding["unit_id"], attempt)
        requests = [c["name"] for c in captures if c["name"].startswith("request-")]
        request = origin.capture(captures, _one(requests, "SINGLE_REQUEST_REQUIRED"))
        call_id = request.get("call_id")
        _require(isinstance(call_id, str) and _ID.fullmatch(call_id), "CALL_ID_INVALID")
        validation = origin.capture(captures, "validation-" + call_id)
        _require(validation.get("call_id") == call_id and validation.get("validation_status") == "failed"
                 and validation.get("error_code") == expected and validation.get("automatic_retry") is False
                 and validation.get("local_fallback") is False
                 and validation.get("reserve_usd_retained") == origin.options["reserve_usd_per_page"], "VALIDATION_CHANGED")
        unit_error = origin.capture(captures, "unit-error")
        _require(unit_error.get("error_code") == expected, "UNIT_FAILURE_CHANGED")
        preflight = origin.capture(captures, _one((c["name"] for c in captures
            if c["name"].startswith("ocr-preflight-") and not c["name"].startswith("ocr-preflight-error-")), "PREFLIGHT_INVALID"))
        reviewed = inputs.get("reviewed_numeric_reference")
        _require(preflight == redact_payload({"identity": identity, "options": origin.options,
            "image_sha256": image_sha256, "numeric_reference": reviewed["reference"] if reviewed else None}), "PREFLIGHT_INVALID")
        _require(request.get("endpoint") == "https://api.deepseek.com/v1/tools/ocr"
                 and request.get("purpose") == purpose and request.get("identity") == identity
                 and request.get("image_sha256") == image_sha256 and request.get("engine_fingerprint") == origin.fingerprint
                 and request.get("automatic_retry") is False and request.get("local_fallback") is False
                 and request.get("expected_pages") == 1, "REQUEST_CHANGED")
        body = origin.binary(request["request_body_ref"])
        suffix = ("\r\n--companybidocrcomparison" + image_sha256[:24] + "--\r\n").encode("ascii")
        prefix = _request_body(b"", image_sha256, purpose)[:-len(suffix)]
        _require(body.startswith(prefix) and body.endswith(suffix), "REQUEST_CHANGED")
        image = body[len(prefix):-len(suffix)]
        _require(_sha(image) == image_sha256 and request.get("body_sha256") == _sha(body)
                 and body == _request_body(image, image_sha256, purpose), "REQUEST_CHANGED")
        with Image.open(BytesIO(image)) as png:
            dimensions = list(png.size)
            _require(png.format == "PNG" and getattr(png, "n_frames", 1) == 1, "IMAGE_INVALID")
            png.verify()
        _require(request.get("image_dimensions") == dimensions, "IMAGE_INVALID")
        related = [e for e in origin.events if e.get("unit_attempt_id") == attempt["attempt_id"]
                   and e.get("event") in {"call.started", "call.completed", "call.failed"}]
        globally = [e for e in origin.events if e.get("call_id") == call_id
                    and e.get("event") in {"call.started", "call.completed", "call.failed"}]
        _require(len(related) == 2 and related == globally, "SINGLE_CALL_REQUIRED")
        started = _one((e for e in related if e["event"] == "call.started"), "CALL_NOT_STARTED")
        terminal = _one((e for e in related if e["event"] == "call.failed"), "CALL_NOT_FAILED")
        for event in (started, terminal):
            _require(all(event.get(k) == v for k, v in {"call_id": call_id, "call_attempt": 1,
                "provider": "deepseek_official_ocr", "operation": "material_ocr", "run_id": origin.run_id,
                "node_key": "N3", "node_attempt_id": attempt["node_attempt_id"], "unit_id": binding["unit_id"],
                "unit_attempt_id": attempt["attempt_id"], "input_hash": _sha(body)}.items()), "CALL_IDENTITY_INVALID")
        _require(terminal.get("error_code") == expected and terminal.get("validation_status") == "failed"
                 and (terminal.get("trace_id"), terminal.get("span_id")) == (started.get("trace_id"), started.get("span_id")),
                 "CALL_TRACE_INVALID")
        span = origin.span(terminal["trace_id"], terminal["span_id"])
        _require(span.get("name") == "provider.call" and span.get("status") == "ERROR"
                 and span.get("parent_span_id") == attempt.get("span_id") and span.get("trace_id") == attempt.get("trace_id")
                 and all(span.get("attributes", {}).get(k) == terminal.get(k) for k in
                     ("call_id", "call_attempt", "provider", "operation", "node_key", "node_attempt_id", "unit_id",
                      "unit_attempt_id", "input_hash", "output_hash", "validation_status", "error_code")), "CALL_TRACE_INVALID")
        reservation = _one((r for r in origin.state.get("call_budget", {}).get("reservations", [])
                            if r.get("call_id") == call_id), "RESERVATION_INVALID")
        from .runtime import call_budget_summary
        ledger = origin.state.get("call_budget", {})
        call_budget_summary(ledger)
        reserved = _one((e for e in origin.events if e.get("call_id") == call_id
                         and e.get("event") == "call.reserved"), "RESERVATION_EVENT_INVALID")
        _require(reserved.get("status") == "admitted" and all(reserved.get(k) == terminal.get(k) for k in
                     ("run_id", "node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "call_attempt"))
                 and origin.events.index(reserved) < origin.events.index(started)
                 and reservation.get("reserved_at") <= started.get("time")
                 and ledger.get("max_run_calls") == origin.options["max_run_calls"]
                 and Decimal(ledger.get("max_run_usd", "0")) == Decimal(origin.options["max_run_usd"]),
                 "RESERVATION_EVENT_INVALID")
        _require(all(reservation.get(k) == terminal.get(k) for k in
                     ("call_attempt", "node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "input_hash"))
                 and reservation.get("settlement_status") == "reserved_unknown_actual_cost"
                 and reservation.get("input_usd_per_million") is None and reservation.get("output_usd_per_million") is None
                 and Decimal(reservation.get("estimated_upper_usd", "0")) == Decimal(origin.options["reserve_usd_per_page"]),
                 "RESERVATION_INVALID")
        raw_ref, raw_sha, billing = _response(origin, captures, call_id, validation, terminal, expected, identity, dimensions)
        review = _review(origin, specification, identity, binding, expected, raw_sha)
        origin.unchanged()
        metadata = {"reused_from_run_id": origin.run_id, "input_hash": _sha(body), "output_hash": raw_sha,
                    "error_code": expected, "operation": "material_ocr_failure_disposition"}
        with ctx._observer.span("material.ocr_failure_disposition", metadata,
                links=[{"trace_id": terminal["trace_id"], "span_id": terminal["span_id"]}]):
            snapshots = []
            for path, raw in origin.evidence.items():
                ref = ctx.store_bytes("ocr-failed-origin-" + _sha(path.encode())[:20] + ".bin", raw)
                _require(ref["sha256"] == _sha(raw), "SNAPSHOT_CHANGED")
                snapshots.append({"origin_path": path, "snapshot_ref": ref})
            provenance = {"origin_run_id": origin.run_id, "origin_node_key": "N3", "origin_unit_id": binding["unit_id"],
                "origin_node_version": node["version"], "origin_unit_input_sha256": unit["input_hash"],
                "origin_call_id": call_id, "origin_trace_id": terminal["trace_id"], "origin_span_id": terminal["span_id"],
                "origin_status": "failed", "identity_sha256": digest(identity), "image_sha256": image_sha256,
                "purpose": purpose, "request_sha256": _sha(body), "raw_response_sha256": raw_sha,
                "raw_response_available": raw_ref is not None, "historical_budget_reservation": reservation,
                "historical_billing": billing, "evidence_snapshots": snapshots,
                "new_external_calls": 0, "new_fee_reservations": 0, "historical_calls_charged_again": 0}
            result = {"schema_version": MATERIAL_OCR_FAILURE_VERSION, "status": "reading_failed_pending_review",
                "identity": copy.deepcopy(identity), "identity_sha256": digest(identity),
                "origin_run_id": origin.run_id, "origin_unit_id": binding["unit_id"], "validation_run_id": ctx.run_id,
                "material_presence": "present_but_unread_or_conflicting",
                "reading_status": "ocr_read_failed", "error_code": expected, "provenance": provenance,
                "source_review": review, "source_review_authenticated": review is not None,
                "machine_text_adopted": False, "reading_completeness": "not_established",
                "qualification_result": "not_determined", "material_presence_conclusion": "not_determined",
                "automatic_fact_adoption": False, "automatic_assembly_allowed": False,
                "original_failure_preserved": True, "local_fallback": False}
            captured = ctx.capture("material-ocr-failure-disposition", result)
            _require(captured and captured.get("status") == "captured", "CAPTURE_REQUIRED")
            origin.unchanged()
            ctx.call_event("material.ocr_failure.authenticated", {**metadata, "validation_status": "failed_source_authenticated"})
        return result
    except MaterialOCRFailureAuthenticationError:
        raise
    except Exception:
        raise MaterialOCRFailureAuthenticationError("SOURCE_INVALID") from None
