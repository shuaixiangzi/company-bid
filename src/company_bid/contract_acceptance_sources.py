"""Authenticate the original lines of a reviewed cross-window contract list."""
from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .response_cover_sources import _member, _native_lines
from .storage import digest


CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_KEY = "contract_insurance_acceptance_source_resolution"
SOURCE_ERROR = "CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_PROOF_MISMATCH"
_CONFIG_ERROR = "CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_INVALID"
_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_STANDARD = re.compile(r"(?:Q_GGW[ \n]+\d+\.\d+—\d{4}|(?:SY/T|GB/T|GB) \d+(?:\.\d+)?-\d{4})")


class _ContractScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    predecessor_window_id: _ID
    successor_window_id: _ID
    insurance_page: int = Field(ge=1)
    acceptance_page: int = Field(ge=1)
    insurance_block_ids: list[_ID] = Field(min_length=5, max_length=5)
    acceptance_block_ids: list[_ID] = Field(min_length=11, max_length=11)
    adjacent_unresolved_block_id: _ID
    raw_candidate_sha256: _SHA
    source_proof_sha256: _SHA
    predecessor_requirement_sha256: _SHA


def normalize_contract_insurance_acceptance_config(value):
    try:
        scope = _ContractScope.model_validate(value).model_dump()
    except ValidationError:
        raise ValueError(_CONFIG_ERROR) from None
    ids = scope["insurance_block_ids"] + scope["acceptance_block_ids"]
    window_ids = {scope[key] for key in ("window_id", "predecessor_window_id", "successor_window_id")}
    if (len(set(ids)) != len(ids) or len(window_ids) != 3 or
            scope["insurance_page"] + 1 != scope["acceptance_page"] or
            scope["adjacent_unresolved_block_id"] not in scope["acceptance_block_ids"][2:-1]):
        raise ValueError(_CONFIG_ERROR)
    return scope


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _check_native(config, lines, subject):
    page = config[subject + "_page"]
    start = re.fullmatch(r"pdf/page\[\d+\]/line\[(\d+)\]", lines[0]["locator"])
    if (not start or [block["block_id"] for block in lines] != config[subject + "_block_ids"] or
            not _native_lines(lines, config["source_id"], page, range(int(start[1]), int(start[1]) + len(lines))) or
            any(left["bbox"][3] >= right["bbox"][1] for left, right in zip(lines, lines[1:]))):
        raise ValueError(SOURCE_ERROR)


def _standards(quote, prefix_end):
    """Index exact identifiers and names without altering the native quote."""
    end = len(quote) - len("】。")
    matches = list(_STANDARD.finditer(quote, prefix_end, end))
    if len(matches) != 9 or matches[0].start() != prefix_end or len({m.group().replace("\n", " ") for m in matches}) != 9:
        raise ValueError(SOURCE_ERROR)
    standards = []
    ends = [match.start() for match in matches[1:]] + [end]
    for index, (match, limit) in enumerate(zip(matches, ends, strict=True)):
        suffix = quote[match.end():limit]
        name = suffix.strip()
        start = match.end() + len(suffix) - len(suffix.lstrip())
        if (not name or not re.search(r"[\u3400-\u9fff]", name) or re.search(r"[\d][_—-]\d{4}", name) or
                (index < 2 and (not match.group().startswith("Q_GGW") or "《" in name or "》" in name)) or
                (index >= 2 and (match.group().startswith("Q_GGW") or not re.fullmatch(r"《[^《》]+》", name)))):
            raise ValueError(SOURCE_ERROR)
        standards.append({"identifier": match.group(), "identifier_start": match.start(), "identifier_end": match.end(),
                          "name_start": start, "name_end": start + len(name), "name_sha256": _sha(name)})
    return standards


def build_contract_insurance_acceptance_proof(config, insurance, acceptance):
    """Split the two independent dispositions at the actual native clause edges."""
    _check_native(config, insurance, "insurance")
    _check_native(config, acceptance, "acceptance")
    insured_quote = "\n".join(line["text"] for line in insurance[:-1])
    insurance_start = re.match(r"[^。]+。(?=乙方负责)", insured_quote)
    insurance_end = re.search(r"(?<=。)甲方不对[^。]+承担任何责任和费\n用。$", insured_quote)
    if (not insurance_start or not insurance_end or not re.match(r"7\.6\s+\S", insurance[-1]["text"]) or
            not re.fullmatch(r"乙方负责[^。]+购买适当的保险[^。]+索赔和费用。",
                             insured_quote[insurance_start.end():insurance_end.start()]) or
            insurance[-1]["bbox"][0] <= max(line["bbox"][0] for line in insurance[:-1])):
        raise ValueError(SOURCE_ERROR)
    body = acceptance[1:-1]
    quote = "\n".join(line["text"] for line in body)
    prefix = re.match(r"\(3\)\s*技术服务工作成果的验收标准：【", quote)
    sibling = acceptance[-1]
    if (not prefix or not quote.endswith("】。") or quote.count("【") != 1 or quote.count("】") != 1 or
            not acceptance[0]["text"].endswith("。") or not re.match(r"\(4\)\s*技术服务工作成果的验收方法：", sibling["text"]) or
            abs(body[0]["bbox"][0] - sibling["bbox"][0]) > 2 or any(line["bbox"][0] >= body[0]["bbox"][0] for line in body[1:])):
        raise ValueError(SOURCE_ERROR)
    predecessor = {"window_id": config["predecessor_window_id"], "block_ids": config["insurance_block_ids"][:-1],
                   "start_locator": insurance[0]["locator"], "end_locator": insurance[-2]["locator"],
                   "quote_sha256": _sha(insured_quote), "quote_length": len(insured_quote),
                   "requirement_sha256": config["predecessor_requirement_sha256"],
                   "insurance_start": insurance_start.end(), "liability_start": insurance_end.start(),
                   "members": [_member(line, role) for line, role in zip(
                       insurance, ("prior_sentence_end_and_insurance_start", "insurance_continuation",
                                   "insurance_and_liability_continuation", "liability_end", "next_contract_clause"), strict=True)]}
    list_proof = {"owner_window_id": config["window_id"], "continuation_window_id": config["successor_window_id"],
                  "block_ids": config["acceptance_block_ids"][1:-1], "quote_sha256": _sha(quote),
                  "standards": _standards(quote, prefix.end()),
                  "members": [_member(acceptance[0], "previous_numbered_item_end"),
                              _member(body[0], "acceptance_list_start"),
                              *[_member(line, "acceptance_list_continuation") for line in body[1:-1]],
                              _member(body[-1], "acceptance_list_end"), _member(sibling, "next_numbered_item_start")]}
    proof = {"rule": "native_contract_insurance_and_acceptance_v1", "source_id": config["source_id"],
             "source_sha256": config["source_sha256"], "window_id": config["window_id"],
             "predecessor": predecessor, "acceptance": list_proof,
             "adjacent_unresolved_block_id": config["adjacent_unresolved_block_id"]}
    return {**proof, "proof_sha256": digest(proof)}
