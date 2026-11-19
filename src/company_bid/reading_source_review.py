"""Source-bound diagnostic transcription of an original PDF screenshot."""

from __future__ import annotations

from collections import Counter
import hashlib
import re
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .storage import digest

VERSION = "pdf-region-source-review-v1"
KIND = "pdf_region_source_review"
_SHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_HEX_ID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
_SOURCE_ID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]
_PIXEL_BOX = Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=4, max_length=4)]
_TEXT = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _visible_text(value: str) -> str:
    if (not value.strip() or any(c == "\ufffd" or
            unicodedata.category(c) in {"Cc", "Cf", "Co", "Cs"} and c not in "\t\n" for c in value)):
        raise ValueError("A source review requires literal, legible text.")
    return value


def _witness(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _overlaps(left, right):
    return max(left[0], right[0]) < min(left[2], right[2]) and max(left[1], right[1]) < min(left[3], right[3])


def _contains(outer, inner):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


class _Entry(_Strict):
    pixel_bbox: _PIXEL_BOX
    text: _TEXT
    role: Literal["instruction", "navigation", "interface", "example_input", "example_result", "context"]

    _legible = field_validator("text")(_visible_text)


class _Unknown(_Strict):
    pixel_bbox: _PIXEL_BOX
    reason: _TEXT
    critical: bool

    _reason = field_validator("reason")(_visible_text)


class _Correction(_Strict):
    pixel_bbox: _PIXEL_BOX
    ocr_text: _TEXT
    source_text: _TEXT

    _legible = field_validator("ocr_text", "source_text")(_visible_text)


class _Origin(_Strict):
    run_id: _HEX_ID
    unit_id: str = Field(pattern=r"^tender-ocr-[0-9a-f]{64}$")
    call_id: _HEX_ID
    raw_response_sha256: _SHA


class PdfRegionReview(_Strict):
    source_id: _SOURCE_ID
    source_sha256: _SHA
    page: int = Field(ge=1)
    locator: str = Field(max_length=100)
    bbox: list[float] = Field(min_length=4, max_length=4)
    image_sha256: _SHA
    width_px: int = Field(ge=1, le=3072)
    height_px: int = Field(ge=1, le=3072)
    reviewed_by: str = Field(min_length=1, max_length=120)
    entries: list[_Entry] = Field(min_length=1, max_length=200)
    unknown: list[_Unknown] = Field(max_length=200)
    origin: _Origin
    corrections: list[_Correction] = Field(default_factory=list, max_length=60)

    _reviewer = field_validator("reviewed_by")(_visible_text)

    @model_validator(mode="after")
    def coherent_source_review(self):
        x0, y0, x1, y1 = self.bbox
        if (not re.fullmatch(re.escape(f"pdf/page[{self.page}]") + r"/image\[(?:0|[1-9][0-9]*)\]", self.locator)
                or not (0 <= x0 < x1 and 0 <= y0 < y1)):
            raise ValueError("A source review requires an exact PDF image locator and nonempty page box.")
        crop = [0, 0, self.width_px, self.height_px]
        boxes = set()
        for fragment in [*self.entries, *self.unknown]:
            box = tuple(fragment.pixel_bbox)
            if not _contains(crop, box) or box in boxes:
                raise ValueError("Reviewed and unresolved pixel fragments must use distinct boxes inside the raster.")
            boxes.add(box)
        for index, item in enumerate(self.corrections):
            owners = [entry for entry in self.entries if _contains(entry.pixel_bbox, item.pixel_bbox)
                      and item.source_text in entry.text]
            if (len(owners) != 1 or _witness(item.ocr_text) == _witness(item.source_text) or
                    any(_overlaps(item.pixel_bbox, other.pixel_bbox) for other in self.corrections[:index])):
                raise ValueError("A correction requires a distinct historic term and a localized original witness.")
        return self


class ReviewProofError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _render(source, page_unit_id: str, review: PdfRegionReview):
    # The shared helper only reads frozen bytes and renders. This invocation
    # recomputes the crop before consulting history or any checkpoint.
    from .ocr import DEEPSEEK_OCR_VERSION, RasterOptions, _prepare_image, _read_source, _Request
    from .provider import ProviderFailure

    request = _Request(source_id=source.source_id, sha256=source.sha256, path=str(source.path),
                       unit_id=page_unit_id, locator=review.locator, page=review.page,
                       ocr_version=DEEPSEEK_OCR_VERSION, object_kind="pdf_region", bbox=review.bbox)
    options = RasterOptions(render_dpi=160)
    try:
        blob, raster = _prepare_image(request, _read_source(request, options), options)
    except ProviderFailure as exc:
        raise ReviewProofError("READING_SOURCE_REVIEW_RENDER_INVALID") from exc
    if (hashlib.sha256(blob).hexdigest(), raster["width"], raster["height"]) != (
            review.image_sha256, review.width_px, review.height_px):
        raise ReviewProofError("READING_SOURCE_REVIEW_RASTER_MISMATCH")
    return blob, raster


def _missing_witnesses(review: PdfRegionReview, witnesses: list[str]) -> list[str]:
    entries = [_witness(entry.text) for entry in review.entries]
    missing, required = [], Counter()
    for witness in witnesses:
        expected = _witness(witness)
        for correction in review.corrections:
            expected = expected.replace(_witness(correction.ocr_text), _witness(correction.source_text))
        required[expected] += 1
        if not expected or required[expected] > sum(text.count(expected) for text in entries):
            missing.append(witness)
    return missing


def prepare_region_review(ctx, source, page_unit_id: str, review: PdfRegionReview) -> dict:
    """Re-render and authenticate on every execution, including a page cache hit."""
    from .provider import ProviderFailure
    from .reading_review_origin import verify_review_origin

    identity = review.model_dump(mode="json", include={"source_id", "source_sha256", "page", "locator", "bbox",
                                                     "image_sha256", "width_px", "height_px", "origin"})
    record = {"kind": KIND, "version": VERSION, "scope": "diagnostic_replay", **identity}
    try:
        image, renderer = _render(source, page_unit_id, review)
        corrections = [item.model_dump(mode="json") for item in review.corrections]
        try:
            history = verify_review_origin(ctx, identity["origin"], source_id=review.source_id,
                source_sha256=review.source_sha256, page=review.page, locator=review.locator,
                bbox=review.bbox, image_sha256=review.image_sha256,
                **({"corrections": corrections} if corrections else {}))
        except ProviderFailure as exc:
            return {**record, "status": "failed", "error_code": "READING_SOURCE_REVIEW_ORIGIN_INVALID",
                    "origin_verification_error": exc.code}
        frozen = {"version": VERSION, "review": review.model_dump(mode="json"),
                  "renderer": renderer, "historical_ocr": history}
        target_id = digest(identity)
        stored = ctx.run_unit("source-review-" + target_id, frozen,
                              lambda: {"image": ctx.store_bytes("tender-source-review.png", image, "image/png")})
        try:
            ref = stored["image"]
            if (set(stored) != {"image"} or ref["sha256"] != review.image_sha256 or
                    ref["size_bytes"] != len(image) or ref["media_type"] != "image/png" or
                    ctx.read_artifact(ref) != image):
                raise ValueError("Source review raster checkpoint disagrees with restoration.")
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            raise ReviewProofError("READING_SOURCE_REVIEW_CHECKPOINT_INVALID") from exc
    except ReviewProofError as exc:
        return {**record, "status": "failed", "error_code": exc.code}

    missing = _missing_witnesses(review, history["historical_required_text"])
    critical = any(fragment.critical for fragment in review.unknown)
    error = ("READING_SOURCE_REVIEW_CRITICAL_UNREAD" if critical else
             "READING_SOURCE_REVIEW_WITNESS_MISSING" if missing else None)
    return {**record, "status": "unresolved" if error else "confirmed", "error_code": error,
            "reviewed_by": review.reviewed_by, "renderer": renderer, "reviewed_image": ref,
            "entries": [item.model_dump(mode="json") | {"locator": review.locator + f"/source-review/entry[{index}]"}
                        for index, item in enumerate(review.entries)],
            "unknown": [item.model_dump(mode="json") for item in review.unknown], "corrections": corrections,
            "historical_ocr": history, "missing_historical_witnesses": missing}


def review_box_on_page(record: dict, pixels: list[int]) -> list[float]:
    """Corresponding PDF point range, clamped to the exact screenshot extent."""
    raster = record["renderer"]
    crop_left, crop_top, _, _ = raster["region"]["pixel_crop"]
    page_width, page_height = raster["region"]["page_size"]
    pixel_width, pixel_height = raster["full_page_pixels"]
    x, y, right, bottom = pixels
    x0, y0, x1, y1 = record["bbox"]
    return [max(x0, (crop_left + x) * page_width / pixel_width),
            max(y0, (crop_top + y) * page_height / pixel_height),
            min(x1, (crop_left + right) * page_width / pixel_width),
            min(y1, (crop_top + bottom) * page_height / pixel_height)]
