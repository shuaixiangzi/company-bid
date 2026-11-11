"""Conservative source-derived clauses; raw reader blocks are never changed.

The caller must supply a frozen, source-hash-verified N1 window. This module
checks internal source consistency and exact spans; it does not authenticate a
self-reported document hash against original file bytes. No model text/offsets
or pre-existing catalog is used to construct the catalog.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


CLAUSE_CATALOG_VERSION = "source-clauses-v1"
MAX_PARENT_BLOCKS = 24
MAX_ITEMS = 30
_HEX = re.compile(r"^[0-9a-f]{64}$")
_LINE = re.compile(r"^pdf/page\[(\d+)\]/line\[(\d+)\]$")
_MARKER = re.compile(r"(?<!\d)([1-9]\d{0,2})([）)])")
_LABEL = re.compile(r"^([1-9]\d{0,2})、([^：:\n]{2,24})[：:]$")
_OUTER = re.compile(r"^[一二三四五六七八九十百]+、\s*([^。；;：:\n]{2,40})[：:]?$")
_SCOPE_TITLE = re.compile(r"(?:主要|具体)?(?:采购|服务|工作|项目|巡检|检测|技术|实施|作业)"
                          r"(?:内容|范围|任务|要求)(?:及(?:相关)?(?:要求|内容|范围|任务))?")
_BAD_LABEL = re.compile(
    r"应|须|必须|不得|不能|未|仅|只有|除|但|否则|若|如|前述|上述|共同|统一|各项|全部|所有|"
    r"条件|补充|特别|约定|说明|备注|示例|举例|根据|按照|投标人|响应人|采购人|\d+(?:\.\d+)?\s*(?:元|万元|天|日|月|年|%)")
_DEPENDENCY = re.compile(
    r"前述|上述|前款|前条|该款|该项|前项|前一项|前一编号|第一项|第二项|第三项|第四项|第五项|"
    r"全部|所有|每项|每个|每一|各|均|以上|以下|本列表|共同|统一|公共|另有约定|例如|比如|示例|举例|"
    r"参见|引用|见\s*[1-9]\d{0,2}[）)]")
_PAGE_NUMBER = re.compile(r"^\s*[-－—]?\s*\d{1,4}\s*[-－—]?\s*$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


class SourceClauseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SourceClause(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    clause_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    block_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    block_ids: list[str] = Field(min_length=1, max_length=MAX_PARENT_BLOCKS)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_proof: dict

    @model_validator(mode="after")
    def consistent(self):
        if (self.block_id != self.block_ids[0] or len(set(self.block_ids)) != len(self.block_ids) or
                self.start >= self.end or self.end - self.start != len(self.quote) or
                self.quote_sha256 != _sha(self.quote) or self.proof_sha256 != _digest(self.boundary_proof)):
            raise ValueError("Source clause internal integrity mismatch.")
        return self


def _native(block: dict) -> bool:
    match = _LINE.fullmatch(block.get("locator", ""))
    return bool(block.get("kind") == "paragraph" and block.get("origin") == "native" and match and
                type(block.get("page")) is int and block["page"] == int(match[1]) and
                type(block.get("ordinal")) is int and isinstance(block.get("unit_id"), str) and block["unit_id"] and
                isinstance(block.get("text"), str) and "\n" not in block["text"] and "\r" not in block["text"])


def _adjacent(left: dict, right: dict) -> bool:
    a, b = _LINE.fullmatch(left["locator"]), _LINE.fullmatch(right["locator"])
    return bool(a and b and left["source_id"] == right["source_id"] and left["unit_id"] == right["unit_id"] and
                left["page"] == right["page"] and right["ordinal"] == left["ordinal"] + 1 and int(b[2]) == int(a[2]) + 1)


def _label(block: dict):
    match = _LABEL.fullmatch(block["text"].strip())
    return match if match and not _BAD_LABEL.search(match[2]) else None


def _outer(block: dict) -> bool:
    match = _OUTER.fullmatch(block["text"].strip())
    return bool(match and _SCOPE_TITLE.fullmatch(match[1].strip()) and not _BAD_LABEL.search(match[1]))


def _scope_label(block: dict):
    match = _label(block)
    return match if match and _SCOPE_TITLE.fullmatch(match[2].strip()) else None


def _identity(block: dict) -> dict:
    return {"block_id": block["block_id"], "locator": block["locator"], "text_sha256": _sha(block["text"])}


def _not_nested(text: str, matches: list) -> bool:
    markers = {m.start(): m.end() for m in matches}
    pairs = {"（": "）", "(": ")", "“": "”", "‘": "’", "《": "》", "【": "】", "[": "]", "{": "}",
             "〔": "〕", "〈": "〉", "「": "」", "『": "』", "〖": "〗", "［": "］", "｛": "｝"}
    stack, index = [], 0
    while index < len(text):
        if index in markers:
            if stack:
                return False
            index = markers[index]
            continue
        char = text[index]
        if char in {'"', "'"}:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return False
        elif unicodedata.category(char) in {"Ps", "Pe", "Pi", "Pf"}:
            return False
        index += 1
    return not stack


def _local_spans(body: list[dict], ranges: list[tuple[int, int]], start: int, end: int):
    parts = []
    for block, (a, b) in zip(body, ranges):
        if start < b and end > a:
            parts.append({"block_id": block["block_id"], "locator": block["locator"],
                          "start": max(start, a) - a, "end": min(end, b) - a,
                          "block_text_sha256": _sha(block["text"])})
    return parts


def _list_catalog(outer: dict, parent: dict, body: list[dict], following: dict, source_sha: str, targets: set[str]) -> list:
    parent_label, next_label = _scope_label(parent), _label(following)
    if (not body or len(body) > MAX_PARENT_BLOCKS or not parent_label or not next_label or
            int(next_label[1]) != int(parent_label[1]) + 1 or any(_PAGE_NUMBER.fullmatch(b["text"]) for b in body)):
        return []
    text = "\n".join(b["text"] for b in body)
    matches = list(_MARKER.finditer(text))
    if (not 2 <= len(matches) <= MAX_ITEMS or matches[0].start() != 0 or
            [int(m[1]) for m in matches] != list(range(1, len(matches) + 1)) or
            len({m[2] for m in matches}) != 1 or _DEPENDENCY.search(text)):
        return []
    # A consecutive marker alone does not authorize a cut inside a reference,
    # example, value or parenthetical expression. Require an explicit item
    # separator before every subsequent marker.
    if any(text[:m.start()].rstrip()[-1:] not in {"；", ";", "。"} for m in matches[1:]):
        return []
    if not _not_nested(text, matches):
        return []
    starts = []
    cursor = 0
    for block in body:
        starts.append((cursor, cursor + len(block["text"])))
        cursor += len(block["text"]) + 1
    # A final extra sentence/unnumbered paragraph might be a common condition.
    # V1 cannot safely assign it to one item, even if it is an exact substring.
    last = text[matches[-1].start():]
    if not re.fullmatch(r"[^。！？!?]*[。！？!?]", last):
        return []
    # Numbered markers may close their own list syntax, but may not be nested
    # inside an additional opening parenthesis or an unfinished quote.
    for marker in matches:
        prefix = text[:marker.start()].rstrip()
        if prefix.endswith(("（", "(", "“", '"', "‘", "《")):
            return []
    marker_refs = []
    for m in matches:
        parts = _local_spans(body, starts, m.start(), m.end())
        if len(parts) != 1:
            return []
        marker_refs.append({"number": int(m[1]), **parts[0], "marker_sha256": _sha(m[0])})
    common = {"schema_version": "source-clause-proof-v1", "rule": "complete_native_parenthetical_list_v1",
              "source_id": parent["source_id"], "source_sha256": source_sha,
              "unit_id": body[0]["unit_id"], "page": body[0]["page"], "marker_style": matches[0][2],
              "outer_heading": _identity(outer), "parent_heading": _identity(parent),
              "termination_heading": _identity(following), "item_count": len(matches), "markers": marker_refs,
              "join_basis": "frozen_native_blocks_lf_v1"}
    list_id = "CLIST-" + _digest(common)[:24]
    result = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # An LF inserted between raw blocks is not an extra character in the
        # preceding item when the next marker starts its own block.
        if end > start and any(end == a for a, _ in starts[1:]):
            end -= 1
        parts = _local_spans(body, starts, start, end)
        if not parts or not any(p["block_id"] in targets for p in parts):
            continue
        identities = [part["block_id"] for part in parts]
        parents = [b for b in body if b["block_id"] in identities]
        parent_quote = "\n".join(b["text"] for b in parents)
        local_start = parts[0]["start"]
        local_end = local_start + end - start
        quote = parent_quote[local_start:local_end]
        if quote != text[start:end]:
            return []
        proof = {**common, "list_id": list_id, "item_number": index + 1, "span_parts": parts}
        identity = {"source_sha256": source_sha, "source_id": parent["source_id"], "span_parts": parts,
                    "quote_sha256": _sha(quote), "proof_sha256": _digest(proof)}
        result.append(SourceClause(clause_id="CLAUSE-" + _digest(identity)[:24], source_id=parent["source_id"],
            source_sha256=source_sha, block_id=identities[0], locator=parts[0]["locator"], block_ids=identities,
            start=local_start, end=local_end, quote=quote, quote_sha256=_sha(quote),
            parent_quote_sha256=_sha(parent_quote), proof_sha256=_digest(proof), boundary_proof=proof).model_dump(mode="json"))
    return result


def build_clause_catalog(window: dict) -> list[dict]:
    """Build only complete same-page native lists available in this window.

    Unsupported/ambiguous source structure returns no catalog. Malformed frozen
    source identities raise a bounded code. Existing catalog fields are ignored.
    """
    if window.get("source_format") != "pdf":
        return []
    source_id, source_sha = window.get("source_id"), window.get("source_sha256")
    if not isinstance(source_id, str) or not source_id or not isinstance(source_sha, str) or not _HEX.fullmatch(source_sha):
        raise SourceClauseError("SOURCE_CLAUSE_WINDOW_IDENTITY_INVALID")
    target_blocks = window.get("blocks", [])
    supplied = [*target_blocks, *window.get("adjacent_context", [])]
    by_id = {}
    for block in supplied:
        identity = block.get("block_id")
        if not isinstance(identity, str) or not identity or identity in by_id or block.get("source_id") != source_id:
            raise SourceClauseError("SOURCE_CLAUSE_BLOCK_IDENTITY_INVALID")
        if block.get("source_sha256", source_sha) != source_sha:
            raise SourceClauseError("SOURCE_CLAUSE_SOURCE_HASH_MISMATCH")
        if "text_sha256" in block and block["text_sha256"] != _sha(block.get("text", "")):
            raise SourceClauseError("SOURCE_CLAUSE_BLOCK_HASH_MISMATCH")
        by_id[identity] = block
    targets = {b["block_id"] for b in target_blocks}
    # Ordinals come from the frozen reader view, not from lexical ID sorting.
    if any(type(b.get("ordinal")) is not int for b in supplied):
        return []
    ordered = sorted(supplied, key=lambda b: b["ordinal"])
    if len({b["ordinal"] for b in ordered}) != len(ordered):
        raise SourceClauseError("SOURCE_CLAUSE_ORDINAL_INVALID")
    result = []
    for index in range(2, len(ordered)):
        start = ordered[index]
        if not _native(start) or not re.match(r"^1[）)]", start["text"]):
            continue
        outer, parent = ordered[index - 2:index]
        if (not all(_native(b) for b in (outer, parent)) or not _adjacent(outer, parent) or
                not _adjacent(parent, start) or not _outer(outer) or not _scope_label(parent)):
            continue
        body, previous = [], parent
        for block in ordered[index:index + MAX_PARENT_BLOCKS + 1]:
            if not _native(block) or not _adjacent(previous, block):
                break
            if _label(block):
                result.extend(_list_catalog(outer, parent, body, block, source_sha, targets))
                break
            # Another structural label cannot be crossed as ordinary body.
            if _OUTER.fullmatch(block["text"].strip()) or _LABEL.fullmatch(block["text"].strip()):
                break
            body.append(block)
            previous = block
    if len(result) > MAX_ITEMS:
        return []
    return result


def validate_clause(window: dict, claimed: dict) -> dict:
    """Reject invented IDs, source/offset changes and self-reported proof data."""
    try:
        normalized = SourceClause.model_validate(claimed).model_dump(mode="json")
    except (ValueError, TypeError):
        raise SourceClauseError("SOURCE_CLAUSE_CLAIM_INVALID") from None
    actual = next((item for item in build_clause_catalog(window) if item["clause_id"] == normalized["clause_id"]), None)
    if actual is None or actual != normalized:
        raise SourceClauseError("SOURCE_CLAUSE_RECONSTRUCTION_MISMATCH")
    return actual
