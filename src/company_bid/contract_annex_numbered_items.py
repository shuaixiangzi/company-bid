"""Separate proven annex scope and numbered items without widening citation spans."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_annex_list import _ReviewPage, _list_entries, _page_inventory, _proof_member
from .contract_attachment_citation_split import _ID, _SHA
from .storage import digest


SOURCE_CONFIG_KEY = "contract_annex_numbered_items_source"
WINDOW_CONTEXT_KEY = "native_contract_annex_numbered_items_context"
RESOLUTION_KEY = "native_contract_annex_numbered_items_resolution"
SOURCE_ERROR = "CONTRACT_ANNEX_NUMBERED_ITEMS_SOURCE_MISMATCH"
CANDIDATE_ERROR = "CONTRACT_ANNEX_NUMBERED_ITEM_SELECTION_MISMATCH"
RULE = "reviewed_native_contract_annex_scope_and_numbered_item_v1"
_GROUP = re.compile(r"^([1-9]\d*(?:[.．][1-9]\d*)+)(?![\d.．])\s*([^\d.．\s].*)$")
_CHILD = re.compile(r"^(?:（[1-9]\d*）|\([1-9]\d*\))")
_SUBMISSION = re.compile(r"响应文件|(?:投标|报价)文件|投标资格|递交|随附")


class NumberedItemSelectionError(ValueError):
    def __init__(self, **diagnostic_context):
        super().__init__(CANDIDATE_ERROR)
        self.diagnostic_context = diagnostic_context


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    heading_block_ids: list[_ID] = Field(min_length=1, max_length=8)
    reviewed_pages: list[_ReviewPage] = Field(min_length=1, max_length=4)


def normalize_contract_annex_numbered_items_config(value):
    error = "CONTRACT_ANNEX_NUMBERED_ITEMS_CONFIG_INVALID"
    try:
        entries = value if isinstance(value, list) else [value]
        scopes = [_Scope.model_validate(scope).model_dump() for scope in entries]
    except ValidationError:
        raise ValueError(error) from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError(error)
    for scope in scopes:
        pages = [page["physical_page"] for page in scope["reviewed_pages"]]
        if (pages != list(range(pages[0], pages[0] + len(pages))) or
                len(set(scope["heading_block_ids"])) != len(scope["heading_block_ids"])):
            raise ValueError(error)
    return scopes if isinstance(value, list) else scopes[0]


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _member(row):
    return _proof_member(row) | {"bbox": row["bbox"], "kind": row["kind"], "origin": row["origin"]}


def _matches(row, member):
    actual = _proof_member(row) | {"kind": row["kind"], "origin": row["origin"]}
    return not row.get("read_disposition") and actual == {key: value for key, value in member.items() if key != "bbox"}


def _ids(members):
    return [row["block_id"] for row in members]


def _path(match):
    return tuple(map(int, re.split(r"[.．]", match[1])))


def _native_groups(lines, pages, scope, owner_ids, window, following):
    footers = {row["footer"]["block_id"] for row in pages}
    body = [row for row in lines if row["block_id"] not in footers]
    boundaries = [(index, _path(match)) for index, row in enumerate(body) if (match := _GROUP.fullmatch(row["text"]))]
    offsets = [index for index, (offset, _) in enumerate(boundaries) if body[offset]["block_id"] in scope["heading_block_ids"]]
    _require(bool(offsets) and len(offsets) == len(scope["heading_block_ids"]) and offsets[-1] + 1 < len(boundaries) and
             offsets == list(range(offsets[0], offsets[0] + len(offsets))) and
             [body[boundaries[index][0]]["block_id"] for index in offsets] == scope["heading_block_ids"])
    groups = []
    for index in offsets:
        (start, number), (end, next_number) = boundaries[index:index + 2]
        _require(len(number) >= 3 and next_number == (*number[:-1], number[-1] + 1) and
                 sum(path == number for _, path in boundaries) == 1)
        heading = body[start]
        expected_owners = {window["window_id"], following["window_id"]} if following is not None else {window["window_id"]}
        first_item = next(i for i in range(start + 1, end) if _CHILD.match(body[i]["text"]))
        context = body[start:first_item]
        _require(heading["text"].endswith("处罚标准") and "乙方" in heading["text"] and
                 not _SUBMISSION.search("".join(row["text"] for row in context)) and
                 (len(context) == 1 or context[-1]["text"].endswith(("：", ":"))) and
                 all(owner_ids[row["block_id"]] == window["window_id"] for row in context))
        source_range = body[start:end]
        _require(len({row["page"] for row in source_range}) == 1 and
                 all(right["ordinal"] == left["ordinal"] + 1 and left["bbox"][3] <= right["bbox"][1]
                     for left, right in zip(source_range, source_range[1:])))
        entries = _list_entries(body, first_item - 1, end, set(), owner_ids)
        items = []
        for entry in entries:
            members = [body[first_item + offset] for offset in range(len(entry["members"]))]
            first_item += len(members)
            _require(_CHILD.match(members[0]["text"]) and abs(heading["bbox"][0] - members[0]["bbox"][0]) <= .5 and
                     all(row["bbox"][0] < members[0]["bbox"][0] for row in members[1:]) and
                     len(entry["owner_window_ids"]) == 1 and entry["owner_window_ids"][0] in expected_owners)
            items.append({"number": entry["number"], "members": [_member(row) for row in members],
                          "owner_window_id": entry["owner_window_ids"][0], "quote_sha256": hashlib.sha256(entry["quote"].encode()).hexdigest()})
        groups.append({"number": ".".join(map(str, number)), "scope_members": [_member(row) for row in context],
                       "items": items, "following_boundary": _member(body[end])})
    _require(all(group["items"][0]["owner_window_id"] == window["window_id"] for group in groups) and
             all(item["owner_window_id"] == window["window_id"] for group in groups[:-1] for item in group["items"]))
    future = [item for item in groups[-1]["items"] if item["owner_window_id"] != window["window_id"]]
    if future:
        _require(future == groups[-1]["items"][-len(future):] and
                 _ids([row for item in future for row in item["members"]]) ==
                 _ids(following["blocks"][:sum(len(item["members"]) for item in future)]))
    return groups


def bind_contract_annex_numbered_items(windows, documents, blocks, footer_skips, manifest, config):
    """Authenticate original PDF page geometry and complete parent/item boundaries."""
    try:
        source_id = config["source_id"]
        source, = [row for row in (manifest or {}).get("sources", []) if row["source_id"] == source_id]
        document, = [row for row in documents if row["source_id"] == source_id]
        index, = [index for index, row in enumerate(windows) if row["window_id"] == config["window_id"]]
        window = windows[index]
        following = windows[index + 1] if index + 1 < len(windows) and windows[index + 1]["source_id"] == source_id else None
        _require(window["source_id"] == source_id and document["format"] == window["source_format"] == "pdf" and
                 source["sha256"] == window["source_sha256"] == document["sha256"] == config["source_sha256"] and
                 (following is None or (following["source_format"], following["source_sha256"]) == ("pdf", source["sha256"])))
        payload = Path(source["path"]).read_bytes()
        _require(hashlib.sha256(payload).hexdigest() == source["sha256"])
        owners = {row["block_id"]: member["window_id"] for member in windows for row in member["blocks"]}
        _require(len(owners) == sum(len(member["blocks"]) for member in windows))
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            lines, pages = _page_inventory(pdf, payload, config["reviewed_pages"], document, blocks, footer_skips)
        groups = _native_groups(lines, pages, config, owners, window, following)
        shared = {"rule": RULE, "source_id": source_id, "source_sha256": source["sha256"], "scope": config,
                  "reviewed_pages": pages, "groups": groups}
        targets = [("declaring_window", window)]
        if following is not None and any(item["owner_window_id"] == following["window_id"] for item in groups[-1]["items"]):
            targets.append(("following_owner", following))
        for role, target in targets:
            _require(WINDOW_CONTEXT_KEY not in target)
            proof = shared | {"role": role, "window_id": target["window_id"]}
            target[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, StopIteration, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def _selector(members):
    return {key: members[0][key] for key in ("source_id", "block_id", "locator")} | {"block_ids": _ids(members)}


def resolve_contract_annex_numbered_items(raw, normalized, window):
    """Validate each actual model selection against one whole native numbered item."""
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None:
        return None
    role = proof["role"]
    declaring = role == "declaring_window"
    raw_hash = digest(raw)
    _require(proof["rule"] == RULE and role in {"declaring_window", "following_owner"} and
             all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")) and
             proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"}) and
             declaring == (window["window_id"] == proof["scope"]["window_id"]) and
             (not declaring or raw_hash == proof["scope"]["raw_candidate_sha256"]))
    targets = {row["block_id"]: row for row in window["blocks"]}
    adjacent = {row["block_id"]: row for row in window["adjacent_context"]}
    owned, future = [], []
    for group in proof["groups"]:
        context = group["scope_members"]
        if declaring:
            _require(all(_matches(targets[row["block_id"]], row) for row in context))
        for item in group["items"]:
            members = item["members"]
            if item["owner_window_id"] == window["window_id"]:
                _require(all(_matches(targets[row["block_id"]], row) for row in members))
                owned.append((group["number"], item, context))
            elif declaring:
                future.append(item)
    future_members = {row["block_id"]: row for item in future for row in item["members"]}
    protected = {member["block_id"] for _number, item, context in owned for member in [*item["members"], *context]} | set(future_members)
    declared = [key for key in normalized["unresolved_block_ids"] if key in future_members]
    if declaring:
        _require(declared == [key for key in future_members if key in declared] and
                 all(key in adjacent and _matches(adjacent[key], future_members[key]) for key in declared))
    by_selection = {}
    for group, item, context in owned:
        body = tuple(_ids(item["members"]))
        by_selection[tuple(_ids(context)) + body] = (group, item, context)
        if not declaring:
            by_selection[body] = (group, item, context)
    projections, seen = [], set()
    categories = {"commercial"} if declaring else {"commercial", "procedure"}
    for index, candidate in enumerate(normalized["requirements"]):
        original = candidate["citation"]
        ids = tuple(original["block_ids"] or [original["block_id"]])
        if not protected.intersection(ids):
            continue
        selected = by_selection.get(ids)
        if selected is None:
            raise NumberedItemSelectionError(candidate_index=index, block_ids=list(ids))
        group, item, context = selected
        identity = (group, item["number"])
        if (identity in seen or not candidate["mandatory"] or candidate["category"] not in categories or
                candidate["response_mode"] != "procedure" or candidate["format_status"] != "not_prescribed" or
                any(candidate[key] for key in ("format_title", "required_fact_labels", "attachment_keywords", "reference_keywords")) or
                any(original[key] for key in ("quote", "clause_id", "table_row_id"))):
            raise NumberedItemSelectionError(candidate_index=index, block_ids=list(ids))
        first = (context if ids[0] == context[0]["block_id"] else item["members"])[0]
        if any(original[key] != first[key] for key in ("block_id", "source_id", "locator")):
            raise NumberedItemSelectionError(candidate_index=index, block_ids=list(ids))
        seen.add(identity)
        projections.append({"candidate_index": index, "group_number": group, "item_number": item["number"],
                            "original_selector": raw["requirements"][index]["citation"], "context_selector": _selector(context),
                            "context_members": context, "operative_selector": _selector(item["members"]),
                            "operative_quote_sha256": item["quote_sha256"]})
    missing = [item for group, item, _context in owned if (group, item["number"]) not in seen]
    if missing:
        raise NumberedItemSelectionError(field="requirements", block_ids=_ids(missing[0]["members"]))
    for index, chapter in enumerate(normalized["format_chapters"]):
        ids = chapter["citation"]["block_ids"] or [chapter["citation"]["block_id"]]
        if protected.intersection([*ids, *chapter["content_block_ids"]]):
            raise NumberedItemSelectionError(field="format_chapters", candidate_index=index, block_ids=ids)
    return {"rule": RULE, "window_id": window["window_id"], "raw_candidate_sha256": raw_hash, "proof": proof,
            "projections": projections, "following_items": future, "declared_following_block_ids": declared,
            "semantic_review": "requires_source_review", "model_candidate_mutation": False}
