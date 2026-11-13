"""Prove one missing native line inside one complete contract clause.

This is a pure source proof, not a correction or semantic acceptance. A caller
must preserve the rejected candidate, permit only the proved block-ID insert,
and run the unchanged full validator after any other independently proved
repairs. In particular, unresolved IDs and business fields are never changed.
"""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata

from pydantic import ValidationError

from .extraction import (
    PlanningError, _LiveWindowResponse, _heading_level, _index,
    _normalize_source_selectors, _validate_window, _windows,
    extraction_window_payload,
)
from .storage import digest


_CHAPTER = re.compile(r"第[零一二三四五六七八九十百千万\d]+章[^\r\n]*")
_CONTRACT = re.compile(r"第[零一二三四五六七八九十百千万\d]+章[ \t]*合同(?:条款|格式|条款及格式)")
_ARTICLE = re.compile(r"第([一二三四五六七八九十百]+|[1-9]\d{0,2})条[ \t]*\S[^\r\n]*")
_CLAUSE = re.compile(r"([1-9]\d{0,2})\.([1-9]\d{0,2})(?![\d.])[ \t]*(\S[^\r\n]*)")


def _number(text):
    if text.isdecimal():
        return int(text)
    digits = "一二三四五六七八九"
    if text in digits:
        return digits.index(text) + 1
    if text == "十":
        return 10
    if text.startswith("十") and text[1:] in digits:
        return 10 + digits.index(text[1:]) + 1
    if len(text) >= 2 and text[0] in digits and text[1] == "十":
        rest = text[2:]
        if not rest or rest in digits:
            return (digits.index(text[0]) + 1) * 10 + (digits.index(rest) + 1 if rest else 0)
    return None


def _line(block, units, source_id):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    location = re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", str(block.get("locator", "")))
    text = block.get("text")
    if (block.get("source_id") != source_id or block.get("kind") != "paragraph" or
            block.get("origin") != "native" or block.get("read_disposition") or not location or
            type(block.get("page")) is not int or int(location[1]) != block["page"] or
            unit.get("page") != block["page"] or unit.get("locator") != f"pdf/page[{block['page']}]" or
            coverage.get("status") != "read" or type(coverage.get("image_count")) is not int or
            coverage["image_count"] != 0 or any(coverage.get(k) for k in
                ("unread_objects", "unmapped_glyphs", "error_code")) or
            any(row.get("kind") == "image" for row in unit.get("blocks", [])) or
            not isinstance(text, str) or not text.strip() or
            any(char == "\ufffd" or unicodedata.category(char) in {"Co", "Cc", "Cf", "Cs", "Cn"} for char in text)):
        return None
    return int(location[2])


def _member(block):
    location = {key: block[key] for key in
                ("block_id", "source_id", "locator", "page", "ordinal", "unit_id", "unit_ordinal")}
    return {**location, "location_sha256": digest(location), "text": block["text"],
            "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _consecutive(members, units, source_id):
    lines = [_line(block, units, source_id) for block in members]
    return (all(line is not None for line in lines) and
            all(left["unit_id"] == right["unit_id"] and left["page"] == right["page"] and
                right["ordinal"] == left["ordinal"] + 1 and
                right["unit_ordinal"] == left["unit_ordinal"] + 1 and b == a + 1
                for left, right, a, b in zip(members, members[1:], lines, lines[1:])))


def native_citation_gap_restore(candidate, request, parsed, manifest):
    """Return proof of a single insert, or ``None`` for every unsupported case.

    Only one requirement may have one gap of exactly one native PDF line. Its
    original first/last IDs must already delimit the entire numbered contract
    clause, terminated by the next consecutive sibling or article heading.
    Frozen N1 reconstructs the complete actual request, including distant
    context. Remaining unresolved IDs are retained, not deemed resolved here.
    """
    try:
        if (request.get("task_type", "extract_requirements") != "extract_requirements" or
                parsed.get("coverage", {}).get("required_complete") is not True):
            return None
        blocks, units, documents = _index(parsed, manifest)
        config = request["window_config"]
        windows, _ = _windows(documents, blocks, config)
        matches = [window for window in windows if window["window_id"] == request["window"]["window_id"]]
        if len(matches) != 1 or extraction_window_payload(matches[0], config) != {
                key: value for key, value in request.items() if key != "task_type"}:
            return None
        window = matches[0]
        normalized, _ = _normalize_source_selectors(candidate, window)
        data = _LiveWindowResponse.model_validate(normalized, strict=True).model_dump(mode="json")
        if (data["window_id"] != window["window_id"] or data["format_status"] in {"unread", "conflict"} or
                any(item["format_status"] in {"unread", "conflict"} for item in data["requirements"]) or
                any("quote" in item["citation"] for field in ("requirements", "format_chapters")
                    for item in candidate[field])):
            return None
        available = {block["block_id"]: block for block in [*window["blocks"], *window.get("adjacent_context", [])]}
        targets = {block["block_id"] for block in window["blocks"]}
        unresolved = data["unresolved_block_ids"]
        if len(set(unresolved)) != len(unresolved) or not set(unresolved) <= set(available):
            return None
        source_id = window["source_id"]
        document, = [document for document in documents if document["source_id"] == source_id]
        if (document.get("format") != "pdf" or not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or
                document["sha256"] == "0" * 64 or
                type(document.get("expected_units", len(document["units"]))) is not int or
                document.get("expected_units", len(document["units"])) != len(document["units"]) or
                any(unit.get("coverage", {}).get("status") not in {"read", "empty"} or
                    any(unit.get("coverage", {}).get(k) for k in
                        ("unread_objects", "unmapped_glyphs", "error_code")) for unit in document["units"])):
            return None
        gaps = []
        safe_skips = {row["block_id"] for row in window.get("safe_skip_blocks", [])}
        for index, item in enumerate(data["requirements"]):
            ids = item["citation"]["block_ids"] or [item["citation"]["block_id"]]
            if (len(ids) != len(set(ids)) or not set(ids) <= set(available) or
                    ids != sorted(ids, key=lambda key: blocks[key]["ordinal"])):
                return None
            for left_id, right_id in zip(ids, ids[1:]):
                left, right = blocks[left_id], blocks[right_id]
                difference = right["ordinal"] - left["ordinal"]
                if difference > 1:
                    between = [block for block in blocks.values()
                               if left["ordinal"] < block["ordinal"] < right["ordinal"]]
                    # An already proved page footer is not a missing source
                    # line. Preserve the existing validator's skip semantics
                    # for the other immutable requirements.
                    if between and all(block["block_id"] in safe_skips for block in between):
                        continue
                    if difference != 2:
                        return None
                    gaps.append((index, left, right))
        if len(gaps) != 1:
            return None
        index, left, right = gaps[0]
        item = data["requirements"][index]
        original_ids = item["citation"]["block_ids"]
        if (not 2 <= len(original_ids) < 24 or item["citation"].get("clause_id") or
                item["citation"].get("table_row_id") or not set(original_ids) & targets):
            return None
        body = [block for block in blocks.values() if block["source_id"] == source_id]
        positions = {block["block_id"]: position for position, block in enumerate(body)}
        first, last = positions[original_ids[0]], positions[original_ids[-1]]
        restored = body[first:last + 1]
        restored_ids = [block["block_id"] for block in restored]
        omitted = [block for block in restored if block["block_id"] not in original_ids]
        if (len(omitted) != 1 or not set(restored_ids) <= set(available) or
                not _consecutive(restored, units, source_id) or
                [key for key in restored_ids if key in original_ids] != original_ids or
                omitted[0]["ordinal"] != left["ordinal"] + 1 or
                right["ordinal"] != omitted[0]["ordinal"] + 1):
            return None
        clause = _CLAUSE.fullmatch(restored[0]["text"])
        if (not clause or not re.search(r"应当|必须|不得|须|应|甲方|乙方", clause[3]) or
                not re.search(r"[。！？!?]$", restored[-1]["text"]) or
                any(_heading_level(block["text"]) is not None or _CLAUSE.fullmatch(block["text"])
                    for block in restored[1:]) or last + 1 >= len(body)):
            return None
        ancestors = [block for block in body[:first] if _CHAPTER.fullmatch(block["text"])]
        if not ancestors or not _CONTRACT.fullmatch(ancestors[-1]["text"]):
            return None
        ancestor = ancestors[-1]
        articles = [block for block in body[positions[ancestor["block_id"]] + 1:first]
                    if _ARTICLE.fullmatch(block["text"])]
        if not articles or _number(_ARTICLE.fullmatch(articles[-1]["text"])[1]) != int(clause[1]):
            return None
        article = articles[-1]
        # A repeated article number (for example a table of contents followed
        # by its body) cannot itself be the proof: use the final actual parent.
        if any(_CHAPTER.fullmatch(block["text"]) or _ARTICLE.fullmatch(block["text"])
               for block in body[positions[article["block_id"]] + 1:first]):
            return None
        boundary = body[last + 1]
        sibling = _CLAUSE.fullmatch(boundary["text"])
        next_article = _ARTICLE.fullmatch(boundary["text"])
        if (not ((sibling and sibling[1] == clause[1] and int(sibling[2]) == int(clause[2]) + 1) or
                 (next_article and _number(next_article[1]) == int(clause[1]) + 1)) or
                not _consecutive([*restored, boundary], units, source_id) or
                any(_line(block, units, source_id) is None for block in (ancestor, article))):
            return None
        corrected = copy.deepcopy(candidate)
        corrected["requirements"][index]["citation"]["block_ids"] = restored_ids
        corrected_normalized, _ = _normalize_source_selectors(corrected, window)
        corrected_data = _LiveWindowResponse.model_validate(corrected_normalized, strict=True).model_dump(mode="json")
        from .structured import _existing_inventory_is_grounded
        if not _existing_inventory_is_grounded(corrected_data, window):
            return None
        # Validate the entire inventory without declaring unresolved reading
        # complete. The original live validator will still reject those IDs.
        inventory = {key: corrected_data[key] for key in ("requirements", "format_chapters", "format_status")}
        inventory["reviewed_block_ids"] = [block["block_id"] for block in window["blocks"]]
        _validate_window(inventory, window)
        proof = {"rule": "native_pdf_complete_contract_clause_single_line_gap_v1",
            "window_id": window["window_id"], "source_id": source_id, "source_sha256": document["sha256"],
            "parsed_sha256": digest(parsed), "manifest_sha256": digest(manifest),
            "source_reading_sha256": digest(document), "request_sha256": digest(request),
            "frozen_window_sha256": digest(window), "candidate_index": index,
            "original_candidate_sha256": digest(candidate), "expected_candidate_sha256": digest(corrected),
            "original_requirement_sha256": digest(candidate["requirements"][index]),
            "expected_requirement_sha256": digest(corrected["requirements"][index]),
            "original_block_ids": original_ids, "restored_block_ids": restored_ids,
            "contract_ancestor": _member(ancestor), "article_heading": _member(article),
            "clause_number": clause[1] + "." + clause[2], "members": [_member(block) for block in restored],
            "omitted_members": [_member(block) for block in omitted], "next_boundary": _member(boundary),
            "coverage_sha256": digest(units[restored[0]["unit_id"]]["coverage"]),
            "unresolved_block_ids_sha256": digest(unresolved), "unresolved_status": "unchanged",
            "source_text_modified": False, "business_fields_modified": False,
            "candidate_mutation": False, "semantic_review": "requires_source_review"}
        return {"candidate_index": index, "original_block_ids": list(original_ids),
                "restored_block_ids": restored_ids, "omitted_members": copy.deepcopy(proof["omitted_members"]),
                "source_proof": proof, "source_proof_sha256": digest(proof)}
    except (PlanningError, ValidationError, KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
