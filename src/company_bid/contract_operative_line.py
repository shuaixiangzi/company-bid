"""Authenticate complete, short operative clauses which resemble numbered headings."""

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
from .pdf_text_lines import pdf_page_text_lines
from .storage import digest


SOURCE_CONFIG_KEY = "contract_operative_line_source"
WINDOW_CONTEXT_KEY = "native_contract_operative_line_context"
RESOLUTION_KEY = "native_contract_operative_line_resolution"
SOURCE_ERROR = "CONTRACT_OPERATIVE_LINE_SOURCE_MISMATCH"
RULE = "reviewed_native_complete_numbered_operative_line_v1"
_NUMBERED = re.compile(r"^([1-9]\d*(?:\.[1-9]\d*)+)\s*(?=[^\s\d.])(.+)$")
_IMPERATIVE = re.compile(r"(?:严格|认真|切实)?(?:执行|遵守|落实|履行)[^；。！？;!?，,：:\n]{2,55}(?:要求|规定|制度|规程|标准|义务|措施)")
_CLOSE = ("。", "！", "？", "!", "?")


class _RoleProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_index: int = Field(ge=0)
    clause_start_block_id: _ID
    annex_heading_block_id: _ID
    governing_heading_block_id: _ID
    expected_fact_labels: list[_ID] = Field(min_length=1)
    ancestor_pages: list[_ReviewPage] = Field(min_length=1)


class _LabelWrap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_index: int = Field(ge=0)
    label_index: int = Field(ge=0)
    label: _ID
    paragraph_start_block_id: _ID


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    candidate_index: int = Field(ge=0)
    operative_block_id: _ID
    reviewed_pages: list[_ReviewPage] = Field(min_length=1)
    role_projection: _RoleProjection | None = None
    label_wrap: _LabelWrap | None = None


def normalize_contract_operative_line_config(value):
    error = "CONTRACT_OPERATIVE_LINE_CONFIG_INVALID"
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
        if role := scope.get("role_projection"):
            ancestors = [review["physical_page"] for review in role["ancestor_pages"]]
            if ancestors != sorted(set(ancestors)) or ancestors[-1] >= pages[0]:
                raise ValueError(error)
    return scopes if isinstance(value, list) else scopes[0]


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _operative_position(lines, pages, config, blocks, window):
    footer_ids = {row["footer"]["block_id"] for row in pages}
    body = [row for row in lines if row["block_id"] not in footer_ids]
    position, = [i for i, row in enumerate(body) if row["block_id"] == config["operative_block_id"]]
    _require(1 <= position < len(body) - 2)
    current, next_start = body[position:position + 2]
    numbers = [(i, match) for i, row in enumerate(body) if (match := _NUMBERED.fullmatch(row["text"]))]
    at, = [i for i, (offset, _) in enumerate(numbers) if offset == position]
    _require(1 <= at < len(numbers) - 2)
    (previous, before_number), (_, number), (after, after_number), (next_stop, _) = numbers[at - 1:at + 3]
    key = [int(part) for part in number[1].split(".")]
    _require(after == position + 1 and _IMPERATIVE.fullmatch(number[2].rstrip("。！？!?")) and
             [int(part) for part in before_number[1].split(".")] == [*key[:-1], key[-1] - 1] and
             [int(part) for part in after_number[1].split(".")] == [*key[:-1], key[-1] + 1] and
             body[position - 1]["text"].endswith(_CLOSE) and body[next_stop - 1]["text"].endswith(_CLOSE))
    annexes = [row for row in blocks.values() if row["source_id"] == window["source_id"] and
               row["ordinal"] < body[previous]["ordinal"] and _ANNEX.fullmatch(row["text"])]
    _require(bool(annexes))
    annex = max(annexes, key=lambda row: row["ordinal"])
    targets = {row["block_id"] for row in window["blocks"]}
    _require(all(row["block_id"] in targets for row in body[previous:next_stop]) and
             next_start["page"] == current["page"] and current["ordinal"] + 1 == next_start["ordinal"] and
             current["bbox"][0] == next_start["bbox"][0] and current["bbox"][3] <= next_start["bbox"][1])
    intervening = [row["footer"] for row in pages if body[previous]["ordinal"] <
                   blocks[row["footer"]["block_id"]]["ordinal"] < body[next_stop - 1]["ordinal"]]
    return {"annex_heading": _proof_member(annex), "operative_number": number[1], "operative_line": _proof_member(current),
            "preceding_clause_members": [_proof_member(row) for row in body[previous:position]],
            "following_clause_members": [_proof_member(row) for row in body[after:next_stop]],
            "preceding_number": before_number[1], "following_number": after_number[1], "page_footers": intervening}


def _complete_paragraph(lines, identity):
    index, = [i for i, row in enumerate(lines) if row["block_id"] == identity]
    _require(_NUMBERED.fullmatch(lines[index]["text"]))
    end = next(i for i, row in enumerate(lines[index + 1:], index + 1) if _NUMBERED.fullmatch(row["text"]))
    _require(lines[end - 1]["text"].endswith(_CLOSE))
    return lines[index:end], lines[end]


def _reviewed_projections(pdf, payload, lines, pages, config, document, blocks, footer_skips, window):
    proof = {}
    footer_ids = {row["footer"]["block_id"] for row in pages}
    body = [row for row in lines if row["block_id"] not in footer_ids]
    if role := config.get("role_projection"):
        from .contract_operative_line_scope import reviewed_hse_performance_role

        ancestors, reviewed = {}, []
        for page in role["ancestor_pages"]:
            values, evidence = _page_inventory(pdf, payload, [page], document, blocks, footer_skips)
            ancestors.update({row["block_id"]: row for row in values})
            reviewed.extend(evidence)
        members, following = _complete_paragraph(body, role["clause_start_block_id"])
        role_proof = reviewed_hse_performance_role(window, members, annex_heading=ancestors[role["annex_heading_block_id"]],
            governing_heading=ancestors[role["governing_heading_block_id"]], following=following,
            labels=role["expected_fact_labels"])
        _require(bool(role_proof))
        proof.update(role_projection_proof=role_proof, role_ancestor_reviewed_pages=reviewed)
    if wrap := config.get("label_wrap"):
        from .contract_operative_line_scope import reviewed_hse_paragraph_label_boundary

        members, _ = _complete_paragraph(body, wrap["paragraph_start_block_id"])
        page, = {row["page"] for row in members}
        native = list(pdf_page_text_lines(pdf.pages[page - 1], ()))
        wrap_proof = reviewed_hse_paragraph_label_boundary(window, native, expected_blocks=members, label=wrap["label"])
        _require(bool(wrap_proof))
        proof["label_wrap_proof"] = wrap_proof
    return proof


def bind_contract_operative_line(windows, documents, blocks, footer_skips, manifest, config):
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
            proof = _operative_position(lines, pages, config, blocks, window)
            proof.update(_reviewed_projections(pdf, payload, lines, pages, config, document, blocks, footer_skips, window))
        proof.update(rule=RULE, source_id=source_id, source_sha256=source["sha256"], window_id=window["window_id"],
                     scope=config, reviewed_pages=pages)
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, StopIteration, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def resolve_contract_operative_line(raw, normalized, window):
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None:
        return None
    scope = proof["scope"]
    index = scope["candidate_index"]
    _require(proof["rule"] == RULE and all(proof[key] == window[key] for key in ("window_id", "source_id", "source_sha256")) and
             proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"}) and
             digest(raw) == scope["raw_candidate_sha256"] and index < len(normalized["requirements"]))
    candidate = normalized["requirements"][index]
    citation = candidate["citation"]
    line = proof["operative_line"]
    targets = {row["block_id"]: row for row in window["blocks"]}
    members = [*proof["preceding_clause_members"], line, *proof["following_clause_members"]]
    _require((candidate["category"], candidate["response_mode"], candidate["mandatory"], candidate["format_status"]) ==
             ("technical", "writing", True, "not_prescribed") and not candidate["attachment_keywords"] and
             not candidate["required_fact_labels"] and not candidate.get("format_title") and
             citation["block_ids"] == [scope["operative_block_id"]] and
             all(citation[key] == line[key] for key in ("source_id", "block_id", "locator")) and
             not any(citation[key] for key in ("quote", "clause_id", "table_row_id")) and
             all(_proof_member(targets[member["block_id"]]) == member for member in members))
    return {"rule": RULE, "window_id": window["window_id"], "candidate_index": index, "raw_candidate_sha256": digest(raw),
            "proof": proof, "guard_disposition": "complete_numbered_imperative_with_separate_following_clause",
            "model_candidate_mutation": False, "semantic_review": "requires_source_review"}
