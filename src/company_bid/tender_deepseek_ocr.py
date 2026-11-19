"""Official OCR for required N1 PDF and DOCX objects."""
from __future__ import annotations

from collections import Counter
import math
import re
from typing import Annotated, Literal
import unicodedata

from pydantic import Field, ValidationError, model_validator

from . import material_deepseek_ocr as official
from .ocr import (DEEPSEEK_OCR_VERSION, RasterOptions, _IDENTITY, _Request, _SHA,
                  _Strict, _fail, _prepare_image, _read_source, renderer_version)
from .provider import ProviderFailure
from .reading import OcrResponse, _bounded_ocr_code
from .storage import digest


_PURPOSE = "rich_layout_recognition"
_PROVIDER_OPTIONS = {"engine", "endpoint", "purpose", "auth_env", "timeout_seconds", "max_attempts",
                     "max_run_calls", "max_run_usd", "reserve_usd_per_page", "expected_engine_fingerprint"}
_TEXT_REGIONS = {"text", "title", "header", "footer", "page_number", "aside_text", "table_footnote"}
_LOCAL_OUTCOMES = {"OCR_TARGET_NOT_APPROVED", "OCR_BLANK_UNCONFIRMED", "OCR_IMAGE_ONLY_UNCONFIRMED",
                   "OCR_IMAGE_REGION_REVIEW_REQUIRED", "OCR_TABLE_SOURCE_REVIEW_REQUIRED", "OCR_LAYOUT_UNSUPPORTED",
                   "OCR_LAYOUT_UNLOCATED", "OCR_CONTENT_UNREADABLE", "OCR_REVIEWED_TEXT_MISSING",
                   "OCR_REPLAY_TARGET_NOT_BOUND"}
_UNREADABLE_MARKER = re.compile(r"[\[【（(](?:无法识别|不可辨认|模糊不清|unreadable|illegible)[\]】）)]|<\?>", re.I)


def _normalize_witness(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


class _LiveTarget(_Strict):
    source_sha256: _SHA
    locator: str = Field(min_length=1, max_length=1000)
    page: int | None
    object_kind: Literal["pdf_page", "pdf_region", "docx_image"]
    rendered_sha256: _SHA
    bbox: list[float] | None = None
    package_part: str | None = None
    image_sha256: _SHA | None = None
    required_text: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_object(self):
        if any(not _normalize_witness(snippet) for snippet in self.required_text):
            raise ValueError("Empty tender OCR text witness.")
        if self.object_kind == "docx_image":
            if (self.page is None and self.bbox is None and self.package_part and self.image_sha256
                    and self.locator.startswith("word/") and re.search(r"/image\[\d+\]$", self.locator)):
                return self
        elif self.page is not None and self.page > 0 and self.package_part is None and self.image_sha256 is None:
            prefix = re.escape(f"pdf/page[{self.page}]")
            if (self.object_kind == "pdf_page" and self.bbox is None
                    and re.fullmatch(prefix + r"(?:/(?:vector-content|unmapped-glyphs))?", self.locator)):
                return self
            box = self.bbox
            if (self.object_kind == "pdf_region" and box and len(box) == 4 and all(map(math.isfinite, box))
                    and 0 <= box[0] < box[2] and 0 <= box[1] < box[3]
                    and re.fullmatch(prefix + r"/image\[\d+\]", self.locator)):
                return self
        raise ValueError("Invalid tender OCR target.")

    def matches(self, request: _Request) -> bool:
        return self.source_sha256 == request.sha256 and all(getattr(self, field) == getattr(request, field) for field in
            ("locator", "page", "object_kind", "bbox", "package_part", "image_sha256"))


class _RecordedOrigin(_Strict):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    unit_id: str = Field(pattern=r"^tender-ocr-[0-9a-f]{64}$")
    call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    raw_response_sha256: _SHA


class _RecordedTarget(_LiveTarget):
    origin: _RecordedOrigin


class _TenderOptions(RasterOptions):
    expected_renderer_version: str = Field(min_length=1, max_length=250)
    live_targets: list[_LiveTarget] | None = Field(default=None, max_length=1000)
    recorded_targets: list[_RecordedTarget] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def unique_targets(self):
        if self.live_targets is not None and self.recorded_targets is not None:
            raise ValueError("Recorded tender OCR cannot make live requests.")
        ids = [(entry.source_sha256, entry.locator) for entry in self.live_targets or self.recorded_targets or []]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate tender OCR target.")
        origins = [(entry.origin.run_id, entry.origin.call_id) for entry in self.recorded_targets or []]
        if len(set(origins)) != len(origins):
            raise ValueError("Duplicate recorded tender OCR call.")
        return self


def _provider_code(exc: official.MaterialDeepSeekOCRFailure) -> str:
    if exc.code == "MATERIAL_OCR_DIAGNOSTICS_REQUIRED":
        return "DIAGNOSTICS_CAPTURE_FAILED"
    return _bounded_ocr_code(exc.code.removeprefix("MATERIAL_")) or "OCR_PROVIDER_FAILED"


def validate_options(options: dict) -> tuple[_TenderOptions, dict]:
    if not isinstance(options, dict):
        _fail("OCR_CONFIG_INVALID")
    try:
        local = _TenderOptions.model_validate({key: value for key, value in options.items() if key not in _PROVIDER_OPTIONS})
        required = {"engine", "max_run_calls", "max_run_usd", "reserve_usd_per_page", "expected_engine_fingerprint"}
        if required - options.keys():
            _fail("OCR_CONFIG_INVALID")
        provider = official._options({key: options[key] for key in _PROVIDER_OPTIONS if key in options},
                                     require_expected=True)
        if provider["purpose"] != _PURPOSE:
            _fail("OCR_PURPOSE_INVALID")
        official.engine_fingerprint(provider)
        if renderer_version() != local.expected_renderer_version:
            _fail("OCR_RENDERER_VERSION_MISMATCH")
    except ValidationError:
        _fail("OCR_CONFIG_INVALID")
    except official.MaterialDeepSeekOCRFailure as exc:
        raise ProviderFailure(_provider_code(exc)) from None
    return local, provider


def _purchase_key(request: _Request, raster: dict) -> str:
    # A second renderer, OCR purpose or technical reason for full-page OCR
    # does not buy another read of the same physical target.
    if request.page is None:
        location = {"locator": request.locator, "package_part": request.package_part}
    else:
        region = raster["region"]
        crop = None
        if region is not None and region["bbox"] != [0.0, 0.0, *region["page_size"]]:
            crop = [round(coord, 6) or 0 for coord in region["bbox"]]
        location = {"page": request.page, "crop": crop}
    return digest({"source_sha256": request.sha256, "location": location})


def _reading_response(value: dict, request: _Request, required_text: list[str]) -> dict:
    regions = value["regions"]
    if value["tables"] or any(region["kind"] == "table" for region in regions):
        # Valid HTML describes proposed cell relations, with no source cell
        # boxes or widths. It cannot be used as the tender's original grid.
        _fail("OCR_TABLE_SOURCE_REVIEW_REQUIRED")
    if not value["text"].strip():
        _fail("OCR_IMAGE_ONLY_UNCONFIRMED" if regions and all(r["kind"] == "image" for r in regions)
              else "OCR_BLANK_UNCONFIRMED")
    if any(region["kind"] == "image" for region in regions):
        _fail("OCR_IMAGE_REGION_REVIEW_REQUIRED")
    if any(not region["normalized_boxes"] for region in regions):
        _fail("OCR_LAYOUT_UNLOCATED")
    if len(regions) > 200 or any(region["kind"] not in _TEXT_REGIONS for region in regions):
        _fail("OCR_LAYOUT_UNSUPPORTED")

    blocks, seen = [], set()
    for region in regions:
        text = region["text"]
        if (not text.strip() or any(marker in text for marker in ("<｜", "｜>", "<|", "|>"))
                or _UNREADABLE_MARKER.search(text) or any(c == "\ufffd" or
                    unicodedata.category(c) in {"Cc", "Cf", "Co", "Cs"} and c not in "\t\r\n" for c in text)):
            _fail("OCR_CONTENT_UNREADABLE")
        if len(re.findall(r"(?m)^\s*\|[^\n]+\|\s*$", text)) >= 2:
            _fail("OCR_TABLE_SOURCE_REVIEW_REQUIRED")
        marker = (tuple(tuple(box) for box in region["normalized_boxes"]), text)
        if marker in seen:
            _fail("OCR_REPETITIVE_OUTPUT")
        seen.add(marker)
        blocks.append({"kind": "paragraph", "text": text})
    if required_text:
        normalized = _normalize_witness("\n".join(block["text"] for block in blocks))
        required = Counter(map(_normalize_witness, required_text))
        missing = []
        for snippet, expected in required.items():
            observed = normalized.count(snippet)
            if observed < expected:
                missing.append({"snippet_hash": digest(snippet), "expected_count": expected, "observed_count": observed})
        if missing:
            raise official.MaterialDeepSeekOCRFailure("OCR_REVIEWED_TEXT_MISSING", details={"missing_witnesses": missing})
    return OcrResponse(**{key: getattr(request, key) for key in _IDENTITY}, blocks=blocks).model_dump(mode="json")


def _capture(ctx, name: str, value: dict) -> None:
    if ctx.capture(name, value).get("status") != "captured":
        _fail("DIAGNOSTICS_CAPTURE_FAILED")


def _replay(ctx, target, image, preparation, provider, request):
    from .tender_ocr_replay import authenticate_tender_ocr

    binding = target.origin.model_dump(mode="json")
    payload, provenance = authenticate_tender_ocr(ctx, binding=binding, image=image, preparation=preparation,
        purpose=provider["purpose"])
    metadata = {"operation": "tender_ocr_replay", "reused_from_run_id": binding["run_id"],
                "input_hash": provenance["request_body_ref"]["sha256"], "output_hash": binding["raw_response_sha256"]}
    with ctx._observer.span("tender.ocr_replay", ctx._metadata(**metadata), links=[binding]):
        _capture(ctx, "tender-ocr-replay-source", provenance)
        raster, identity = preparation["renderer"], preparation["identity"]
        try:
            candidate = official.parse_response(payload, width=raster["width"], height=raster["height"],
                image_sha256=identity["image_sha256"], identity=identity, options=provider, call_id=binding["call_id"])
            required = Counter(map(_normalize_witness, provenance["historical_required_text"])) | Counter(
                map(_normalize_witness, target.required_text))
            reply = _reading_response(candidate, request, list(required.elements()))
            if provenance["historical_error_code"]:
                _fail(provenance["historical_error_code"])
        except (official.MaterialDeepSeekOCRFailure, ProviderFailure) as exc:
            code = _provider_code(exc) if isinstance(exc, official.MaterialDeepSeekOCRFailure) else exc.code
            _capture(ctx, "tender-ocr-replay-validation", {"status": "failed", "error_code": code,
                "origin": binding, "details": getattr(exc, "details", {})})
            ctx.call_event("tender.ocr_replay.rejected", {**metadata, "error_code": code})
            raise ProviderFailure(code) from None
        _capture(ctx, "tender-ocr-replay-validation", {"status": "passed", "origin": binding,
            "reading_candidate": candidate, "reply": reply})
        ctx.call_event("tender.ocr_response_replayed", {**metadata, "validation_status": "passed"})
    return {"tender_ocr_response": reply, "call_id": binding["call_id"], "provider_request_id": payload["id"],
            "response_raw_ref": provenance["raw_response_ref"], "response_raw_sha256": binding["raw_response_sha256"],
            "replay_provenance": provenance}


def create_deepseek_ocr(ctx, options: dict):
    local, provider = validate_options(options)
    replay_only = local.recorded_targets is not None
    if replay_only and (ctx.execution_config.get("run_purpose") != "diagnostic_replay" or ctx.artifact_scope != "diagnostic"):
        _fail("OCR_REPLAY_DIAGNOSTIC_SCOPE_REQUIRED")
    fingerprint = official.engine_fingerprint(provider)
    stopped = None

    def recognize(raw_request: dict) -> dict:
        nonlocal stopped
        if stopped is not None:
            _fail("OCR_BLOCKED_BY_PRIOR_FAILURE")
        try:
            try:
                request = _Request.model_validate(raw_request)
            except ValidationError:
                _fail("OCR_REQUEST_INVALID")
            if request.ocr_version != DEEPSEEK_OCR_VERSION:
                _fail("OCR_VERSION_MISMATCH")
            recorded = next((target for target in local.recorded_targets or [] if target.matches(request)), None)
            if replay_only and recorded is None:
                _fail("OCR_REPLAY_TARGET_NOT_BOUND")
            approved = recorded or next((target for target in local.live_targets or [] if target.matches(request)), None)
            if local.live_targets is not None and approved is None:
                _fail("OCR_TARGET_NOT_APPROVED")
            image, raster = _prepare_image(request, _read_source(request, local), local)
            image_sha = raster["image_sha256"]
            if approved and approved.rendered_sha256 != image_sha:
                _fail("OCR_TARGET_RENDERED_HASH_MISMATCH")
            required_text = approved.required_text if approved else []
            purchase_key = _purchase_key(request, raster)
            identity = {**request.model_dump(mode="json", exclude={"path", "sha256", "image_sha256"}),
                        "source_sha256": request.sha256, "embedded_image_sha256": request.image_sha256,
                        "image_sha256": image_sha}
            preparation = {"identity": identity, "source_path": request.path, "renderer": raster,
                           "engine_fingerprint": fingerprint, "purchase_key": purchase_key,
                           "required_text": required_text}

            def perform() -> dict:
                image_ref = ctx.store_bytes("tender-ocr-image.png", image, "image/png")
                _capture(ctx, "ocr-preparation", {**preparation, "image": image_ref})
                if recorded is not None:
                    result = _replay(ctx, recorded, image, preparation, provider, request)
                else:
                    image_path = ctx.executor.store.resolve(image_ref["path"])
                    try:
                        result = official.recognize(ctx, image_path=image_path, image_sha256=image_sha, identity=identity,
                            options=provider, purchase_key=purchase_key, operation="tender_ocr",
                            validate_result=lambda result: _reading_response(result, request, required_text))
                    except official.MaterialDeepSeekOCRFailure as exc:
                        raise ProviderFailure(_provider_code(exc)) from None
                checkpoint = {"identity": identity, "image": image_ref, "reply": result["tender_ocr_response"],
                        "call_id": result["call_id"], "provider_request_id": result["provider_request_id"],
                        "response_raw_ref": result["response_raw_ref"], "response_raw_sha256": result["response_raw_sha256"]}
                if recorded is not None:
                    checkpoint["replay_provenance"] = result["replay_provenance"]
                return checkpoint

            # A page/DOCX unit can contain multiple requests. A succeeded
            # region survives a sibling failure and is reused by run_unit.
            inputs = {**preparation, "options": provider}
            if recorded is not None:
                inputs["recorded_target"] = recorded.model_dump(mode="json")
            result = ctx.run_unit("tender-ocr-" + digest(identity), inputs, perform)
            if (result["identity"] != identity or result["image"]["sha256"] != image_sha or
                    result["response_raw_ref"]["sha256"] != result["response_raw_sha256"]):
                _fail("OCR_CHECKPOINT_IDENTITY_INVALID")
            return result["reply"]
        except Exception as exc:
            code = _bounded_ocr_code(getattr(exc, "code", None)) or "OCR_ADAPTER_FAILED"
            if code not in _LOCAL_OUTCOMES:
                stopped = code
            if code != "DIAGNOSTICS_CAPTURE_FAILED":
                _capture(ctx, "ocr-failure", {"error_code": code,
                    "identity": {key: raw_request.get(key) for key in _IDENTITY} if isinstance(raw_request, dict) else {}})
            if isinstance(exc, ProviderFailure):
                raise
            raise ProviderFailure(code) from None

    recognize.independent_object_errors = frozenset(_LOCAL_OUTCOMES - {"OCR_TARGET_NOT_APPROVED"})
    return recognize
