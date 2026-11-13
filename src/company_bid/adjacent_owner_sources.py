"""Prove one duplicate requirement already carried by its successful owner.

The caller must first verify ``owner_witness`` with DiagnosticReplay._load_entry
against a sealed source run. This pure helper rechecks source scope, the raw
owner candidate and exact typed business fields; it does not authenticate HTTP
or trace records, remove candidates, resolve unknown text, or mutate inputs.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
import unicodedata

from .extraction import (
    _LiveWindowResponse, _index, _normalize_source_selectors, _windows,
    extraction_window_payload, validate_live_window,
)
from .storage import digest


_CLAUSE = re.compile(r"([1-9][0-9]{0,2})\.([1-9][0-9]{0,2})[ \t]*\S[^\r\n]*")
_ARTICLE = re.compile(r"第([一二三四五六七八九十]+|[1-9][0-9]{0,2})条[ \t]*\S[^\r\n]*")
_CHAPTER = re.compile(r"第[零一二三四五六七八九十百0-9]+章[^\r\n]*")


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(size) + r"}", value) and value != "0" * size


def _number(value):
    if value.isascii() and value.isdecimal():
        return int(value)
    digits = "一二三四五六七八九"
    if value in digits:
        return digits.index(value) + 1
    if value.count("十") == 1:
        left, right = value.split("十")
        if (not left or left in digits) and (not right or right in digits):
            return (digits.index(left) + 1 if left else 1) * 10 + (digits.index(right) + 1 if right else 0)
    return None


def _native_line(block, units):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    line = re.fullmatch(r"pdf/page\[([1-9][0-9]*)\]/line\[([0-9]+)\]", block.get("locator", ""))
    box = block.get("bbox")
    if (not line or block.get("kind") != "paragraph" or block.get("origin") != "native" or
            block.get("read_disposition") or coverage.get("status") != "read" or
            any(coverage.get(key) for key in ("unread_objects", "unmapped_glyphs", "error_code", "ignored_objects")) or
            type(coverage.get("image_count")) is not int or coverage["image_count"] != 0 or
            any(item.get("kind") == "image" for item in unit["blocks"]) or
            type(block.get("page")) is not int or int(line[1]) != block["page"] or
            unit.get("page") != block["page"] or unit.get("locator") != f"pdf/page[{block['page']}]" or
            not isinstance(block.get("text"), str) or not block["text"].strip() or
            any(char == "\ufffd" or unicodedata.category(char) in {"Co", "Cc", "Cf", "Cs", "Cn"}
                for char in block["text"]) or
            not isinstance(box, list) or len(box) != 4 or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in box) or
            box[0] >= box[2] or box[1] >= box[3]):
        return None
    return int(line[2])


def _member(block):
    return {**{key: copy.deepcopy(block[key]) for key in (
        "block_id", "source_id", "unit_id", "locator", "page", "ordinal", "unit_ordinal", "bbox")},
        "text": block["text"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _complete_clause(ids, blocks, units, document):
    """A same-page numbered clause through its next native sibling boundary."""
    body = [block for block in blocks.values() if block["source_id"] == document["source_id"]]
    position = next(index for index, block in enumerate(body) if block["block_id"] == ids[0])
    first = _CLAUSE.fullmatch(body[position]["text"])
    if not first:
        return None
    end = None
    for index in range(position + 1, len(body)):
        block = body[index]
        sibling, article = _CLAUSE.fullmatch(block["text"]), _ARTICLE.fullmatch(block["text"])
        if sibling or article:
            if not ((sibling and int(sibling[1]) == int(first[1]) and int(sibling[2]) == int(first[2]) + 1) or
                    (article and _number(article[1]) == int(first[1]) + 1)):
                return None
            end = index
            break
        if _CHAPTER.fullmatch(block["text"]):
            return None
    if end is None or [block["block_id"] for block in body[position:end]] != ids:
        return None
    span = body[position:end + 1]
    if (any(_native_line(block, units) is None for block in span) or
            any(block["unit_id"] != span[0]["unit_id"] for block in span) or
            any(right["ordinal"] != left["ordinal"] + 1 or
                right["unit_ordinal"] != left["unit_ordinal"] + 1 or
                _native_line(right, units) != _native_line(left, units) + 1
                for left, right in zip(span, span[1:]))):
        return None
    return {"rule": "same_page_complete_native_numbered_clause_v1",
        "source_id": document["source_id"], "source_sha256": document["sha256"],
        "clause_number": first[1] + "." + first[2], "members": [_member(block) for block in span[:-1]],
        "next_boundary": _member(span[-1]),
        "coverage_sha256": digest(units[span[0]["unit_id"]]["coverage"]),
        "quote_sha256": hashlib.sha256("\n".join(block["text"] for block in span[:-1]).encode()).hexdigest()}


def _witness_identity(witness, owner_task):
    """Check consistency only; actual call authenticity belongs to the caller."""
    binding, entry = witness["binding"], witness["entry"]
    origin = entry["origin"]
    if (not isinstance(binding, dict) or set(binding) != {"alias", "run_id", "node_key", "unit_id"} or
            not isinstance(binding["alias"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", binding["alias"]) or
            not _hex(binding["run_id"], 32) or binding["node_key"] not in {"N2", "N2_WINDOW_DIAGNOSTIC"} or
            any(origin.get(key) != binding[value] for key, value in (
                ("source_alias", "alias"), ("run_id", "run_id"), ("node_key", "node_key"), ("unit_id", "unit_id"))) or
            origin.get("external_call") is not False or
            not isinstance(origin.get("historical_producer_version"), str) or not origin["historical_producer_version"] or
            origin.get("candidate_sha256") != digest(entry["candidate"]) or
            origin.get("base_request_sha256") != digest(owner_task) or entry.get("request_sha256") != digest(owner_task) or
            not isinstance(origin.get("calls"), list) or not origin["calls"]):
        return False
    for call in origin["calls"]:
        if (not isinstance(call, dict) or not isinstance(call.get("source_alias"), str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", call["source_alias"]) or
                not _hex(call.get("run_id"), 32) or not _hex(call.get("call_id"), 32) or
                type(call.get("call_attempt")) is not int or call["call_attempt"] != 1 or
                not _hex(call.get("trace_id"), 32) or not _hex(call.get("span_id"), 16) or
                not _hex(call.get("request_sha256"), 64) or not _hex(call.get("response_sha256"), 64)):
            return False
    final = origin["calls"][-1]
    return (origin.get("actual_final_wire_sha256") == final["request_sha256"] and
            final["source_alias"] == binding["alias"] and final["run_id"] == binding["run_id"])


def owned_adjacent_requirement_proof(candidate, request, parsed, manifest, owner_witness):
    """Return proof for one already-owned duplicate, otherwise ``None``.

    ``entry`` must be the untouched result of a successful, independent
    DiagnosticReplay._load_entry, not a reviewed-block ledger. The caller must
    reverify and seal that lineage before using this proof in a correction.
    Other requirements, formats and unresolved IDs receive no permission here.
    """
    try:
        if (request.get("task_type") != "extract_requirements" or
                parsed.get("coverage", {}).get("required_complete") is not True or
                "reviewed_block_ids" in candidate or
                any("quote" in item.get("citation", {}) for field in ("requirements", "format_chapters")
                    for item in candidate.get(field, []))):
            return None
        blocks, units, documents = _index(parsed, manifest)
        windows, _ = _windows(documents, blocks, request["window_config"])
        current, = [window for window in windows if window["window_id"] == request["window"]["window_id"]]
        if extraction_window_payload(current, request["window_config"]) != {key: value for key, value in request.items() if key != "task_type"}:
            return None
        normalized, _ = _normalize_source_selectors(copy.deepcopy(candidate), current)
        data = _LiveWindowResponse.model_validate(normalized, strict=True).model_dump(mode="json")
        targets = {block["block_id"] for block in current["blocks"]}
        adjacent = {block["block_id"] for block in current["adjacent_context"]}
        unresolved = data["unresolved_block_ids"]
        if (data["window_id"] != current["window_id"] or len(set(unresolved)) != len(unresolved) or
                not set(unresolved) <= targets | adjacent):
            return None
        outside = [(index, item) for index, item in enumerate(data["requirements"])
                   if not set(item["citation"]["block_ids"] or [item["citation"]["block_id"]]) & targets]
        if len(outside) != 1:
            return None
        index, original = outside[0]
        ids = original["citation"]["block_ids"] or [original["citation"]["block_id"]]
        if (not 1 <= len(ids) <= 24 or len(set(ids)) != len(ids) or not set(ids) <= adjacent or
                set(ids) & set(unresolved) or original["citation"].get("clause_id") or original["citation"].get("table_row_id") or
                any(any(position >= index for position in chapter["requirement_indices"]) for chapter in data["format_chapters"])):
            return None
        owners = [window for window in windows if set(ids) & {block["block_id"] for block in window["blocks"]}]
        if len(owners) != 1:
            return None
        owner = owners[0]
        if (owner["window_id"] == current["window_id"] or owner["source_id"] != current["source_id"] or
                not set(ids) <= {block["block_id"] for block in owner["blocks"]} or owner_witness["full_window"] != owner or
                owner_witness["binding"]["unit_id"] != owner["window_id"]):
            return None
        document, = [document for document in documents if document["source_id"] == current["source_id"]]
        if (document.get("format") != "pdf" or document.get("role") != "tender" or not _hex(document["sha256"], 64) or
                document.get("expected_units", len(document["units"])) != len(document["units"]) or
                any(unit.get("coverage", {}).get("status") != "read" or any(unit.get("coverage", {}).get(key)
                    for key in ("unread_objects", "unmapped_glyphs", "error_code")) for unit in document["units"])):
            return None
        source = _complete_clause(ids, blocks, units, document)
        if source is None:
            return None
        owner_task = {**extraction_window_payload(owner, request["window_config"]), "task_type": "extract_requirements"}
        if not _witness_identity(owner_witness, owner_task):
            return None
        raw_owner = owner_witness["entry"]["candidate"]
        validated = validate_live_window(copy.deepcopy(raw_owner), owner)
        typed_owner, _ = _normalize_source_selectors(copy.deepcopy(raw_owner), owner)
        typed_owner = _LiveWindowResponse.model_validate(typed_owner, strict=True).model_dump(mode="json")
        matches = [(position, item) for position, item in enumerate(typed_owner["requirements"])
                   if (item["citation"]["block_ids"] or [item["citation"]["block_id"]]) == ids]
        if len(matches) != 1:
            return None
        owner_index, owned = matches[0]
        if ({key: value for key, value in original.items() if key != "citation"} !=
                {key: value for key, value in owned.items() if key != "citation"} or
                original["citation"] != owned["citation"] or
                validated["requirements"][owner_index]["citation"]["quote_sha256"] != source["quote_sha256"]):
            return None
        source.update(parsed_sha256=digest(parsed), source_reading_sha256=digest(document),
            current_window_sha256=digest(current), owner_window_sha256=digest(owner))
        origin = owner_witness["entry"]["origin"]
        proof = {"resolution_kind": "source_bound_successful_owner_duplicate_requirement",
            "rule": "one_adjacent_requirement_already_carried_by_successful_owner_v1",
            "candidate_index": index, "original_typed_item": copy.deepcopy(original),
            "original_typed_item_sha256": digest(original),
            "received_requirement_sha256": digest(candidate["requirements"][index]),
            "owner_candidate_index": owner_index, "owner_typed_item_sha256": digest(owned),
            "binding": copy.deepcopy(owner_witness["binding"]), "origin": copy.deepcopy(origin),
            "owner_response_sha256": origin["calls"][-1]["response_sha256"],
            "owner_candidate_sha256": digest(raw_owner), "owner_request_sha256": digest(owner_task),
            "window_id": current["window_id"], "owner_window_id": owner["window_id"], "block_ids": list(ids),
            "source_proof": source, "source_proof_sha256": digest(source),
            "owner_evidence_authentication": "caller_verified_DiagnosticReplay_load_entry_required",
            "candidate_mutation": False, "semantic_review": "requires_source_review"}
        proof["proof_sha256"] = digest(proof)
        return proof
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, StopIteration):
        return None
