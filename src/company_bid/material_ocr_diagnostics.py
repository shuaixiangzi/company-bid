"""Read-only, content-free index of captured local OCR invocations."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import re

from .storage import digest


SCHEMA_VERSION = "material.local-ocr.diagnostic-index.v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_CALL = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_EVENTS = {"ocr.local.started", "ocr.local.completed", "ocr.local.failed"}
_IDENTITY = ("node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "call_id", "call_attempt")


def _sha(value):
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _identity(value):
    return tuple(value.get(key) for key in _IDENTITY)


def _elapsed(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def index_local_ocr(state, events, spans, *, artifact_json, ref, gap):
    """Index invocation evidence separately from provider calls and budgets.

    The parent inspector owns artifact integrity checks. This helper reads only
    captures belonging to the exact node/unit attempt. Source text, filesystem
    paths and subprocess messages are deliberately absent from its flat rows.
    """
    calls, local_gaps = {}, []
    nodes = {node["key"]: node for node in state["nodes"]}
    spans_by_id = {(row.get("trace_id"), row.get("span_id")): row for row in spans}

    def problem(code, row=None, **metadata):
        details = {"call_id": row["call_id"]} if row and row.get("call_id") else {}
        details.update(metadata)
        gap(code, **details)
        local_gaps.append({"code": code, **details})
        if row is not None:
            row.setdefault("evidence_gaps", []).append(code)

    def admitted(value):
        return (isinstance(value, dict) and all(_token(value.get(key)) for key in _IDENTITY[:4])
                and isinstance(value.get("call_id"), str) and _CALL.fullmatch(value["call_id"])
                and type(value.get("call_attempt")) is int and value["call_attempt"] >= 1)

    def call_row(value):
        if not admitted(value):
            problem("LOCAL_OCR_CALL_IDENTITY_INVALID")
            return None
        key = _identity(value)
        return calls.setdefault(key, {**dict(zip(_IDENTITY, key)), "events": [], "evidence_gaps": []})

    for event in events:
        if event.get("event") in _EVENTS:
            row = call_row(event)
            if row is not None:
                row["events"].append(event)
    # Orphan spans are visible even when both ordinary event records disappeared.
    for span in spans:
        if span.get("name") == "ocr.local.call":
            attributes = span.get("attributes", {})
            related = [row for row in calls.values() if isinstance(attributes, dict)
                       and row["call_id"] == attributes.get("call_id")
                       and row["call_attempt"] == attributes.get("call_attempt")]
            # A damaged span identity is a gap on the existing invocation,
            # not evidence of a second physical OCR invocation.
            row = related[0] if len(related) == 1 else call_row(attributes)
            if row is not None:
                row.setdefault("spans", []).append(span)

    def attempt_captures(row):
        node = nodes.get(row.get("node_key"), {})
        attempts = [a for a in node.get("attempts", []) if a.get("attempt_id") == row.get("node_attempt_id")]
        unit = node.get("units", {}).get(row.get("unit_id"), {})
        unit_attempts = [a for a in unit.get("attempts", [])
                         if a.get("attempt_id") == row.get("unit_attempt_id")
                         and a.get("node_attempt_id") == row.get("node_attempt_id")]
        if len(attempts) != 1 or len(unit_attempts) != 1:
            problem("LOCAL_OCR_ATTEMPT_NOT_FOUND", row)
            return [], None
        captures = [c for c in attempts[0].get("captures", [])
                    if c.get("node_attempt_id") == row["node_attempt_id"]
                    and c.get("unit_id") == row["unit_id"] and c.get("unit_attempt_id") == row["unit_attempt_id"]]
        return captures, unit_attempts[0]

    def capture(row, captures, name, *, required=True):
        matches = [c for c in captures if c.get("name") == name]
        if not matches and not required:
            return None, None
        if len(matches) != 1:
            problem("LOCAL_OCR_CAPTURE_MISSING_OR_DUPLICATE", row)
            return None, None
        entry = matches[0]
        status = entry.get("status")
        if status != "captured":
            problem("LOCAL_OCR_CAPTURE_UNAVAILABLE", row,
                    capture_status=status if status in {"partial", "write_failed", "disabled_by_policy"} else "invalid")
            return None, None
        reference = ref(entry.get("ref"))
        if not reference or reference.get("integrity") != "valid":
            problem("LOCAL_OCR_CAPTURE_INTEGRITY_FAILED", row)
            return None, reference
        value = artifact_json(entry["ref"], node_key=row.get("node_key"), unit_id=row.get("unit_id"))
        if not isinstance(value, dict):
            problem("LOCAL_OCR_CAPTURE_JSON_INVALID", row)
            return None, reference
        return value, reference

    results = []
    for row in calls.values():
        recorded = row.pop("events")
        started = [e for e in recorded if e["event"] == "ocr.local.started"]
        terminal = [e for e in recorded if e["event"] in {"ocr.local.completed", "ocr.local.failed"}]
        if len(started) != 1 or len(terminal) != 1:
            problem("LOCAL_OCR_ATTEMPT_INCOMPLETE", row)
        latest = terminal[-1] if terminal else started[-1] if started else {}
        row["outcome"] = ("completed" if latest.get("event") == "ocr.local.completed" else
                          "failed" if latest.get("event") == "ocr.local.failed" else "unknown")
        row.update(trace_id=latest.get("trace_id") if isinstance(latest.get("trace_id"), str)
                       and re.fullmatch(r"[0-9a-f]{32}", latest["trace_id"]) else None,
                   span_id=latest.get("span_id") if isinstance(latest.get("span_id"), str)
                       and re.fullmatch(r"[0-9a-f]{16}", latest["span_id"]) else None,
                   elapsed_ms=_elapsed(latest.get("elapsed_ms")), request_sha256=_sha(latest.get("input_hash")),
                   raw_response_sha256=_sha(latest.get("output_hash")), api_cost_estimate_usd=0,
                   billing_basis="local_engine_no_external_provider", qualification_result="not_determined")
        row["error_code"] = (_token(latest.get("error_code")) if row["outcome"] == "failed" else None)
        if terminal and row["elapsed_ms"] is None:
            problem("LOCAL_OCR_ELAPSED_MISSING_OR_INVALID", row)
        for event in recorded:
            if (event.get("provider") != "windows_ocr" or event.get("operation") != "material_ocr"
                    or event.get("usage_status") != "local_no_api_charge"
                    or type(event.get("cost")) not in {int, float} or event["cost"] != 0
                    or event.get("input_hash") != row["request_sha256"]
                    or (event.get("trace_id"), event.get("span_id")) != (row["trace_id"], row["span_id"])):
                problem("LOCAL_OCR_EVENT_IDENTITY_MISMATCH", row)
        found_spans = row.pop("spans", [])
        span = spans_by_id.get((row["trace_id"], row["span_id"]))
        captures, unit_attempt = attempt_captures(row)
        if len(found_spans) != 1 or span is not found_spans[0]:
            problem("LOCAL_OCR_TRACE_MISSING_OR_DUPLICATE", row)
        elif (_identity(span.get("attributes", {})) != _identity(row)
              or span["attributes"].get("input_hash") != row["request_sha256"]
              or span["attributes"].get("output_hash") != row["raw_response_sha256"]
              or unit_attempt is None or span.get("parent_span_id") != unit_attempt.get("span_id")
              or span.get("status") == "ERROR" and row["outcome"] == "completed"):
            problem("LOCAL_OCR_TRACE_IDENTITY_MISMATCH", row)
        if span:
            start, end = span.get("start_time_unix_nano"), span.get("end_time_unix_nano")
            row["span_duration_ms"] = ((end - start) / 1_000_000
                if type(start) is int and type(end) is int and end >= start else None)
        request, request_ref = capture(row, captures, "ocr-request-" + row["call_id"])
        response, response_ref = capture(row, captures, "ocr-response-" + row["call_id"], required=row["outcome"] == "completed")
        validation, validation_ref = capture(row, captures, "ocr-validation-" + row["call_id"], required=row["outcome"] == "completed")
        error, error_ref = capture(row, captures, "ocr-error-" + row["call_id"], required=row["outcome"] == "failed")
        row["captures"] = {"request": request_ref, "response": response_ref, "validation": validation_ref, "error": error_ref}
        if request:
            wire, identity = request.get("request"), request.get("identity", {})
            if (request.get("call_id") != row["call_id"] or not isinstance(wire, dict)
                    or digest(wire) != row["request_sha256"]):
                problem("LOCAL_OCR_REQUEST_HASH_MISMATCH", row)
            elif isinstance(identity, dict):
                row.update(selected_image_sha256=_sha(wire.get("image_sha256")),
                           source_sha256=_sha(identity.get("source_sha256")),
                           selection_sha256=_sha(identity.get("selection_sha256")),
                           selected_scope_sha256=digest(identity),
                           engine_fingerprint_sha256=digest(wire.get("expected_engine_fingerprint")))
                if row["selected_image_sha256"] is None or (identity.get("image_sha256") is not None
                        and identity["image_sha256"] != row["selected_image_sha256"]):
                    problem("LOCAL_OCR_IMAGE_IDENTITY_MISMATCH", row)
        raw_value = None
        if response:
            stdout = response.get("stdout")
            if (response.get("call_id") != row["call_id"] or not isinstance(stdout, str)
                    or response.get("stdout_status") != "captured"
                    or response.get("output_sha256") != row["raw_response_sha256"]
                    or hashlib.sha256(stdout.encode("utf-8")).hexdigest() != row["raw_response_sha256"]):
                problem("LOCAL_OCR_RESPONSE_HASH_MISMATCH", row)
            else:
                try:
                    raw_value = json.loads(stdout.lstrip("\ufeff"))
                except (ValueError, TypeError):
                    if row["outcome"] == "completed":
                        problem("LOCAL_OCR_COMPLETED_RESPONSE_INVALID", row)
        if validation:
            validated = validation.get("validated_output")
            row["validated_output_sha256"] = _sha(validation.get("validated_output_hash"))
            if (validation.get("call_id") != row["call_id"] or validation.get("status") != "passed"
                    or not isinstance(validated, dict) or validation.get("validated_output_hash") != digest(validated)
                    or not isinstance(validated.get("text"), str)
                    or validated.get("call_id") != row["call_id"]
                    or validated.get("image_sha256") != row.get("selected_image_sha256")
                    or not isinstance(raw_value, dict) or raw_value.get("status") != "succeeded"
                    or any(raw_value.get(key) != validated.get(key) for key in
                           ("schema_version", "text", "lines", "width", "height", "image_sha256", "engine_fingerprint"))):
                problem("LOCAL_OCR_VALIDATION_HASH_MISMATCH", row)
            else:
                row["transcription_status"] = ("ocr_transcribed_unverified" if validated.get("text", "").strip()
                                               else "ocr_no_text_unresolved")
        if error and (error.get("call_id") != row["call_id"] or error.get("status") != "failed"
                      or error.get("error_code") != row["error_code"]):
            problem("LOCAL_OCR_ERROR_CAPTURE_MISMATCH", row)
        row["evidence_gaps"] = list(dict.fromkeys(row["evidence_gaps"]))
        row["evidence_status"] = "incomplete" if row["evidence_gaps"] else "available"
        results.append(row)

    preflight = []
    for event in events:
        if event.get("event") != "material_ocr.preflight.failed" or event.get("provider") != "windows_ocr":
            continue
        row = {key: _token(event.get(key)) for key in ("node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "error_code")}
        row.update(trace_id=event.get("trace_id"), span_id=event.get("span_id"),
                   outcome="preflight_failed_no_recognition_attempt", evidence_gaps=[])
        captures, _ = attempt_captures(row)
        matches = [c for c in captures if isinstance(c.get("name"), str) and c["name"].startswith("ocr-preflight-error-")]
        if len(matches) != 1:
            problem("LOCAL_OCR_PREFLIGHT_CAPTURE_MISSING_OR_DUPLICATE", row)
            row["error_capture"] = None
        else:
            value, reference = capture(row, captures, matches[0]["name"])
            row["error_capture"] = reference
            if value and value.get("error_code") != row["error_code"]:
                problem("LOCAL_OCR_PREFLIGHT_ERROR_MISMATCH", row)
        span = spans_by_id.get((row["trace_id"], row["span_id"]))
        if not span or span.get("name") != "ocr.local.preflight":
            problem("LOCAL_OCR_PREFLIGHT_TRACE_MISSING", row)
        row["evidence_status"] = "incomplete" if row["evidence_gaps"] else "available"
        preflight.append(row)

    inherited = [{"node_key": node["key"], "origin_run_id": node["reused_from_run_id"],
                  "origin_output": ref(node.get("output_ref")), "new_local_ocr_attempts": 0,
                  "source_status": "inherited_node_evidence_not_reexecuted"}
                 for node in state["nodes"] if node.get("reused_from_run_id") and any(
                     key.startswith("material-ocr-") for key in node.get("units", {}))]
    counts = Counter(row["outcome"] for row in results)
    return {"local_ocr_schema_version": SCHEMA_VERSION, "local_ocr_calls": results,
            "actual_local_ocr_attempts": len(results), "local_ocr_completed_attempts": counts["completed"],
            "local_ocr_failed_attempts": counts["failed"], "local_ocr_unknown_attempts": counts["unknown"],
            "local_ocr_preflight_failures": preflight, "inherited_local_ocr_sources": inherited,
            "local_ocr_evidence_status": "incomplete" if local_gaps else "available",
            "local_ocr_api_cost_estimate_usd": 0, "local_ocr_cost_basis": "local_engine_no_external_provider"}
