"""N1 native reading with source-addressable units and an honest coverage ledger.

Independent implementation. Design references: reviewed legacy baseline
53f20a3a, extraction/parser/{pdf,docx}.py and extraction/ocr/service.py.
No legacy imports, ORM, provider, background task, or document mutation.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import hashlib
import io
import json
import math
from pathlib import Path
import re
from typing import Any, Literal
import unicodedata
import zipfile

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
import pdfplumber
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pypdf import PdfReader

from .pdf_table_geometry import discover_pdf_tables, embedded_pdf_glyph_measurements, object_box
from .pdf_text_lines import horizontal_matrix as _horizontal_matrix, native_baseline_tops, pdf_page_text_lines, projected_chars
from .reading_source_review import (KIND as _SOURCE_REVIEW_KIND, VERSION as _SOURCE_REVIEW_VERSION,
                                    PdfRegionReview, prepare_region_review, review_box_on_page)

READER_VERSION = "native-reading-4"
Role = Literal["tender", "company_material", "reference_bid", "project_supplement"]
Status = Literal["read", "empty", "needs_ocr", "failed", "unsupported"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GlyphMap(StrictModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    font: str = Field(min_length=1)
    codepoint: str = Field(pattern=r"^U\+[0-9A-Fa-f]{4,6}$")
    replacement: str = Field(min_length=1, max_length=1)
    pages: list[int] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def precise_rule(self):
        code = int(self.codepoint[2:], 16)
        if code > 0x10FFFF or any(page < 1 for page in self.pages) or len(set(self.pages)) != len(self.pages):
            raise ValueError("invalid glyph identity or page scope")
        if (not self.font.strip() or not self.reason.strip() or not self.replacement.strip()
                or unicodedata.category(chr(code)) != "Co" or unicodedata.category(self.replacement) == "Co"):
            raise ValueError("glyph maps require an interpreted replacement and recorded reason")
        return self


class NativeIgnoreRule(StrictModel):
    """A manual, source-bound decision; never an automatic visibility test."""

    rule_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    source_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Role
    locator: str = Field(pattern=r"^pdf/page\[[1-9][0-9]*\]/line\[[0-9]+\]$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox: list[float] = Field(min_length=4, max_length=4)
    reason: Literal["occluded_by_verified_decorative_image"]
    explanation: str = Field(min_length=1, max_length=2000)
    visual_evidence_path: Path
    visual_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("bbox", mode="before")
    @classmethod
    def finite_box(cls, value):
        if (not isinstance(value, list) or len(value) != 4 or
                any(type(number) not in (int, float) or not math.isfinite(number) for number in value)):
            raise ValueError("native ignore requires four finite coordinates")
        if value[0] < 0 or value[1] < 0 or value[2] <= value[0] or value[3] <= value[1]:
            raise ValueError("native ignore requires a nonempty page box")
        return value

    @model_validator(mode="after")
    def explicit_explanation(self):
        if not self.explanation.strip():
            raise ValueError("native ignore requires a controlled review explanation")
        return self


class DiagnosticPdfRange(StrictModel):
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_page: int = Field(strict=True, ge=1)
    last_page: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.first_page > self.last_page:
            raise ValueError("reversed diagnostic page range")
        return self


class ReadingConfig(StrictModel):
    diagnostic_pdf_range: DiagnosticPdfRange | None = None
    required_roles: list[Role] = Field(default_factory=lambda: ["tender"])
    ocr_roles: list[Role] | None = None
    ocr_version: str = "unconfigured"
    pdf_min_text_chars: int = Field(default=15, ge=1, le=1000)
    table_settings: dict[str, Any] = Field(default_factory=dict)
    # Exact '<source_sha256>::<source_id>::<locator>' -> reason; no wildcard.
    ignored_objects: dict[str, str] = Field(default_factory=dict)
    glyph_maps: list[GlyphMap] = Field(default_factory=list)
    native_ignored_objects: list[NativeIgnoreRule] = Field(default_factory=list)
    pdf_region_reviews: list[PdfRegionReview] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def explicit_ignores(self):
        if any(not re.fullmatch(r"[0-9a-f]{64}::.+::.+", key) or not reason.strip()
               for key, reason in self.ignored_objects.items()):
            raise ValueError("ignored objects require a hash, exact scoped locator and reason")
        if any("/unmapped-glyphs" in key for key in self.ignored_objects):
            raise ValueError("unmapped glyphs require interpretation or OCR, never an ignore rule")
        for index, rule in enumerate(self.glyph_maps):
            for other in self.glyph_maps[:index]:
                if (rule.source_sha256, rule.font, int(rule.codepoint[2:], 16)) == (
                    other.source_sha256, other.font, int(other.codepoint[2:], 16)) and set(rule.pages) & set(other.pages):
                    raise ValueError("overlapping glyph rules")
        identities, anchors = set(), set()
        for rule in self.native_ignored_objects:
            anchor = (rule.source_id, rule.source_sha256, rule.role, rule.locator)
            if rule.rule_id in identities or anchor in anchors:
                raise ValueError("duplicate native ignore rule or target")
            identities.add(rule.rule_id)
            anchors.add(anchor)
        targets = {(review.source_id, review.source_sha256, review.locator) for review in self.pdf_region_reviews}
        origins = {(review.origin.run_id, review.origin.call_id) for review in self.pdf_region_reviews}
        if len(targets) != len(self.pdf_region_reviews) or len(origins) != len(self.pdf_region_reviews):
            raise ValueError("A PDF screenshot review requires a unique target and historical call.")
        for review in self.pdf_region_reviews:
            prefix = f"{review.source_sha256}::{review.source_id}::pdf/page[{review.page}]/image["
            if any(key.startswith(prefix) for key in self.ignored_objects):
                raise ValueError("Screenshots on a source review page require explicit reading.")
        return self


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Role
    title: str = Field(min_length=1)


class Merge(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)


class ReadingBlock(StrictModel):
    block_id: str
    kind: Literal["paragraph", "table", "image"]
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    merges: list[Merge] = Field(default_factory=list)
    column_widths: list[float] = Field(default_factory=list)
    header_rows: list[int] = Field(default_factory=list)
    locator: str
    source_id: str
    origin: Literal["native", "ocr", "source_review"] = "native"
    bbox: list[float] | None = None
    image_ref: dict[str, Any] | None = None

    @model_validator(mode="after")
    def grid_integrity(self):
        if self.kind != "table":
            if self.rows or self.merges or self.column_widths or self.header_rows:
                raise ValueError("only tables have grid fields")
            return self
        columns = len(self.rows[0]) if self.rows else 0
        if not columns or any(len(row) != columns for row in self.rows):
            raise ValueError("table grid must be nonempty and rectangular")
        if len(self.column_widths) != columns or any(width <= 0 for width in self.column_widths):
            raise ValueError("table widths must follow all grid columns")
        occupied: set[tuple[int, int]] = set()
        for merge in self.merges:
            if merge.row + merge.row_span > len(self.rows) or merge.column + merge.col_span > columns:
                raise ValueError("merge leaves table")
            for row in range(merge.row, merge.row + merge.row_span):
                for col in range(merge.column, merge.column + merge.col_span):
                    if (row, col) in occupied:
                        raise ValueError("overlapping table merges")
                    occupied.add((row, col))
                    if (row, col) != (merge.row, merge.column) and self.rows[row][col]:
                        raise ValueError("merged continuation must be blank")
        if any(index < 0 or index >= len(self.rows) for index in self.header_rows):
            raise ValueError("invalid header row")
        return self


class UnitCoverage(StrictModel):
    status: Status
    origin: Literal["native", "ocr", "mixed", "source_review"] = "native"
    native_text_chars: int = 0
    table_count: int = 0
    image_count: int = 0
    unread_objects: list[str] = Field(default_factory=list)
    ignored_objects: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    transformations: list[dict[str, Any]] = Field(default_factory=list)
    unmapped_glyphs: list[dict[str, Any]] = Field(default_factory=list)


class ReadingUnit(StrictModel):
    unit_id: str
    source_id: str
    locator: str
    page: int | None = None
    blocks: list[ReadingBlock] = Field(default_factory=list)
    coverage: UnitCoverage


class OcrResponse(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    unit_id: str
    locator: str
    page: int | None
    text: str = ""
    # Only content fields are accepted. The reader owns source IDs and block IDs.
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class ReadingFailure(RuntimeError):
    def __init__(self, code: str, result: dict | None = None):
        self.code = code
        self.result = result
        super().__init__(code)


class _UnitFailure(ReadingFailure):
    def __init__(self, code: str, unit: ReadingUnit):
        super().__init__(code)
        self.unit = unit


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_NATIVE_IGNORE_BASIS = "explicit_source_bound_manual_ignore"
_NATIVE_BOX_TOLERANCE = 0.000001


def _native_rule_metadata(rule: NativeIgnoreRule) -> dict:
    # The mutable source PNG path belongs only in the frozen unit input.
    return rule.model_dump(mode="json", exclude={"visual_evidence_path"})


def _native_capture(ctx, name: str, value: dict) -> None:
    result = ctx.capture(name, value)
    if isinstance(result, dict) and result.get("status") == "write_failed":
        raise ReadingFailure("READING_CAPTURE_FAILED")


def _prepare_native_rules(ctx, sources: list[SourceDocument], config: ReadingConfig) -> dict[str, dict]:
    """Freeze reviewed visual evidence once, then resume from its artifact."""
    evidence = {}
    by_id = {source.source_id: source for source in sources}
    for rule in config.native_ignored_objects:
        metadata = _native_rule_metadata(rule)
        key = hashlib.sha256(json.dumps(rule.model_dump(mode="json"), sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()[:24]
        capture_name = "native-ignore-rule-" + key
        source = by_id.get(rule.source_id)
        if source is None or source.sha256 != rule.source_sha256 or source.role != rule.role:
            _native_capture(ctx, capture_name, {"basis": _NATIVE_IGNORE_BASIS, "status": "source_mismatch", "rule": metadata})
            raise ReadingFailure("READING_NATIVE_IGNORE_SOURCE_MISMATCH")

        def freeze(rule=rule):
            try:
                content = rule.visual_evidence_path.read_bytes()
                if hashlib.sha256(content).hexdigest() != rule.visual_evidence_sha256:
                    raise ValueError
                with Image.open(io.BytesIO(content)) as image:
                    if image.format != "PNG":
                        raise ValueError
                    image.verify()
            except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
                raise ReadingFailure("READING_NATIVE_IGNORE_EVIDENCE_INVALID") from exc
            reference = ctx.store_bytes("native-ignore-visual", content, "image/png")
            return {"rule_id": rule.rule_id, "visual_evidence": reference}

        try:
            frozen = ctx.run_unit("N1-native-ignore-proof-" + key,
                                  {"reader_version": READER_VERSION, "basis": _NATIVE_IGNORE_BASIS,
                                   "rule": rule.model_dump(mode="json")}, freeze)
            reference = frozen.get("visual_evidence") if isinstance(frozen, dict) else None
            if (not isinstance(reference, dict) or frozen.get("rule_id") != rule.rule_id or
                    not isinstance(reference.get("artifact_id"), str) or
                    not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", reference["artifact_id"]) or
                    not isinstance(reference.get("path"), str) or not reference["path"] or
                    type(reference.get("size_bytes")) is not int or reference["size_bytes"] < 0 or
                    reference.get("sha256") != rule.visual_evidence_sha256 or reference.get("media_type") != "image/png"):
                raise ReadingFailure("READING_NATIVE_IGNORE_EVIDENCE_INVALID")
            try:
                content = ctx.read_artifact(reference)
            except Exception as exc:
                raise ReadingFailure("READING_NATIVE_IGNORE_EVIDENCE_INVALID") from exc
            if len(content) != reference["size_bytes"] or hashlib.sha256(content).hexdigest() != rule.visual_evidence_sha256:
                raise ReadingFailure("READING_NATIVE_IGNORE_EVIDENCE_INVALID")
        except ReadingFailure as exc:
            _native_capture(ctx, capture_name, {"basis": _NATIVE_IGNORE_BASIS, "status": "failed",
                                               "error_code": exc.code, "rule": metadata})
            raise
        evidence[rule.rule_id] = reference
        _native_capture(ctx, capture_name, {"basis": _NATIVE_IGNORE_BASIS, "status": "evidence_frozen",
                                           "rule": metadata, "visual_evidence": reference,
                                           "automatic_visual_verification": False})
    return evidence


def _native_fail(unit: ReadingUnit, code: str):
    unit.coverage.status, unit.coverage.error_code = "failed", code
    raise _UnitFailure(code, unit)


def _native_decisions(source: SourceDocument, unit: ReadingUnit, config: ReadingConfig,
                      evidence: dict[str, dict]) -> list[dict[str, str]]:
    decisions = []
    for rule in config.native_ignored_objects:
        if rule.source_id != source.source_id or rule.locator.rsplit("/line[", 1)[0] != unit.locator:
            continue
        blocks = [block for block in unit.blocks if block.locator == rule.locator]
        if not blocks:
            _native_fail(unit, "READING_NATIVE_IGNORE_UNMATCHED")
        if len(blocks) != 1:
            _native_fail(unit, "READING_NATIVE_IGNORE_OBJECT_MISMATCH")
        block = blocks[0]
        if (block.source_id != rule.source_id or source.sha256 != rule.source_sha256 or source.role != rule.role or
                block.kind != "paragraph" or block.origin != "native" or
                hashlib.sha256(block.text.encode("utf-8")).hexdigest() != rule.text_sha256 or
                block.bbox is None or len(block.bbox) != 4 or
                any(not math.isfinite(actual) or abs(actual - expected) > _NATIVE_BOX_TOLERANCE
                    for actual, expected in zip(block.bbox, rule.bbox))):
            _native_fail(unit, "READING_NATIVE_IGNORE_OBJECT_MISMATCH")
        reference = evidence[rule.rule_id]
        decisions.append({"native_ignore_rule_id": rule.rule_id, "basis": _NATIVE_IGNORE_BASIS,
                          "locator": block.locator, "reason": rule.reason, "explanation": rule.explanation,
                          "source_id": source.source_id, "source_sha256": source.sha256, "role": source.role,
                          "text_sha256": rule.text_sha256,
                          "bbox_sha256": hashlib.sha256(json.dumps(block.bbox, separators=(",", ":")).encode()).hexdigest(),
                          "visual_evidence_artifact_id": reference["artifact_id"],
                          "visual_evidence_sha256": reference["sha256"]})
    return decisions


def _verify_native_decisions(source, unit, config, evidence):
    expected = _native_decisions(source, unit, config, evidence)
    locators = {rule.locator for rule in config.native_ignored_objects if rule.source_id == source.source_id}
    actual = [row for row in unit.coverage.ignored_objects if
              row.get("basis") == _NATIVE_IGNORE_BASIS or "native_ignore_rule_id" in row or row.get("locator") in locators]
    if actual != expected:
        _native_fail(unit, "READING_NATIVE_IGNORE_CHECKPOINT_INVALID")


def _native_rule_ledger(documents: list[dict], config: ReadingConfig, evidence: dict[str, dict]) -> list[dict]:
    rows = []
    for rule in config.native_ignored_objects:
        matches = [{"source_id": doc["source_id"], "unit_id": unit["unit_id"], "block_locator": entry["locator"]}
                   for doc in documents for unit in doc["units"]
                   for entry in unit["coverage"]["ignored_objects"]
                   if entry.get("native_ignore_rule_id") == rule.rule_id and
                   not (unit["coverage"].get("error_code") or "").startswith("READING_NATIVE_IGNORE_")]
        rows.append({**_native_rule_metadata(rule), "basis": _NATIVE_IGNORE_BASIS,
                     "visual_evidence": evidence[rule.rule_id], "matches": matches, "match_count": len(matches),
                     "status": "matched" if len(matches) == 1 else "unmatched_or_duplicate",
                     "automatic_visual_verification": False})
    return rows


def _unit_id(source: SourceDocument, suffix: str) -> str:
    return "N1-" + hashlib.sha256(source.source_id.encode()).hexdigest()[:16] + "-" + suffix


def _block(source: SourceDocument, locator: str, kind: str, **values) -> ReadingBlock:
    return ReadingBlock(block_id="b-" + hashlib.sha256((source.source_id + "::" + locator).encode()).hexdigest()[:24],
                        kind=kind, source_id=source.source_id, locator=locator, **values)


def _inside(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _ignored(config: ReadingConfig, source: SourceDocument, locator: str, coverage: UnitCoverage) -> bool:
    reason = config.ignored_objects.get(source.sha256 + "::" + source.source_id + "::" + locator)
    if reason is None:
        return False
    coverage.ignored_objects.append({"locator": locator, "reason": reason})
    return True


def _bounded_ocr_code(value) -> str | None:
    if (isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value) and
            value.startswith(("PROVIDER_", "OCR_", "DIAGNOSTICS_"))):
        return value
    return None


def _ocr_content(source: SourceDocument, unit: ReadingUnit, locator: str, config: ReadingConfig,
                 ocr: Callable | None, request_extra: dict) -> list[ReadingBlock] | None:
    roles = set(config.ocr_roles if config.ocr_roles is not None else config.required_roles) | {"tender"}
    if source.role not in roles:
        return None
    if ocr is None:
        raise _UnitFailure("OCR_REQUIRED", unit)
    if config.ocr_version == "unconfigured" or not config.ocr_version.strip():
        raise _UnitFailure("OCR_VERSION_REQUIRED", unit)
    request = {"source_id": source.source_id, "sha256": source.sha256, "path": str(source.path),
               "unit_id": unit.unit_id, "locator": locator, "page": unit.page,
               "ocr_version": config.ocr_version, **request_extra}
    try:
        response = OcrResponse.model_validate(ocr(request))
        if (response.source_id, response.unit_id, response.locator, response.page) != (
                source.source_id, unit.unit_id, locator, unit.page):
            raise ValueError("identity mismatch")
        blocks = []
        if response.text.strip():
            blocks.append(_block(source, locator + "/ocr/text", "paragraph", text=response.text.strip(), origin="ocr",
                                 bbox=request_extra.get("bbox")))
        for index, raw in enumerate(response.blocks):
            if set(raw) - {"kind", "text", "rows", "merges", "column_widths", "header_rows", "bbox"}:
                raise ValueError("OCR cannot assign source identities")
            if raw.get("kind") not in {"paragraph", "table"}:
                raise ValueError("unsupported OCR content")
            content = {"bbox": request_extra.get("bbox"), **raw}
            blocks.append(_block(source, locator + f"/ocr/block[{index}]", origin="ocr", **content))
        if not any(block.text.strip() or any(any(cell.strip() for cell in row) for row in block.rows) for block in blocks):
            raise _UnitFailure("OCR_EMPTY_RESULT", unit)
        if request_extra.get("object_kind") == "pdf_page" and unit.coverage.table_count and not any(b.kind == "table" for b in blocks):
            raise _UnitFailure("OCR_STRUCTURED_TABLE_REQUIRED", unit)
        return blocks
    except _UnitFailure:
        raise
    except Exception as exc:
        code = _bounded_ocr_code(getattr(exc, "code", None)) or _bounded_ocr_code(getattr(exc, "error_code", None))
        raise _UnitFailure(code or "OCR_FAILED_OR_IDENTITY_INVALID", unit) from None


def _source_review_blocks(source, record):
    return [_block(source, entry["locator"], "paragraph", text=entry["text"], origin="source_review",
                   bbox=review_box_on_page(record, entry["pixel_bbox"])) for entry in record.get("entries", [])]


def _block_position(block):
    return (block.bbox or [0, 0])[1], (block.bbox or [0, 0])[0]


def _source_review_failure(unit, code):
    unit.coverage.status, unit.coverage.error_code = "failed", code
    return _UnitFailure(code, unit)


def _source_review_coverage_origin(unit):
    return ("mixed" if any(block.origin != "source_review" and block.kind != "image" for block in unit.blocks)
            else "source_review")


def _verify_source_review_checkpoint(source, unit, reviews):
    records = [record for record in unit.coverage.transformations if record.get("kind") == _SOURCE_REVIEW_KIND]
    blocks = [block for block in unit.blocks if block.origin == "source_review" or "/source-review/" in block.locator]
    expected = sorted((block for record in reviews.values() for block in _source_review_blocks(source, record)),
                      key=_block_position)
    if records != list(reviews.values()) or blocks != expected:
        raise ReadingFailure("READING_SOURCE_REVIEW_CHECKPOINT_INVALID")
    if reviews and (unit.coverage.origin != _source_review_coverage_origin(unit) or
                    unit.coverage.status != "read" or unit.coverage.error_code is not None or
                    unit.coverage.unread_objects):
        raise ReadingFailure("READING_SOURCE_REVIEW_CHECKPOINT_INVALID")
    for record in reviews.values():
        locator = record["locator"]
        original = [block for block in unit.blocks if block.locator == locator]
        if (record["status"] != "confirmed" or len(original) != 1 or original[0].kind != "image" or
                original[0].source_id != source.source_id or original[0].origin != "native" or
                original[0].bbox != record["bbox"] or any(row.get("locator") == locator for row in unit.coverage.ignored_objects)):
            raise ReadingFailure("READING_SOURCE_REVIEW_CHECKPOINT_INVALID")


def _resolve_unread(source, unit, config, ocr, requests, reviews=None):
    reviews = reviews or {}
    pending = [(locator, request) for locator, request in requests
               if not _ignored(config, source, locator, unit.coverage)]
    unit.coverage.unread_objects.extend(locator for locator, _ in pending)
    unmatched = reviews.keys() - set(unit.coverage.unread_objects)
    if unmatched:
        unit.coverage.unread_objects.extend(sorted(unmatched))
        raise _source_review_failure(unit, "READING_SOURCE_REVIEW_OBJECT_MISMATCH")
    # A whole-page request covers its regions too; do not make redundant calls
    # or count native text and whole-page OCR as two independent paragraphs.
    full_page = next(((locator, request) for locator, request in pending if request.get("object_kind") == "pdf_page"), None)
    if full_page is not None:
        if reviews:
            # An overlapping full-page replacement cannot certify independent
            # screenshot fragments or preserve the native context around them.
            raise _source_review_failure(unit, "READING_SOURCE_REVIEW_WHOLE_PAGE_REQUIRED")
        pending = [full_page]
    first_content_failure = None
    for locator, request in pending:
        if locator in reviews:
            review = reviews[locator]
            unit.blocks.extend(_source_review_blocks(source, review))
            if review["status"] == "confirmed":
                unit.coverage.unread_objects.remove(locator)
            elif first_content_failure is None:
                first_content_failure = _source_review_failure(unit, review["error_code"])
            continue
        try:
            blocks = _ocr_content(source, unit, locator, config, ocr, request)
        except _UnitFailure as exc:
            unit.coverage.status = "needs_ocr" if exc.code == "OCR_REQUIRED" else "failed"
            unit.coverage.error_code = exc.code
            if exc.code in getattr(ocr, "independent_object_errors", ()):
                unit.coverage.transformations.append({"kind": "ocr_object_unread", "locator": locator,
                                                      "error_code": exc.code})
                if first_content_failure is None:
                    first_content_failure = exc
                continue
            raise
        if blocks is not None:
            if request.get("object_kind") == "pdf_page":
                replaced = sum(block.kind != "image" for block in unit.blocks)
                unit.blocks = [block for block in unit.blocks if block.kind == "image"]
                unit.coverage.transformations.append({"kind": "ocr_full_page_replacement", "locator": locator,
                                                       "native_content_blocks_replaced": replaced})
                unit.coverage.unread_objects.clear()
            else:
                unit.coverage.unread_objects.remove(locator)
            unit.blocks.extend(blocks)
    if any(block.origin == "source_review" for block in unit.blocks):
        unit.coverage.origin = _source_review_coverage_origin(unit)
    if first_content_failure is not None:
        unit.coverage.status = "failed"
        unit.coverage.error_code = first_content_failure.code
        raise first_content_failure
    if unit.coverage.unread_objects:
        unit.coverage.status = "needs_ocr"
    elif reviews:
        unit.coverage.status = "read"
    elif any(block.origin == "ocr" for block in unit.blocks):
        unit.coverage.status = "read"
        unit.coverage.origin = "mixed" if any(b.origin == "native" and b.kind != "image" for b in unit.blocks) else "ocr"
    else:
        unit.coverage.status = "read" if unit.coverage.native_text_chars or unit.coverage.table_count else "empty"


class PdfTableCellOwnershipError(ValueError):
    """A native glyph has conflicting table cell ownership evidence."""


def _same_table_baseline(char, matrix, peer):
    other = _horizontal_matrix(peer)
    return (other is not None and abs(peer["size"] - char["size"]) <= .25
            and all(abs(a - b) <= .0001 for a, b in zip(matrix[:4], other[:4]))
            and abs(matrix[5] - other[5]) <= .15)


def _bracketed_table_glyphs(separator, matrix, misplaced, anchors):
    """Prove each adjacent excursion on a separator's line with two local anchors."""
    gap = separator["size"] * .15
    line = sorted((peer for peer in misplaced if _same_table_baseline(separator, matrix, peer)),
                  key=lambda peer: peer["x0"])
    spans = []
    for peer in line:
        if not spans or abs(peer["x0"] - spans[-1][-1]["x1"]) > gap:
            spans.append([])
        spans[-1].append(peer)
    anchors = [peer for peer in anchors if _same_table_baseline(separator, matrix, peer)
               and (peer["text"].isalnum() or peer["text"] in {"(", "（", ")", "）", ";", "；"})]
    results = []
    for span in spans:
        left, right = span[0], span[-1]
        before = [peer for peer in anchors if peer["x0"] < left["x0"] and abs(left["x0"] - peer["x1"]) <= gap]
        after = [peer for peer in anchors if peer["x1"] > right["x1"] and abs(peer["x0"] - right["x1"]) <= gap]
        if (len(before) != 1 or len(after) != 1
                or round(before[0]["top"], 1) != round(after[0]["top"], 1)
                or not (before[0]["text"].isalnum() or after[0]["text"].isalnum())):
            raise PdfTableCellOwnershipError
        results.extend((peer, before[0]["top"]) for peer in span)
    return results


def _pdf_table_cell_characters(page, table, locator, transformations=None, *, source_path=None, outside_claims=None):
    """Share native cell membership across reading and response-format geometry."""
    boxes = [tuple(float(value) for value in box) for box in table.cells if box is not None]
    if not boxes:
        raise ValueError("table has no cells")
    groups = {box: [] for box in boxes}
    border_candidates = []
    for char in page.chars:
        x, y = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
        owners = [box for box in groups if _inside(box, x, y)]
        if char["text"].strip() and _inside(table.bbox, x, y) and not owners:
            raise ValueError("unassigned table text")
        if char["text"].strip() and len(owners) > 1:
            raise PdfTableCellOwnershipError
        for owner in owners:
            groups[owner].append(char)
        if char["text"].strip() and len(owners) <= 1:
            owner = owners[0] if owners else None
            if (owner is not None and (char["top"] < owner[1] or char["bottom"] > owner[3])
                    or owner is None and outside_claims is not None and table.bbox[0] <= x < table.bbox[2]
                       and any(char["top"] < border < char["bottom"] for border in (table.bbox[1], table.bbox[3]))):
                border_candidates.append((char, x, y, owner))

    order_tops = {}
    if getattr(page, "rotation", None) != 0:
        return groups, order_tops
    crossings = {}
    for char, x, y, owner in border_candidates:
        matrix = _horizontal_matrix(char)
        if matrix is None:
            continue
        # pdfminer applies the MediaBox translation to the text matrix. Use
        # the page adapter's conversion from that coordinate system to top-left.
        _, baseline = page.point2coord((matrix[4], matrix[5]))
        if owner is not None and _inside(owner, x, baseline):
            continue
        contenders = [box for box in groups if _inside(box, x, baseline)]
        if len(contenders) != 1:
            if contenders:
                raise PdfTableCellOwnershipError
            continue
        target, = contenders
        if owner is not None:
            border = owner[1] if baseline < owner[1] else owner[3]
            target_border = target[3] if baseline < owner[1] else target[1]
            left, right = max(owner[0], target[0]), min(owner[2], target[2])
        else:
            border = table.bbox[1] if y < table.bbox[1] else table.bbox[3]
            target_border = target[1] if y < table.bbox[1] else target[3]
            left, right = target[0], target[2]
        if (abs(border - target_border) > .01 or not char["top"] < border < char["bottom"]
                or not left <= char["x0"] <= char["x1"] <= right
                or not any(abs(edge["top"] - border) <= .75 and abs(edge["bottom"] - border) <= .75
                           and edge.get("stroke") is not False and edge["x0"] <= char["x0"]
                           and char["x1"] <= edge["x1"] for edge in page.edges)):
            raise PdfTableCellOwnershipError
        crossings.setdefault((owner, target), []).append(char)

    measurements = (embedded_pdf_glyph_measurements(source_path, page, [char for glyphs in crossings.values() for char in glyphs])
                    if crossings and source_path is not None else {})
    for (owner, target), chars in crossings.items():
        unresolved = [char for char in chars if id(char) not in measurements]
        if unresolved:
            anchors = [peer for peer in groups[target] if target[1] <= peer["top"] <= peer["bottom"] <= target[3]]
            for separator in unresolved:
                if owner is not None and separator["text"] in {"/", "／"} and id(separator) not in order_tops:
                    moves = _bracketed_table_glyphs(separator, separator["matrix"], unresolved, anchors)
                    order_tops.update((id(peer), line_top) for peer, line_top in moves)
            if any(id(char) not in order_tops for char in unresolved):
                raise PdfTableCellOwnershipError
        for char in chars:
            measured = measurements.get(id(char))
            tight = measured["ink_bbox"] if measured is not None else None
            if measured is not None:
                mode, width = measured["text_render_mode"], measured["stroke_width_pt"]
                if mode not in (0, 1, 2) or mode != 0 and (width is None or width < 0):
                    raise PdfTableCellOwnershipError
                stroke = width / 2 if mode != 0 else 0
                if not (target[0] <= tight[0] - stroke < tight[2] + stroke <= target[2]
                        and target[1] <= tight[1] - stroke < tight[3] + stroke <= target[3]):
                    raise PdfTableCellOwnershipError
            if owner is None:
                if id(char) in outside_claims:
                    raise PdfTableCellOwnershipError
                outside_claims.add(id(char))
            else:
                groups[owner].remove(char)
            groups[target].append(char)
            if transformations is not None:
                kind = ("native_pdf_table_separator_ownership" if char["text"] in {"/", "／"}
                        else "native_pdf_table_baseline_ownership")
                evidence = {"kind": kind, "locator": locator, "glyph_bbox": list(object_box(char)),
                    "baseline_top": page.point2coord(char["matrix"][4:])[1],
                    "center_cell_bbox": list(owner) if owner is not None else None, "owning_cell_bbox": list(target),
                    "basis": "embedded_font_glyph_outline" if tight is not None else "bracketed_native_baseline"}
                if tight is not None:
                    evidence["embedded_glyph_bbox"] = list(tight)
                transformations.append(evidence)
    return groups, order_tops


def _table_text(chars, order_tops):
    # Only the extraction projection uses line tops. Native run rectangles and
    # the source PDF keep their measured coordinates and every original glyph.
    extracted = projected_chars(chars, native_baseline_tops(chars, order_tops))
    return (pdfplumber.utils.extract_text(extracted) or "").strip() if extracted else ""


def _pdf_table(source, page, table, locator, *, assignment=None, transformations=None, outside_claims=None):
    groups, order_tops = assignment if assignment is not None else _pdf_table_cell_characters(
        page, table, locator, transformations, source_path=source.path, outside_claims=outside_claims)
    boxes = list(groups)
    # Native rectangle boundaries preserve horizontal and vertical spans.
    xs = sorted({round(v, 3) for box in boxes for v in (box[0], box[2])})
    ys = sorted({round(v, 3) for box in boxes for v in (box[1], box[3])})
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("invalid table boundaries")
    rows = [["" for _ in xs[:-1]] for _ in ys[:-1]]
    merges = []
    for box in boxes:
        left, top, right, bottom = (round(v, 3) for v in box)
        col, row = xs.index(left), ys.index(top)
        span_c, span_r = xs.index(right) - col, ys.index(bottom) - row
        rows[row][col] = _table_text(groups[box], order_tops)
        if span_c > 1 or span_r > 1:
            merges.append(Merge(row=row, column=col, row_span=span_r, col_span=span_c))
    return _block(source, locator, "table", rows=rows, merges=merges,
                  column_widths=[xs[i + 1] - xs[i] for i in range(len(xs) - 1)], bbox=list(table.bbox))


def _map_pdf_glyphs(source, page, number, config, coverage):
    """Interpret only explicitly scoped glyphs in memory; original bytes stay unchanged."""
    for char in page.chars:
        text = str(char.get("text", ""))
        font = str(char.get("fontname", ""))
        result = []
        for symbol in text:
            rule = next((rule for rule in config.glyph_maps if rule.source_sha256 == source.sha256
                         and rule.font == font and int(rule.codepoint[2:], 16) == ord(symbol)
                         and number in rule.pages), None)
            location = {"page": number, "font": font, "codepoint": f"U+{ord(symbol):04X}",
                        "bbox": [float(char[key]) for key in ("x0", "top", "x1", "bottom")]}
            if rule is not None:
                result.append(rule.replacement)
                coverage.transformations.append({"kind": "glyph_map", "source_sha256": source.sha256,
                    **location, "original_symbol": symbol, "replacement": rule.replacement, "reason": rule.reason})
            else:
                result.append(symbol)
                if unicodedata.category(symbol) == "Co":
                    coverage.unmapped_glyphs.append(location)
        if "".join(result) != text:
            char["text"] = "".join(result)


def _read_pdf_page(source, page, number, config, ocr, reviews=None):
    locator = f"pdf/page[{number}]"
    unit = ReadingUnit(unit_id=_unit_id(source, f"pdf-p{number:05d}"), source_id=source.source_id,
                       locator=locator, page=number, coverage=UnitCoverage(status="empty"))
    if reviews:
        unit.coverage.transformations.extend(reviews.values())
    try:
        _map_pdf_glyphs(source, page, number, config, unit.coverage)
        discovery = discover_pdf_tables(page, config.table_settings)
        tables = discovery.tables
        if discovery.text_backgrounds or discovery.diagonal_rules:
            unit.coverage.transformations.append(discovery.transformation(locator))
        table_boxes = [tuple(table.bbox) for table in tables]
        outside_claims = set()
        for index, table in enumerate(tables):
            unit.blocks.append(_pdf_table(source, page, table, locator + f"/table[{index}]",
                                          transformations=unit.coverage.transformations, outside_claims=outside_claims))
        for index, text, bbox, _, _ in pdf_page_text_lines(page, table_boxes, outside_claims):
            unit.blocks.append(_block(source, locator + f"/line[{index}]", "paragraph", text=text, bbox=bbox))
        requests = []
        for index, image in enumerate(page.images):
            image_locator = locator + f"/image[{index}]"
            box = [float(image[k]) for k in ("x0", "top", "x1", "bottom")]
            unit.blocks.append(_block(source, image_locator, "image", bbox=box,
                                      image_ref={"pdf_image_index": index, "width_px": image.get("srcsize", [None, None])[0],
                                                 "height_px": image.get("srcsize", [None, None])[1]}))
            requests.append((image_locator, {"object_kind": "pdf_region", "bbox": box}))
        native = sum(len(block.text) + sum(len(cell) for row in block.rows for cell in row) for block in unit.blocks)
        unit.coverage.native_text_chars = native
        if native < config.pdf_min_text_chars and page.images:
            unit.coverage.warnings.append("LOW_NATIVE_TEXT_WITH_IMAGES")
        unit.coverage.table_count = len(tables)
        unit.coverage.image_count = len(page.images)
        unit.coverage.status = "read" if native or tables else "empty"
        # A vector-only page is content, not a blank page. Curves outside a
        # detected table can be a diagram and are not silently discarded.
        curves = [curve for curve in page.curves if not any(_inside(box, (curve["x0"] + curve["x1"]) / 2,
                                                                 (curve["top"] + curve["bottom"]) / 2) for box in table_boxes)]
        if curves or (not native and not tables and (page.lines or page.rects) and not requests):
            requests.append((locator + "/vector-content", {"object_kind": "pdf_page"}))
        if unit.coverage.unmapped_glyphs:
            requests.append((locator + "/unmapped-glyphs", {"object_kind": "pdf_page"}))
        if requests or reviews:
            unit.coverage.status = "needs_ocr"
            _resolve_unread(source, unit, config, ocr, requests, reviews)
        unit.blocks.sort(key=_block_position)
        return unit
    except _UnitFailure:
        if reviews:
            unit.blocks.sort(key=_block_position)
        raise
    except PdfTableCellOwnershipError:
        unit.coverage.status, unit.coverage.error_code = "failed", "PDF_TABLE_CELL_OWNERSHIP_AMBIGUOUS"
        raise _UnitFailure("PDF_TABLE_CELL_OWNERSHIP_AMBIGUOUS", unit) from None
    except Exception:
        unit.coverage.status, unit.coverage.error_code = "failed", "PDF_PAGE_PARSE_FAILED"
        raise _UnitFailure("PDF_PAGE_PARSE_FAILED", unit) from None


def _xml_root(blob):
    # python-docx's entity-disabled parser also installs the OOXML element
    # classes required by Paragraph.text (a plain lxml element does not).
    return parse_xml(blob)


def _docx_paragraph(source, paragraph, part, locator):
    blocks, requests, unsupported = [], [], []
    text = Paragraph(paragraph, part).text.strip()
    if text:
        blocks.append(_block(source, locator, "paragraph", text=text))
    for index, textbox in enumerate(paragraph.iter(qn("w:txbxContent"))):
        if list(textbox.iter(qn("w:tbl"))):
            unsupported.append(locator + f"/textbox[{index}]/table")
        else:
            text = "\n".join("".join(t.text or "" for t in p.iter(qn("w:t"))) for p in textbox.iter(qn("w:p"))).strip()
            if text:
                blocks.append(_block(source, locator + f"/textbox[{index}]", "paragraph", text=text))
    # Word stores both modern drawings and legacy VML images through relationships.
    relationships = []
    for node in paragraph.iter():
        if node.tag.endswith("}blip"):
            if node.get(qn("r:link")):
                unsupported.append(locator + "/external-image")
            if node.get(qn("r:embed")):
                relationships.append(node.get(qn("r:embed")))
        elif node.tag.endswith("}imagedata") and node.get(qn("r:id")):
            relationships.append(node.get(qn("r:id")))
    for index, rid in enumerate(relationships):
        image_locator = locator + f"/image[{index}]"
        try:
            image_part = part.related_parts[rid]
            data = image_part.blob
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
            image_hash = hashlib.sha256(data).hexdigest()
            package_part = str(image_part.partname).lstrip("/")
            blocks.append(_block(source, image_locator, "image", image_ref={"package_part": package_part,
                                  "sha256": image_hash, "content_type": image_part.content_type,
                                  "width_px": width, "height_px": height}))
            requests.append((image_locator, {"object_kind": "docx_image", "package_part": package_part,
                                              "image_sha256": image_hash}))
        except Exception:
            unsupported.append(image_locator + "/unreadable-image")
    for name in ("object", "altChunk", "ins", "del", "moveFrom", "moveTo"):
        if list(paragraph.iter(qn("w:" + name))):
            unsupported.append(locator + "/" + name)
    for index, drawing in enumerate(paragraph.iter(qn("w:drawing"))):
        if not any(node.tag.endswith("}blip") for node in drawing.iter()):
            unsupported.append(locator + f"/drawing[{index}]")
    for index, pict in enumerate(paragraph.iter(qn("w:pict"))):
        if not any(node.tag.endswith("}imagedata") or node.tag == qn("w:txbxContent") for node in pict.iter()):
            unsupported.append(locator + f"/pict[{index}]")
    instructions, field_stack = [], []
    for node in paragraph.iter():
        if node.tag == qn("w:fldChar"):
            field_type = node.get(qn("w:fldCharType"))
            if field_type == "begin":
                field_stack.append([])
            elif field_type == "end" and field_stack:
                instructions.append("".join(field_stack.pop()))
        elif node.tag == qn("w:instrText"):
            if field_stack:
                field_stack[-1].append(node.text or "")
            else:
                instructions.append(node.text or "")
    instructions.extend("".join(parts) for parts in field_stack)
    instructions.extend(node.get(qn("w:instr"), "") for node in paragraph.iter(qn("w:fldSimple")))
    if any(text.strip().split(" ")[0].upper() not in {"PAGE", "NUMPAGES", "SECTIONPAGES", "TOC"} for text in instructions if text.strip()):
        unsupported.append(locator + "/uninterpreted-field")
    if any(unicodedata.category(symbol) == "Co" for block in blocks for symbol in block.text):
        unsupported.append(locator + "/unmapped-glyphs")
    return blocks, requests, unsupported


def _docx_table(source, element, part, locator):
    table = Table(element, part)
    widths = [float(node.get(qn("w:w"), "0")) for node in element.findall("./" + qn("w:tblGrid") + "/" + qn("w:gridCol"))]
    if not widths or any(width <= 0 for width in widths):
        raise ValueError("missing native grid widths")
    rows = [["" for _ in widths] for _ in table.rows]
    merges, active, headers, extra_blocks, requests, unsupported = [], {}, [], [], [], []
    for ri, tr in enumerate(element.findall(qn("w:tr"))):
        before = tr.find("./" + qn("w:trPr") + "/" + qn("w:gridBefore"))
        col = int(before.get(qn("w:val"), "0")) if before is not None else 0
        if tr.find("./" + qn("w:trPr") + "/" + qn("w:tblHeader")) is not None:
            headers.append(ri)
        current_active = {}
        for tc in tr.findall(qn("w:tc")):
            span_node = tc.find("./" + qn("w:tcPr") + "/" + qn("w:gridSpan"))
            span = int(span_node.get(qn("w:val"), "1")) if span_node is not None else 1
            if span < 1 or col + span > len(widths):
                raise ValueError("cell leaves grid")
            vm = tc.find("./" + qn("w:tcPr") + "/" + qn("w:vMerge"))
            value = vm.get(qn("w:val"), "continue") if vm is not None else None
            cell_locator = locator + f"/row[{ri}]/cell[{col}]"
            if value == "continue":
                if col not in active or active[col].col_span != span:
                    raise ValueError("orphan vertical continuation")
                if any((node.text or "").strip() for node in tc.iter(qn("w:t"))) or list(tc.iter(qn("w:drawing"))):
                    raise ValueError("vertical continuation has unaccounted content")
                merge = active[col]
                merge.row_span += 1
                current_active[col] = merge
            else:
                texts = []
                for pi, p in enumerate(tc.findall(qn("w:p"))):
                    content, new_requests, new_unsupported = _docx_paragraph(source, p, part, cell_locator + f"/paragraph[{pi}]")
                    texts.extend(block.text for block in content if block.kind == "paragraph")
                    extra_blocks.extend(block for block in content if block.kind != "paragraph")
                    requests.extend(new_requests)
                    unsupported.extend(new_unsupported)
                rows[ri][col] = "\n".join(texts)
                if tc.findall(qn("w:tbl")):
                    unsupported.append(cell_locator + "/nested-table")
                for ci, child in enumerate(tc):
                    if child.tag not in {qn("w:tcPr"), qn("w:p"), qn("w:tbl")}:
                        unsupported.append(cell_locator + f"/uninterpreted-child[{ci}]")
                if span > 1 or value == "restart":
                    merge = Merge(row=ri, column=col, col_span=span)
                    merges.append(merge)
                    if value == "restart":
                        current_active[col] = merge
            col += span
        active = current_active
    merges = [merge for merge in merges if merge.row_span > 1 or merge.col_span > 1]
    block = _block(source, locator, "table", rows=rows, merges=merges, column_widths=widths, header_rows=headers)
    return [block, *extra_blocks], requests, unsupported


def _read_docx_unit(source, element, part, locator, suffix, config, ocr):
    unit = ReadingUnit(unit_id=_unit_id(source, suffix), source_id=source.source_id,
                       locator=locator, coverage=UnitCoverage(status="empty"))
    try:
        if element.tag == qn("w:p"):
            blocks, requests, unsupported = _docx_paragraph(source, element, part, locator)
        elif element.tag == qn("w:tbl"):
            blocks, requests, unsupported = _docx_table(source, element, part, locator)
        elif element.tag == qn("w:body") and all(child.tag == qn("w:sectPr") for child in element):
            blocks, requests, unsupported = [], [], []
        else:
            blocks, requests, unsupported = [], [], [locator]
        unit.blocks = blocks
        unit.coverage.native_text_chars = sum(len(b.text) + sum(len(c) for r in b.rows for c in r) for b in blocks)
        unit.coverage.table_count = sum(b.kind == "table" for b in blocks)
        unit.coverage.image_count = sum(b.kind == "image" for b in blocks)
        unit.coverage.status = "read" if unit.coverage.native_text_chars or unit.coverage.table_count else "empty"
        unsupported = [item for item in unsupported if not _ignored(config, source, item, unit.coverage)]
        if unsupported:
            unit.coverage.status, unit.coverage.error_code = "unsupported", "DOCX_OBJECT_UNSUPPORTED"
            unit.coverage.unread_objects.extend(unsupported)
            unit.coverage.unread_objects.extend(loc for loc, _ in requests if not _ignored(config, source, loc, unit.coverage))
            if source.role in set(config.required_roles) | {"tender"}:
                raise _UnitFailure("DOCX_OBJECT_UNSUPPORTED", unit)
            return unit
        if requests:
            unit.coverage.status = "needs_ocr"
            _resolve_unread(source, unit, config, ocr, requests)
        return unit
    except _UnitFailure:
        raise
    except Exception:
        unit.coverage.status, unit.coverage.error_code = "failed", "DOCX_BLOCK_PARSE_FAILED"
        raise _UnitFailure("DOCX_BLOCK_PARSE_FAILED", unit) from None


def _docx_specs(document):
    specs = []
    def walk(parent, part, locator, suffix):
        for index, child in enumerate(parent):
            child_locator = locator + f"/block[{index}]"
            child_suffix = suffix + f"-b{index:05d}"
            if child.tag == qn("w:sectPr"):
                continue  # Native page setup is metadata, not unparsed body content.
            if child.tag == qn("w:sdt"):
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    walk(content, part, child_locator + "/sdt", child_suffix + "-sdt")
                    continue
            specs.append((child, part, child_locator, child_suffix))
    walk(document.element.body, document.part, "word/document.xml/body", "docx-body")
    for part in sorted(document.part.package.parts, key=lambda item: str(item.partname)):
        name = str(part.partname).lstrip("/")
        if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
            walk(_xml_root(part.blob), part, name, "docx-" + Path(name).stem)
        elif name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}:
            for note in _xml_root(part.blob):
                if note.get(qn("w:type")) in {"separator", "continuationSeparator"}:
                    continue
                note_id = note.get(qn("w:id"), "unknown")
                walk(note, part, name + f"/note[{note_id}]", "docx-" + Path(name).stem + "-" + note_id)
    if not specs:
        specs.append((document.element.body, document.part, "word/document.xml/body", "docx-empty"))
    return specs


def _summary(documents, config):
    counts = Counter(unit["coverage"]["status"] for doc in documents for unit in doc["units"])
    required = set(config.required_roles) | {"tender"}
    unresolved = [{"source_id": doc["source_id"], "unit_id": unit["unit_id"], "locator": unit["locator"],
                   "page": unit["page"], "status": unit["coverage"]["status"], "error_code": unit["coverage"].get("error_code")}
                  for doc in documents for unit in doc["units"] if unit["coverage"]["status"] not in {"read", "empty"}]
    blocked = [item for item in unresolved if next(doc["role"] for doc in documents if doc["source_id"] == item["source_id"]) in required]
    return {"status": "failed" if blocked else "partial" if unresolved else "complete", "complete": not unresolved,
            "required_complete": not blocked, "required_roles": sorted(required),
            "document_count": len(documents), "unit_count": sum(len(doc["units"]) for doc in documents),
            "counts": {status: counts[status] for status in ("read", "empty", "needs_ocr", "failed", "unsupported")},
            "unresolved_units": unresolved, "required_unresolved_units": blocked,
            "page_numbering": "PDF physical pages are 1-based; DOCX page is null; object indices are 0-based"}


def _review_targets(ctx, inputs, sources, options, ocr):
    if not options.pdf_region_reviews:
        return {}
    runtime = getattr(ctx, "execution_config", {})
    ocr_options = runtime.get("ocr")
    recorded = (isinstance(ocr_options, dict) and ocr_options.get("engine") == "deepseek_ocr" and
                isinstance(ocr_options.get("recorded_targets"), list) and "live_targets" not in ocr_options)
    if (runtime.get("run_purpose") != "diagnostic_replay" or getattr(ctx, "artifact_scope", None) != "diagnostic" or
            runtime.get("debug_capture", True) is not True or
            (ocr_options is not None or ocr is not None) and not recorded):
        raise ReadingFailure("READING_SOURCE_REVIEW_DIAGNOSTIC_SCOPE_REQUIRED")
    by_id = {source.source_id: (source, item) for source, item in zip(sources, inputs)}
    by_page = {}
    for review in options.pdf_region_reviews:
        source, item = by_id.get(review.source_id, (None, {}))
        if (source is None or source.sha256 != review.source_sha256 or source.role != "tender" or
                item.get("kind") != "tender" or item.get("provenance") != "original" or item.get("synthetic") is not False):
            raise ReadingFailure("READING_SOURCE_REVIEW_SOURCE_MISMATCH")
        by_page.setdefault((review.source_id, review.page), []).append(review)
    return by_page


def read_documents(ctx, documents: list[dict], config: dict, ocr: Callable | None = None) -> dict:
    """Read frozen sources. OCR is a synchronous, caller-observed callback.

    Callback responses echo source_id/unit_id/locator/page and contain text
    and/or paragraph/table content blocks. Source paths/part names are for the
    adapter, never an instruction granting model file access. Optional roles
    keep explicit partial coverage without OCR by default. On required gaps,
    ReadingFailure.result and capture('reading-coverage') retain the ledger.
    """
    try:
        options = ReadingConfig.model_validate(config)
        sources = [SourceDocument.model_validate(item) for item in documents]
    except (ValueError, TypeError):
        raise ReadingFailure("READING_INPUT_INVALID") from None
    if not sources or len({s.source_id for s in sources}) != len(sources):
        raise ReadingFailure("READING_SOURCE_SET_INVALID")
    review_targets = _review_targets(ctx, documents, sources, options, ocr)
    selected = options.diagnostic_pdf_range
    if selected and (getattr(ctx, "execution_config", {}).get("run_purpose") != "diagnostic_replay"
                     or getattr(ctx, "artifact_scope", None) != "diagnostic"
                     or len(sources) != 1 or sources[0].role != "tender" or documents[0].get("kind") != "tender"
                     or documents[0].get("provenance") != "original" or documents[0].get("synthetic")
                     or (sources[0].source_id, sources[0].sha256) != (selected.source_id, selected.source_sha256)
                     or "tender" not in options.required_roles):
        raise ReadingFailure("DIAGNOSTIC_ORIGINAL_PDF_SCOPE_INVALID")
    native_evidence = _prepare_native_rules(ctx, sources, options)
    results = []
    for source in sources:
        if not source.path.is_file():
            raise ReadingFailure("READING_SOURCE_MISSING")
        if _hash(source.path) != source.sha256:
            raise ReadingFailure("READING_SOURCE_HASH_MISMATCH")
        with source.path.open("rb") as stream:
            magic = stream.read(8)
        result = {"source_id": source.source_id, "role": source.role, "title": source.title,
                  "sha256": source.sha256, "format": "", "units": []}
        results.append(result)
        unit_inputs = {"source_id": source.source_id, "sha256": source.sha256, "role": source.role,
                       "reader_version": READER_VERSION,
                       "config": options.model_dump(mode="json", exclude={"pdf_region_reviews"}),
                       "ocr_configured": ocr is not None}
        def execute(unit_id, locator, page, fn, reviews=None):
            def parse_unit():
                try:
                    unit = fn()
                except _UnitFailure as exc:
                    # A manual exemption does not resolve any other unread
                    # object on this page or change its failure status.
                    if options.native_ignored_objects:
                        exc.unit.coverage.ignored_objects.extend(_native_decisions(source, exc.unit, options, native_evidence))
                    raise
                if options.native_ignored_objects:
                    unit.coverage.ignored_objects.extend(_native_decisions(source, unit, options, native_evidence))
                return unit.model_dump(mode="json")

            try:
                identity = {**unit_inputs, "locator": locator, "page": page}
                if reviews:
                    identity["source_reviews"] = {"version": _SOURCE_REVIEW_VERSION, "targets": [
                        review.model_dump(mode="json") for review in review_targets[(source.source_id, page)]]}
                value = ctx.run_unit(unit_id, identity, parse_unit)
                unit = ReadingUnit.model_validate(value)
                if (unit.unit_id, unit.source_id, unit.locator, unit.page) != (unit_id, source.source_id, locator, page):
                    raise ReadingFailure("READING_CHECKPOINT_IDENTITY_INVALID")
                if options.native_ignored_objects:
                    # Recompute identity and decisions even after a page-cache
                    # hit; the stored exemption is not its own authority.
                    _verify_native_decisions(source, unit, options, native_evidence)
                if review_targets:
                    _verify_source_review_checkpoint(source, unit, reviews or {})
                result["units"].append(unit.model_dump(mode="json"))
            except _UnitFailure as exc:
                result["units"].append(exc.unit.model_dump(mode="json"))
        if selected and not magic.startswith(b"%PDF-"):
            raise ReadingFailure("DIAGNOSTIC_ORIGINAL_PDF_SCOPE_INVALID")
        review_pages = {page for source_id, page in review_targets if source_id == source.source_id}
        if review_pages and not magic.startswith(b"%PDF-"):
            raise ReadingFailure("READING_SOURCE_REVIEW_SOURCE_MISMATCH")
        if magic.startswith(b"%PDF-"):
            result["format"] = "pdf"
            try:
                reader = PdfReader(source.path)
                if reader.is_encrypted:
                    raise ReadingFailure("READING_SOURCE_PASSWORD_PROTECTED")
                page_count = len(reader.pages)
                if not page_count:
                    raise ValueError("no pages")
                pdf = pdfplumber.open(source.path)
                if len(pdf.pages) != page_count:
                    pdf.close()
                    raise ValueError("page identities disagree")
            except ReadingFailure:
                raise
            except Exception:
                raise ReadingFailure("READING_PDF_CORRUPTED") from None
            result["expected_units"] = page_count
            with pdf:
                first, last = 1, page_count
                if selected:
                    first, last = selected.first_page, selected.last_page
                    if last > page_count:
                        raise ReadingFailure("DIAGNOSTIC_ORIGINAL_PDF_SCOPE_INVALID")
                    result["expected_units"] = last - first + 1
                    result["diagnostic_pdf_range"] = {**selected.model_dump(), "source_page_count": page_count,
                        "outside_scope_page_count": page_count - result["expected_units"]}
                if any(not first <= number <= last for number in review_pages):
                    raise ReadingFailure("READING_SOURCE_REVIEW_PAGE_OUTSIDE_SCOPE")
                for index in range(first, last + 1):
                    page = pdf.pages[index - 1]
                    unit_id = _unit_id(source, f"pdf-p{index:05d}")
                    reviews = {review.locator: prepare_region_review(ctx, source, unit_id, review)
                               for review in review_targets.get((source.source_id, index), [])}
                    review_options = {"reviews": reviews} if reviews else {}
                    execute(unit_id, f"pdf/page[{index}]", index,
                            lambda: _read_pdf_page(source, page, index, options, ocr, **review_options), reviews)
                    page.close()
        elif magic.startswith(b"PK"):
            result["format"] = "docx"
            try:
                with zipfile.ZipFile(source.path) as archive:
                    if any(item.flag_bits & 1 for item in archive.infolist()):
                        raise ReadingFailure("READING_SOURCE_PASSWORD_PROTECTED")
                    if not {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist()):
                        raise ValueError("not DOCX")
                document = Document(source.path)
                specs = _docx_specs(document)
            except ReadingFailure:
                raise
            except Exception:
                raise ReadingFailure("READING_DOCX_CORRUPTED") from None
            result["expected_units"] = len(specs)
            for element, part, locator, suffix in specs:
                execute(_unit_id(source, suffix), locator, None,
                        lambda element=element, part=part, locator=locator, suffix=suffix:
                        _read_docx_unit(source, element, part, locator, suffix, options, ocr))
        else:
            raise ReadingFailure("READING_SOURCE_FORMAT_UNSUPPORTED")
        if _hash(source.path) != source.sha256:
            raise ReadingFailure("READING_SOURCE_CHANGED_DURING_READ")
    output = {"schema_version": "1.0", "producer_version": READER_VERSION,
              "documents": results, "coverage": _summary(results, options)}
    if selected:
        output["coverage"].update(coverage_basis="diagnostic_original_pdf_range",
                                  diagnostic_pdf_range=results[0]["diagnostic_pdf_range"],
                                  publication_status="diagnostic_only")
    if review_targets:
        review_records = [record for doc in results for unit in doc["units"] for record in unit["coverage"]["transformations"]
                          if record.get("kind") == _SOURCE_REVIEW_KIND]
        output["coverage"].update(publication_status="diagnostic_only", pdf_region_source_review={
            "version": _SOURCE_REVIEW_VERSION, "scope": "diagnostic_replay", "target_count": len(options.pdf_region_reviews),
            "confirmed_count": sum(record["status"] == "confirmed" for record in review_records),
            "unresolved_count": sum(record["status"] != "confirmed" for record in review_records)})
    native_rules = _native_rule_ledger(results, options, native_evidence)
    invalid_native_rules = any(row["match_count"] != 1 for row in native_rules)
    if native_rules:
        output["coverage"]["native_ignore_rules"] = native_rules
        # Source reading and configured-rule validation have different
        # denominators. A nonexistent rule target does not invent an unread
        # source page; it still prevents the N1 node from completing.
        output["coverage"]["native_ignore_validation"] = {
            "status": "failed" if invalid_native_rules else "passed", "basis": _NATIVE_IGNORE_BASIS,
            "rule_count": len(native_rules), "matched_rule_count": sum(row["match_count"] == 1 for row in native_rules)}
    # A fully saved ledger can describe unread source objects. That is a
    # content limitation, not a truncated/missing diagnostic capture.
    capture = ctx.capture("reading-coverage", {"schema_version": "1.0", "content_status": output["coverage"]["status"], "coverage": output["coverage"],
                            "documents": [{k: v for k, v in doc.items() if k != "units"} | {
                                "units": [{k: v for k, v in unit.items() if k != "blocks"} for unit in doc["units"]]}
                                          for doc in results]})
    if isinstance(capture, dict) and capture.get("status") == "write_failed":
        raise ReadingFailure("READING_CAPTURE_FAILED", output)
    if invalid_native_rules:
        code = next((unit["coverage"]["error_code"] for doc in results for unit in doc["units"]
                     if (unit["coverage"].get("error_code") or "").startswith("READING_NATIVE_IGNORE_")),
                    "READING_NATIVE_IGNORE_UNMATCHED")
        raise ReadingFailure(code, output)
    if not output["coverage"]["required_complete"]:
        gaps = output["coverage"]["required_unresolved_units"]
        # Preserve a bounded application failure after saving the entire
        # denominator; the ledger still reports every other unresolved unit.
        code = next((gap["error_code"] for gap in gaps
                     if (gap.get("error_code") or "").startswith(("READING_NATIVE_IGNORE_", "READING_SOURCE_REVIEW_"))), None)
        code = code or next((code for gap in gaps if (code := _bounded_ocr_code(gap.get("error_code")))), None)
        if code is None:
            code = "OCR_REQUIRED" if any(gap["status"] == "needs_ocr" for gap in gaps) else "READING_INCOMPLETE"
        raise ReadingFailure(code, output)
    return output
