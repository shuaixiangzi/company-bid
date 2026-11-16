"""N3 semantic candidate recall; no qualification or assembly authority.

Admission, calls and issue annotations use the normal node checkpoints. The
full deterministic candidate set is retained even when the model returns three
suggestions. A frozen explicit scope may defer queries, never claim them read.
"""
from __future__ import annotations

import copy

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .provider import ChatConfig, ChatProposalProvider, ProviderFailure
from .semantic_materials import (SEMANTIC_VERSION, check_declared_credential_conditions,
                                 recall_route, semantic_probe)
from .storage import digest


RECALL_VERSION = "n3-material-recall.v1"


class RecallOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    max_queries: int = Field(default=12, ge=1, le=32)
    requirement_ids: list[str] | None = None
    provider: dict = Field(default_factory=dict)
    additional_evidence: list[dict] = Field(default_factory=list)
    recorded_source: dict | None = None


def admission_route(requirement: dict, query: dict) -> str:
    """Scope by response purpose before spending on catalog relevance."""
    mode = requirement.get("response_mode")
    if mode != "attachment" and not (mode == "factual_response" and requirement.get("category") == "qualification"):
        return "outside_material_recall_scope"
    route = recall_route(requirement, query)
    if route != "semantic_recall_needed":
        return route
    if "current_signed_commitment" in query["request"]["kinds"]:
        return "requires_current_signed_document"
    if (query["request"]["rule"] == "performance_proof_paths_require_separate_review"
            and query["candidate_group_ids"]):
        return "existing_performance_candidates_require_original_checks"
    return route


def plan_recall_scope(requirements: list, report: dict, settings: dict, handled_routes=None) -> dict:
    try:
        options = RecallOptions.model_validate(settings)
    except ValidationError:
        raise ProviderFailure("SEMANTIC_PLANNING_CONFIG_INVALID") from None
    reqs = {r["requirement_id"]: r for r in requirements}
    if len(reqs) != len(requirements):
        raise ProviderFailure("SEMANTIC_REQUIREMENT_ID_DUPLICATE")
    queries = {q["requirement_id"]: q for q in report["proof_queries"]}
    if set(queries) != set(reqs):
        raise ProviderFailure("SEMANTIC_REQUIREMENT_SCOPE_MISMATCH")
    routes = {rid: admission_route(req, queries[rid]) for rid, req in reqs.items()}
    handled_routes = handled_routes or {}
    if not set(handled_routes) <= set(reqs):
        raise ProviderFailure("SEMANTIC_HANDLED_SCOPE_INVALID")
    for rid, proof in handled_routes.items():
        if routes[rid] == "semantic_recall_needed":
            routes[rid] = proof["route"]
    selected = options.requirement_ids
    if selected is not None and (len(selected) != len(set(selected)) or any(
            rid not in reqs or routes[rid] == "outside_material_recall_scope" for rid in selected)):
        raise ProviderFailure("SEMANTIC_SELECTED_SCOPE_INVALID")
    rows = []
    for rid, route in routes.items():
        status = route
        if route == "semantic_recall_needed":
            status = ("disabled_not_searched" if not options.enabled else
                      "deferred_not_searched" if selected is not None and rid not in selected else "selected")
        row = {"requirement_id": rid, "admission_route": route, "status": status}
        if rid in handled_routes:
            row["source_handling"] = copy.deepcopy(handled_routes[rid])
        rows.append(row)
    count = sum(r["status"] == "selected" for r in rows)
    if count > options.max_queries:
        raise ProviderFailure("SEMANTIC_PLANNED_QUERY_LIMIT_EXCEEDED")
    return {"version": RECALL_VERSION, "enabled": options.enabled, "queries": rows,
            "counts": {"total": len(rows), "eligible": sum(r["admission_route"] == "semantic_recall_needed" for r in rows),
                       "selected": count, "deferred": sum(r["status"] == "deferred_not_searched" for r in rows),
                       "outside_scope": sum(r["status"] == "outside_material_recall_scope" for r in rows)},
            "scope_policy": "attachments_and_qualification_factual_responses",
            "explicit_requirement_scope": selected, "query_limit": options.max_queries}


def integrate_material_recall(ctx, requirements: list, manifest: dict, parsed: dict,
                              report: dict, settings: dict, handled_routes=None) -> dict:
    try:
        options = RecallOptions.model_validate(settings)
    except ValidationError:
        raise ProviderFailure("SEMANTIC_PLANNING_CONFIG_INVALID") from None
    admission = ctx.run_unit("material-recall-admission", {
        "version": RECALL_VERSION, "requirements": requirements,
        "matching_report_sha256": digest(report), "settings": settings, "handled_routes": handled_routes},
        lambda: plan_recall_scope(requirements, report, settings, handled_routes))
    selected = {r["requirement_id"] for r in admission["queries"] if r["status"] == "selected"}
    results = {}
    if selected:
        if not options.provider:
            raise ProviderFailure("SEMANTIC_PROVIDER_REQUIRED")
        chat_config = ChatConfig(**options.provider)
        if options.recorded_source is not None:
            from .semantic_replay import AuthenticatedSemanticProvider
            provider = AuthenticatedSemanticProvider(ctx, chat_config, options.recorded_source)
        else:
            provider = ChatProposalProvider(chat_config)
        value = {"manifest": manifest, "parsed": parsed,
                 "requirements": [r for r in requirements if r["requirement_id"] in selected]}
        result = semantic_probe(ctx, value, provider, matching_report=report, query_limit=options.max_queries)
        results = {r["requirement_id"]: r for r in result["queries"]}
    queries = {q["requirement_id"]: q for q in report["proof_queries"]}
    rows = []
    for admitted in admission["queries"]:
        rid = admitted["requirement_id"]
        query = queries[rid]
        actual = results.get(rid)
        row = {**admitted, "qualification_result": "not_determined", "project_assignment": "not_made",
               "automatic_assembly_allowed": False, "absence_conclusion": "not_established",
               "deterministic_group_ids": list(query["candidate_group_ids"]), "candidates": [],
               "condition_findings": []}
        if actual:
            row.update(actual)
            accepted = [c for c in actual["candidates"] if c["decision"] != "blocked_candidate"]
            row["status"] = ("candidates_pending_original_checks" if accepted else
                             "all_candidates_blocked" if actual["candidates"] else "no_candidate_in_frozen_catalog")
        elif admitted["admission_route"] not in {"outside_material_recall_scope", "skip_explicit_instruction_guard"}:
            row["condition_findings"] = check_declared_credential_conditions(query, query["candidate_group_ids"], report)
        query["semantic_recall_status"] = row["status"]
        query["semantic_recall"] = copy.deepcopy(row)
        rows.append(row)
    counts = {**admission["counts"], "completed": len(results),
              "candidate_queries": sum(r["status"] == "candidates_pending_original_checks" for r in rows),
              "blocked_candidates": sum(c["decision"] == "blocked_candidate" for r in rows for c in r["candidates"])}
    report["report_scope"] = "deterministic_matching_with_semantic_candidate_recall"
    report["semantic_recall"] = {"version": RECALL_VERSION, "semantic_version": SEMANTIC_VERSION,
        "enabled": options.enabled, "status": "completed_with_deferred_queries" if counts["deferred"] else "completed" if options.enabled else "disabled",
        "counts": counts, "queries": rows, "source_scope": "catalog_labels_and_bound_declarations_only",
        "original_content_verification": "not_established_by_semantic_recall", "assembly_candidates_added": 0,
        "response_mode": "authenticated_recorded_responses" if options.recorded_source is not None else "live_provider",
        "new_model_query_count": sum(bool(r.get("model_called")) for r in rows),
        "reused_response_count": sum(bool(r.get("historical_response_reused")) for r in rows),
        "qualification_result": "not_determined"}
    return report


def annotate_material_issues(issues: list, report: dict) -> list:
    """Keep every gap and its ID; append program-authored candidate context."""
    groups = {g["group_id"]: g for g in report["proof_groups"]}
    rows = {r["requirement_id"]: r for r in report.get("semantic_recall", {}).get("queries", [])}
    changes = []
    for issue in issues:
        additions, refs = [], []
        for rid in issue["requirement_refs"]:
            row = rows.get(rid)
            if not row:
                continue
            previous_count = len(additions)
            usable = [c for c in row["candidates"] if c["decision"] != "blocked_candidate"]
            if usable:
                titles = list(dict.fromkeys(groups[c["group_id"]]["title"] for c in usable))
                additions.append("语义补找的待核候选：" + "；".join(titles) + "。目录名称和历史声明仅提供检索线索，原件内容及本条条件仍待核对，原缺项继续保留。")
            elif row["status"] == "no_candidate_in_frozen_catalog":
                additions.append("本次已补查冻结材料目录，未找到可用候选；扫描原件内容尚未读全，不能据此认定公司没有材料。")
            elif row["status"] == "all_candidates_blocked":
                additions.append("语义补找返回的候选未通过程序的来源、类别或人员条件检查，本条仍待补充有效证明。")
            elif row["status"] == "deferred_not_searched":
                additions.append("本条不在本次语义补找的冻结范围内，尚未完成补找，不按已搜无材料处理。")
            for finding in row["condition_findings"]:
                additions.append(finding["reason"])
            if len(additions) != previous_count:
                refs.append(rid)
        if additions:
            before = copy.deepcopy(issue)
            issue["reason"] += "\n" + "\n".join(dict.fromkeys(additions))
            changes.append({"issue_id": issue["issue_id"], "requirement_refs": refs,
                            "before": before, "after": copy.deepcopy(issue),
                            "rule": "append_candidate_context_preserve_missing_and_conditions"})
    return changes
