"""Prove the complete reviewed HSE annex introduction across frozen windows."""

from __future__ import annotations

import re

import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_attachment_citation_split import _ID, _SHA, _member, _page_lines
from .storage import digest


SOURCE_CONFIG_KEY = "contract_annex_intro_source"
WINDOW_CONTEXT_KEY = "native_contract_annex_intro_context"
RESOLUTION_KEY = "native_contract_annex_intro_resolution"
SOURCE_ERROR = "CONTRACT_ANNEX_INTRO_SOURCE_MISMATCH"
RULE = "reviewed_native_hse_contract_introduction_v1"
_TITLE = re.compile(r"附件\s*[1-9]\d*\s*[：:]\s*健康、安全与环境[（(]\s*HSE\s*[）)]管理条款(?:【可根据项目实际管理需求增加】)?")
_EMPLOYER = re.compile(r"甲方[（(]发包人[）)][：:]\s*([^。；;！？!?\n]+)")
_CONTRACTOR_BLANK = re.compile(r"乙方[（(]承包商[）)][：:]")
_DEFINITIONS = re.compile(r"1[.．]\s*定义及解释")
_FIRST_DEFINITION = re.compile(r"1\.1\s+HSE\s*[：:]\s*指健康、安全与环境。")
_DECLARATION = re.compile(r"鉴于甲[、，,]?乙双方签署了《[^》]+》[（(]以下简称[“\"]主合同[”\"][）)]，")
_OPERATIVE = re.compile(r"必须|不得|应当|须于|应于|应按|应提供|应提交|甲方应|乙方应|承包(?:人|商|方)应|发包(?:人|方)应")


class _IntroScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    physical_page: int = Field(ge=1)
    rendered_rgb_sha256: _SHA
    annex_heading_block_id: _ID


def normalize_contract_annex_intro_config(value):
    try:
        if not isinstance(value, list):
            return _IntroScope.model_validate(value).model_dump()
        scopes = [_IntroScope.model_validate(scope).model_dump() for scope in value]
    except ValidationError:
        raise ValueError("CONTRACT_ANNEX_INTRO_CONFIG_INVALID") from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError("CONTRACT_ANNEX_INTRO_CONFIG_INVALID")
    return scopes


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _check_recital(lines):
    # Whitespace folding is used only to test the clause grammar. The proof
    # retains every native line and its original separation.
    quote = "\n".join(line["text"] for line in lines)
    text = re.sub(r"\s", "", quote)
    _require(_DECLARATION.match(text) and "依照《" in text and "遵循" in text and
             re.search(r"双方就[^。]+协商一致，订立本协议。$", text) and text.count("。") == 1 and
             not _OPERATIVE.search(text) and text.count("《") == text.count("》"))
    return quote


def following_owner_matches(proof, owner):
    """Use actual target positions. Adjacency alone cannot establish ownership."""
    expected = [*proof["continuation_members"], proof["next_heading"], proof["next_first_definition"]]
    observed = owner.get("blocks", [])[:len(expected)]
    return (owner.get("window_id") == proof["following_owner_window_id"] and
            owner.get("source_id") == proof["source_id"] and owner.get("source_sha256") == proof["source_sha256"] and
            [_member(block) for block in observed] == expected)


def bind_contract_annex_intro(windows, documents, blocks, manifest, config):
    """Prove the entire page and the neighboring window's continuation prefix."""
    try:
        source_id = config["source_id"]
        source, = [source for source in (manifest or {}).get("sources", []) if source["source_id"] == source_id]
        document, = [document for document in documents if document["source_id"] == source_id]
        window, = [window for window in windows if window["window_id"] == config["window_id"]]
        _require(document["format"] == window["source_format"] == "pdf" and window["source_id"] == source_id and
                 source["sha256"] == document["sha256"] == window["source_sha256"] == config["source_sha256"])
        lines, coverage_hash = _page_lines(config, document, blocks, source)
        heading, employer, contractor = lines[:3]
        _require(heading["block_id"] == config["annex_heading_block_id"] and _TITLE.fullmatch(heading["text"]) and
                 _EMPLOYER.fullmatch(employer["text"]) and _CONTRACTOR_BLANK.fullmatch(contractor["text"]))
        first_article, = [i for i, line in enumerate(lines) if _DEFINITIONS.fullmatch(line["text"])]
        _require(first_article >= 5 and _FIRST_DEFINITION.fullmatch(lines[first_article + 1]["text"]) and
                 all(left["bbox"][3] <= right["bbox"][1] for left, right in zip(lines[:first_article + 1], lines[1:])))
        recital = lines[3:first_article]
        quote = _check_recital(recital)
        targets = {row["block_id"] for row in window["blocks"]}
        handoff = next(i for i, row in enumerate(recital) if row["block_id"] not in targets)
        local = [*lines[:3], *recital[:handoff]]
        continuation = recital[handoff:]
        _require(handoff > 0 and not recital[handoff - 1]["text"].endswith(("。", "；", "！", "？")) and
                 [row["block_id"] for row in window["blocks"][-len(local):]] == [row["block_id"] for row in local] and
                 not any(row["block_id"] in targets for row in [*continuation, *lines[first_article:first_article + 2]]))
        available = {row["block_id"]: row for row in window.get("adjacent_context", [])}
        first_remote = continuation[0]["block_id"]
        _require(first_remote in available and _member(available[first_remote]) == _member(continuation[0]))
        owner = windows[windows.index(window) + 1]
        proof = {"rule": RULE, "source_id": source_id, "source_sha256": config["source_sha256"],
                 "window_id": config["window_id"], "physical_page": config["physical_page"], "scope": config,
                 "heading": _member(heading), "employer_line": _member(employer), "contractor_blank_line": _member(contractor),
                 "contractor_value_status": "visibly_unfilled_in_contract_template",
                 "local_members": [_member(line) for line in local],
                 "continuation_members": [_member(line) for line in continuation], "recital_source_quote": quote,
                 "next_heading": _member(lines[first_article]), "next_first_definition": _member(lines[first_article + 1]),
                 "following_owner_window_id": owner["window_id"], "native_coverage_sha256": coverage_hash,
                 "rendered_rgb_sha256": config["rendered_rgb_sha256"],
                 "source_page_lines_sha256": digest([_member(line) for line in lines])}
        _require(following_owner_matches(proof, owner))
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, StopIteration, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def resolve_contract_annex_intro(raw, normalized, window, critical):
    """Retain every declaration and debit only the independently reviewed roles."""
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None or not critical:
        return None
    scoped = proof["local_members"]
    ids = [member["block_id"] for member in scoped]
    first_remote = proof["continuation_members"][0]
    observed = next((row for row in window.get("adjacent_context", []) if row["block_id"] == first_remote["block_id"]), None)
    selected = {identity for item in [*normalized["requirements"], *normalized["format_chapters"]]
                for identity in (item["citation"]["block_ids"] or [item["citation"]["block_id"]])}
    selected.update(identity for item in normalized["format_chapters"] for identity in item["content_block_ids"])
    if (proof["rule"] != RULE or critical != ids or set(ids) & selected or observed is None or _member(observed) != first_remote or
            digest(raw) != proof["scope"]["raw_candidate_sha256"] or
            [_member(row) for row in window["blocks"][-len(scoped):]] != scoped or
            any(proof[key] != window[key] for key in ("window_id", "source_id", "source_sha256")) or
            digest({key: value for key, value in proof.items() if key != "proof_sha256"}) != proof["proof_sha256"]):
        return None
    return {"rule": RULE, "window_id": window["window_id"], "raw_candidate_sha256": digest(raw), "proof": proof,
            "resolved_block_ids": ids, "model_candidate_mutation": False,
            "disposition": "retain_annex_heading_parties_and_recital_without_generating_bid_obligation_or_company_fact",
            "semantic_review": "requires_source_review"}
