"""Search frozen declarations and proof packages without deciding eligibility.

Maintained captions establish a candidate document type, not the contents of a
scan.  In particular, a personnel package is linked by its exact native name
and frozen original hash; neither that link nor a historical role appoints a
person to the current project.  The caller still validates source artifacts.
"""
from __future__ import annotations

import re
from collections import Counter


_CAPTIONS = {
    "营业执照": "business_license", "企业营业执照": "business_license",
    "事业单位法人证书": "legal_entity_certificate",
    "安全生产许可证": "safety_production_license",
    "防腐蚀施工资质证书": "anti_corrosion_construction_qualification",
    "中国腐蚀控制资质证书": "corrosion_control_qualification",
    "中国腐蚀控制设计资格证书": "corrosion_control_design_qualification",
    "开户许可证": "bank_account_license", "高新技术企业资质": "high_tech_enterprise",
    "高新技术企业证书": "high_tech_enterprise",
    "质量管理体系认证证书": "quality_management_certificate",
    "环境管理体系认证证书": "environment_management_certificate",
    "职业健康安全管理体系认证证书": "occupational_health_safety_certificate",
}
_PERFORMANCE_TYPES = {
    "项目合同": "performance_contract", "合同主要页": "performance_contract",
    "中标通知书": "performance_award_notice", "项目竣工验收文件": "performance_acceptance",
    "项目交工验收文件": "performance_acceptance", "竣工验收报告": "performance_acceptance",
    "交工验收报告": "performance_acceptance", "订单": "performance_order",
    "用户评价材料": "performance_user_evaluation",
}
_COMMON_CHECKS = ["核对原件内容、证书类别、等级及有效期；附件存在不代表满足本次要求。"]
_PERSON_CHECKS = ["历史人员资料不代表本项目派任；确认拟投入人员及岗位。",
                  "逐页核对人员、证书、等级、有效期及本次要求的网上查询或社保证明。"]
_HISTORY_CHECKS = ["历史业绩声明不能代替合同、中标或验收证明。",
                   "按本条允许的证明路径核对项目范围、签署日期和证明主要页，不以服务日期代替签署日期。",
                   "历史参与人员不代表本项目派任，历史合同额不作本项目报价。"]
_SOURCE_KINDS = {"company_material", "manual_input", "attachment"}


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _normal(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _caption(value: str) -> str:
    value = _normal(value)
    value = re.sub(r"^(?:[（(]\d+[）)]|\d+(?:\.\d+)*[、．.:：]?)", "", value)
    return re.sub(r"(?:原件扫描件|扫描件|复印件)$", "", value).strip("：:。.")


def _field(value):
    if isinstance(value, dict):
        return value.get("value", "")
    return value if isinstance(value, str) else ""


def _record_sources(record: dict) -> list[dict]:
    fields = ("source_id", "source_sha256", "unit_id", "block_id", "locator", "page", "row", "row_sha256")
    return [{key: item[key] for key in fields if key in item} for item in record.get("citations", [])]


def _record_view(record: dict) -> dict:
    values = {}
    for key, value in record.get("fields", {}).items():
        if key == "certificate_declarations":
            values[key] = [{"declaration_id": item.get("declaration_id"), "status": item.get("status"),
                            **{name: _field(item.get(name)) for name in
                               ("certificate_name", "level", "number", "specialty")}}
                           for item in value]
        elif isinstance(value, list):
            values[key] = [_field(item) for item in value]
        else:
            values[key] = _field(value)
    return {"record_id": record["record_id"], "kind": record["kind"], "status": record["status"],
            "declared_fields": values, "citations": _record_sources(record),
            "source_refs": list(dict.fromkeys(item["source_id"] for item in record.get("citations", []))),
            "qualification_result": "not_determined", "project_assignment": "not_made",
            "assurance": record.get("assurance", "historical_declaration_not_independent_proof"),
            "pending_checks": _PERSON_CHECKS[:] if record["kind"] == "personnel_declaration" else _HISTORY_CHECKS[:]}


def _historical_derivation(binding: dict, selection: dict | None, title: str, count: int,
                           records: list[dict], source_map: dict) -> dict | None:
    """A frozen declared split relation permits display, never qualification.

    Agent visual review is intentionally separate from user verification. The
    parent's PDF page interval remains an explicitly unverified declaration.
    """
    child = source_map.get(binding.get("child_source_id"), {})
    parent = source_map.get(binding.get("parent_source_id"), {})
    candidates = [record for record in records if record["record_id"] == binding.get("record_id") and
                  record["kind"] == "historical_performance" and record["status"] == "usable_provisional"]
    if len(candidates) != 1 or selection is None:
        return None
    record = candidates[0]
    name = _field(record.get("fields", {}).get("project_name"))
    visual = binding.get("visual_review", {})
    pages = list(range(1, count + 1))
    reviewed = visual.get("physical_pages", [])
    citations = [item for item in _record_sources(record) if
                 item.get("source_id") == binding.get("parent_source_id") and
                 item.get("source_sha256") == binding.get("parent_source_sha256")]
    if not (binding.get("relation") == "user_authorized_bid_split" and binding.get("derivation_id") and
            child.get("provenance") == "derived_from_bid" and child.get("kind") == "company_material" and
            parent.get("kind") == "company_material" and _sha(binding.get("child_source_sha256")) and
            _sha(binding.get("parent_source_sha256")) and
            child.get("sha256") == binding.get("child_source_sha256") == selection.get("source_sha256") and
            parent.get("sha256") == binding.get("parent_source_sha256") and citations and
            selection.get("source_id") == binding.get("child_source_id") and
            selection.get("selection_id") == binding.get("selection_id") and selection.get("format") == "pdf_pages" and
            selection.get("pages") == binding.get("physical_pages") == pages and
            visual.get("method") == "agent_visual_review_of_local_render" and
            visual.get("status") == "candidate_identity_and_boundary_reviewed" and
            visual.get("project_title") == name == title and name and
            isinstance(reviewed, list) and len(set(reviewed)) == len(reviewed) and
            {1, count}.issubset(reviewed) and set(reviewed).issubset(pages) and visual.get("observations") and
            binding.get("parent_locator", {}).get("status") == "declared_unverified_parent_page_range" and
            binding.get("parent_locator", {}).get("text") and
            binding.get("qualification_result") == "not_determined" and
            binding.get("project_assignment") == "not_made"):
        return None
    return {"record_id": record["record_id"], "kind": record["kind"], "derivation_id": binding["derivation_id"],
            "basis": "frozen_declared_bid_split_and_agent_visual_project_mapping", "citations": citations,
            "child_source_id": binding["child_source_id"], "child_source_sha256": binding["child_source_sha256"],
            "parent_locator_status": binding["parent_locator"]["status"]}


def _proof_groups(attachments: list[dict], records: list[dict], sources: list[dict] | None,
                  selections: list[dict] | None, derivations: list[dict] | None = None) -> list[dict]:
    grouped = {}
    for attachment in attachments:
        key = attachment.get("bundle_id") or attachment["attachment_id"]
        grouped.setdefault(key, []).append(attachment)
    source_map = {item["source_id"]: item for item in sources or []}
    selection_map = {item["selection_id"]: item for item in selections or []}
    output = []
    for key, pages in sorted(grouped.items()):
        pages = sorted(pages, key=lambda item: (item.get("page_index", 1), item["attachment_id"]))
        refs = list(dict.fromkeys(ref for page in pages for ref in page.get("source_refs", [])))
        title = pages[0]["title"]
        counts = {page.get("page_count", 1) for page in pages}
        complete = (len(counts) == 1 and len(pages) == next(iter(counts)) and
                    [page.get("page_index", 1) for page in pages] == list(range(1, len(pages) + 1)) and
                    len({page["attachment_id"] for page in pages}) == len(pages) and
                    all(page["title"] == title for page in pages))
        assets_complete = all(page.get("path") and _sha(page.get("sha256")) and page.get("source_refs") for page in pages)
        bound = bool(refs and sources is not None and all(
            ref in source_map and source_map[ref].get("kind") in _SOURCE_KINDS and
            _sha(source_map[ref].get("sha256")) for ref in refs))
        selection = selection_map.get(key)
        origin = None
        if selection:
            original = source_map.get(selection["source_id"], {})
            count = len(selection.get("pages", [])) + len(selection.get("package_parts", []))
            bound = bool(bound and original.get("sha256") == selection["source_sha256"] and
                         original.get("kind") in _SOURCE_KINDS and count == len(pages) and
                         selection["title"] == title and all(
                             source_map[ref].get("sha256") == selection["source_sha256"] for ref in refs))
            origin = {name: selection[name] for name in
                      ("selection_id", "source_id", "source_sha256", "format", "pages", "package_parts") if name in selection}
        elif selections is not None:
            bound = False
        original_hashes = {source_map[ref].get("sha256") for ref in refs if ref in source_map}
        linked = []
        history_kind = None
        for record in records:
            if record["status"] != "usable_provisional":
                continue
            person = record["kind"] == "personnel_declaration"
            name = _normal(_field(record.get("fields", {}).get("name" if person else "project_name")))
            if not name:
                continue
            identity_matches = _normal(title) == name
            suffix_kind = None
            if not person:
                for suffix, kind in _PERFORMANCE_TYPES.items():
                    if _normal(title) == name + suffix:
                        identity_matches, suffix_kind = True, kind
                if identity_matches and name.endswith("合同"):
                    suffix_kind = "performance_contract"
            if not identity_matches:
                continue
            lineage = [item for item in _record_sources(record) if item.get("source_sha256") in original_hashes]
            if bound and lineage:
                history_kind = suffix_kind or history_kind
                linked.append({"record_id": record["record_id"], "kind": record["kind"],
                               "basis": "exact_native_name_and_same_frozen_original_hash" if person else
                               "exact_native_project_title_and_same_frozen_original_hash",
                               "citations": lineage})
        proof_kind = _CAPTIONS.get(_caption(title)) or _PERFORMANCE_TYPES.get(_caption(title), "unclassified_proof")
        if linked and all(link["kind"] == "personnel_declaration" for link in linked):
            proof_kind = "personnel_proof_package"
        elif history_kind:
            proof_kind = history_kind
        declared_derivations = [item for item in derivations or [] if item.get("selection_id") == key]
        derived_links = [_historical_derivation(item, selection, title, len(pages), records, source_map)
                         for item in declared_derivations]
        derivation_valid = len(derived_links) == 1 and derived_links[0] is not None
        if derivation_valid and bound:
            linked = [derived_links[0]]
            proof_kind = "historical_performance_package"
        problems = []
        if declared_derivations and not derivation_valid:
            problems.append("派生资料与历史业绩声明的冻结来源、项目或完整物理页绑定不成立，不可装配。")
        if not complete:
            problems.append("证明包页码或页数不完整，不可拆页冒充完整证明。")
        if not bound:
            problems.append("证明包尚未绑定完整冻结来源与明确选择范围。")
        if not assets_complete:
            problems.append("证明页缺少冻结文件路径、hash或来源，不能装配。")
        if proof_kind == "unclassified_proof":
            problems.append("证明类别未可靠识别，不能按文件名中的宽泛关键词归入具体要求。")
        if len(linked) > 1:
            problems.append("同名历史记录不唯一，须明确人员或项目身份后再装配。")
        pending = (_PERSON_CHECKS if proof_kind == "personnel_proof_package" else _HISTORY_CHECKS if
                   proof_kind.startswith("performance_") or proof_kind == "historical_performance_package" else _COMMON_CHECKS)
        if proof_kind == "historical_performance_package":
            pending = [*pending, "本包是人工标书选取的历史证明；未认定合同全文或本次所需主要页逐页完整。",
                       "本次agent视觉查看不是用户核验；父文档页范围仍为未核实的登记关系。"]
        output.append({"group_id": key, "title": title, "proof_kind": proof_kind,
                       "attachment_ids": [page["attachment_id"] for page in pages], "page_count": len(pages),
                       "source_refs": refs, "origin": origin, "record_links": linked,
                       "bundle_complete": complete, "source_bound": bound,
                       "can_assemble": complete and bound and assets_complete and proof_kind != "unclassified_proof" and len(linked) <= 1
                       and (not declared_derivations or derivation_valid),
                       "status": "assemblable" if not problems else "applicability_review",
                       "derivation_proofs": declared_derivations,
                       "pending_checks": [*problems, *pending], "qualification_result": "not_determined",
                       "project_assignment": "not_made", "asset_validation": "frozen_manifest_metadata_only"})
    return output


def _proof_request(requirement: dict) -> dict | None:
    text = _normal(requirement["text"])
    heading = _caption(requirement["text"])
    result = {"rule": "explicit_proof_type_search", "kinds": [], "alternatives": []}
    if re.search(r"(?:无需|无须|不得|禁止|不需|不必|不要求)(?:再|另行|重复)?(?:提供|提交|附)", text):
        result["rule"] = "negative_proof_instruction_not_searched"
        return result
    if heading in {"资质材料", "公司资质材料", "企业资质材料"}:
        result.update(rule="explicit_qualification_materials_heading", kinds=sorted(set(_CAPTIONS.values())))
    elif heading in {"人员要求材料", "人员材料", "项目人员资料"}:
        result.update(rule="explicit_personnel_materials_heading", kinds=["personnel_proof_package"])
    elif "社会保险" in text or "社保证明" in text:
        result["kinds"] = ["social_insurance_proof"]
    elif "裁判文书网" in text:
        result["kinds"] = ["court_query_screenshot"]
    elif "业绩" in text and requirement.get("response_mode") in {"attachment", "table", "factual_response"}:
        result["rule"] = "performance_proof_paths_require_separate_review"
        paths = []
        if "合同" in text:
            paths.append({"proof_kind": "performance_contract", "date_basis": "contract_signature_date",
                          "required_contents": ["合同首页", "服务或承包范围页", "合同签署页"]})
        if "中标通知书" in text:
            paths.append({"proof_kind": "performance_award_notice", "date_basis": "award_issue_date"})
        if re.search(r"竣工|交工|验收", text):
            paths.append({"proof_kind": "performance_acceptance", "date_basis": "acceptance_signature_date",
                          "required_contents": ["验收单位及人员", "验收内容", "验收结论", "验收时间"]})
        for token, kind in (("订单", "performance_order"), ("用户评价", "performance_user_evaluation")):
            if token in text:
                paths.append({"proof_kind": kind, "qualification_use": "not_established_by_compilation_heading"})
        result.update(kinds=[item["proof_kind"] for item in paths], alternatives=paths,
                      alternatives_operator="or" if "或" in text or "之一" in text else "not_determined",
                      historical_package_candidates=True)
        if (re.match(r"(?:\d+(?:\.\d+)*[、.．]?)?业绩材料[:：]", text) or
                re.match(r"(?:[（(]\d+[）)])?业绩[（(]包含有效业绩证明文件汇总", text)):
            result["rule"] = "explicit_historical_performance_compilation"
    elif re.search(r"承诺|说明.*关系", text) and requirement.get("response_mode") == "attachment":
        result["kinds"] = ["current_signed_commitment"]
    elif "营业执照或事业单位法人证书" in text:
        result.update(kinds=["business_license", "legal_entity_certificate"],
                      alternatives_operator="or", alternatives=[{"proof_kind": "business_license"},
                                                              {"proof_kind": "legal_entity_certificate"}])
    elif "中国腐蚀控制资质证书" in text:
        result["kinds"] = ["corrosion_control_qualification"]
    elif re.search(r"项目负责人|项目技术负责人|项目组人员|职称|AMPP|CP[234]", text, re.I) and (
            re.search(r"证书|工程师|人员要求", text) and requirement.get("response_mode") in {"attachment", "factual_response"}):
        result["kinds"] = ["personnel_proof_package"]
    elif requirement.get("response_mode") == "attachment":
        # A closed single-document instruction can use its full maintained
        # name. Merely mentioning a licence in a financial or status clause
        # does not request that licence as the proof for the whole clause.
        for caption, kind in _CAPTIONS.items():
            if re.fullmatch(r"(?:响应人|投标人|供应商|企业|公司)?(?:须|应|需|必须)?"
                            r"(?:提供|提交|附|附上|具有|具备|持有)(?:有效)?" + re.escape(caption) +
                            r"(?:原件)?(?:扫描件|复印件)?[。.]?", text):
                result["kinds"] = [kind]
                break
        if not result["kinds"]:
            result.update(rule="unsupported_proof_expression_not_searched")
    else:
        return None
    return result


def _reading_scope(parsed: dict | None) -> dict:
    if parsed is None:
        return {"status": "not_supplied", "unread_unit_count": None}
    units = [unit for doc in parsed.get("documents", []) if doc.get("role") != "tender"
             for unit in doc.get("units", [])]
    statuses = Counter(unit.get("coverage", {}).get("status", "unknown") for unit in units)
    unread = sum(count for status, count in statuses.items() if status not in {"read", "empty"})
    status = "unparsed" if not units or not statuses.get("read") else "partially_parsed" if unread else "parsed_scope_only"
    return {"status": status, "unread_unit_count": unread,
            "unit_status_counts": dict(statuses), "whole_company_search_completed": False}


def _person_family_candidate(group: dict, request: dict, text: str, records: list[dict]) -> bool:
    """A declared certificate family is a lead, never a level comparison."""
    if group["proof_kind"] != "personnel_proof_package" or request["rule"] == "explicit_personnel_materials_heading":
        return True
    linked = {link["record_id"] for link in group["record_links"]}
    names = []
    for record in records:
        if record["record_id"] in linked:
            fields = record.get("fields", {})
            names.extend(_field(item) for item in fields.get("titles", []))
            names.extend(_field(item.get("certificate_name")) for item in fields.get("certificate_declarations", [])
                         if item.get("status") == "usable_provisional")
    if re.search(r"CP[234]", text, re.I):
        return any(re.search(r"CP[234]", name, re.I) for name in names)
    if "高级工程师" in text or "中级工程师" in text:
        return any(term in text and any(term in name for name in names) for term in ("高级工程师", "中级工程师"))
    return False


def material_candidate_report(requirements: list[dict], records: list[dict], *,
                              attachments: list[dict] | None = None, sources: list[dict] | None = None,
                              parsed: dict | None = None, evidence_selections: list[dict] | None = None,
                              proof_derivations: list[dict] | None = None) -> dict:
    """The search scope is the frozen prepared records, never the entire company."""
    queries = []
    for requirement in requirements:
        text = requirement["text"]
        kinds = []
        if re.search(r"人员|项目负责人|项目经理|职称|检测员|工程师", text):
            kinds.append("personnel_declaration")
        if re.search(r"业绩|类似项目|类似合同|已完成项目|历史项目", text):
            kinds.append("historical_performance")
        for kind in kinds:
            selected = [record for record in records if record["kind"] == kind]
            usable = [record["record_id"] for record in selected if record["status"] == "usable_provisional"]
            queries.append({"requirement_id": requirement["requirement_id"], "kind": kind,
                            "status": "declarations_require_applicability_review" if usable else "no_usable_prepared_declarations",
                            "candidate_record_ids": usable,
                            "excluded_records": [{"record_id": record["record_id"], "status": record["status"]}
                                                 for record in selected if record["status"] != "usable_provisional"],
                            "qualification_result": "not_determined", "project_assignment": "not_made"})
    report = {"scope": "frozen_prepared_declarations_only", "counts": dict(Counter(record["kind"] for record in records)),
              "queries": queries, "unparsed_catalog_is_not_evidence_of_company_absence": True}
    # Preserve the small original API for consumers that only supplied records.
    if attachments is None and sources is None and parsed is None and evidence_selections is None and proof_derivations is None:
        return report
    groups = _proof_groups(attachments or [], records, sources, evidence_selections, proof_derivations)
    reading = _reading_scope(parsed)
    proof_queries = []
    assembly = []
    for requirement in requirements:
        request = _proof_request(requirement)
        if request is None:
            continue
        selected = [group for group in groups if (group["proof_kind"] in request["kinds"] or
                    (request.get("historical_package_candidates") and group["proof_kind"] == "historical_performance_package")) and
                    _person_family_candidate(group, request, requirement["text"], records)]
        can_assemble = [group for group in selected if group["can_assemble"] and
                        not group["proof_kind"].startswith("performance_") and
                        (group["proof_kind"] != "historical_performance_package" or
                         request["rule"] == "explicit_historical_performance_compilation")]
        supported = bool(request["kinds"] or request.get("historical_package_candidates"))
        status = ("not_searched" if attachments is None or not supported else
                  "assemblable" if can_assemble else "applicability_review" if selected else "no_candidate")
        pending = (_PERSON_CHECKS if "personnel_proof_package" in request["kinds"] else
                   _HISTORY_CHECKS if request.get("historical_package_candidates") else _COMMON_CHECKS)
        row = {"requirement_id": requirement["requirement_id"], "status": status, "request": request,
               "candidate_group_ids": [group["group_id"] for group in selected],
               "assemblable_group_ids": [group["group_id"] for group in can_assemble],
               "search_status": "searched_frozen_prepared_attachments" if attachments is not None and supported else "not_searched",
               "catalog_status": reading["status"], "unread_scope_present": bool(reading["unread_unit_count"]),
               "pending_checks": pending[:], "qualification_result": "not_determined", "project_assignment": "not_made",
               "absence_conclusion": "not_established"}
        if "corrosion_control_qualification" in request["kinds"]:
            row["distinct_related_group_ids"] = [group["group_id"] for group in groups if group["proof_kind"] in
                {"corrosion_control_design_qualification", "anti_corrosion_construction_qualification"}]
        proof_queries.append(row)
        # A performance path requires project identity and main-page validation;
        # a title by itself cannot identify a complete, applicable project proof.
        for group in can_assemble:
            if group["proof_kind"].startswith("performance_"):
                continue
            assembly.append({"requirement_id": requirement["requirement_id"], "group_id": group["group_id"],
                             "attachment_ids": group["attachment_ids"], "source_refs": group["source_refs"],
                             "basis": {"rule": request["rule"], "maintained_title": group["title"],
                                       "proof_kind": group["proof_kind"], "origin": group["origin"],
                                       "record_links": group["record_links"], "proof_derivations": group["derivation_proofs"]},
                             "pending_checks": list(dict.fromkeys([*group["pending_checks"], *pending])),
                             "qualification_result": "not_determined", "project_assignment": "not_made",
                             "presentation": "historical_proof_candidate_pending_review",
                             "assembly_scope": "historical_supplement" if request["rule"] in {
                                 "explicit_qualification_materials_heading", "explicit_personnel_materials_heading",
                                 "explicit_historical_performance_compilation"}
                             else "requested_proof_candidate"})
    report.update(schema_version="material.candidates.v2", scope="frozen_prepared_declarations_and_proof_packages_only",
                  reading_scope=reading, record_candidates=[_record_view(record) for record in records],
                  proof_groups=groups, proof_queries=proof_queries, assembly_candidates=assembly,
                  proof_counts={"pages": len(attachments or []), "groups": len(groups),
                                "assemblable_groups": sum(group["can_assemble"] for group in groups)})
    return report
