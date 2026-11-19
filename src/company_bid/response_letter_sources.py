"""Join a reviewed response letter across its two frozen target windows."""
from __future__ import annotations

import re

from .response_review_table_sources import RESPONSE_REVIEW_TABLE_RULE
from .table_continuations import _hash


RESPONSE_LETTER_CONFIG_KEY = "response_letter_source_resolution"
RESPONSE_LETTER_RULE = "native_pdf_reviewed_response_letter_continuation_v1"
_KEYS = {"source_id", "source_sha256", "window_id", "owner_window_id", "physical_page",
         "raw_candidate_sha256", "source_proof_sha256"}


def normalize_response_letter_config(value):
    hashes = {"source_sha256", "raw_candidate_sha256", "source_proof_sha256"}
    if (not isinstance(value, dict) or set(value) != _KEYS or
            any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 120 for k in _KEYS - {"physical_page"}) or
            any(not re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in hashes) or
            type(value["physical_page"]) is not int or value["physical_page"] < 5 or
            value["owner_window_id"] == value["window_id"]):
        raise ValueError("RESPONSE_LETTER_CONFIGURATION_INVALID")
    return dict(value)


def build_response_letter_proof(scope, review_proof):
    """Derive the join only from a rebuilt preliminary form/letter source proof."""
    scope_keys = ("source_id", "source_sha256", "physical_page", "owner_window_id", "window_id")
    if (not isinstance(scope, dict) or any(key not in scope for key in scope_keys) or
            type(scope["physical_page"]) is not int or not isinstance(review_proof, dict)):
        return None
    source = {key: scope[key] for key in scope_keys}
    if (review_proof.get("rule") != RESPONSE_REVIEW_TABLE_RULE or
            any(review_proof.get(key) != source[key] for key in ("source_id", "source_sha256")) or
            review_proof.get("physical_page") != source["physical_page"] - 3):
        return None
    members = review_proof["letter_members"]
    next_heading = review_proof["following_heading"]
    page = source["physical_page"]
    if (len(members) != 18 or [member["locator"] for member in members] !=
            [f"pdf/page[{page}]/line[{index}]" for index in range(18)] or
            next_heading["locator"] != f"pdf/page[{page + 1}]/line[0]"):
        return None
    ids = [member["block_id"] for member in members]
    proof = {**source, "rule": RESPONSE_LETTER_RULE, "owner_source_proof_sha256": review_proof["proof_sha256"],
             "heading_block_id": ids[0], "owner_block_ids": ids[:12], "selected_block_ids": ids[12:],
             "following_heading_block_id": next_heading["block_id"], "letter_footer": review_proof["footer_proofs"][-1]}
    return {**proof, "proof_sha256": _hash(proof)}
