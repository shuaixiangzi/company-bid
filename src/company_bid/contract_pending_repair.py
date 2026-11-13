"""Bound corrective additions to complete, independently read contract slots."""
from __future__ import annotations

import copy

from .contract_blank_sources import CONTRACT_BLANK_FIELDS
from .contract_review_slots import contract_review_slot_catalog
from .extraction import _index, _windows, extraction_window_payload
from .storage import digest


def contract_pending_source_groups(candidate, request, parsed, manifest):
    try:
        if request.get("task_type", "extract_requirements") != "extract_requirements":
            return None
        blocks, _, documents = _index(parsed, manifest)
        windows, _ = _windows(documents, blocks, request["window_config"])
        window, = [w for w in windows if w["window_id"] == request["window"]["window_id"]]
        if extraction_window_payload(window, request["window_config"]) != {k: v for k, v in request.items() if k != "task_type"}:
            return None
        targets = {b["block_id"] for b in window["blocks"]}
        available = targets | {b["block_id"] for b in window.get("adjacent_context", [])}
        unresolved = candidate["unresolved_block_ids"]
        if (candidate["window_id"] != window["window_id"] or candidate.get("format_status") in {"unread", "conflict"} or
                len(set(unresolved)) != len(unresolved) or not set(unresolved) <= available):
            return None
        rows = [item for item in contract_review_slot_catalog(parsed, manifest)["items"]
                if set(item["block_ids"]) & set(unresolved) & targets]
        groups, covered = [], set()
        for item in rows:
            ids = item["block_ids"]
            if (not 1 <= len(ids) <= 24 or not set(ids) <= available or covered & set(ids) or
                    item["reading_status"] != "read" or item["values_inferred"] is not False or
                    item["value_status"] != "value_not_supplied_in_read_text"):
                return None
            covered.update(ids)
            original = item["source_proof"]
            structure = {key: value for key, value in original.items() if key not in {"parsed_sha256", "source_reading_sha256"}}
            # Full words live in the frozen N1/request. Compact repeated proof
            # text without changing the established 20K inventory budget.
            proof = {"rule": "source_bound_contract_pending_v1", "window_id": window["window_id"],
                "source_id": item["source_id"], "source_sha256": original["source_sha256"],
                "parsed_sha256": original["parsed_sha256"], "source_reading_sha256": original["source_reading_sha256"],
                "scope": copy.deepcopy(item["scope"]), "clause_number": item["clause_number"],
                "subclause_number": item["subclause_number"], "source_span_kind": item["source_span_kind"],
                "source_structure_sha256": digest(structure), "source_text_sha256": digest(item["text"]),
                "members": [{k: member[k] for k in ("block_id", "locator", "text_sha256")}
                            for member in original["members"]],
                "pending_fields": copy.deepcopy(item["pending_fields"]), "slot_forms": list(item["slot_forms"]),
                "blocking_issues": copy.deepcopy(item["blocking_issues"]), "reading_status": "read",
                "value_status": item["value_status"], "physical_blank_status": "not_determined",
                "reference_status": item["reference_status"], "satisfaction": "not_determined", "values_inferred": False}
            groups.append({"resolution_kind": "source_bound_contract_pending", "block_ids": list(ids),
                "required_target_ids": [key for key in ids if key in unresolved and key in targets],
                "basis": "complete_native_contract_span_with_source_lexical_pending_fields",
                "required_response_fields": copy.deepcopy(CONTRACT_BLANK_FIELDS),
                "source_proof": proof, "source_proof_sha256": digest(proof)})
        if not 1 <= len(groups) <= 8 or covered & set(unresolved) & targets != set(unresolved) & targets:
            return None
        return groups
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
