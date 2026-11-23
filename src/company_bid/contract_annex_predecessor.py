"""Prove an unresolved contract annex seam already cited by its preceding window."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_annex_list import _ANNEX, _ReviewPage, _page_inventory, _proof_member
from .contract_attachment_citation_split import _ID, _SHA
from .storage import digest


SOURCE_CONFIG_KEY = "contract_annex_predecessor_source"
WINDOW_CONTEXT_KEY = "native_contract_annex_predecessor_context"
RESOLUTION_KEY = "native_contract_annex_predecessor_resolution"
SOURCE_ERROR = "CONTRACT_ANNEX_PREDECESSOR_SOURCE_MISMATCH"
PREDECESSOR_ERROR = "CONTRACT_ANNEX_PREDECESSOR_CHECKPOINT_MISMATCH"
RULE = "reviewed_native_annex_clause_already_cited_by_predecessor_v1"
_NUMBERED = re.compile(r"^([1-9]\d*)[.．]([1-9]\d*)(?![\d.．])\s*\S")
_SECTION = re.compile(r"^([1-9]\d*)[.．]\s*(?!\d)\S")
_NUMBERING = re.compile(r"^[1-9]\d*[.．](?:[1-9]\d*(?:[.．][1-9]\d*)*[.．]?)?\s*[^\d\s.．]")
_SEAM_TOKEN = re.compile(r"[\u3400-\u9fff]{2,12}")
_SUBMISSION = re.compile(r"投标|响应文件|报价|报名|递交|资格审查")
_END = ("。", "！", "？")


class _Predecessor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    window_id: _ID
    raw_candidate_sha256: _SHA
    candidate_index: int = Field(ge=0)
    requirement_sha256: _SHA


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    predecessor: _Predecessor
    annex_heading_block_id: _ID
    annex_page: _ReviewPage
    section_heading_block_id: _ID
    clause_start_block_id: _ID
    reviewed_pages: list[_ReviewPage] = Field(min_length=1, max_length=4)
    deferred_block_ids: list[_ID] = Field(max_length=12)
    next_section_block_id: _ID | None = None
    seam_token: _ID | None = None


def normalize_contract_annex_predecessor_config(value):
    error = "CONTRACT_ANNEX_PREDECESSOR_CONFIG_INVALID"
    try:
        entries = value if isinstance(value, list) else [value]
        scopes = [_Scope.model_validate(scope).model_dump(exclude_none=True) for scope in entries]
    except ValidationError:
        raise ValueError(error) from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError(error)
    for scope in scopes:
        pages = [review["physical_page"] for review in scope["reviewed_pages"]]
        deferred = scope["deferred_block_ids"]
        if (pages != list(range(pages[0], pages[0] + len(pages))) or scope["annex_page"]["physical_page"] >= pages[0] or
                scope["predecessor"]["window_id"] == scope["window_id"] or len(set(deferred)) != len(deferred)):
            raise ValueError(error)
        if ("next_section_block_id" in scope) != ("seam_token" in scope) or (
                "seam_token" in scope and (len(pages) < 2 or not _SEAM_TOKEN.fullmatch(scope["seam_token"]))):
            raise ValueError(error)
    return scopes if isinstance(value, list) else scopes[0]


def _require(condition, error=SOURCE_ERROR):
    if not condition:
        raise ValueError(error)


def _located_member(row):
    return _proof_member(row) | {"bbox": row["bbox"]}


def _same_members(actual, reviewed):
    return all(row["origin"] == "native" and row["kind"] == "paragraph" and not row.get("read_disposition")
               for row in actual) and [_proof_member(row) for row in actual] == [
        {key: value for key, value in row.items() if key != "bbox"} for row in reviewed]


def _clause_bounds(lines, scope):
    opening, = [i for i, row in enumerate(lines) if row["block_id"] == scope["clause_start_block_id"]]
    headings = [(i, match) for i, row in enumerate(lines)
                if (match := _NUMBERED.match(row["text"]))]
    at, = [i for i, (offset, _) in enumerate(headings) if offset == opening]
    _require(0 < at < len(headings) - 1)
    (before, previous), (_, current), (after, following) = headings[at - 1:at + 2]
    _require(previous[1] == current[1] == following[1] and
             int(previous[2]) + 1 == int(current[2]) == int(following[2]) - 1 and
             sum(match.groups() == current.groups() for _, match in headings) == 1)
    section, = [row for row in lines[:before] if row["block_id"] == scope["section_heading_block_id"]]
    heading = _SECTION.match(section["text"])
    _require(heading and heading[1] == current[1] and
             not any(_SECTION.match(row["text"]) for row in lines if section["ordinal"] < row["ordinal"] < lines[after]["ordinal"]))
    clause = lines[opening:after]
    reviewed = lines[before:after + 1]
    _require(len(clause) >= 2 and len({row["page"] for row in reviewed}) == 1 and
             all(right["ordinal"] == left["ordinal"] + 1 and left["bbox"][3] <= right["bbox"][1]
                 for left, right in zip(reviewed, reviewed[1:])) and
             lines[opening - 1]["text"].endswith(_END) and clause[-1]["text"].endswith(_END) and
             not any(row["text"].endswith(_END) for row in clause[:-1]) and
             max(abs(row["bbox"][0] - clause[0]["bbox"][0]) for row in (lines[before], lines[after])) <= .5 and
             all(row["bbox"][0] < clause[0]["bbox"][0] and abs(row["bbox"][0] - clause[-1]["bbox"][0]) <= .5
                 for row in clause[1:]))
    quote = "\n".join(row["text"] for row in clause)
    _require("乙方" in clause[0]["text"] and "应" in clause[0]["text"] and not _SUBMISSION.search(quote))
    return section, lines[before:opening], clause, lines[after], f"{current[1]}.{current[2]}", quote


def _cross_page_bounds(lines, pages, scope):
    """Prove the last subsection continues above the next numbered root on a fresh page."""
    by_id = {row["block_id"]: i for i, row in enumerate(lines)}
    section_index, opening, after = (by_id[scope[key]] for key in (
        "section_heading_block_id", "clause_start_block_id", "next_section_block_id"))
    section, later = lines[section_index], lines[after]
    heading, next_heading = _SECTION.match(section["text"]), _SECTION.match(later["text"])
    number = _NUMBERED.match(lines[opening]["text"])
    _require(section_index + 1 < opening < after and heading and next_heading and number and
             heading[1] == number[1] and int(next_heading[1]) == int(heading[1]) + 1 and
             not any(_SECTION.match(row["text"]) for row in lines[section_index + 1:after]))
    children = [(index, match) for index in range(section_index + 1, after)
                if (match := _NUMBERED.match(lines[index]["text"]))]
    _require(len(children) >= 2 and children[-1][0] == opening and
             [match.groups() for _, match in children] ==
             [(number[1], str(child)) for child in range(1, int(number[2]) + 1)])
    before = children[-2][0]
    clause, earlier = lines[opening:after], lines[before:opening]
    _require(len(clause) >= 2 and lines[opening - 1]["text"].endswith(_END) and clause[-1]["text"].endswith(_END) and
             not any(_NUMBERING.match(row["text"]) for row in clause[1:]) and
             abs(earlier[0]["bbox"][0] - clause[0]["bbox"][0]) <= .5 and abs(later["bbox"][0] - clause[0]["bbox"][0]) <= .5 and
             all(row["bbox"][0] < clause[0]["bbox"][0] and abs(row["bbox"][0] - clause[-1]["bbox"][0]) <= .5 for row in clause[1:]))
    reviewed = [*earlier, *clause, later]
    pairs = list(zip(reviewed, reviewed[1:]))
    (left, right), = [(left, right) for left, right in pairs if left["page"] != right["page"]]
    footer, = [page["footer"] for page in pages if page["physical_page"] == left["page"]]
    token = scope["seam_token"]
    split, = [i for i in range(1, len(token)) if left["text"].endswith(token[:i]) and right["text"].startswith(token[i:])]
    _require(left in clause[:-1] and right in clause[1:] and right["page"] == left["page"] + 1 == later["page"] and
             right["locator"].endswith("/line[0]") and
             footer["rule"] == "pdf-page-footer-v1" and footer["page"] == left["page"] and
             footer["locator"] != left["locator"] and right["ordinal"] == left["ordinal"] + 2 and
             all(b["ordinal"] == a["ordinal"] + 1 and a["bbox"][3] <= b["bbox"][1]
                 for a, b in pairs if a["page"] == b["page"]))
    quote = "\n".join(row["text"] for row in clause)
    _require(not _SUBMISSION.search(quote))
    seam = {"last_predecessor_member": _located_member(left), "first_target_member": _located_member(right),
            "footer": footer, "split_token": token, "split_at": split}
    return section, earlier, clause, later, f"{number[1]}.{number[2]}", quote, seam


def _source_proof(lines, pages, annex_lines, annex_page, scope, blocks, previous, window, following):
    footers = {page["footer"]["block_id"] for page in pages}
    lines = [row for row in lines if row["block_id"] not in footers]
    seam = None
    if "next_section_block_id" in scope:
        section, earlier, clause, later, number, quote, seam = _cross_page_bounds(lines, pages, scope)
    else:
        section, earlier, clause, later, number, quote = _clause_bounds(lines, scope)
    annex, = [row for row in annex_lines if row["block_id"] == scope["annex_heading_block_id"]]
    _require(_ANNEX.fullmatch(annex["text"].split("【", 1)[0]) and annex["ordinal"] < section["ordinal"] and
             not any(row["source_id"] == scope["source_id"] and annex["ordinal"] < row["ordinal"] < section["ordinal"] and
                     _ANNEX.fullmatch(row["text"].split("【", 1)[0]) for row in blocks.values()))
    prior_owned = {row["block_id"]: row for row in previous["blocks"]}
    target = {row["block_id"]: row for row in window["blocks"]}
    adjacent = {row["block_id"]: row for row in window["adjacent_context"]}
    previous_members = [row for row in clause if row["page"] == clause[0]["page"]] if seam else clause[:1]
    tail = clause[len(previous_members):]
    trailer = []
    if seam:
        footer = seam["footer"]
        footer_id = footer["block_id"]
        _require(blocks[footer_id]["ordinal"] == previous_members[-1]["ordinal"] + 1 and
                 footer in previous["safe_skip_blocks"] and footer in window["safe_skip_blocks"] and
                 previous_members[-1]["block_id"] == seam["last_predecessor_member"]["block_id"] and
                 tail[0]["block_id"] == seam["first_target_member"]["block_id"])
        trailer = [footer_id]
    prior_ids = [row["block_id"] for row in previous_members]
    _require(list(prior_owned)[-(len(prior_ids) + len(trailer)):] == [*prior_ids, *trailer] and
             list(target)[:len(tail) + 1] == [row["block_id"] for row in [*tail, later]] and
             _same_members([prior_owned[key] for key in prior_ids], [_located_member(row) for row in previous_members]) and
             _same_members([adjacent[key] for key in prior_ids], [_located_member(row) for row in previous_members]) and
             _same_members([target[row["block_id"]] for row in [*tail, later]], [_located_member(row) for row in [*tail, later]]))
    prior_adjacent = {row["block_id"]: row for row in previous["adjacent_context"]}
    _require(_same_members([prior_adjacent[row["block_id"]] for row in tail], [_located_member(row) for row in tail]))
    deferred = [row for row in lines if row["block_id"] in scope["deferred_block_ids"]]
    _require([row["block_id"] for row in deferred] == scope["deferred_block_ids"])
    if deferred:
        _require(following is not None and all(row["ordinal"] > later["ordinal"] for row in deferred) and
                 [row["block_id"] for row in following["blocks"][:len(deferred)]] == scope["deferred_block_ids"] and
                 _same_members(following["blocks"][:len(deferred)], [_located_member(row) for row in deferred]) and
                 _same_members([adjacent[row["block_id"]] for row in deferred], [_located_member(row) for row in deferred]))
    proof = {
        "annex_heading": _located_member(annex), "annex_page": annex_page, "section_heading": _located_member(section),
        "preceding_sibling": [_located_member(row) for row in earlier],
        "clause_number": number, "clause_members": [_located_member(row) for row in clause],
        "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
        "following_context": [{"owner_window_id": following["window_id"], "member": _located_member(row),
                               "status": "deferred_to_owning_window"} for row in deferred],
    }
    if seam:
        proof.update(following_section=_located_member(later), page_seam=seam)
    else:
        proof["following_sibling"] = _located_member(later)
    return proof


def bind_contract_annex_predecessor(windows, documents, blocks, footer_skips, manifest, config):
    """Verify native text, exact geometry, render hashes, and unique target ownership."""
    try:
        source_id = config["source_id"]
        source, = [row for row in (manifest or {}).get("sources", []) if row["source_id"] == source_id]
        document, = [row for row in documents if row["source_id"] == source_id]
        index, = [i for i, row in enumerate(windows) if row["window_id"] == config["window_id"]]
        _require(index > 0)
        previous, window = windows[index - 1:index + 1]
        following = windows[index + 1] if index + 1 < len(windows) else None
        _require(previous["window_id"] == config["predecessor"]["window_id"])
        neighbors = [previous, window] + ([following] if config["deferred_block_ids"] and following is not None else [])
        _require(document["format"] == "pdf" and source["sha256"] == document["sha256"] == config["source_sha256"] and
                 all(row["source_id"] == source_id and row["source_format"] == "pdf" and
                     row["source_sha256"] == source["sha256"] for row in neighbors))
        payload = Path(source["path"]).read_bytes()
        _require(hashlib.sha256(payload).hexdigest() == source["sha256"])
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            annex_lines, (annex_page,) = _page_inventory(pdf, payload, [config["annex_page"]], document, blocks, footer_skips)
            lines, pages = _page_inventory(pdf, payload, config["reviewed_pages"], document, blocks, footer_skips)
        proof = _source_proof(lines, pages, annex_lines, annex_page, config, blocks, previous, window, following)
        member_ids = {row["block_id"] for row in proof["clause_members"]} | set(config["deferred_block_ids"])
        _require(sum(row["block_id"] in member_ids for member in windows for row in member["blocks"]) == len(member_ids))
        proof.update(rule=RULE, source_id=source_id, source_sha256=source["sha256"], window_id=window["window_id"],
                     scope=config, reviewed_pages=pages)
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def _candidate_ids(candidate):
    citation = candidate["citation"]
    return citation.get("block_ids") or [citation["block_id"]]


def verify_contract_annex_predecessor(predecessor_result, proof):
    """Require the complete native quote and the original raw selection of that quote."""
    prior = proof["scope"]["predecessor"]
    try:
        result = predecessor_result() if callable(predecessor_result) else predecessor_result
        _require(result and result["window_id"] == prior["window_id"] and result["status"] == "complete" and
                 result["candidate_origin"] == "application_normalized", PREDECESSOR_ERROR)
        raw = result["raw_model_candidate"]
        _require(raw["window_id"] == prior["window_id"] and digest(raw) == prior["raw_candidate_sha256"] and
                 result["live_confirmation"]["raw_structured_candidate_sha256"] == prior["raw_candidate_sha256"], PREDECESSOR_ERROR)
        index = prior["candidate_index"]
        model = raw["requirements"][index]
        validated = result["validated"]["requirements"]
        requirement = validated[index]
        members = proof["clause_members"]
        first, last = members[0], members[-1]
        block_ids = [row["block_id"] for row in members]
        quote = "\n".join(row["text"] for row in members)
        citation = requirement["citation"]
        expected = {key: first[key] for key in ("source_id", "unit_id", "locator", "block_id", "page")}
        seam = proof.get("page_seam")
        expected.update(block_ids=block_ids, end_locator=last["locator"], start=0, end=len(quote), quote=quote,
                        quote_sha256=proof["quote_sha256"], pages=list(dict.fromkeys(row["page"] for row in members)),
                        basis="consecutive_blocks_lf_v1", skipped_block_ids=[seam["footer"]["block_id"]] if seam else [],
                        clause_id=None, table_row_id=None, label_grounding=[])
        _require(model["citation"] == {"block_ids": block_ids} and citation == expected and
                 digest(requirement) == prior["requirement_sha256"] and
                 {key: value for key, value in requirement.items() if key != "citation"} ==
                 {key: value for key, value in model.items() if key != "citation"} and
                 requirement["category"] in {"commercial", "procedure"} and requirement["response_mode"] == "procedure" and
                 requirement["mandatory"] is True and requirement["format_status"] == "not_prescribed" and
                 not any(requirement[key] for key in ("format_title", "attachment_keywords", "required_fact_labels", "reference_keywords")),
                 PREDECESSOR_ERROR)
        ids = set(block_ids)
        _require([i for i, row in enumerate(validated) if ids.intersection(_candidate_ids(row))] == [index] and
                 [i for i, row in enumerate(raw["requirements"]) if ids.intersection(_candidate_ids(row))] == [index] and
                 not any(ids.intersection([*_candidate_ids(row), *row["content_block_ids"]])
                         for row in result["validated"]["format_chapters"]), PREDECESSOR_ERROR)
    except (KeyError, TypeError, IndexError):
        raise ValueError(PREDECESSOR_ERROR) from None


def resolve_contract_annex_predecessor(raw, normalized, window, critical, predecessor_result):
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None:
        return None
    scope = proof["scope"]
    _require(proof["rule"] == RULE and digest(raw) == scope["raw_candidate_sha256"] and
             all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")) and
             digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"])
    members = proof["clause_members"]
    seam = proof.get("page_seam")
    previous_members = [row for row in members if row["page"] == members[0]["page"]] if seam else members[:1]
    tail = members[len(previous_members):]
    target_ids = [row["block_id"] for row in tail]
    context = proof["following_context"]
    future_ids = [row["member"]["block_id"] for row in context]
    targets = {row["block_id"]: row for row in window["blocks"]}
    adjacent = {row["block_id"]: row for row in window["adjacent_context"]}
    _require(critical == target_ids and normalized["unresolved_block_ids"] == [*target_ids, *future_ids] and
             future_ids == scope["deferred_block_ids"] and
             _same_members([targets[key] for key in target_ids], tail) and
             _same_members([*(adjacent[row["block_id"]] for row in previous_members), *(adjacent[key] for key in future_ids)],
                           [*previous_members, *(row["member"] for row in context)]))
    selected = {key for row in [*normalized["requirements"], *normalized["format_chapters"]] for key in _candidate_ids(row)}
    selected.update(key for row in normalized["format_chapters"] for key in row["content_block_ids"])
    following = proof["following_section"] if seam else proof["following_sibling"]
    _require(not selected.intersection({*(row["block_id"] for row in previous_members), *target_ids, *future_ids}) and
             any(_candidate_ids(row)[0] == following["block_id"] for row in normalized["requirements"]))
    verify_contract_annex_predecessor(predecessor_result, proof)
    return {"rule": RULE, "window_id": window["window_id"], "raw_candidate_sha256": digest(raw), "proof": proof,
            "resolved_block_ids": target_ids, "deferred_to_following_window": context,
            "resolution_basis": "existing_complete_predecessor_contract_requirement",
            "model_candidate_mutation": False, "new_requirement_created": False, "semantic_review": "requires_source_review"}
