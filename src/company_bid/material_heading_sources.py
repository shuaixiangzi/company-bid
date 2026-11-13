"""Bound empty material headings using complete frozen N1, without model calls.

The repair's old-item validation and immutability checks remain the caller's
responsibility. This helper proves source structure only: it does not resolve
the quoted reference, invent material thresholds, or establish qualification.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .extraction import _index, _windows, extraction_window_payload
from .storage import digest


MATERIAL_HEADING_RESPONSE_FIELDS = {
    "category": "qualification", "response_mode": "factual_response", "mandatory": True,
    "confidence": "low", "format_status": "not_prescribed", "format_title": None,
    "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": [],
}
_PARENT = re.compile(r"([1-9]\d{0,2})[.、．]?[ \t]*资格审查(?:材料|资料)")
_MATERIAL = r"(?:资质|资格|财务|业绩|信誉|人员)(?:要求)?(?:材料|资料)"
_CHILD = re.compile(r"([1-9]\d{0,2})\.([1-9]\d{0,2})[、．.]?[ \t]*(" + _MATERIAL + r")[：:]")
# Other materials may terminate a preceding known category. They can never
# themselves earn a repair response from this finite category recognizer.
_BOUNDARY = re.compile(r"([1-9]\d{0,2})\.([1-9]\d{0,2})[、．.]?[ \t]*(?:" + _MATERIAL + r"|其他(?:材料|资料))[：:]")
_CHAPTER = re.compile(r"第[零一二三四五六七八九十百千万\d]+章[^\r\n]{0,100}")
_FORMAT = re.compile(r"第[零一二三四五六七八九十百千万\d]+章[ \t]*(?:响应|投标)文件(?:要求及)?格式")
_NOTE = re.compile(r"注[：:][ \t]*所有材料按照[“\"]([^”\"\r\n]{1,60})[”\"]中要求提供")
_TOP = re.compile(r"([1-9]\d{0,2})(?:[、．.]|[ \t]+)(?!\d)\S")


def _line(block, units, source_id):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    match = re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", str(block.get("locator", "")))
    if (block.get("source_id") != source_id or block.get("kind") != "paragraph" or
            block.get("origin") != "native" or block.get("read_disposition") or not match or
            type(block.get("page")) is not int or int(match[1]) != block["page"] or
            coverage.get("status") != "read" or coverage.get("unread_objects") or
            coverage.get("unmapped_glyphs") or coverage.get("error_code") or
            not isinstance(block.get("text"), str) or not block["text"].strip() or
            "\n" in block["text"] or "\r" in block["text"]):
        return None
    return int(match[2])


def _adjacent(left, right, units, source_id):
    a, b = _line(left, units, source_id), _line(right, units, source_id)
    unit = units[left["unit_id"]]
    image_count = unit.get("coverage", {}).get("image_count")
    return (a is not None and b is not None and b == a + 1 and
            type(image_count) is int and image_count == 0 and
            not any(block.get("kind") == "image" for block in unit.get("blocks", [])) and
            left["page"] == right["page"] and left["unit_id"] == right["unit_id"] and
            right["ordinal"] == left["ordinal"] + 1 and
            right["unit_ordinal"] == left["unit_ordinal"] + 1)


def _proof_block(block):
    return {**{key: block[key] for key in ("block_id", "source_id", "locator", "page", "ordinal", "unit_id", "unit_ordinal")},
            "text": block["text"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _family(body, parent_index, parent_match, units, source_id):
    """A TOC name is not a parent: its own positive structure must qualify."""
    if parent_index + 1 >= len(body):
        return None
    parent, note = body[parent_index], body[parent_index + 1]
    reference = _NOTE.fullmatch(note["text"])
    if not reference or not _adjacent(parent, note, units, source_id):
        return None
    chapters = [index for index, block in enumerate(body[:parent_index])
                if block["kind"] == "paragraph" and _CHAPTER.fullmatch(block["text"])]
    if not chapters:
        return None
    ancestor = body[chapters[-1]]
    if not _FORMAT.fullmatch(ancestor["text"]) or _line(ancestor, units, source_id) is None:
        return None
    chapter_end = next((index for index in range(parent_index + 1, len(body))
        if body[index]["kind"] == "paragraph" and _CHAPTER.fullmatch(body[index]["text"])), len(body))
    scope_end = next((index for index in range(parent_index + 2, chapter_end)
        if body[index]["kind"] == "paragraph" and _TOP.match(body[index]["text"])), chapter_end)
    prefix = re.compile(re.escape(parent_match[1]) + r"\.(\d+)")
    siblings = []
    for block in body[parent_index + 2:scope_end]:
        if block["kind"] != "paragraph":
            continue
        numbered = re.match(r"([1-9]\d{0,2})\.(\d+)", block["text"])
        if numbered and numbered[1] != parent_match[1]:
            return None
        if prefix.match(block["text"]):
            match = _BOUNDARY.fullmatch(block["text"])
            if not match or match[1] != parent_match[1] or _line(block, units, source_id) is None:
                return None
            siblings.append((block, int(match[2])))
    if len(siblings) < 2 or not _adjacent(note, siblings[0][0], units, source_id):
        return None
    numbers = [number for _, number in siblings]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return None
    return {"parent": parent, "note": note, "ancestor": ancestor, "reference": reference[1], "siblings": siblings}


def material_heading_source_groups(candidate: dict, request: dict, parsed: dict, manifest: dict) -> list | None:
    """Prove every current target-unresolved heading, preserving adjacent IDs.

    The exact production request is rebuilt from the complete N1 source. A
    remote parent is legitimate only inside its original format chapter, with
    one positive common instruction and consecutive same-parent siblings. Only
    an empty category immediately followed by its next sibling is supported.
    """
    try:
        if "task_type" in request and request["task_type"] != "extract_requirements":
            return None
        source_request = {key: value for key, value in request.items() if key != "task_type"}
        blocks, units, documents = _index(parsed, manifest)
        config = request["window_config"]
        windows, _ = _windows(documents, blocks, config)
        matches = [window for window in windows if window["window_id"] == request["window"]["window_id"]]
        if len(matches) != 1 or extraction_window_payload(matches[0], config) != source_request:
            return None
        window = matches[0]
        source_id = window["source_id"]
        document = next(doc for doc in documents if doc["source_id"] == source_id)
        if (document.get("format") != "pdf" or document["sha256"] == "0" * 64 or
                not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or
                ("expected_units" in document and document["expected_units"] != len(document["units"])) or
                any(unit.get("coverage", {}).get("unread_objects") or unit.get("coverage", {}).get("unmapped_glyphs") or
                    unit.get("coverage", {}).get("error_code") for unit in document["units"]) or
                candidate.get("window_id") != window["window_id"] or
                candidate.get("format_status") in {"unread", "conflict"}):
            return None
        available = {block["block_id"]: block for block in [*window["blocks"], *window.get("adjacent_context", [])]}
        targets = {block["block_id"] for block in window["blocks"]}
        unresolved = candidate["unresolved_block_ids"]
        if (not isinstance(unresolved, list) or any(not isinstance(key, str) for key in unresolved) or
                len(set(unresolved)) != len(unresolved) or not set(unresolved) <= set(available)):
            return None
        target_ids = [block["block_id"] for block in window["blocks"] if block["block_id"] in unresolved]
        if not 1 <= len(target_ids) <= 8 or any(_line(blocks[key], units, source_id) is None for key in unresolved):
            return None
        body = [block for block in blocks.values() if block["source_id"] == source_id]
        families = [family for index, block in enumerate(body) if block["kind"] == "paragraph"
                    and (match := _PARENT.fullmatch(block["text"]))
                    and (family := _family(body, index, match, units, source_id)) is not None]
        if len(families) != 1:
            return None
        family = families[0]
        parent, note, ancestor, siblings = (family[key] for key in ("parent", "note", "ancestor", "siblings"))
        numbers = [number for _, number in siblings]
        positions = {block["block_id"]: index for index, (block, _) in enumerate(siblings)}
        for key in target_ids:
            index = positions.get(key)
            if (index is None or index + 1 >= len(siblings) or not _CHILD.fullmatch(blocks[key]["text"]) or
                    not _adjacent(blocks[key], siblings[index + 1][0], units, source_id)):
                return None
        sibling_proofs = [_proof_block(block) for block, _ in siblings]
        base = {"rule": "native_pdf_source_bound_material_heading_v1", "window_id": window["window_id"],
                "source_id": source_id, "source_sha256": document["sha256"],
                "parsed_sha256": digest(parsed), "source_reading_sha256": digest(document),
                "format_ancestor": _proof_block(ancestor), "parent_heading": _proof_block(parent),
                "common_instruction": _proof_block(note), "reference_title": family["reference"],
                "reference_resolution_status": "not_resolved", "semantic_review": "requires_source_review",
                "sibling_numbers": numbers, "sibling_block_ids": [block["block_id"] for block, _ in siblings],
                "sibling_sequence_sha256": digest(sibling_proofs)}
        groups = []
        for key in target_ids:
            next_sibling = siblings[positions[key] + 1][0]
            boundary_ids = list(dict.fromkeys([parent["unit_id"], blocks[key]["unit_id"]]))
            proof = {**copy.deepcopy(base), "target_heading": _proof_block(blocks[key]),
                     "next_sibling": _proof_block(next_sibling),
                     "boundary_units": [{"unit_id": identity, "page": units[identity]["page"],
                        "image_count": units[identity]["coverage"]["image_count"],
                        "coverage_sha256": digest(units[identity]["coverage"])} for identity in boundary_ids]}
            groups.append({"resolution_kind": "source_bound_material_heading", "block_ids": [key],
                "required_target_ids": [key], "basis": "complete_native_N1_format_parent_and_empty_next_sibling",
                "required_response_fields": copy.deepcopy(MATERIAL_HEADING_RESPONSE_FIELDS),
                "source_proof": proof, "source_proof_sha256": digest(proof)})
        return groups
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
