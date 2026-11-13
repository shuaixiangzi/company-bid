"""Recognize bounded native contract-template blanks, never missing reading.

Only finite, wholly empty clause forms are accepted. Source words and empty
slots remain immutable; this proves neither a value nor contractual acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .extraction import _index, _windows, extraction_window_payload
from .storage import digest


CONTRACT_BLANK_FIELDS = {
    "category": "commercial", "response_mode": "factual_response", "mandatory": True,
    "confidence": "low", "format_status": "not_prescribed", "format_title": None,
    "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": [],
}
_CHAPTER = re.compile(r"第[零一二三四五六七八九十百\d]+章[^\r\n]*")
_CONTRACT = re.compile(r"第[零一二三四五六七八九十百\d]+章[ \t]*合同(?:条款|格式|条款及格式)")
_ARTICLE = re.compile(r"第([一二三四五六七八九十]+|[1-9]\d?)条[ \t]*\S[^\r\n]*")
_CLAUSE = re.compile(r"([1-9]\d?)\.([1-9]\d?)[ \t]*(.*)")
_EMPTY = r"[ \t_＿]*"
_PERIOD = re.compile(_EMPTY + r"[。．.]" + _EMPTY)


def _line(block, units):
    unit = units[block["unit_id"]]
    coverage = unit.get("coverage", {})
    match = re.fullmatch(r"pdf/page\[(\d+)\]/line\[(\d+)\]", block.get("locator", ""))
    if (not match or block.get("kind") != "paragraph" or block.get("origin") != "native" or
            block.get("read_disposition") or coverage.get("status") != "read" or
            coverage.get("unread_objects") or coverage.get("unmapped_glyphs") or coverage.get("error_code") or
            type(coverage.get("image_count")) is not int or coverage["image_count"] != 0 or
            any(item.get("kind") == "image" for item in unit["blocks"]) or
            unit.get("locator") != f"pdf/page[{block['page']}]" or
            int(match[1]) != block["page"] or unit["page"] != block["page"] or
            "\n" in block["text"] or "\r" in block["text"]):
        return None
    return int(match[2])


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
    return None


def _member(block):
    return {**{key: block[key] for key in ("block_id", "source_id", "locator", "page", "ordinal", "unit_id", "unit_ordinal")},
            "text": block["text"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}


def contract_blank_catalog(parsed, manifest):
    """Source-only catalog. Unrecognized or incomplete spans earn no exemption."""
    blocks, units, documents = _index(parsed, manifest)
    groups = []
    for document in documents:
        if (document.get("format") != "pdf" or document.get("role") != "tender" or
                not re.fullmatch(r"[a-f0-9]{64}", document["sha256"]) or document["sha256"] == "0" * 64 or
                document.get("expected_units", len(document["units"])) != len(document["units"]) or
                any(u.get("coverage", {}).get("status") != "read" or
                    any(u.get("coverage", {}).get(k) for k in ("unread_objects", "unmapped_glyphs", "error_code"))
                    for u in document["units"])):
            continue
        body = [b for b in blocks.values() if b["source_id"] == document["source_id"]]
        ancestor = article = None
        for index, block in enumerate(body):
            text = block["text"]
            if _CHAPTER.fullmatch(text):
                ancestor, article = block, None
            if _ARTICLE.fullmatch(text):
                article = block
            clause = _CLAUSE.fullmatch(text)
            if (not clause or ancestor is None or not _CONTRACT.fullmatch(ancestor["text"]) or article is None or
                    _number(_ARTICLE.fullmatch(article["text"])[1]) != int(clause[1])):
                continue
            length, fields, kind = 0, [], None
            if re.fullmatch(r"其他[：:]" + _EMPTY, clause[3]):
                length, fields, kind = 1, ["其他条款"], "other_terms"
            elif re.fullmatch(r"乙方(?:应当|应)按照下列进度要求向甲方提供技术服务[：:]", clause[3]):
                if (index + 2 < len(body) and re.fullmatch(
                        r"本技术服务项目的履行期限为[：:]" + _EMPTY + r"至" + _EMPTY +
                        r"[。．.]具体进度要求为[：:]" + _EMPTY, body[index + 1]["text"]) and
                        _PERIOD.fullmatch(body[index + 2]["text"])):
                    length, fields, kind = 3, ["履行期限起止日期", "具体进度要求"], "schedule"
            elif re.fullmatch(r"乙方(?:将|应当|应)按照下列质量要求向甲方提供技术服务[：:]", clause[3]):
                if index + 1 < len(body) and _PERIOD.fullmatch(body[index + 1]["text"]):
                    length, fields, kind = 2, ["质量要求"], "quality"
            if not length or index + length >= len(body):
                continue
            selected = body[index:index + length]
            boundary = body[index + length]
            sibling, next_article = _CLAUSE.fullmatch(boundary["text"]), _ARTICLE.fullmatch(boundary["text"])
            if not ((sibling and sibling[1] == clause[1] and int(sibling[2]) == int(clause[2]) + 1) or
                    (next_article and _number(next_article[1]) == int(clause[1]) + 1)):
                continue
            span = [*selected, boundary]
            if (any(_line(b, units) is None for b in [ancestor, article, *span]) or
                    any(b["unit_id"] != span[0]["unit_id"] for b in span) or
                    any(_line(right, units) != _line(left, units) + 1 or
                        right["ordinal"] != left["ordinal"] + 1 or right["unit_ordinal"] != left["unit_ordinal"] + 1
                        for left, right in zip(span, span[1:]))):
                continue
            proof = {"rule": "native_contract_template_blank_v1", "source_id": document["source_id"],
                "source_sha256": document["sha256"], "parsed_sha256": digest(parsed),
                "source_reading_sha256": digest(document), "contract_ancestor": _member(ancestor),
                "article_heading": _member(article), "members": [_member(b) for b in selected],
                "next_boundary": _member(boundary), "coverage_sha256": digest(units[block["unit_id"]]["coverage"]),
                "blank_kind": kind, "clause_number": clause[1] + "." + clause[2], "pending_fields": fields,
                "reading_status": "read", "value_status": "blank_in_source", "response_status": "pending_manual",
                "acceptance_status": "not_determined", "values_inferred": False}
            groups.append({"resolution_kind": "source_bound_contract_blank", "block_ids": [b["block_id"] for b in selected],
                "required_response_fields": copy.deepcopy(CONTRACT_BLANK_FIELDS), "source_proof": proof,
                "source_proof_sha256": digest(proof)})
    return groups


def contract_blank_source_groups(candidate, request, parsed, manifest):
    try:
        if request.get("task_type", "extract_requirements") != "extract_requirements":
            return None
        blocks, _, documents = _index(parsed, manifest)
        windows, _ = _windows(documents, blocks, request["window_config"])
        window, = [w for w in windows if w["window_id"] == request["window"]["window_id"]]
        if extraction_window_payload(window, request["window_config"]) != {k: v for k, v in request.items() if k != "task_type"}:
            return None
        unresolved = candidate["unresolved_block_ids"]
        targets = {b["block_id"] for b in window["blocks"]}
        available = targets | {b["block_id"] for b in window.get("adjacent_context", [])}
        if (candidate["window_id"] != window["window_id"] or candidate.get("format_status") in {"unread", "conflict"} or
                len(set(unresolved)) != len(unresolved) or not set(unresolved) <= available):
            return None
        groups = [g for g in contract_blank_catalog(parsed, manifest)
                  if set(g["block_ids"]) & set(unresolved) & targets and set(g["block_ids"]) <= targets]
        if (not 1 <= len(groups) <= 8 or
                set().union(*(set(g["block_ids"]) for g in groups)) & set(unresolved) != set(unresolved) & targets):
            return None
        for group in groups:
            group["required_target_ids"] = [key for key in group["block_ids"] if key in unresolved]
            group["basis"] = "complete_native_contract_clause_with_empty_slots_and_next_boundary"
        return groups
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def contract_blank_plan_items(requirements, parsed, manifest):
    """Recheck N1 independently; never adopt a model's assertion of emptiness."""
    from .extraction import PlanningError

    results = {}
    for group in contract_blank_catalog(parsed, manifest):
        ids = group["block_ids"]
        for item in requirements:
            citations = item.get("citations", [])
            selected = [c.get("block_ids") or [c.get("block_id")] for c in citations]
            if not any(set(keys) & set(ids) for keys in selected):
                continue
            expected_text = "\n".join(member["text"] for member in group["source_proof"]["members"])
            if (len(selected) != 1 or selected[0] != ids or item["text"] != expected_text or
                    item.get("category") != "commercial" or item.get("response_mode") != "factual_response" or
                    item.get("format_status") != "not_prescribed" or item.get("format_title") or
                    any(item.get(key) for key in ("required_fact_labels", "attachment_keywords", "reference_keywords"))):
                raise PlanningError("CONTRACT_BLANK_RESPONSE_UNRESOLVED")
            results[item["requirement_id"]] = copy.deepcopy(group)
    return results
