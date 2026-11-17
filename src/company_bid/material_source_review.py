"""Independent source-image review records for controlled diagnostics only.

The caller authenticates the referenced image, OCR response and review evidence.
These pure functions validate and bind the supplied record; they do not perform
image recognition, verify that a reviewer actually viewed a file, amend OCR,
complete a node, fill a fact slot or determine qualification. Store full records
and rendered notes only in the controlled artifact area, never ordinary logs.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from .storage import digest


MATERIAL_SOURCE_REVIEW_VERSION = "material-source-review.v1"
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")]
Excerpt = Annotated[str, StringConstraints(max_length=300)]


class MaterialSourceReviewFailure(RuntimeError):
    """Safe error codes do not disclose source text or invalid input values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Identity(_Contract):
    source_id: Identifier
    source_sha256: Sha256
    selection_sha256: Sha256
    image_sha256: Sha256


class _OCR(_Contract):
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    node_key: Identifier
    unit_id: Identifier
    response_sha256: Sha256
    status: Literal["succeeded", "failed"]
    error_code: Identifier | None

    @model_validator(mode="after")
    def status_matches_error(self):
        if (self.status == "failed") != bool(self.error_code):
            raise ValueError("status_error_mismatch")
        return self


class _Reviewer(_Contract):
    review_id: Identifier
    review_method: Literal["operator_source_image_review", "agent_source_image_review"]
    evidence_sha256: Sha256


class _TableCell(_Contract):
    table_index: int = Field(ge=0)
    row: int = Field(ge=0)
    column: int = Field(ge=0)


class _Scope(_Contract):
    # Coordinates are in the selected (already cropped/rotated) original image.
    # A machine-proposed table index alone cannot establish a source location.
    image_region_normalized: list[float] = Field(min_length=4, max_length=4)
    table_cell: _TableCell | None = None

    @model_validator(mode="after")
    def valid_region(self):
        x0, y0, x1, y1 = self.image_region_normalized
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("invalid_region")
        return self


class _FieldReview(_Contract):
    field_id: Identifier
    scope: _Scope
    disposition: Literal["machine_error", "source_ambiguous", "source_blank"]
    source_observation: Literal["clear_value", "ambiguous_or_unreadable", "independently_confirmed_blank"]
    recognized_text: Excerpt | None
    source_text: Excerpt | None

    @model_validator(mode="after")
    def mutually_exclusive_observations(self):
        for text in (self.recognized_text, self.source_text):
            if text is not None and any(ord(c) < 32 for c in text):
                raise ValueError("control_character")
        if self.disposition == "machine_error":
            if self.source_observation != "clear_value" or not self.source_text or not self.source_text.strip():
                raise ValueError("clear_source_required")
            if not self.recognized_text or not self.recognized_text.strip():
                raise ValueError("recognized_value_required")
            normalize = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()
            if normalize(self.recognized_text) == normalize(self.source_text):
                raise ValueError("no_substantive_mismatch")
        elif self.disposition == "source_ambiguous":
            if self.source_observation != "ambiguous_or_unreadable" or self.source_text is not None:
                raise ValueError("ambiguous_source_cannot_supply_value")
        elif self.source_observation != "independently_confirmed_blank" or self.source_text != "":
            raise ValueError("independent_blank_confirmation_required")
        return self


class _Input(_Contract):
    identity: _Identity
    ocr: _OCR
    reviewer: _Reviewer
    field: _FieldReview


_GUARDS = {
    "scope": "independent_field_review_diagnostic_only",
    "ocr_response_modified": False,
    "ocr_status_modified": False,
    "automatic_numeric_adoption": False,
    "automatic_fact_adoption": False,
    "automatic_assembly_allowed": False,
    "qualification_result": "not_determined",
    "reading_completeness": "not_established",
    "node_completion_authorized": False,
    "material_presence_conclusion": "not_determined",
}


def build_material_source_review(value: dict) -> dict:
    """Bind one reviewed field without changing either source or OCR result.

    ``material_missing`` is deliberately unsupported: missing OCR text or an
    unreadable image is not evidence of missing company material. Upstream
    inventory/matching contracts own missing-material conclusions.
    """
    try:
        frozen = _Input.model_validate(value).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError):
        raise MaterialSourceReviewFailure("MATERIAL_SOURCE_REVIEW_INVALID") from None
    record = {"schema_version": MATERIAL_SOURCE_REVIEW_VERSION, **frozen, **_GUARDS}
    record["record_sha256"] = digest(record)
    return record


def validate_material_source_review(record: dict) -> dict:
    """Revalidate a stored record and reject tampered bindings or safety claims."""
    try:
        candidate = build_material_source_review({key: record[key] for key in ("identity", "ocr", "reviewer", "field")})
        if digest(record) != digest(candidate):
            raise ValueError("record_mismatch")
    except (KeyError, TypeError, ValueError, MaterialSourceReviewFailure):
        raise MaterialSourceReviewFailure("MATERIAL_SOURCE_REVIEW_RECORD_INVALID") from None
    return candidate


def material_source_review_note(record: dict) -> str:
    """Render a controlled diagnostic note, preserving errors and unknown values."""
    checked = validate_material_source_review(record)
    field, ocr = checked["field"], checked["ocr"]
    if field["disposition"] == "machine_error":
        finding = (f"机器识别错误：独立原图复核记录为“{field['source_text']}”，"
                   f"OCR 转录为“{field['recognized_text']}”。原响应保留，未自动替换转录或填入事实。")
    elif field["disposition"] == "source_ambiguous":
        finding = "来源模糊：独立原图复核仍无法确认本字段；待补清晰原件或人工核对依据，不猜填。"
        if field["recognized_text"]:
            finding += f"OCR 曾转录“{field['recognized_text']}”，该值不作为已确认事实。"
    else:
        finding = "原文留空：独立看图已确认指定区域为空；不据此认定整页空白或公司缺少该材料。"
        if field["recognized_text"]:
            finding += f"OCR 曾转录“{field['recognized_text']}”，与已审空白不符，原响应保留。"
    state = f"失败（{ocr['error_code']}）" if ocr["status"] == "failed" else "执行成功（不代表字段准确）"
    return (f"【{field['field_id']}｜复核 {checked['reviewer']['review_id']}】{finding}"
            f"所关联 OCR 状态保持{state}。本条仅覆盖指定字段；其余数字、日期、单位、表格关系及资格仍待核。")
