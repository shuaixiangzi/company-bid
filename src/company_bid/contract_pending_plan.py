"""Source-only N3 bindings for finite, explicitly pending contract fields.

Existing N2 objects are never changed. Derived items retain a complete native
source span and are labelled as application derivations, not model output.
Neither binding nor derivation supplies a value or accepts an obligation.
"""
from __future__ import annotations

import copy

from .contract_review_slots import contract_review_slot_catalog
from .extraction import PlanningError, _citation, _index, _pdf_footer_skips
from .storage import digest


RULE = "contract_pending_plan_v1"
_WIRE_FIELDS = ("source_id", "block_id", "locator", "block_ids", "quote")
_IDENTITY_FIELDS = ("unit_id", "page", "start", "end", "end_locator", "pages", "skipped_block_ids", "quote_sha256")


def _error(reason, **context):
    return {"code": "requires_source_review", "reason": reason, **context}


def _citation_ids(citation):
    ids = citation.get("block_ids", [citation.get("block_id")])
    return ids if isinstance(ids, list) and all(isinstance(key, str) for key in ids) else []


def _checked_citation(citation, requirement, blocks, skips):
    """Recheck literal source coverage, independent of any N2 semantic label.

    Clause/table selector identities are not adopted as independent proof. A
    native full-block citation is reconstructed from its actual words instead.
    A genuine single-block substring remains partial coverage, not a full slot.
    """
    if (not isinstance(citation, dict) or not isinstance(citation.get("quote"), str) or
            citation["quote"] != requirement["text"] or
            not all(isinstance(citation.get(key), str) for key in ("source_id", "block_id", "locator")) or
            not _citation_ids(citation)):
        raise PlanningError("CONTRACT_PENDING_CITATION_INVALID")
    wire = {key: copy.deepcopy(citation[key]) for key in _WIRE_FIELDS if key in citation}
    checked = _citation(wire, blocks, skips)
    if any(key in citation and citation[key] != checked.get(key) for key in _IDENTITY_FIELDS):
        raise PlanningError("CONTRACT_PENDING_CITATION_IDENTITY_MISMATCH")
    ids = checked.get("block_ids") or [checked["block_id"]]
    if any(blocks[key]["kind"] != "paragraph" or blocks[key].get("origin") != "native" for key in ids):
        raise PlanningError("CONTRACT_PENDING_NATIVE_CITATION_REQUIRED")
    # _citation requires all words for a multiblock span. Single-block quotes
    # may legally select a substring, which cannot prove a complete slot block.
    complete = set(ids) if len(ids) > 1 else ({ids[0]} if checked["start"] == 0 and checked["end"] == len(blocks[ids[0]]["text"]) else set())
    return checked, complete


def _derived_identity(item):
    proof = item["source_proof"]
    return {"rule": RULE, "source_id": proof["source_id"], "source_sha256": proof["source_sha256"],
            "scope": item["scope"], "clause_number": item["clause_number"], "subclause_number": item["subclause_number"],
            "members": [{key: member[key] for key in ("block_id", "locator", "text_sha256")} for member in proof["members"]],
            "pending_fields": item["pending_fields"], "blocking_issues": item["blocking_issues"]}


def contract_pending_plan(requirements: list[dict], parsed: dict, manifest: dict) -> dict:
    """Bind complete source coverage or derive a complete pending source item.

    ``errors`` must block automatic writing/adoption; callers must not discard
    them. ``uncovered_clauses`` remain a finite-catalogue limitation, not proof
    that those clauses have values, have no obligations, or are satisfied.
    """
    result = {"rule": RULE, "derived_requirements": [], "bindings": {}, "binding_proofs": {},
              "derivations": {}, "errors": [], "n2_candidate_mutation": False,
              "values_inferred": False, "satisfaction": "not_determined",
              "catalog_summary": {"exhaustive": False, "coverage_status": "not_evaluated"},
              "limitations": ["This helper covers only the source catalogue's finite supported forms.",
                              "Bindings preserve original N2 modes and text; derived items do not replace original obligations.",
                              "Pending fields and literal-reference clarification issues require explicit review; no fact is filled."]}
    if not isinstance(requirements, list) or any(not isinstance(item, dict) for item in requirements):
        result["errors"].append(_error("requirements_input_invalid"))
        return result
    result["n2_requirements_sha256"] = digest(requirements)
    try:
        catalog = contract_review_slot_catalog(parsed, manifest)
        blocks, _, _ = _index(parsed, manifest)
    except (PlanningError, KeyError, TypeError, ValueError, AttributeError) as exc:
        result["errors"].append(_error("frozen_contract_source_unavailable", source_error_code=getattr(exc, "code", "SOURCE_STRUCTURE_INVALID")))
        return result
    result["catalog_summary"] = {"exhaustive": False, "coverage_status": catalog["coverage_status"],
        "rule": catalog["rule"], "catalog_sha256": digest(catalog), "item_count": len(catalog["items"]),
        "pending_field_count": sum(len(item["pending_fields"]) for item in catalog["items"]),
        "supported_forms": copy.deepcopy(catalog["supported_forms"]),
        "uncovered_clauses": copy.deepcopy(catalog["uncovered_clauses"]),
        "unavailable_sources": copy.deepcopy(catalog["unavailable_sources"]),
        "unresolved_scopes": copy.deepcopy(catalog["unresolved_scopes"])}
    result["limitations"].extend(copy.deepcopy(catalog["limitations"]))
    for unavailable in catalog["unavailable_sources"]:
        result["errors"].append(_error("source_reading_not_eligible_for_contract_slots", **unavailable))
    for unresolved in catalog["unresolved_scopes"]:
        result["errors"].append(_error("contract_scope_not_proven", source_id=unresolved["source_id"],
                                       block_id=unresolved["heading"]["block_id"]))
    if result["errors"]:
        return result

    ids = [item.get("requirement_id") for item in requirements]
    if (any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids) or
            any(not isinstance(item.get("text"), str) or not isinstance(item.get("citations"), list) or not item["citations"]
                for item in requirements)):
        result["errors"].append(_error("requirement_identity_or_citations_invalid"))
        return result
    for requirement in requirements:
        for citation in requirement["citations"]:
            selected = _citation_ids(citation) if isinstance(citation, dict) else []
            if (not isinstance(citation, dict) or not all(isinstance(citation.get(key), str) for key in
                    ("source_id", "block_id", "locator", "quote")) or not selected or
                    selected[0] != citation["block_id"] or len(set(selected)) != len(selected) or
                    any(key not in blocks or blocks[key]["source_id"] != citation["source_id"] for key in selected)):
                result["errors"].append(_error("requirement_source_citation_missing_or_invalid", requirement_id=requirement["requirement_id"]))
    if result["errors"]:
        return result
    skips = _pdf_footer_skips(blocks)
    catalog_ids = {key for item in catalog["items"] for key in item["block_ids"]}
    checked_requirements = {}
    for requirement in requirements:
        rid = requirement["requirement_id"]
        candidates = [citation for citation in requirement["citations"]
                      if isinstance(citation, dict) and set(_citation_ids(citation)) & catalog_ids]
        checked, complete, overlapping = [], set(), set()
        invalid = False
        for citation in candidates:
            overlapping.update(_citation_ids(citation))
            try:
                canonical, covered = _checked_citation(citation, requirement, blocks, skips)
                checked.append(canonical)
                complete.update(covered)
            except (PlanningError, KeyError, TypeError, ValueError) as exc:
                invalid = True
                result["errors"].append(_error("overlapping_citation_not_proven", requirement_id=rid,
                                               source_error_code=getattr(exc, "code", "CITATION_STRUCTURE_INVALID")))
        unsafe_mode = bool(candidates and (requirement.get("category") == "technical" or requirement.get("response_mode") == "writing"))
        if unsafe_mode:
            result["errors"].append(_error("contract_pending_source_overlaps_technical_or_writing", requirement_id=rid,
                                           category=requirement.get("category"), response_mode=requirement.get("response_mode")))
        checked_requirements[rid] = {"overlapping": overlapping, "complete": complete,
                                     "citations": checked, "unsafe": unsafe_mode or invalid}

    def bind(rid, item, kind, evidence):
        result["bindings"].setdefault(rid, []).append(copy.deepcopy(item))
        result["binding_proofs"].setdefault(rid, []).append({
            "kind": kind, "catalog_pending_id": item["pending_id"], "source_proof_sha256": item["source_proof_sha256"],
            "complete_source_block_ids": list(item["block_ids"]), "citation_sha256": digest(evidence),
            "original_requirement_unchanged": kind == "existing_complete_source_citations",
            "satisfaction": "not_determined", "values_inferred": False})

    existing_ids = set(ids)
    for item in catalog["items"]:
        source_ids = set(item["block_ids"])
        unsafe = [rid for rid, record in checked_requirements.items() if record["unsafe"] and source_ids & record["overlapping"]]
        if unsafe:
            # Do not derive an alternative that could hide a conflicting route.
            continue
        owners = [rid for rid, record in checked_requirements.items() if source_ids <= record["complete"]]
        if owners:
            for rid in owners:
                bind(rid, item, "existing_complete_source_citations", checked_requirements[rid]["citations"])
            continue
        members = item["source_proof"]["members"]
        wire = {key: members[0][key] for key in ("source_id", "block_id", "locator")}
        wire.update(block_ids=list(item["block_ids"]), quote=item["text"])
        try:
            citation = _citation(wire, blocks, skips)
        except (PlanningError, KeyError, TypeError, ValueError) as exc:
            result["errors"].append(_error("derived_source_span_not_proven", catalog_pending_id=item["pending_id"],
                                           source_error_code=getattr(exc, "code", "CITATION_STRUCTURE_INVALID")))
            continue
        rid = "R-CONTRACT-" + digest(_derived_identity(item))[:24]
        if rid in existing_ids:
            result["errors"].append(_error("derived_requirement_id_collision", requirement_id=rid, catalog_pending_id=item["pending_id"]))
            continue
        provenance = {"kind": "derived_from_frozen_source", "not_model_output": True, "rule": RULE,
            "catalog_pending_id": item["pending_id"], "source_proof_sha256": item["source_proof_sha256"],
            "scope": copy.deepcopy(item["scope"]), "source_span_kind": item["source_span_kind"],
            "overlapping_original_requirement_ids": [key for key, record in checked_requirements.items() if source_ids & record["overlapping"]],
            "original_context": {key: copy.deepcopy(item["source_proof"][key]) for key in
                                 ("contract_ancestor", "scope_heading", "article_heading", "article_preamble", "parent_context")},
            "n2_candidate_mutation": False, "values_inferred": False}
        requirement = {"requirement_id": rid, "text": item["text"], "category": "commercial", "response_mode": "factual_response",
            "mandatory": True, "confidence": "low", "format_status": "not_prescribed", "format_title": None,
            "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": [],
            "citations": [citation], "window_ids": [], "derivation": copy.deepcopy(provenance)}
        result["derived_requirements"].append(requirement)
        result["derivations"][rid] = provenance
        existing_ids.add(rid)
        bind(rid, item, "derived_complete_frozen_source_span", [citation])
    result["catalog_summary"].update(bound_item_count=len({item["pending_id"] for items in result["bindings"].values() for item in items}),
                                      derived_requirement_count=len(result["derived_requirements"]))
    return result
