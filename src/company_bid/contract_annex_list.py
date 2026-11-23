"""Reconstruct reviewed page-spanning numbered obligations in a contract annex."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_attachment_citation_split import _ID, _SHA, _member
from .contract_heading_only_annex import rendered_page_rgb_sha256
from .pdf_table_geometry import discover_pdf_tables
from .pdf_text_lines import pdf_page_text_lines
from .response_cover_sources import _native_lines
from .storage import digest


SOURCE_CONFIG_KEY = "contract_annex_list_source"
WINDOW_CONTEXT_KEY = "native_contract_annex_list_context"
RESOLUTION_KEY = "native_contract_annex_list_resolution"
SOURCE_ERROR = "CONTRACT_ANNEX_LIST_SOURCE_MISMATCH"
RULE = "reviewed_native_contract_annex_numbered_list_v1"
_ANNEX = re.compile(r"附件\s*[1-9]\d*\s*[：:].*(?:条款|合同|协议|规定)")
_HEADING = re.compile(r"^(?P<number>[1-9]\d*(?:\.[1-9]\d*)*)(?:\.(?!\d))?\s*(?P<title>[^\d\s.].+)$")
_ITEM = re.compile(r"^(?:[（(])?([1-9]\d*)[）)]\s*(\S.*)$")
_EMBEDDED_ITEM = re.compile(r"[。；！？]\s*(?:[（(])?[1-9]\d*[）)]\s*(?=\S)")
_SUBMISSION = re.compile(r"投标|响应文件|提交.+证明|递交")
_CLOSE = "。；;！!？?"


class _ReviewPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    physical_page: int = Field(ge=1)
    rendered_rgb_sha256: _SHA


class _ReferenceWrap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clause_number: _ID
    candidate_index: int = Field(ge=0)
    label_index: int = Field(ge=0)
    label: _ID


class _FocusRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parent_heading_block_id: _ID
    before_block_id: _ID
    after_block_id: _ID
    reference_wrap: _ReferenceWrap | None = None


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    list_heading_block_id: _ID
    reviewed_pages: list[_ReviewPage] = Field(min_length=2)
    focus_range: _FocusRange | None = None


def normalize_contract_annex_list_config(value):
    error = "CONTRACT_ANNEX_LIST_CONFIG_INVALID"
    try:
        entries = value if isinstance(value, list) else [value]
        scopes = [_Scope.model_validate(scope).model_dump(exclude_none=True) for scope in entries]
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


def _proof_member(block):
    return _member(block) | {"text": block["text"]}


def _page_inventory(pdf, payload, reviews, document, blocks, footer_skips):
    units = {unit["page"]: unit for unit in document["units"]}
    lines, pages = [], []
    for review in reviews:
        page = review["physical_page"]
        source_page, unit = pdf.pages[page - 1], units[page]
        coverage = unit["coverage"]
        current = [blocks[row["block_id"]] for row in unit["blocks"]]
        _require(coverage["status"] == "read" and coverage["origin"] == "native" and not any(coverage[key] for key in (
            "unread_objects", "ignored_objects", "unmapped_glyphs", "error_code", "warnings", "table_count", "image_count")) and
            _native_lines(current, document["source_id"], page, range(len(current))))
        _require(source_page.rotation == 0 and source_page.bbox == source_page.cropbox == source_page.mediabox and
                 source_page.bbox[:2] == (0, 0) and set(source_page.objects) <= {"char", "rect"})
        links = source_page.annots or []
        _require(all(str(link["data"].get("Subtype")) == "/'Link'" and
                     link["data"].get("BS", {}).get("W") == 0 and
                     not {"AP", "Contents"}.intersection(link["data"]) for link in links))
        discovery = discover_pdf_tables(source_page)
        _require(not discovery.tables and not discovery.diagonal_rules and
                 len(source_page.rects) == len(discovery.text_backgrounds))
        actual_transforms = [discovery.transformation(unit["locator"])] if discovery.text_backgrounds else []
        _require(coverage["transformations"] == actual_transforms and
                 [(i, text, box) for i, text, box, _, _ in pdf_page_text_lines(source_page, ())] ==
                 [(i, row["text"], row["bbox"]) for i, row in enumerate(current)] and
                 rendered_page_rgb_sha256(payload, page) == review["rendered_rgb_sha256"])
        footer = footer_skips.get(current[-1]["block_id"])
        _require(footer and footer["rule"] == "pdf-page-footer-v1" and footer["page"] == page and
                 footer["printed_page"] == int(current[-1]["text"]))
        pages.append({**review, "page_lines_sha256": digest(current), "coverage_sha256": digest(coverage),
                      "footer": footer, "borderless_link_boxes": [[link[key] for key in ("x0", "top", "x1", "bottom")]
                                                               for link in links]})
        lines.extend(current)
    _require(all(right["ordinal"] == left["ordinal"] + 1 for left, right in zip(lines, lines[1:])) and
             len({row["footer"]["printed_page"] - row["footer"]["page"] for row in pages}) == 1)
    return lines, pages


def _list_entries(lines, start, stop, footer_ids, owners):
    items = []
    for row in lines[start + 1:stop]:
        if row["block_id"] in footer_ids:
            continue
        _require(not _EMBEDDED_ITEM.search(row["text"]))
        marker = _ITEM.fullmatch(row["text"])
        if marker:
            _require(int(marker[1]) == len(items) + 1)
            items.append([])
        else:
            _require(items and not items[-1][-1]["text"].endswith(tuple(_CLOSE)))
        items[-1].append(row)
    _require(bool(items) and all(item[-1]["text"].endswith(tuple(_CLOSE)) for item in items))
    return [{"number": number, "members": [_proof_member(row) for row in members],
             "owner_window_ids": list(dict.fromkeys(owners[row["block_id"]] for row in members)),
             "quote": "\n".join(row["text"] for row in members)} for number, members in enumerate(items, 1)]


def _list_proof(pdf, lines, pages, config, blocks, windows, window):
    heading_index, = [i for i, row in enumerate(lines) if row["block_id"] == config["list_heading_block_id"]]
    headings = [(i, marker["number"].split(".")) for i, row in enumerate(lines) if (marker := _HEADING.fullmatch(row["text"]))
                and not _ITEM.fullmatch(row["text"])]
    number, = [number for i, number in headings if i == heading_index]
    parents = [[i for i, candidate in headings if i < heading_index and candidate == number[:depth]]
               for depth in range(1, len(number))]
    _require(len(number) >= 3 and all(len(indices) == 1 for indices in parents))
    parent_indices = [indices[0] for indices in parents]
    annexes = [row for row in blocks.values() if row["source_id"] == window["source_id"] and
               row["ordinal"] < lines[parent_indices[0]]["ordinal"] and _ANNEX.fullmatch(row["text"])]
    _require(bool(annexes))
    annex = max(annexes, key=lambda row: row["ordinal"])
    head, governing = parent_indices[:2]
    governing_end = next(i for i, _ in headings if i > governing)
    governing_rows = lines[governing:governing_end]
    governing_text = "".join(row["text"] for row in governing_rows)
    _require(re.search(r"乙方|承包(?:方|商|人)", lines[head]["text"]) and
             re.search(r"履行|项目实施", governing_text) and "遵守" in governing_text and not _SUBMISSION.search(governing_text))
    following, next_boundary = [(i, path) for i, path in headings if i > heading_index][:2]
    stop, following_number = following
    end, _ = next_boundary
    _require(len(following_number) <= len(number) and following_number[:-1] == number[:len(following_number) - 1] and
             int(following_number[-1]) == int(number[len(following_number) - 1]) + 1)
    owner_ids = {block["block_id"]: item["window_id"] for item in windows for block in item["blocks"]}
    footer_ids = {row["footer"]["block_id"] for row in pages}
    entries = _list_entries(lines, heading_index, stop, footer_ids, owner_ids)
    next_entries = _list_entries(lines, stop, end, footer_ids, owner_ids)
    _require(all(re.match(r"^(?:[（(])?[1-9]\d*[）)]\s*(?:严禁|禁止|不得)", item["quote"]) and
                 not _SUBMISSION.search(item["quote"]) for item in entries) and
             len({member["page"] for item in entries for member in item["members"]}) >= 2)
    catalog, label_boundary = [], None
    if focus := config.get("focus_range"):
        from .native_numbered_clause_spans import reviewed_numbered_clause_spans

        source = {row["block_id"]: row for row in lines}
        anchor = source[focus["before_block_id"]]
        catalog = reviewed_numbered_clause_spans(window, [row for row in lines if row["page"] == anchor["page"]],
            parent=source[focus["parent_heading_block_id"]], before_id=focus["before_block_id"], after_id=focus["after_block_id"])
        _require(bool(catalog))
        if wrap := focus.get("reference_wrap"):
            from .native_numbered_clause_spans import reviewed_numbered_label_boundary

            clause, = [row for row in catalog if row["boundary_proof"]["clause_number"] == wrap["clause_number"]]
            native = list(pdf_page_text_lines(pdf.pages[anchor["page"] - 1], ()))
            label_boundary = reviewed_numbered_label_boundary(window, native, clause, label=wrap["label"])
            _require(bool(label_boundary))
    return {"annex_heading": _proof_member(annex), "governing_headings": [_proof_member(lines[i]) for i in parent_indices],
            "governing_scope_text": [_proof_member(row) for row in governing_rows], "list_heading": _proof_member(lines[heading_index]),
            "list_number": ".".join(number), "list_items": entries, "following_heading": _proof_member(lines[stop]),
            "following_list_number": ".".join(following_number), "following_items": next_entries,
            "following_end_heading": _proof_member(lines[end]), "focused_clause_catalog": catalog,
            "focused_label_boundary": label_boundary}


def bind_contract_annex_list(windows, documents, blocks, footer_skips, manifest, config):
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
            proof = _list_proof(pdf, lines, pages, config, blocks, windows, window)
        proof.update(rule=RULE, source_id=source_id, source_sha256=source["sha256"], window_id=window["window_id"],
                     scope=config, reviewed_pages=pages, following_owner_window_id=windows[windows.index(window) + 1]["window_id"])
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, StopIteration, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def resolve_contract_annex_list(raw, normalized, window, critical):
    """Match the saved unresolved inventory against complete owned source items."""
    proof = window.get(WINDOW_CONTEXT_KEY)
    if not proof or not critical:
        return None
    _require(proof["rule"] == RULE and digest(raw) == proof["scope"]["raw_candidate_sha256"] and
             proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"}) and
             all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")))
    target = {row["block_id"]: row for row in window["blocks"]}
    adjacent = {row["block_id"]: row for row in window["adjacent_context"]}
    ids = set(critical)
    items = [item for item in proof["list_items"] if any(member["block_id"] in ids for member in item["members"])]
    _require(bool(items) and [item["number"] for item in items] == list(range(items[0]["number"], len(proof["list_items"]) + 1)))
    members = [member for item in items for member in item["members"]]
    _require([member["block_id"] for member in members] == critical and
             all(item["owner_window_ids"] == [window["window_id"]] for item in items) and
             all(_proof_member(target[member["block_id"]]) == member for member in members))
    declarations = [identity for identity in normalized["unresolved_block_ids"] if identity not in target]
    following = [item for item in proof["following_items"] if any(row["block_id"] in declarations for row in item["members"])]
    deferred_members = [member for item in following for member in item["members"]]
    _require([member["block_id"] for member in deferred_members] == declarations and
             all(item["owner_window_ids"] == [proof["following_owner_window_id"]] for item in following) and
             all(_proof_member(adjacent[member["block_id"]]) == member for member in deferred_members))
    selected = {identity for row in [*normalized["requirements"], *normalized["format_chapters"]]
                for identity in (row["citation"]["block_ids"] or [row["citation"]["block_id"]])}
    selected.update(identity for row in normalized["format_chapters"] for identity in row["content_block_ids"])
    _require(not ids.intersection(selected) and items[0]["number"] > 1)
    witnesses = []
    for item in proof["list_items"][:items[0]["number"] - 1]:
        sibling_ids = [row["block_id"] for row in item["members"]]
        index, = [i for i, row in enumerate(normalized["requirements"]) if row["citation"]["block_ids"] == sibling_ids and
                  (row["category"], row["response_mode"], row["mandatory"], row["format_status"]) ==
                  ("technical", "writing", True, "not_prescribed")]
        witnesses.append({"number": item["number"], "candidate_index": index, "block_ids": sibling_ids})
    return {"rule": RULE, "raw_candidate_sha256": digest(raw), "window_id": window["window_id"], "proof": proof,
            "resolved_block_ids": list(critical), "items": [dict(item) for item in items], "same_list_routing_witnesses": witnesses,
            "deferred_following_items": [{"number": item["number"], "owner_window_id": proof["following_owner_window_id"],
                                          "block_ids": [row["block_id"] for row in item["members"]],
                                          "semantic_status": "await_owner_window_review"} for item in following],
            "route_basis": "contract_performance_prohibition_with_same_list_technical_writing_witness",
            "model_candidate_mutation": False, "semantic_review": "requires_source_review"}
