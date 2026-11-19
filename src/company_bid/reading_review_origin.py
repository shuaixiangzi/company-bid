"""Authenticate the official call cited by an independent PDF image transcription."""
from __future__ import annotations

from .material_ocr_quality import _regions
from .provider import ProviderFailure
from .tender_ocr_replay import REVIEWABLE_FAILURE, _TenderOrigin, _authenticate, _one, _require, _same, _sha


def _correction_regions(corrections, payload, renderer):
    width, height = renderer["width"], renderer["height"]
    regions = _regions(payload["pages"][0]["text"], width, height)
    matched = []
    for correction in corrections:
        left, top, right, bottom = correction["pixel_bbox"]
        x, y = (left + right) / 2, (top + bottom) / 2
        hits = []
        for region in regions:
            if correction["ocr_text"] not in region["raw_text"]:
                continue
            for a, b, c, d in region["normalized_boxes"]:
                if a * width / 1000 - 2 <= x <= c * width / 1000 + 2 and b * height / 1000 - 2 <= y <= d * height / 1000 + 2:
                    hits.append(region["region_index"])
                    break
        matched.append({**correction, "origin_region_index": _one(hits, "CORRECTION_TARGET_MISMATCH")})
    return matched


def verify_review_origin(ctx, binding: dict, *, source_id, source_sha256, page, locator, bbox, image_sha256,
                         corrections=()) -> dict:
    """Validate original bytes, outcome, trace and billable receipt independently of the new text."""
    try:
        _require(ctx.node_key == "N1" and ctx.artifact_scope == "diagnostic" and ctx._state["capture_policy"] is True
                 and ctx.execution_config.get("run_purpose") == "diagnostic_replay"
                 and binding["run_id"] != ctx.run_id, "DIAGNOSTIC_SCOPE_REQUIRED")
        origin = _TenderOrigin(ctx.executor.store, binding["run_id"])
        unit_id = binding["unit_id"]
        node, unit, inputs, _, _ = origin.unit("N1", unit_id, successful=False)
        identity = inputs["identity"]
        expected_status = "succeeded" if corrections else "failed"
        _require(unit["status"] == expected_status and _same(identity, {
            "source_id": source_id, "source_sha256": source_sha256, "page": page,
            "locator": locator, "bbox": list(bbox), "image_sha256": image_sha256, "object_kind": "pdf_region",
        }), "REVIEW_REGION_MISMATCH")
        attempt = _one(unit["attempts"], "SINGLE_ATTEMPT_REQUIRED")
        captures = origin.captures(node, unit_id, attempt)
        prep = origin.capture(captures, "ocr-preparation")
        image = origin.binary(prep["image"])
        _require(_sha(image) == image_sha256, "TARGET_RASTER_MISMATCH")
        event = "call.completed" if corrections else "call.failed"
        outcome = _one((e for e in origin.events if e["event"] == event and
                       e.get("call_id") == binding["call_id"] and e.get("unit_id") == unit_id), "CALL_OUTCOME_INVALID")
        linked = {**binding, "trace_id": outcome["trace_id"], "span_id": outcome["span_id"]}
        payload, _, _, source_ref, reservation, billing, code, witnesses = _authenticate(
            origin, linked, image, prep, origin.options["purpose"],
            allowed_failures=(REVIEWABLE_FAILURE, "OCR_IMAGE_ONLY_UNCONFIRMED", "OCR_OUTPUT_INCOMPLETE"),
        )
        origin.unchanged()
        return {"origin": linked, "historical_status": expected_status,
                "source_corrections": _correction_regions(corrections, payload, prep["renderer"]) if corrections else [],
                "historical_error_code": code, "unit_error_code": attempt.get("error_code"),
                "finish_reason": payload["pages"][0]["finish_reason"], "original_source_ref": source_ref,
                "original_image_ref": prep["image"], "historical_required_text": witnesses,
                "historical_billing": billing, "original_request_sha256": reservation["input_hash"]}
    except ProviderFailure:
        raise
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise ProviderFailure("OCR_REPLAY_SOURCE_INVALID") from exc
