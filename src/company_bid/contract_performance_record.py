"""Keep source-proved performance records out of tender attachment routing."""

from __future__ import annotations

import copy
import hashlib
import io
import re
from pathlib import Path

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_annex_list import _ANNEX, _HEADING, _ReviewPage, _page_inventory, _proof_member
from .contract_attachment_citation_split import _ID, _SHA
from .storage import digest


SOURCE_CONFIG_KEY = "contract_performance_record_source"
WINDOW_CONTEXT_KEY = "native_contract_performance_record_context"
RESOLUTION_KEY = "native_contract_performance_record_resolution"
SOURCE_ERROR = "CONTRACT_PERFORMANCE_RECORD_SOURCE_MISMATCH"
RULE = "reviewed_native_commissioned_equipment_performance_record_v1"
_ITEM = re.compile(r"^[（(]([1-9]\d*)[）)]\s*\S")
_SUBMISSION = re.compile(r"投标文件|响应文件|随标|投标时|递交投标|递交响应|资格审查")


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    candidate_index: int = Field(ge=0)
    clause_start_block_id: _ID
    expected_record_label: _ID
    owner_duties_heading_block_id: _ID
    contractor_rights_heading_block_id: _ID
    reviewed_pages: list[_ReviewPage] = Field(min_length=1)


def normalize_contract_performance_record_config(value):
    error = "CONTRACT_PERFORMANCE_RECORD_CONFIG_INVALID"
    try:
        entries = value if isinstance(value, list) else [value]
        scopes = [_Scope.model_validate(scope).model_dump() for scope in entries]
    except ValidationError:
        raise ValueError(error) from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError(error)
    for scope in scopes:
        pages = [review["physical_page"] for review in scope["reviewed_pages"]]
        if pages != list(range(pages[0], pages[0] + len(pages))):
            raise ValueError(error)
    return scopes if isinstance(value, list) else scopes[0]


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _record_context(body, headings, position, targets, title, role):
    parent, = [i for i, (offset, _, _) in enumerate(headings) if offset == position]
    scope_path, scope_text = headings[parent][1:]
    _require(scope_text == title)
    siblings = []
    for index in range(parent + 1, len(headings) - 1):
        start, number, _ = headings[index]
        if number[:-1] != scope_path:
            break
        _require(int(number[-1]) == len(siblings) + 1)
        stop = headings[index + 1][0]
        rows = body[start:stop]
        _require(rows[-1]["text"].endswith("。"))
        siblings.append({"number": ".".join(number), "quote": "\n".join(row["text"] for row in rows),
                         "members": [_proof_member(row) for row in rows]})
    _require(siblings)
    owned = [item for item in siblings if all(row["block_id"] in targets for row in item["members"])]
    _require(owned)
    return {"role": role, "parent_heading": _proof_member(body[position]), "owned_items": owned,
            "disposition": "retain_contract_context_without_tender_submission_or_fact_promotion"}


def _reviewed_positions(lines, pages, config, blocks, window):
    footers = {row["footer"]["block_id"] for row in pages}
    body = [row for row in lines if row["block_id"] not in footers]
    by_id = {row["block_id"]: i for i, row in enumerate(body)}
    pos = by_id[config["clause_start_block_id"]]
    markers = [(index, int(mark[1])) for index, row in enumerate(body) if (mark := _ITEM.match(row["text"]))]
    at, = [i for i, (offset, _) in enumerate(markers) if offset == pos]
    _require(1 <= at < len(markers) - 1)
    (before, previous_number), (_, number), (after, next_number) = markers[at - 1:at + 2]
    _require((previous_number, next_number) == (number - 1, number + 1) and body[pos - 1]["text"].endswith("。") and
             body[after - 1]["text"].endswith("。"))
    members = body[pos:after]
    raw = "\n".join(row["text"] for row in members)
    full = "".join(row["text"] for row in members)
    record = config["expected_record_label"]
    _require(not _SUBMISSION.search(full) and full.count(record) == 1 and
             re.search(r"乙方受甲方委托提供的.{2,130}必须符合.{2,55}并有" + re.escape(record), full) and
             "乙方为完成合同而自用" in full and "费用由乙方承担" in full)
    headings = [(index, match["number"].split("."), match["title"])
                for index, row in enumerate(body) if (match := _HEADING.fullmatch(row["text"] or ""))]
    under = [(index, path, title) for index, path, title in headings if index < before]
    list_heading, parent, _ = under[-1]
    obligations, obligations_path, obligations_title = under[-2]
    _require(parent[:-1] == obligations_path and obligations_title == "乙方的义务" and obligations < list_heading < before)
    origin = min(lines, key=lambda row: row["ordinal"])
    annexes = [row for row in blocks.values() if row["source_id"] == window["source_id"] and
               row["ordinal"] < origin["ordinal"] and _ANNEX.fullmatch(row["text"])]
    _require(annexes)
    annex = max(annexes, key=lambda row: row["ordinal"])
    targets = {row["block_id"] for row in window["blocks"]}
    _require(all(row["block_id"] in targets for row in members))
    context = [_record_context(body, headings, by_id[config["owner_duties_heading_block_id"]], targets,
                               "甲方的义务", "employer_contract_duty"),
               _record_context(body, headings, by_id[config["contractor_rights_heading_block_id"]], targets,
                               "乙方的权利", "contractor_contract_right")]
    return {"annex_heading": _proof_member(annex), "operative_parent": _proof_member(body[obligations]),
            "list_heading": _proof_member(body[list_heading]), "item_number": number, "item_members": [_proof_member(row) for row in members],
            "previous_item": _proof_member(body[before]), "following_item": _proof_member(body[after]),
            "quote_sha256": hashlib.sha256(raw.encode()).hexdigest(), "source_contexts": context,
            "stage": "contract_performance", "actor": "contractor", "capability_status": "not_checked"}


def bind_contract_performance_record(windows, documents, blocks, footer_skips, manifest, config):
    try:
        source_id = config["source_id"]
        source, = [row for row in (manifest or {}).get("sources", []) if row["source_id"] == source_id]
        document, = [row for row in documents if row["source_id"] == source_id]
        window, = [row for row in windows if row["window_id"] == config["window_id"]]
        _require(window["source_id"] == source_id and document["format"] == window["source_format"] == "pdf" and
                 source["sha256"] == document["sha256"] == window["source_sha256"] == config["source_sha256"])
        payload = Path(source["path"]).read_bytes()
        _require(hashlib.sha256(payload).hexdigest() == source["sha256"])
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            lines, pages = _page_inventory(pdf, payload, config["reviewed_pages"], document, blocks, footer_skips)
        proof = _reviewed_positions(lines, pages, config, blocks, window)
        proof.update(rule=RULE, source_id=source_id, source_sha256=source["sha256"], window_id=window["window_id"],
                     scope=config, reviewed_pages=pages)
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, StopIteration, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def project_contract_performance_record(raw, requirement, window):
    proof = window[WINDOW_CONTEXT_KEY]
    config = proof["scope"]
    citation = requirement["citation"]
    targets = {row["block_id"]: row for row in window["blocks"]}
    contexts = [member for group in proof["source_contexts"] for item in group["owned_items"] for member in item["members"]]
    members = proof["item_members"]
    _require(all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")) and
             proof["rule"] == RULE and digest(raw) == config["raw_candidate_sha256"] and
             proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"}) and
             all(_proof_member(targets[row["block_id"]]) == row for row in [*members, *contexts]) and
             (requirement["category"], requirement["response_mode"], requirement["mandatory"], requirement["format_status"],
              requirement["format_title"]) == ("technical", "attachment", True, "not_prescribed", None) and
             not requirement["required_fact_labels"] and not requirement["reference_keywords"] and
             requirement["attachment_keywords"] == [config["expected_record_label"]] and
             citation["block_ids"] == [row["block_id"] for row in members] and
             citation["quote_sha256"] == proof["quote_sha256"] == hashlib.sha256(citation["quote"].encode()).hexdigest())
    projected = copy.deepcopy(requirement)
    projected.update(category="commercial", response_mode="procedure", attachment_keywords=[],
                     contract_evidence_scope={key: proof[key] for key in ("stage", "rule", "actor", "capability_status", "proof_sha256")})
    return {"rule": RULE, "window_id": window["window_id"], "raw_candidate_sha256": digest(raw), "proof": proof,
            "candidate_index": config["candidate_index"], "original_requirement_sha256": digest(requirement),
            "original_attachment_keywords": list(requirement["attachment_keywords"]), "requirement": projected,
            "model_candidate_mutation": False, "semantic_review": "requires_source_review"}
