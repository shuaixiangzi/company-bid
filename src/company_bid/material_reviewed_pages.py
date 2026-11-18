"""Requirement-scoped page candidates from configured original-image review."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from .evidence import EvidenceSelection
from .provider import ProviderFailure
from .storage import digest

_Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
_Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReviewedProofPage(BaseModel):
    """A frozen reviewer observation about a page's type, without fact values."""
    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: _Id
    requirement_text_sha256: _Hash
    proof_kind: Literal["environment_management_certificate", "social_insurance_proof"]
    source_id: _Id
    source_sha256: _Hash
    selection_sha256: _Hash
    image_sha256: _Hash
    attachment_id: _Id
    content_unit_id: _Id
    review_id: _Id
    evidence_sha256: _Hash


def _require(condition, code):
    if not condition:
        raise ProviderFailure(code)


def bind_reviewed_pages(manifest, report, reviews: list[ReviewedProofPage]) -> list[dict]:
    """Bind each observation to an exact requested type, accepted source and selected image."""
    index = report["original_content_index"]
    sources = {s["source_id"]: s for s in manifest["sources"]}
    attachments = {a["attachment_id"]: a for a in manifest["attachments"]}
    selections = {s["selection_id"]: EvidenceSelection.model_validate(s)
                  for s in manifest["evidence_selections"]}
    groups = {g["group_id"]: g for g in index["groups"]}
    proof_groups = {g["group_id"]: g for g in report["proof_groups"]}
    queries = {q["requirement_id"]: q for q in report["proof_queries"]}
    units = {(u["source_id"], u["unit_id"]): u for u in index["units"]}
    seen = set()
    result = []
    for review in reviews:
        key = (review.requirement_id, review.content_unit_id)
        _require(key not in seen, "MATERIAL_REVIEWED_PAGE_DUPLICATE")
        seen.add(key)
        query = queries.get(review.requirement_id, {}).get("request", {})
        _require(review.proof_kind in query.get("kinds", []) and
                 digest(query.get("original_text")) == review.requirement_text_sha256,
                 "MATERIAL_REVIEWED_PAGE_REQUIREMENT_MISMATCH")
        source = sources.get(review.source_id, {})
        unit = units.get((review.source_id, review.content_unit_id), {})
        attachment = attachments.get(review.attachment_id, {})
        _require(source.get("kind") in {"company_material", "attachment"} and
                 source.get("sha256") == unit.get("source_sha256") == review.source_sha256 and
                 attachment.get("sha256") == unit.get("ocr_image_sha256") == review.image_sha256,
                 "MATERIAL_REVIEWED_PAGE_SOURCE_MISMATCH")
        if unit["format"] == "pdf_pages":
            locator = f"pdf/page[{unit['target']}]"
        else:
            locator = f"docx/{unit['target']}"
        matching_groups = []
        for group_id in unit["group_ids"]:
            group = groups[group_id]
            origin = group["origin"]
            selection = selections[origin["selection_id"]]
            expected_attachment = "PROOF-" + digest([selection.selection_id, locator])[:24]
            if (expected_attachment == review.attachment_id and
                    expected_attachment in group["attachment_ids"] and group["source_bound"] and
                    not proof_groups[group_id]["role_exclusions"] and
                    selection.source_id == review.source_id and selection.source_sha256 == review.source_sha256 and
                    digest(selection.model_dump(mode="json")) == review.selection_sha256):
                matching_groups.append(group_id)
        _require(len(matching_groups) == 1, "MATERIAL_REVIEWED_PAGE_SELECTION_MISMATCH")
        result.append({
            **review.model_dump(mode="json"), "locator": locator,
            "group_id": matching_groups[0],
            "status": "reviewed_page_candidate_pending_conditions",
            "transcription_status": unit["transcription_status"],
            "automatic_assembly_allowed": False, "automatic_fact_adoption": False,
            "qualification_result": "not_determined", "project_assignment": "not_made",
        })
    return result


def reviewed_page_note(candidate: dict) -> str:
    locator = candidate["locator"]
    where = f"Word 原件中的 {Path(locator).name}" if locator.startswith("docx/") else locator
    if candidate["proof_kind"] == "social_insurance_proof":
        check = ("本页为职工参保登记明细。请核对拟派项目负责人、用工主体及正式递交截止时点，"
                 "并查规定窗口内的实际缴费凭据。")
    else:
        check = ("本页为环境管理体系认证证书。请按评分子项核对认证范围、"
                 "递交时的有效状态及年度监督记录。")
    reading = {
        "ocr_no_text_unresolved": "该图的 OCR 未取得文字。",
        "ocr_failed_pending_review": "该图的 OCR 读取或校验失败，原状态待核。",
    }.get(candidate["transcription_status"], "")
    return f"原图复核候选（{where}）。{check}{reading}"
