"""Explicit source-review additions to pending work, never model answers.

An omission review may select complete frozen native passages for follow-up.
These additions cannot adopt company facts, satisfy a requirement, change N2,
or replace an existing writing task. The review classification is visible and
remains pending; source text is reconstructed from N1, not supplied prose.
"""
from __future__ import annotations

import hashlib
import re

from .extraction import PlanningError, _citation, _pdf_footer_skips
from .storage import digest


VERSION = "reviewed-source-actions-v1"


def reviewed_source_actions(config, blocks, documents, existing):
    if config is None:
        return [], {"version": VERSION, "status": "not_configured", "items": [], "n2_candidate_mutation": False}
    if not isinstance(config, dict) or set(config) != {"version", "review_sha256", "reviewer", "items"} or (
            config["version"] != VERSION or config["reviewer"] not in {"assistant_source_review", "human_source_review"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(config["review_sha256"]))
            or not isinstance(config["items"], list) or not 1 <= len(config["items"]) <= 40):
        raise PlanningError("SOURCE_REVIEW_CONFIG_INVALID")
    additions, proofs, seen = [], [], set()
    document_map = {d["source_id"]: d for d in documents}
    for item in config["items"]:
        if not isinstance(item, dict) or set(item) != {"source_id", "source_sha256", "block_ids", "quote_sha256",
                                                     "category", "instruction", "review_reason"}:
            raise PlanningError("SOURCE_REVIEW_ITEM_INVALID")
        ids = item["block_ids"]
        doc = document_map.get(item["source_id"])
        if (doc is None or doc["role"] != "tender" or doc["sha256"] != item["source_sha256"] or
                not isinstance(ids, list) or not 1 <= len(ids) <= 24 or len(set(ids)) != len(ids) or
                any(key not in blocks or blocks[key]["source_id"] != item["source_id"] for key in ids)):
            raise PlanningError("SOURCE_REVIEW_SOURCE_INVALID")
        members = [blocks[key] for key in ids]
        if any(b["kind"] != "paragraph" or b.get("origin") != "native" or b.get("read_disposition") for b in members):
            raise PlanningError("SOURCE_REVIEW_UNSUPPORTED_READING")
        if item["category"] not in {"commercial", "procedure"} or any(
                not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 1200
                for key in ("instruction", "review_reason")):
            raise PlanningError("SOURCE_REVIEW_CLASSIFICATION_INVALID")
        first = members[0]
        citation = _citation({"source_id": item["source_id"], "block_id": first["block_id"],
                              "block_ids": ids, "locator": first["locator"]}, blocks, _pdf_footer_skips(blocks))
        if hashlib.sha256(citation["quote"].encode()).hexdigest() != item["quote_sha256"]:
            raise PlanningError("SOURCE_REVIEW_QUOTE_CHANGED")
        identity = digest({"source_sha256": doc["sha256"], "block_ids": ids, "quote_sha256": item["quote_sha256"]})
        if identity in seen or any(r["text"] == citation["quote"] for r in existing):
            raise PlanningError("SOURCE_REVIEW_DUPLICATE_REQUIREMENT")
        seen.add(identity)
        rid = "R-REVIEW-" + identity[:20]
        additions.append({"requirement_id": rid, "text": citation["quote"], "category": item["category"],
            "response_mode": "procedure", "mandatory": True, "confidence": "medium",
            "format_status": "not_prescribed", "format_title": None, "required_fact_labels": [],
            "attachment_keywords": [], "reference_keywords": [], "citations": [citation], "window_ids": []})
        proofs.append({"requirement_id": rid, **item, "reviewer": config["reviewer"],
            "review_sha256": config["review_sha256"], "classification_status": "reviewed_pending_action",
            "satisfaction_status": "not_determined", "source_citation": citation})
    return additions, {"version": VERSION, "status": "source_bound_pending_actions", "items": proofs,
                       "n2_candidate_mutation": False, "review_sha256": config["review_sha256"]}
