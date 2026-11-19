"""Source boundaries for a reviewed native contract continuation."""
from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .response_cover_sources import _member, _native_lines
from .storage import digest


CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_KEY = "contract_interested_parties_source_resolution"
_SOURCE_ERROR = "CONTRACT_INTERESTED_PARTIES_SOURCE_PROOF_MISMATCH"
_CONFIG_ERROR = "CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_INVALID"
_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class _ContractContinuationScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    predecessor_window_id: _ID
    physical_page: int = Field(ge=1)
    block_ids: list[_ID] = Field(min_length=3, max_length=3)
    raw_candidate_sha256: _SHA
    source_proof_sha256: _SHA
    predecessor_requirement_sha256: _SHA


def normalize_contract_interested_parties_config(value):
    try:
        scope = _ContractContinuationScope.model_validate(value).model_dump()
    except ValidationError:
        raise ValueError(_CONFIG_ERROR) from None
    if scope["window_id"] == scope["predecessor_window_id"] or len(set(scope["block_ids"])) != 3:
        raise ValueError(_CONFIG_ERROR)
    return scope


def build_contract_interested_parties_proof(config, lines, edge):
    """Prove that a numbered clause ends before the following numbered sibling."""
    source_id, page = config["source_id"], config["physical_page"]
    first, tail, following = lines
    match = re.fullmatch(r"pdf/page\[\d+\]/line\[(\d+)\]", first["locator"])
    if (not match or not _native_lines(lines, source_id, page, range(int(match[1]), int(match[1]) + 3))
            or [line["block_id"] for line in lines] != config["block_ids"]):
        raise ValueError(_SOURCE_ERROR)
    first_box, tail_box, next_box = (line["bbox"] for line in lines)
    if (not edge or not re.match(r"[（(]3[）)]\s*\S", first["text"])
            or not re.match(r"[（(]4[）)]\s*\S", following["text"])
            or not re.match(r"[\u3400-\u9fff]", tail["text"]) or not tail["text"].endswith("。")
            or not first_box[1] < tail_box[1] < next_box[1] or tail_box[3] >= next_box[1]
            or abs(first_box[0] - next_box[0]) > 2 or tail_box[0] <= first_box[0]):
        raise ValueError(_SOURCE_ERROR)
    body_ids = config["block_ids"][:2]
    quote = "\n".join(line["text"] for line in lines[:2])
    members = [_member(line, role) for line, role in zip(lines, ("numbered_clause_start", "clause_tail",
                                                               "following_numbered_clause"), strict=True)]
    proof = {"rule": "native_contract_interested_parties_continuation_v1", "source_id": source_id,
             "source_sha256": config["source_sha256"], "physical_page": page,
             "window_id": config["window_id"], "members": members, "native_line_edge": edge,
             "predecessor": {"window_id": config["predecessor_window_id"], "block_ids": body_ids,
                             "requirement_sha256": config["predecessor_requirement_sha256"],
                             "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                             "start_locator": first["locator"], "end_locator": tail["locator"]},
             "following_block_id": following["block_id"], "disposition": "already_cited_in_predecessor"}
    return {**proof, "proof_sha256": digest(proof)}
