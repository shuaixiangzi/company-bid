"""Rebuild separately selected confidentiality scope and contract duty from a reviewed PDF page."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Annotated

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .contract_heading_only_annex import rendered_page_rgb_sha256
from .pdf_table_geometry import discover_pdf_tables
from .pdf_text_lines import pdf_page_text_lines
from .response_cover_sources import _native_lines
from .storage import digest


SOURCE_CONFIG_KEY = "contract_attachment_citation_split_source"
WINDOW_CONTEXT_KEY = "native_contract_attachment_citation_split_context"
RESOLUTION_KEY = "native_contract_attachment_citation_split_resolution"
SOURCE_ERROR = "CONTRACT_ATTACHMENT_CITATION_SPLIT_SOURCE_MISMATCH"
RULE = "reviewed_native_confidentiality_scope_and_duty_v1"
_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_ANNEX = re.compile(r"附件\s*[1-9]\d*\s*[：:]\s*保密条款")
_SCOPE_TITLE = re.compile(r"第一条\s+保密信息(?:定义)?")
_DUTY_TITLE = re.compile(r"第二条\s+保密(?:责任|义务)")
_NUMBERED = re.compile(r"^([1-9]\d*(?:\.[1-9]\d*)+)(?![\d.])\s+\S")
_SLASH = re.compile(r"1\.4\s+其他\s*[：:]\s*[【（(]?\s*[/／]\s*[】）)]?")


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    candidate_index: int = Field(ge=0)
    gap_heading_block_id: _ID
    physical_page: int = Field(ge=1)
    rendered_rgb_sha256: _SHA


def normalize_contract_attachment_citation_split_config(value):
    try:
        if not isinstance(value, list):
            return _Scope.model_validate(value).model_dump()
        scopes = [_Scope.model_validate(scope).model_dump() for scope in value]
    except ValidationError:
        raise ValueError("CONTRACT_ATTACHMENT_CITATION_SPLIT_CONFIG_INVALID") from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError("CONTRACT_ATTACHMENT_CITATION_SPLIT_CONFIG_INVALID")
    return scopes


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _member(block):
    return {key: block[key] for key in ("block_id", "source_id", "unit_id", "locator", "page", "ordinal")} | {
        "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _page_lines(config, document, blocks, source):
    page = config["physical_page"]
    units = [unit for unit in document["units"] if unit.get("page") == page]
    _require(len(units) == 1 and units[0]["locator"] == f"pdf/page[{page}]")
    unit, = units
    coverage = unit["coverage"]
    _require(coverage["status"] == "read" and coverage["origin"] == "native" and not any(coverage[key] for key in (
        "unread_objects", "ignored_objects", "unmapped_glyphs", "error_code", "warnings", "table_count", "image_count")))
    native = [blocks[line["block_id"]] for line in unit["blocks"]]
    _require(len(native) >= 8 and _native_lines(native, config["source_id"], page, range(len(native))))
    payload = Path(source["path"]).read_bytes()
    _require(hashlib.sha256(payload).hexdigest() == config["source_sha256"])
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        original = pdf.pages[page - 1]
        _require(original.rotation == 0 and original.bbox == original.cropbox == original.mediabox and
                 original.bbox[:2] == (0, 0) and set(original.objects) <= {"char", "rect"} and not original.annots)
        discovery = discover_pdf_tables(original)
        _require(not discovery.tables and not discovery.diagonal_rules and
                 len(original.rects) == len(discovery.text_backgrounds))
        transformations = [discovery.transformation(unit["locator"])] if discovery.text_backgrounds else []
        _require(coverage["transformations"] == transformations)
        exact_lines = [(i, text, bbox) for i, text, bbox, _, _ in pdf_page_text_lines(original, ())]
        _require(exact_lines == [(i, line["text"], line["bbox"]) for i, line in enumerate(native)])
    _require(rendered_page_rgb_sha256(payload, page) == config["rendered_rgb_sha256"])
    return native, digest(coverage)


def _derive_groups(lines, config):
    gap, = [i for i, row in enumerate(lines) if row["block_id"] == config["gap_heading_block_id"]]
    _require(gap > 2 and _ANNEX.fullmatch(lines[0]["text"]) and _SCOPE_TITLE.fullmatch(lines[1]["text"]) and
             _DUTY_TITLE.fullmatch(lines[gap]["text"]))
    scope = lines[2:gap]
    numbered = [(number[1], row["text"]) for row in scope if (number := _NUMBERED.match(row["text"]))]
    main = {number: text for number, text in numbered if number.count(".") == 1}
    _require(list(main) in [["1.1", "1.2", "1.3"], ["1.1", "1.2", "1.3", "1.4"]] and
             bool(re.match(r"1\.1\s+本保密条款项下的保密信息(?:是)?指", main["1.1"])) and
             "本保密条款将不适用" in main["1.2"] and
             bool(re.match(r"1\.3\s+本保密条款所称的接收方指", main["1.3"])))
    if "1.4" in main:
        _require(_SLASH.fullmatch(main["1.4"]) and scope[-1]["text"] == main["1.4"])
    exceptions = len(numbered) - len(main)
    _require(exceptions > 0 and [number for number, _ in numbered] == [
        "1.1", "1.2", *(f"1.2.{part}" for part in range(1, exceptions + 1)), *list(main)[2:]])
    after = [(i, number[1]) for i, row in enumerate(lines[gap + 1:], gap + 1)
             if (number := _NUMBERED.match(row["text"]))]
    _require(len(after) >= 2 and after[0] == (gap + 1, "2.1") and after[1][1] == "2.2")
    next_start = after[1][0]
    duty = lines[gap + 1:next_start]
    _require(re.match(r"2\.1\s+接收方(?:应|须|不得)", duty[0]["text"]) and duty[-1]["text"].endswith("。"))
    return scope, lines[gap], duty, lines[next_start]


def bind_contract_attachment_citation_split(windows, documents, blocks, manifest, config):
    """Verify the reviewed original, then bind the context only to the affected window."""
    try:
        source_id, page = config["source_id"], config["physical_page"]
        source, = [source for source in (manifest or {}).get("sources", []) if source["source_id"] == source_id]
        document, = [document for document in documents if document["source_id"] == source_id]
        window, = [window for window in windows if window["window_id"] == config["window_id"]]
        _require(document["format"] == window["source_format"] == "pdf" and window["source_id"] == source_id and
                 source["sha256"] == document["sha256"] == window["source_sha256"] == config["source_sha256"])
        lines, coverage_sha256 = _page_lines(config, document, blocks, source)
        scope, heading, duty, following = _derive_groups(lines, config)
        targets = {row["block_id"] for row in window["blocks"]}
        _require(all(row["block_id"] in targets for row in [*lines[:2], *scope, heading, *duty]) and
                 following["block_id"] not in targets and any(row["block_id"] == following["block_id"]
                                                            for row in window["adjacent_context"]))
        owner, = [item for item in windows if any(row["block_id"] == following["block_id"] for row in item["blocks"])]
        _require(windows.index(owner) == windows.index(window) + 1 and owner["blocks"][0]["block_id"] == following["block_id"])
        proof = {"rule": RULE, "source_id": source_id, "source_sha256": config["source_sha256"],
                 "window_id": config["window_id"], "physical_page": page, "scope": config,
                 "annex_heading": _member(lines[0]), "scope_heading": _member(lines[1]),
                 "omitted_heading": _member(heading), "context_members": [_member(row) for row in scope],
                 "operative_members": [_member(row) for row in duty],
                 "following_clause_start": _member(following) | {"owner_window_id": owner["window_id"]},
                 "native_coverage_sha256": coverage_sha256,
                 "original_page_lines_sha256": digest([_member(row) for row in lines]),
                 "rendered_rgb_sha256": config["rendered_rgb_sha256"]}
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def split_contract_attachment_selection(raw, normalized, window):
    """Bind both disjoint selections without making the unselected heading citable."""
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None:
        return None
    config = proof["scope"]
    index = config["candidate_index"]
    _require(proof["rule"] == RULE and all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")) and
             proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"}) and
             digest(raw) == config["raw_candidate_sha256"] and index < len(normalized["requirements"]))
    candidate = normalized["requirements"][index]
    original = candidate["citation"]
    _require(candidate["category"] == "commercial" and candidate["response_mode"] == "procedure" and
             candidate["format_status"] == "not_prescribed" and not candidate["format_title"] and
             not any(candidate[field] for field in ("required_fact_labels", "attachment_keywords", "reference_keywords")) and
             not any(original[field] for field in ("quote", "clause_id", "table_row_id")))
    targets = {row["block_id"]: row for row in window["blocks"]}
    adjacent = {row["block_id"]: row for row in window["adjacent_context"]}
    groups = [proof["context_members"], proof["operative_members"]]
    members = [proof["annex_heading"], proof["scope_heading"], *groups[0], proof["omitted_heading"], *groups[1]]
    following = {key: value for key, value in proof["following_clause_start"].items() if key != "owner_window_id"}
    _require(all(member["block_id"] in targets and member == _member(targets[member["block_id"]]) for member in members) and
             following["block_id"] in adjacent and following == _member(adjacent[following["block_id"]]) and
             all(right["ordinal"] == left["ordinal"] + 1 for left, right in zip(members, [*members[1:], following], strict=True)))
    selectors = [{key: group[0][key] for key in ("source_id", "block_id", "locator")} |
                 {"block_ids": [member["block_id"] for member in group]} for group in groups]
    _require(all(original[key] == selectors[0][key] for key in ("source_id", "block_id", "locator")) and
             original["block_ids"] == selectors[0]["block_ids"] + selectors[1]["block_ids"])
    return {"rule": RULE, "window_id": window["window_id"], "raw_candidate_sha256": config["raw_candidate_sha256"],
            "candidate_index": index, "proof": proof, "original_selector": raw["requirements"][index]["citation"],
            "context_selector": selectors[0], "operative_selector": selectors[1], "model_candidate_mutation": False,
            "semantic_review": "requires_source_review"}
