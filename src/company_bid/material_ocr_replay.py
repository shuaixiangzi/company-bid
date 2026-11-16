"""Authenticate recorded DeepSeek OCR bytes without a call or a local fallback.

This narrow adapter accepts the existing OCR_COMPARISON evidence contract only.
It proves the historical request and response; the caller must run today's OCR
quality checks before consuming the returned payload.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .diagnostic_replay import _OriginRun
from .observability import redact_payload
from .storage import digest

MATERIAL_OCR_REPLAY_VERSION = "material-ocr-replay.v1"
_ENDPOINT = "https://api.deepseek.com/v1/tools/ocr"
_NODE = "OCR_COMPARISON"
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")


class MaterialOCRReplayFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise MaterialOCRReplayFailure("MATERIAL_OCR_REPLAY_" + code)


def _one(items, code):
    values = list(items)
    _require(len(values) == 1, code)
    return values[0]


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class _OCROrigin(_OriginRun):
    def __init__(self, store, run_id):
        self.store, self.root, self.run_id = store, store.root, run_id
        self.evidence = {}
        _require(store.run_dir(run_id).is_dir(), "SOURCE_MISSING")
        self.state_raw = self._file("state.json")
        self.state = json.loads(self.state_raw)
        _require(self.state.get("schema_version") == "m1.local.v1"
                 and self.state.get("run_id") == run_id and self.state.get("status") == "succeeded",
                 "SOURCE_NOT_SEALED")
        config = self.state.get("execution_config", {})
        _require(self.state.get("config_hash") == digest(config)
                 and config.get("run_purpose") == "diagnostic_replay"
                 and self.state.get("artifact_scope") == "diagnostic"
                 and self.state.get("capture_policy") is True
                 and self.state.get("observability", {}).get("trace_status") == "captured"
                 and self.state.get("observability", {}).get("capture_status") == "captured",
                 "SOURCE_DIAGNOSTICS_INVALID")
        self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")
        node = self.node(_NODE)
        _require(node.get("status") == "succeeded" and node.get("attempts")
                 and node["attempts"][-1].get("status") == "succeeded", "SOURCE_NODE_NOT_SUCCESSFUL")
        manifest = _one((item for item in self.state.get("node_manifest", []) if item.get("key") == _NODE),
                        "NODE_VERSION_INVALID")
        _require(manifest.get("version") == node.get("version"), "NODE_VERSION_INVALID")
        settings = config.get("ocr_comparison", {})
        _require(settings.get("endpoint") == _ENDPOINT and settings.get("purpose") == "rich_layout_recognition"
                 and settings.get("max_attempts") == 1 and settings.get("tls_verify") is True
                 and settings.get("follow_redirects") is False, "SOURCE_SERVICE_INVALID")
        self.settings = settings

    def binary(self, ref):
        _require(isinstance(ref, dict) and isinstance(ref.get("artifact_id"), str)
                 and _ID.fullmatch(ref["artifact_id"]) and isinstance(ref.get("path"), str)
                 and re.fullmatch(r"runs/[0-9a-f]{32}/artifacts/[^/\\]+", ref["path"]),
                 "ARTIFACT_REFERENCE_INVALID")
        raw = self.store.read_bytes(ref)
        _require(len(raw) <= 25_000_000, "ARTIFACT_TOO_LARGE")
        self.evidence[ref["path"]] = raw
        return raw

    def selected_image(self, ref):
        _require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                 and isinstance(ref.get("sha256"), str) and _SHA.fullmatch(ref["sha256"]),
                 "IMAGE_REFERENCE_INVALID")
        path = Path(ref["path"]).resolve(strict=True)
        _require(path.is_relative_to((self.root / "runs").resolve()) and path.parent.name == "artifacts"
                 and 0 < path.stat().st_size <= 20_000_000, "IMAGE_REFERENCE_INVALID")
        raw = path.read_bytes()
        _require(_sha(raw) == ref["sha256"], "IMAGE_HASH_INVALID")
        if "size_bytes" in ref:
            _require(len(raw) == ref["size_bytes"], "IMAGE_SIZE_INVALID")
        self.evidence[path.relative_to(self.root.resolve()).as_posix()] = raw
        return raw


def _request_body(image, image_sha, purpose):
    # This is the frozen comparison wire, not a new live provider implementation.
    boundary = "companybidocrcomparison" + image_sha[:24]
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.png"\r\n'
              'Content-Type: image/png\r\n\r\n').encode("ascii")
    return header + image + f"\r\n--{boundary}--\r\n".encode("ascii")


def _authenticate(origin, binding, image_sha256, identity, purpose):
    node, unit, unit_input, observation, attempt = origin.unit(_NODE, binding["unit_id"])
    _require(len(unit.get("attempts", [])) == 1, "SOURCE_ATTEMPT_AMBIGUOUS")
    node_attempt = _one((a for a in node["attempts"] if a["attempt_id"] == attempt["node_attempt_id"]),
                        "NODE_ATTEMPT_INVALID")
    _require(node_attempt.get("status") == "succeeded"
             and node_attempt.get("trace_id") == attempt.get("trace_id"), "NODE_ATTEMPT_INVALID")
    spec = unit_input.get("spec", {})
    _require(unit_input.get("config") == origin.settings
             and spec.get("unit_id") == binding["unit_id"]
             and spec.get("identity") == observation.get("identity") == identity
             and spec.get("image_file", {}).get("sha256") == observation.get("image_sha256") == image_sha256
             and identity.get("image_sha256") == image_sha256, "INPUT_IDENTITY_CHANGED")
    _require(observation.get("unit_id") == binding["unit_id"]
             and observation.get("transport_contract_passed") is True
             and observation.get("http_status") == 200 and observation.get("error_code") is None
             and observation.get("raw_response_complete") is True
             and observation.get("raw_response_sha256_scope") == "received_body"
             and observation.get("automatic_retry") is False, "SOURCE_RESPONSE_NOT_SUCCESSFUL")
    call_id = observation.get("call_id")
    _require(isinstance(call_id, str) and re.fullmatch(r"ocrcompare-[0-9a-f]{32}", call_id), "CALL_ID_INVALID")
    captures = origin.captures(node, binding["unit_id"], attempt)
    request = origin.capture(captures, "request-" + call_id)
    response = origin.capture(captures, "response-" + call_id)
    validation = origin.capture(captures, "validation-" + call_id)
    _require(validation == redact_payload(observation), "VALIDATION_CAPTURE_CHANGED")
    _require(request.get("call_id") == call_id and request.get("endpoint") == _ENDPOINT
             and request.get("purpose") == purpose == origin.settings["purpose"]
             and request.get("identity") == identity and request.get("image_sha256") == image_sha256
             and request.get("single_png_expected_pages") == 1 and request.get("tls_verify") is True
             and request.get("follow_redirects") is False and request.get("automatic_retry") is False,
             "REQUEST_CHANGED")
    image = origin.selected_image(spec["image_file"])
    body = origin.binary(request["request_body_ref"])
    body_sha = _sha(body)
    _require(body == _request_body(image, image_sha256, purpose)
             and request.get("body_sha256") == body_sha, "EXACT_REQUEST_CHANGED")
    _require(response.get("call_id") == call_id and response.get("http_status") == 200
             and response.get("outcome_error") is None and response.get("raw_capture_status") == "captured"
             and response.get("raw_response_ref") == observation.get("raw_response_ref"), "RESPONSE_CAPTURE_INVALID")
    raw = origin.binary(observation["raw_response_ref"])
    raw_sha = _sha(raw)
    payload = json.loads(raw)
    digest(payload)
    _require(response.get("received_raw_sha256") == observation.get("raw_response_sha256") == raw_sha
             and response.get("response") == observation.get("response") == payload, "RESPONSE_BYTES_CHANGED")
    _require(isinstance(payload, dict) and "error" not in payload and payload.get("object") == "ocr.result"
             and payload.get("purpose") == purpose and type(payload.get("page_count")) is int
             and payload["page_count"] == 1 and isinstance(payload.get("pages"), list)
             and len(payload["pages"]) == 1, "SOURCE_PAGE_INVALID")
    page = payload["pages"][0]
    _require(isinstance(page, dict) and type(page.get("index")) is int and page["index"] == 0
             and page.get("finish_reason") == "stop" and isinstance(page.get("text"), str),
             "SOURCE_PAGE_INCOMPLETE")
    provider_id = payload.get("id")
    _require(isinstance(provider_id, str) and re.fullmatch(r"ocr-[A-Za-z0-9_-]{1,150}", provider_id)
             and observation.get("provider_request_id") == provider_id, "PROVIDER_ID_INVALID")

    related = [e for e in origin.events if e.get("unit_attempt_id") == attempt["attempt_id"]
               and e.get("event") in {"call.started", "call.completed", "call.failed"}]
    globally_related = [e for e in origin.events if e.get("call_id") == call_id
                        and e.get("event") in {"call.started", "call.completed", "call.failed"}]
    _require(len(related) == 2 and globally_related == related, "SINGLE_CALL_REQUIRED")
    started = _one((e for e in related if e["event"] == "call.started"), "SINGLE_CALL_REQUIRED")
    terminal = _one((e for e in related if e["event"] in {"call.completed", "call.failed"}), "SINGLE_CALL_REQUIRED")
    _require(terminal["event"] == "call.completed" and terminal.get("validation_status") == "passed"
             and terminal.get("error_code") is None and terminal.get("output_hash") == raw_sha,
             "SUCCESSFUL_CALL_REQUIRED")
    for event in (started, terminal):
        _require(event.get("call_id") == call_id and event.get("call_attempt") == 1
                 and event.get("provider") == "deepseek_official_ocr" and event.get("operation") == "ocr_comparison"
                 and event.get("run_id") == origin.run_id and event.get("node_key") == _NODE
                 and event.get("unit_id") == binding["unit_id"]
                 and event.get("node_attempt_id") == attempt["node_attempt_id"]
                 and event.get("input_hash") == body_sha, "CALL_IDENTITY_INVALID")
    _require((started.get("trace_id"), started.get("span_id")) == (terminal.get("trace_id"), terminal.get("span_id"))
             == (observation.get("trace_id"), observation.get("span_id")), "CALL_TRACE_CHANGED")
    span = origin.span(terminal["trace_id"], terminal["span_id"])
    _require(span.get("name") == "provider.call" and span.get("status") != "ERROR"
             and span.get("parent_span_id") == attempt.get("span_id")
             and span.get("trace_id") == attempt.get("trace_id")
             and all(span.get("attributes", {}).get(k) == terminal.get(k) for k in
                     ("call_id", "call_attempt", "provider", "operation", "node_key", "node_attempt_id", "unit_id",
                      "unit_attempt_id", "input_hash", "output_hash", "validation_status", "provider_request_id")),
             "CALL_SPAN_INVALID")
    reservation = _one((r for r in origin.state.get("call_budget", {}).get("reservations", [])
                        if r.get("call_id") == call_id), "RESERVATION_INVALID")
    _require(all(reservation.get(k) == terminal.get(k) for k in
                 ("call_attempt", "node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "input_hash"))
             and reservation.get("settlement_status") == "reserved_unknown_actual_cost"
             and reservation.get("input_usd_per_million") is None
             and reservation.get("output_usd_per_million") is None
             and Decimal(reservation.get("estimated_upper_usd", "0")) > 0
             and reservation["estimated_upper_usd"] == observation.get("usd_conservative_reservation_retained"),
             "RESERVATION_INVALID")
    initial = origin.artifact(origin.state["initial_input_ref"])
    _require(digest(initial) == origin.state["initial_input_hash"]
             and _one((s for s in initial.get("units", []) if s.get("unit_id") == binding["unit_id"]),
                      "INITIAL_UNIT_AMBIGUOUS") == spec, "INITIAL_SCOPE_CHANGED")
    node_output = origin.checkpoint(node, _NODE)["data"]
    _require(_one((o for o in node_output.get("observations", []) if o.get("unit_id") == binding["unit_id"]),
                  "NODE_OBSERVATION_AMBIGUOUS") == observation, "NODE_OBSERVATION_CHANGED")
    return payload, observation, request, reservation, body_sha, raw_sha


def replay_material_ocr(ctx, *, binding, image_sha256, identity, purpose):
    """Return authenticated raw payload + provenance. Never perform a fallback."""
    try:
        _require(isinstance(binding, dict) and set(binding) == {"run_id", "node_key", "unit_id"}
                 and isinstance(binding["run_id"], str) and _ID.fullmatch(binding["run_id"])
                 and binding["run_id"] != ctx.run_id and binding["node_key"] == _NODE
                 and isinstance(binding["unit_id"], str) and re.fullmatch(r"compare-[a-z_]{1,80}", binding["unit_id"]),
                 "BINDING_INVALID")
        _require(ctx.node_key == "N3" and isinstance(ctx._active_unit, dict)
                 and ctx.execution_config.get("debug_capture", True) is True
                 and ctx._state.get("capture_policy") is True, "CAPTURED_N3_UNIT_REQUIRED")
        _require(isinstance(identity, dict) and isinstance(image_sha256, str) and _SHA.fullmatch(image_sha256)
                 and purpose == "rich_layout_recognition", "SCOPE_INVALID")
        origin = _OCROrigin(ctx.executor.store, binding["run_id"])
        payload, observation, request, reservation, request_sha, response_sha = _authenticate(
            origin, binding, image_sha256, identity, purpose)
        origin.unchanged()
        metadata = {"reused_from_run_id": origin.run_id, "input_hash": request_sha, "output_hash": response_sha,
                    "unit_id": ctx._active_unit["unit_id"], "operation": "material_ocr_replay"}
        with ctx._observer.span("material.ocr_replay", metadata,
                links=[{"trace_id": observation["trace_id"], "span_id": observation["span_id"]}]):
            snapshots = {}
            for path, raw in origin.evidence.items():
                ref = ctx.store_bytes("ocr-origin-" + _sha(path.encode())[:20] + ".bin", raw)
                _require(ref["sha256"] == _sha(raw), "SNAPSHOT_HASH_CHANGED")
                snapshots[path] = {"origin_path": path, "snapshot_ref": ref}
            provenance = {"version": MATERIAL_OCR_REPLAY_VERSION, "origin_run_id": origin.run_id,
                "origin_node_key": _NODE, "origin_unit_id": binding["unit_id"],
                "origin_node_version": origin.node(_NODE)["version"],
                "origin_unit_input_sha256": origin.node(_NODE)["units"][binding["unit_id"]]["input_hash"],
                "origin_unit_output_sha256": origin.node(_NODE)["units"][binding["unit_id"]]["output_hash"],
                "origin_call_id": observation["call_id"], "origin_trace_id": observation["trace_id"],
                "origin_span_id": observation["span_id"], "provider_request_id": observation["provider_request_id"],
                "request_sha256": request_sha, "raw_response_sha256": response_sha,
                "image_sha256": image_sha256, "identity_sha256": digest(identity), "purpose": purpose,
                "raw_response_snapshot": snapshots[observation["raw_response_ref"]["path"]]["snapshot_ref"],
                "request_body_snapshot": snapshots[request["request_body_ref"]["path"]]["snapshot_ref"],
                "evidence_snapshots": list(snapshots.values()), "historical_budget_reservation": reservation,
                "historical_page_fee_estimate_cny": observation.get("official_returned_page_fee_estimate_cny"),
                "new_external_calls": 0, "new_fee_reservations": 0, "historical_calls_charged_again": 0,
                "response_reused": True, "current_quality_validation": "required_by_caller",
                "qualification_result": "not_determined"}
            capture = ctx.capture("material-ocr-replay-" + ctx._active_unit["unit_id"], provenance)
            _require(capture["status"] == "captured", "CAPTURE_FAILED")
            origin.unchanged()
            ctx.call_event("material.ocr_response_replayed", {**metadata, "validation_status": "source_authenticated"})
        return copy.deepcopy(payload), provenance
    except MaterialOCRReplayFailure:
        raise
    except Exception:
        # Storage paths, raw OCR and exception text do not enter ordinary logs.
        raise MaterialOCRReplayFailure("MATERIAL_OCR_REPLAY_SOURCE_INVALID") from None
