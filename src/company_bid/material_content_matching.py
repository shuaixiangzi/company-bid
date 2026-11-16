"""Source-text matching after catalog recall, without replaying a changed prompt."""
from __future__ import annotations

import copy

from .material_matching import MATERIAL_MATCHING_VERSION, material_candidate_report
from .semantic_materials import assess_candidates, check_declared_credential_conditions
from .storage import digest

CONTENT_MATCHING_VERSION = "material-content-matching.v1"


def integrate_original_content(ctx, requirements, manifest, parsed, catalog_report):
    from .material_content_index import build_material_content_index

    index = build_material_content_index(ctx, manifest, parsed)
    scope = {"attachments": manifest.get("attachments", []), "sources": manifest["sources"],
             "evidence_selections": manifest.get("evidence_selections"),
             "proof_derivations": manifest.get("proof_derivations", []), "parsed": index["parsed"]}
    records = manifest.get("material_records", [])
    scope_hash = digest({"records": records, **scope})
    version = {"version": CONTENT_MATCHING_VERSION, "matching_version": MATERIAL_MATCHING_VERSION}
    report = ctx.run_unit("material-content-matching-index", {**version, "scope_sha256": scope_hash},
                          lambda: material_candidate_report([], records, **scope))
    report = copy.deepcopy(report)
    keys = ("queries", "proof_queries", "assembly_candidates")
    inventory_hash = digest(report)
    for key in keys:
        report[key] = []
    report["checkpoint_units"] = []
    for requirement in requirements:
        unit_id = "material-content-query-" + digest(requirement["requirement_id"])[:20]
        def evaluate(requirement=requirement):
            value = material_candidate_report([requirement], records, **scope)
            return {key: value[key] for key in keys}
        value = ctx.run_unit(unit_id, {**version, "scope_sha256": scope_hash,
            "inventory_sha256": inventory_hash, "requirement": requirement}, evaluate)
        for key in keys:
            report[key].extend(value[key])
        report["checkpoint_units"].append({"unit_id": unit_id, "requirement_id": requirement["requirement_id"],
                                           "output_sha256": digest(value)})
    # The old, authenticated model response belongs to the catalog snapshot
    # actually sent. Only the program's source checks run against the new index.
    semantic = copy.deepcopy(catalog_report.get("semantic_recall"))
    if semantic is not None:
        queries = {q["requirement_id"]: q for q in report["proof_queries"]}
        catalog_queries = {q["requirement_id"]: q for q in catalog_report["proof_queries"]}
        for row in semantic["queries"]:
            query = queries[row["requirement_id"]]
            catalog_query = catalog_queries[row["requirement_id"]]
            row["catalog_stage_dictionary_search_status"] = catalog_query["search_status"]
            row["catalog_stage_dictionary_candidate_group_ids"] = list(catalog_query["candidate_group_ids"])
            old_candidates = copy.deepcopy(row["candidates"])
            checked = assess_candidates({"candidates": old_candidates}, report, query)
            for before, after in zip(old_candidates, checked):
                after["blockers"] = list(dict.fromkeys([*before["blockers"], *after["blockers"]]))
                if after["blockers"] or before["decision"] == "blocked_candidate":
                    after["decision"] = "blocked_candidate"
                after["catalog_stage_assessment_sha256"] = digest(before)
            row["catalog_stage_status"] = row["status"]
            row["candidates"] = checked
            if checked:
                row["status"] = ("candidates_pending_original_checks" if any(
                    c["decision"] != "blocked_candidate" for c in checked) else "all_candidates_blocked")
            row["condition_findings"] = list({digest(item): item for item in [
                *row["condition_findings"], *check_declared_credential_conditions(query,
                    [c["group_id"] for c in checked if c["decision"] != "blocked_candidate"], report)]}.values())
            row["current_original_index_reassessment_sha256"] = digest({"index": index,
                                                                                 "query": query, "candidates": checked})
            query["semantic_recall_status"] = row["status"]
            query["semantic_recall"] = copy.deepcopy(row)
        semantic["catalog_stage_report_sha256"] = digest(catalog_report)
        semantic["current_program_checks"] = "selected_original_text_index_after_catalog_recall"
        semantic["counts"]["candidate_queries"] = sum(r["status"] == "candidates_pending_original_checks" for r in semantic["queries"])
        semantic["counts"]["blocked_candidates"] = sum(c["decision"] == "blocked_candidate" for r in semantic["queries"] for c in r["candidates"])
        report["semantic_recall"] = semantic
    report["original_content_index"] = {key: value for key, value in index.items() if key != "parsed"}
    report["original_content_index"]["catalog_stage_report_sha256"] = digest(catalog_report)
    report["original_content_index"]["indexed_parsed_sha256"] = digest(index["parsed"])
    report["report_scope"] = "selected_original_text_matching_with_prior_catalog_recall"
    report["matching_version"] = MATERIAL_MATCHING_VERSION
    report["content_matching_version"] = CONTENT_MATCHING_VERSION
    return report
