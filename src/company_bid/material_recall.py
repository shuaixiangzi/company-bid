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
    read_selected_originals: bool = False
    original_ocr: dict | None = None
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
    if options.original_ocr is not None and not options.read_selected_originals:
        raise ProviderFailure("MATERIAL_OCR_SELECTED_ORIGINALS_REQUIRED")
    if options.original_ocr is not None and options.original_ocr.get("engine") != "deepseek_ocr":
        raise ProviderFailure("MATERIAL_OCR_DEEPSEEK_REQUIRED_NO_LOCAL_FALLBACK")
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


def _dictionary_stage(query: dict, row: dict) -> str:
    search_status = row.get("catalog_stage_dictionary_search_status", query.get("search_status"))
    candidate_ids = row.get("catalog_stage_dictionary_candidate_group_ids", query.get("candidate_group_ids"))
    if search_status == "not_searched":
        return "词典阶段：表达未支持，未检索"
    if candidate_ids:
        return "词典阶段：有候选，条件待核"
    if search_status == "searched_frozen_prepared_attachments":
        return "词典阶段：冻结范围内未命中"
    return "词典阶段：检索状态见匹配记录"


def _catalog_stage(row: dict, groups: dict) -> str:
    usable = [c for c in row["candidates"] if c["decision"] != "blocked_candidate"]
    if usable:
        titles = list(dict.fromkeys(groups[c["group_id"]]["title"] for c in usable))
        result = "语义目录补查：语义补找的待核候选：" + "；".join(titles)
    else:
        result = "语义目录补查：" + {
            "candidates_pending_original_checks": "历史响应曾给出候选，读取后检查另列",
            "no_candidate_in_frozen_catalog": "已补查冻结目录，未找到可用候选",
            "all_candidates_blocked": "候选未通过来源、类别或人员检查",
            "deferred_not_searched": "不在本次冻结范围，尚未完成补找",
            "disabled_not_searched": "本次未启用，未补查",
            "deterministic_candidate_available": "词典已有候选，未另补查",
            "requires_current_signed_document": "需本次签署文件，未补查历史材料",
            "existing_performance_candidates_require_original_checks": "已有业绩候选，转原件核对",
            "outside_material_recall_scope": "不属材料补查范围，沿用原办理要求",
            "skip_explicit_instruction_guard": "按原文明示处理，未补查",
        }.get(row.get("catalog_stage_status", row["status"]), "未执行目录补查，原因见匹配记录")
    if row.get("historical_response_reused"):
        result += "（复用历史响应）"
    return result


def _numeric_review_scope(content_index: dict, query: dict, row: dict) -> dict:
    """Bind OCR warnings to this requirement's candidate groups, never the whole library."""
    candidate_ids = set(query.get("candidate_group_ids", []))
    candidate_ids.update(row.get("deterministic_group_ids", []))
    candidate_ids.update(row.get("catalog_stage_dictionary_candidate_group_ids", []))
    candidate_ids.update(candidate["group_id"] for candidate in row.get("candidates", []))
    units, linked_groups = {}, set()
    for group in content_index.get("groups", []):
        if group["group_id"] not in candidate_ids:
            continue
        for unit in group.get("units", []):
            numeric = unit.get("numeric_validation")
            if not isinstance(numeric, dict):
                continue
            linked_groups.add(group["group_id"])
            # A selected image can belong to multiple material groups. Count its
            # authenticated unit once while retaining every candidate binding.
            identity = (unit["source_id"], unit["unit_id"], unit.get("ocr_image_sha256"))
            units[identity] = {key: copy.deepcopy(unit.get(key)) for key in
                ("source_id", "unit_id", "target", "ocr_image_sha256", "ocr_unit_id", "ocr_output_sha256")}
            units[identity].update(status=numeric.get("status", "pending_source_check"),
                numeric_validation_sha256=digest(numeric),
                finding_codes=[finding["code"] for finding in numeric.get("findings", [])])
    codes = {}
    for unit in units.values():
        for code in unit["finding_codes"]:
            codes[code] = codes.get(code, 0) + 1
    return {"group_ids": sorted(linked_groups), "units": list(units.values()),
            "selected_unit_count": len(units),
            "pending_unit_count": sum(unit["status"] not in {"no_numeric_tokens", "no_numeric_content"}
                                      for unit in units.values()),
            "finding_count": sum(codes.values()), "finding_codes": codes,
            "scope": "requirement_candidate_groups_only", "automatic_numeric_adoption": False}


def annotate_material_issues(issues: list, report: dict) -> list:
    """Append scoped stage results; retain original gaps and response provenance."""
    groups = {g["group_id"]: g for g in report["proof_groups"]}
    queries = {q["requirement_id"]: q for q in report.get("proof_queries", [])}
    rows = {r["requirement_id"]: r for r in report.get("semantic_recall", {}).get("queries", [])}
    annotated = {"candidates_pending_original_checks", "no_candidate_in_frozen_catalog",
                 "all_candidates_blocked", "deferred_not_searched", "disabled_not_searched"}
    changes = []
    for issue in issues:
        relevant = [(rid, rows[rid]) for rid in issue["requirement_refs"] if rid in rows]
        numeric_scopes = {rid: _numeric_review_scope(report.get("original_content_index") or {},
                                                     queries.get(rid, {}), row) for rid, row in relevant}
        if not any(row.get("catalog_stage_status", row["status"]) in annotated or row["condition_findings"]
                   or numeric_scopes[rid]["pending_unit_count"] or numeric_scopes[rid]["finding_count"]
                   for rid, row in relevant):
            continue
        additions, contexts = [], []
        for rid, row in relevant:
            query = queries.get(rid, {})
            original = query.get("request", {}).get("original_text", "")
            short = " ".join(original.split())
            short = short[:36] + ("…" if len(short) > 36 else "")
            label = rid + ("｜" + short if short else "")
            line = f"【{label}】{_dictionary_stage(query, row)}；{_catalog_stage(row, groups)}"
            if report.get("original_content_index"):
                line += "；原件读取后匹配：" + ("有候选，条件待核" if query.get("candidate_group_ids") else "未形成候选")
                if row.get("catalog_stage_status") != row["status"] and row["status"] == "all_candidates_blocked":
                    line += "；读取后候选复查：未通过来源、类别或人员检查"
            additions.append(line + "。")
            for finding in row["condition_findings"]:
                # The preceding requirement ID binds the unchanged condition text.
                additions.append("条件核对：" + finding["reason"])
            numeric = numeric_scopes[rid]
            if numeric["pending_unit_count"] or numeric["finding_count"]:
                additions.append(f"本条候选证明的 OCR 核对：已关联{numeric['selected_unit_count']}页/图，"
                                 f"{numeric['pending_unit_count']}项的数字、日期或单位仍需原图核对。")
                if numeric["finding_count"]:
                    labels = {"OCR_DATE_INVALID": "日期不合法", "OCR_NUMERIC_GLYPH_AMBIGUOUS": "数字字形有歧义",
                              "OCR_TABLE_ARITHMETIC_INCONSISTENT": "表内数量、单价与金额不一致"}
                    findings = "、".join(f"{labels.get(code, '其他数字核对疑点')}{amount}处"
                                         for code, amount in sorted(numeric["finding_codes"].items()))
                    additions.append(f"本条候选证明的 OCR 数字校验发现{numeric['finding_count']}处疑点（{findings}）；"
                                     "对应页/图及来源保留在逐条诊断中，需回到原图核对，不自动改数或认定原件有错。")
            contexts.append({"requirement_id": rid, "original_requirement": original,
                             "original_conditions": copy.deepcopy(query.get("request", {}).get("conditions", {})),
                             "deterministic_search_status": query.get("search_status"),
                             "deterministic_read_scope_status": query.get("read_scope_status"),
                             "semantic_recall": copy.deepcopy(row), "ocr_numeric_review": numeric})
        content_index = report.get("original_content_index")
        if content_index:
            coverage = content_index["coverage"]
            count = coverage["selected_unit_count"]
            read = coverage["status_counts"].get("read", 0)
            unread = coverage["unread_unit_count"]
            unbound = coverage.get("unbound_group_count", 0)
            missing_scope = f"，另{unbound}组范围未绑定" if unbound else ""
            empty = coverage["status_counts"].get("empty", 0)
            empty_scope = f"，{empty}项读取后无文字" if empty else ""
            if "ocr_processed_unit_count" in coverage:
                transcribed = coverage["ocr_transcribed_unverified_count"]
                pending = coverage["pending_ocr_unit_count"]
                crops = coverage["ocr_transcribed_crop_count"]
                engine_name = "DeepSeek OCR" if coverage.get("ocr_engine", {}).get("engine") == "deepseek_ocr" else "历史OCR"
                additions.append(f"原件读取：目录索引内全部所选{count}页/图，{transcribed}项已有{engine_name}转录文字（含{crops}项裁剪区域），{pending}项仍未取得可用识别文字；字段准确性、手写内容和表格关系待核。机器转录不等于原件完整读取或资格核验。")
            else:
                additions.append(f"原件读取：目录索引内所选{count}页/图，{read}项读取完整，{unread}项未读全或不支持读取{empty_scope}{missing_scope}；正文读取不等于资格核验。")
            for context in contexts:
                query = queries.get(context["requirement_id"], {})
                context["current_content_matching"] = {
                    "candidate_group_ids": copy.deepcopy(query.get("candidate_group_ids", [])),
                    "read_scope_status": query.get("read_scope_status"),
                    "unread_scope_present": query.get("unread_scope_present")}
            additions.append("目录补查仅使用名称及同源历史声明，本次目录补查未检查原件内容；原件读取另列如上，适用条件待核，原缺项继续保留。未检索或未找到候选均不证明公司没有材料。")
        else:
            additions.append("目录补查仅使用名称及同源历史声明，本次目录补查未检查原件内容；原件及适用条件待核，原缺项继续保留。未检索或未找到候选均不证明公司没有材料。")
        before = copy.deepcopy(issue)
        issue["reason"] += "\n" + "\n".join(additions)
        changes.append({"issue_id": issue["issue_id"], "requirement_refs": [rid for rid, _ in relevant],
                        "before": before, "after": copy.deepcopy(issue),
                        "requirement_contexts": contexts,
                        "response_mode": report.get("semantic_recall", {}).get("response_mode"),
                        "original_content_index_sha256": digest(content_index) if content_index else None,
                        "rule": "append_scoped_stage_context_preserve_missing_conditions_and_provenance"})
    return changes
