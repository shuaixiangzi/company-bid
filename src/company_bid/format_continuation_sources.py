"""Pure, frozen-source proof for a falsely redeclared letter continuation.

This module never removes a candidate or approves company facts. The caller
must preserve the rejected inventory, obtain a new bounded candidate, and run
the unchanged full validator. N3 still requires every source window to finish.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .extraction import (
    PlanningError, _LiveWindowResponse, _citation, _hash, _heading_level,
    _index, _normalize_source_selectors, _pdf_footer_skips, _windows,
    extraction_window_payload, validate_live_window,
)
from .storage import digest


def fixed_letter_items(body: list, anchors: dict) -> dict:
    """Prove numbered declarations locally without changing source headings.

    Only a prescribed letter with an explicit declaration introducer and a
    consecutive list starting at one can override the broad heading heuristic.
    An unknown heading, number gap, other form or chapter ends that proof.
    """
    numbered = re.compile(r"^(\d{1,3})([.．、])\s*(?!\d)(\S.*)$")
    form_title = re.compile(r"(?:表|函|书|资料|材料|方案|声明|承诺|证明|清单|目录)$")
    declaration = re.compile(r"(?:承诺|声明|保证)\s*[:：]$")
    clause = re.compile(r"^(?:我(?:公司|方|单位)|本(?:公司|单位|项目)|不存在|未(?:被|在|参与|参加|发生|出现|提供|受到|列入)|不(?:存在|属于|在|得|与)|具有|具备|参加|按照|遵守|承诺|保证|声明)")
    addition_note = re.compile(r"^[（(][^。；;\n]*可(?:自行)?(?:补充|增加|添加)[^。；;\n]*[）)]$")
    result = {}
    for position, anchor in enumerate(body):
        chapter = anchors.get(anchor["block_id"])
        if (not chapter or chapter["status"] != "prescribed" or
                not re.search(r"(?:函|承诺书|声明书|保证书)$", chapter["title"].strip())):
            continue
        introducer, previous, first, marker, number = None, None, None, None, 0
        for block in body[position + 1:]:
            key, text = block["block_id"], block.get("text", "").strip()
            level = _heading_level(text)
            if key in anchors or level == 0 or block["kind"] != "paragraph":
                break
            item = numbered.fullmatch(text)
            if item:
                value, item_marker, content = int(item[1]), item[2], item[3]
                if (introducer is None or value != number + 1 or
                        marker is not None and item_marker != marker or
                        level is not None and (previous is None or form_title.search(content) or
                            not (clause.search(content) or addition_note.fullmatch(content)))):
                    break
                first = first or key
                if level is not None:
                    proof = {"rule": "fixed_letter_consecutive_declarations_v1",
                             "anchor_block_id": anchor["block_id"], "introducer_block_id": introducer,
                             "list_start_block_id": first, "previous_item_block_id": previous,
                             "item_number": value, "number_marker": item_marker,
                             "original_heading_level": level,
                             "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}
                    result[key] = {**proof, "proof_sha256": _hash(proof)}
                number, marker, previous = value, item_marker, key
            elif level is not None:
                break
            elif not number and declaration.search(text):
                introducer = key
    return result


_NUMBERED = re.compile(r"^(\d{1,3})([.．、])\s*(?!\d)(\S.*)$")
_FORM_END = re.compile(r"(?:表|函|书|资料|材料|方案|声明|承诺|证明|清单|目录)$")
_LETTER = re.compile(r"(?:函|承诺书|声明书|保证书)$")


def _member(block):
    location = {key: block[key] for key in
                ("block_id", "source_id", "locator", "page", "ordinal", "unit_id", "unit_ordinal")}
    return {**location, "location_sha256": digest(location), "text": block["text"],
            "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def _native(block, units, source_id):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    location = re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", str(block.get("locator", "")))
    return (block.get("source_id") == source_id and block.get("kind") == "paragraph" and
            block.get("origin") == "native" and not block.get("read_disposition") and
            location is not None and type(block.get("page")) is int and int(location[1]) == block["page"] and
            unit.get("locator") == f"pdf/page[{block['page']}]" and
            coverage.get("status") == "read" and type(coverage.get("image_count")) is int and
            coverage["image_count"] == 0 and
            not coverage.get("unread_objects") and not coverage.get("unmapped_glyphs") and
            not coverage.get("error_code") and isinstance(block.get("text"), str) and
            bool(block["text"].strip()) and "\r" not in block["text"] and "\n" not in block["text"] and
            not any(value.get("kind") == "image" for value in unit.get("blocks", [])))


def _consecutive(members, all_blocks, skips):
    """No missing native line/page may be hidden by source ordering."""
    for left, right in zip(members, members[1:]):
        between = [block for block in all_blocks if left["ordinal"] < block["ordinal"] < right["ordinal"]]
        if any(block["block_id"] not in skips for block in between):
            return False
        left_line = int(re.fullmatch(r"pdf/page\[\d+\]/line\[(\d+)\]", left["locator"])[1])
        right_line = int(re.fullmatch(r"pdf/page\[\d+\]/line\[(\d+)\]", right["locator"])[1])
        if left["unit_id"] == right["unit_id"]:
            if right_line != left_line + 1 or right["unit_ordinal"] != left["unit_ordinal"] + 1:
                return False
        elif right["page"] != left["page"] + 1 or right_line != 0 or any(
                block["page"] != left["page"] for block in between):
            return False
    return True


def format_continuation_source_proof(candidate, request, parsed, manifest, format_index) -> dict | None:
    """Prove one duplicate declaration of an earlier native letter's body.

    The actual request is rebuilt, the real title must be unique in full N1,
    and all removed citations/content must survive unchanged in same-title
    prescribed fixed-text requirements. A heading catalog alone proves none
    of these facts. This intentionally supports numbered native letters only.
    """
    try:
        if type(format_index) is not int or request.get("task_type", "extract_requirements") != "extract_requirements":
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
        if (data["window_id"] != window["window_id"] or data["format_status"] != "specified" or
                not 0 <= format_index < len(data["format_chapters"])):
            return None
        # Only this exact deterministic error may disappear. All other items,
        # unresolved IDs and business fields retain the production validator.
        try:
            validate_live_window(copy.deepcopy(candidate), window)
        except PlanningError as error:
            if (error.code != "FORMAT_HEADING_NOT_GROUNDED" or
                    error.diagnostic_context.get("candidate_index") != format_index or
                    error.diagnostic_context.get("candidate_type") != "format_chapter"):
                return None
        else:
            return None
        chapter = data["format_chapters"][format_index]
        title = chapter["title"]
        if chapter["status"] != "prescribed" or not _LETTER.search(title) or title != title.strip():
            return None
        source_id = window["source_id"]
        document = next(doc for doc in documents if doc["source_id"] == source_id)
        if (document.get("format") != "pdf" or not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or
                document["sha256"] == "0" * 64 or
                type(document.get("expected_units", len(document["units"]))) is not int or
                document.get("expected_units", len(document["units"])) != len(document["units"]) or
                any(unit.get("coverage", {}).get("unread_objects") or
                    unit.get("coverage", {}).get("unmapped_glyphs") or
                    unit.get("coverage", {}).get("error_code") for unit in document["units"])):
            return None
        all_blocks = sorted((block for block in blocks.values() if block["source_id"] == source_id),
                            key=lambda block: block["ordinal"])
        skips = _pdf_footer_skips(blocks)
        body = [block for block in all_blocks if block["block_id"] not in skips]
        titles = [index for index, block in enumerate(body) if block.get("text") == title]
        if len(titles) != 1:
            return None
        start = titles[0]
        anchor = body[start]
        if not _native(anchor, units, source_id) or _heading_level(title) is None:
            return None
        ancestors = [block for block in body[:start] if _heading_level(block.get("text", "")) == 0]
        if not ancestors or not re.search(r"(?:投标|响应|报价).{0,18}(?:格式|组成)", ancestors[-1]["text"]):
            return None
        ancestor = ancestors[-1]
        if not _native(ancestor, units, source_id):
            return None
        owners = [item for item in windows if anchor["block_id"] in {block["block_id"] for block in item["blocks"]}]
        if len(owners) != 1 or windows.index(owners[0]) >= windows.index(window):
            return None
        owner = owners[0]
        declarations = fixed_letter_items(body, {anchor["block_id"]: {"title": title, "status": "prescribed"}})
        end = len(body)
        for index in range(start + 1, len(body)):
            block = body[index]
            if block["kind"] != "paragraph":
                return None  # Neither a table nor an image is a known letter boundary.
            level = _heading_level(block["text"])
            if level is not None and block["block_id"] not in declarations:
                if level != 0 and not _FORM_END.search(block["text"].strip()):
                    return None  # Unknown heading or broken declaration sequence.
                end = index
                break
        members = body[start:end]
        boundary = body[end] if end < len(body) else None
        if (not 3 <= len(members) <= 96 or any(not _native(block, units, source_id) for block in members) or
                boundary is not None and not _native(boundary, units, source_id) or
                not _consecutive([*members, *([boundary] if boundary else [])], all_blocks, skips)):
            return None
        numbered = [(block, _NUMBERED.fullmatch(block["text"].strip())) for block in members[1:]]
        numbered = [(block, match) for block, match in numbered if match]
        if (len(numbered) < 2 or [int(match[1]) for _, match in numbered] != list(range(1, len(numbered) + 1)) or
                len({match[2] for _, match in numbered}) != 1 or
                not any(re.search(r"(?:承诺|声明|保证)\s*[:：]$", block["text"].strip())
                        for block in members[1:members.index(numbered[0][0])])):
            return None
        introducer = next(block for block in reversed(members[1:members.index(numbered[0][0])])
                          if re.search(r"(?:承诺|声明|保证)\s*[:：]$", block["text"].strip()))
        targets = {block["block_id"] for block in window["blocks"]}
        available = {block["block_id"]: block for block in [*window["blocks"], *window.get("adjacent_context", [])]}
        citation = _citation(chapter["citation"], available, skips)
        selected = citation["block_ids"] or [citation["block_id"]]
        content = chapter["content_block_ids"]
        continuation = [block["block_id"] for block in members[1:]]
        removed = list(dict.fromkeys([*selected, *content]))
        if (chapter["citation"].get("clause_id") or chapter["citation"].get("table_row_id") or
                not content or len(set(content)) != len(content) or not set(removed) <= targets or
                not set(removed) <= set(continuation) or anchor["block_id"] in removed or
                content != sorted(content, key=lambda key: blocks[key]["ordinal"])):
            return None
        linked = chapter["requirement_indices"]
        if (not linked or len(set(linked)) != len(linked) or
                any(type(index) is not int or not 0 <= index < len(data["requirements"]) for index in linked)):
            return None
        retained, covered, links = [], set(), []
        for index, requirement in enumerate(data["requirements"]):
            if (requirement["format_title"] != title or requirement["format_status"] != "prescribed" or
                    requirement["response_mode"] != "fixed_text"):
                if index in linked:
                    return None
                continue
            req_citation = _citation(requirement["citation"], available, skips)
            req_ids = req_citation["block_ids"] or [req_citation["block_id"]]
            if not set(req_ids) <= set(continuation):
                return None
            retained.append(index)
            covered.update(req_ids)
            links.append({"requirement_index": index, "requirement_sha256": digest(requirement),
                          "citation_block_ids_sha256": digest(req_ids)})
        if not set(linked) <= set(retained) or not set(removed) <= covered:
            return None
        hypothetical = copy.deepcopy(candidate)
        del hypothetical["format_chapters"][format_index]
        expected_status = "specified" if hypothetical["format_chapters"] else "none_verified"
        hypothetical["format_status"] = expected_status
        validate_live_window(hypothetical, window)
        # The immutable N1 already contains the original text. Hash every
        # original word/location here without duplicating 96 full paragraphs
        # in the paid feedback; title/ancestor/boundary stay readable below.
        member_proofs = [{key: value for key, value in _member(block).items() if key in
                         {"block_id", "locator", "ordinal", "text_sha256"}}
                         for block in members]
        proof = {"rule": "native_pdf_source_bound_format_continuation_v1",
            "resolution_kind": "source_bound_format_continuation", "format_index": format_index,
            "format_sha256": digest(chapter), "received_format_sha256": digest(candidate["format_chapters"][format_index]),
            "expected_format_status": expected_status, "retained_requirement_indices": retained,
            "original_requirement_indices": linked, "retained_links": links, "title": title,
            "window_id": window["window_id"], "source_id": source_id, "source_sha256": document["sha256"],
            "parsed_sha256": digest(parsed), "source_reading_sha256": digest(document),
            "canonical_anchor": _member(anchor), "format_ancestor": _member(ancestor),
            "declaration_introducer": _member(introducer),
            "owner_window_id": owner["window_id"], "owner_processing_status": "not_asserted_by_source_proof",
            "full_format_members": member_proofs, "full_format_members_sha256": digest(member_proofs),
            "full_format_locations_sha256": digest([_member(block)["location_sha256"] for block in members]),
            "continuation_block_ids_sha256": digest(continuation),
            "removed_format_citation_block_ids_sha256": digest(selected),
            "removed_format_content_block_ids_sha256": digest(content),
            "target_block_ids": [block["block_id"] for block in window["blocks"] if block["block_id"] in removed],
            "numbered_items": [{"block_id": block["block_id"], "item_number": int(match[1]),
                "number_marker": match[2], **({"continuation_basis_sha256":
                    declarations[block["block_id"]]["proof_sha256"]} if block["block_id"] in declarations else {})}
                    for block, match in numbered],
            "numbered_declaration_proofs_sha256": digest(declarations),
            "boundary": _member(boundary) if boundary else None,
            "boundary_basis": "next_source_heading" if boundary else "source_end",
            "skipped_footer_block_ids": [block["block_id"] for block in all_blocks
                if members[0]["ordinal"] < block["ordinal"] < (boundary["ordinal"] if boundary else members[-1]["ordinal"] + 1)
                and block["block_id"] in skips],
            "semantic_review": "requires_source_review", "candidate_mutation": False}
        proof["proof_sha256"] = digest(proof)
        return proof
    except (PlanningError, KeyError, IndexError, TypeError, ValueError, AttributeError, StopIteration):
        return None
