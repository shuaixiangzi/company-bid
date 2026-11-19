"""Prove a contract annex reference from complete native PDF pages."""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .response_cover_sources import _member, _native_lines
from .storage import digest


CONTRACT_ANNEX_SOURCE_CONFIG_KEY = "contract_annex_source_resolution"
_SOURCE_ERROR = "CONTRACT_ANNEX_SOURCE_PROOF_MISMATCH"
_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_TITLE = "技术服务内容"


class _ContractAnnexScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    owner_window_id: _ID
    reference_block_id: _ID
    reference_page: int = Field(ge=1)
    annex_page: int = Field(ge=4)
    raw_candidate_sha256: _SHA
    source_proof_sha256: _SHA


def normalize_contract_annex_config(value):
    try:
        scope = _ContractAnnexScope.model_validate(value).model_dump()
    except ValidationError:
        raise ValueError("CONTRACT_ANNEX_SOURCE_CONFIG_INVALID") from None
    if scope["window_id"] == scope["owner_window_id"] or scope["reference_page"] >= scope["annex_page"] - 2:
        raise ValueError("CONTRACT_ANNEX_SOURCE_CONFIG_INVALID")
    return scope


def _guard(condition):
    if not condition:
        raise ValueError(_SOURCE_ERROR)


def contract_annex_pages(config, document, blocks):
    """Get only complete, natively read pages with no skipped source objects."""
    _guard(document.get("source_id") == config["source_id"] and document.get("sha256") == config["source_sha256"] and
           document.get("format") == "pdf")
    annex = config["annex_page"]
    pages = []
    for page in (config["reference_page"], annex - 2, annex - 1, annex, annex + 1):
        units = [unit for unit in document["units"] if unit["page"] == page]
        _guard(len(units) == 1)
        unit = units[0]
        coverage = unit["coverage"]
        _guard(coverage["status"] == "read" and coverage["origin"] == "native" and
               not any(coverage[key] for key in ("unread_objects", "ignored_objects", "unmapped_glyphs", "transformations",
                                                 "error_code", "table_count", "image_count")))
        pages.append([blocks[block["block_id"]] for block in unit["blocks"]])
    return pages


def _match_lines(lines, pattern):
    return [index for index, value in enumerate(lines) if re.fullmatch(pattern, value)]


def build_contract_annex_proof(config, pages, footer):
    """Distinguish the cross-reference, list entry, signature and annex payload."""
    reference_page, annex_page = config["reference_page"], config["annex_page"]
    page_numbers = (reference_page, annex_page - 2, annex_page - 1, annex_page, annex_page + 1)
    _guard(len(pages) == len(page_numbers) and all(page and _native_lines(
        page, config["source_id"], number, range(len(page))) for page, number in zip(pages, page_numbers, strict=True)))
    ref, listing, signature, annex, following = pages
    ref_lines = [row["text"] for row in ref]
    refs = _match_lines(ref_lines, rf"1\.1\s*{_TITLE}[：:]\s*详见附件\s*1\s*{_TITLE}[。.]")
    _guard(len(refs) == 1 and 0 < refs[0] < len(ref) - 1)
    i = refs[0]
    reference = ref[i]
    _guard(reference["block_id"] == config["reference_block_id"] and
           re.fullmatch(r"第一条\s*技术服务范围", ref_lines[i - 1]) and
           re.fullmatch(r"1\.2\s*技术服务要求[：:]", ref_lines[i + 1]))
    list_lines = [row["text"] for row in listing]
    title = rf"附件\s*1\s*[：:]\s*{_TITLE}"
    list_matches = _match_lines(list_lines, title)
    _guard(len(list_matches) == 1 and 0 < list_matches[0] < len(list_lines) - 1)
    j = list_matches[0]
    _guard(any(re.match(r"^20\.5\s*本合同附件", line) for line in list_lines[:j]) and
           re.match(r"^附件\s*2\s*[：:]", list_lines[j + 1]) and
           re.fullmatch(r"[（(]本页无正文[，,]\s*为签章页[）)]", signature[0]["text"]) and
           re.match(r"^附件\s*2\s*[：:]", following[0]["text"]) and len(annex) == 4)
    heading, first, second, page_number = annex
    _guard(re.fullmatch(title, heading["text"]) and re.fullmatch(
        r"对[^\n]{2,28}的[^\n（）]+（共\s*[1-9]\d*\s*个）、[^\n（）]+（共\s*[1-9]\d*\s*台）进行运维",
        first["text"]) and re.fullmatch(r"并开展预防性维检修工作[。.]",
        second["text"]) and all(left["bbox"][1] < right["bbox"][1] for left, right in zip(annex, annex[1:])))
    _guard(isinstance(footer, dict) and footer.get("rule") == "pdf-page-footer-v1" and
           all(footer.get(key) == page_number[key] for key in ("block_id", "locator", "source_id", "bbox", "page")) and
           str(footer.get("printed_page")) == page_number["text"])
    members = [
        _member(reference, "reference"),
        _member(listing[j], "contract_attachment_listing"),
        _member(signature[0], "contract_signature_boundary"),
        _member(heading, "annex_title"),
        _member(first, "annex_body"),
        _member(second, "annex_body"),
        _member(following[0], "next_annex_boundary"),
    ]
    proof = {"rule": "native_pdf_contract_annex_reference_v1", "source_id": config["source_id"],
             "source_sha256": config["source_sha256"], "window_id": config["window_id"],
             "owner_window_id": config["owner_window_id"], "members": members, "footer_proof": footer,
             "block_ids": [heading["block_id"], first["block_id"], second["block_id"]],
             "body_block_ids": [first["block_id"], second["block_id"]],
             "owner_processing": "separate_frozen_window"}
    return {**proof, "proof_sha256": digest(proof)}
