"""Search frozen declarations and proof packages without deciding eligibility.

Maintained captions establish a candidate document type, not the contents of a
scan.  In particular, a personnel package is linked by its exact native name
and frozen original hash; neither that link nor a historical role appoints a
person to the current project.  The caller still validates source artifacts.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import PureWindowsPath


MATERIAL_MATCHING_VERSION = "material-matching.v3"

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
# Equivalence is deliberately narrower than semantic relevance. ISO 9001 is
# a related standard that must still be checked on the actual certificate.
_ALIASES = {
    "工商营业执照": "business_license", "安许证": "safety_production_license",
    "高企证书": "high_tech_enterprise", "质量体系证书": "quality_management_certificate",
    "质量体系认证证书": "quality_management_certificate",
    "质量管理体系证书": "quality_management_certificate",
}
_TERMS = {**_CAPTIONS, **_ALIASES}
_STANDARD_TERMS = {"ISO9001": "quality_management_certificate", "ISO14001": "environment_management_certificate",
                   "ISO45001": "occupational_health_safety_certificate"}
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
_BROAD_ASSEMBLY_RULES = {"explicit_qualification_materials_heading", "explicit_personnel_materials_heading",
                         "explicit_historical_performance_compilation"}
_PROOF_LABELS = {kind: caption for caption, kind in reversed(list(_CAPTIONS.items()))}
_PROOF_LABELS.update({"personnel_proof_package": "人员证明资料", "performance_contract": "业绩合同主要页",
                      "performance_award_notice": "中标通知书", "performance_acceptance": "项目验收文件",
                      "social_insurance_proof": "社会保险证明"})


def _term_hits(value: str) -> list[dict]:
    text = _normal(value).upper()
    hits = [{"term": term, "proof_kind": kind, "relation": "equivalent_alias" if term in _ALIASES else "maintained_type"}
            for term, kind in _TERMS.items() if term.upper() in text]
    hits.extend({"term": term, "proof_kind": kind, "relation": "related_standard_requires_validation"}
                for term, kind in _STANDARD_TERMS.items() if re.search(re.escape(term) + r"(?!\d)", text))
    return hits


def _proof_type_operators(text: str, hits: list[dict]) -> list[str]:
    """Read connectors between requested proof types, excluding other clauses.

    Full requirement logic remains in conditions.boolean_tokens. The choice
    between licence documents must not inherit the earlier organisation-type
    alternatives or the rights-and-capacity conjunction in the same sentence.
    """
    spans = sorted({(match.start(), match.end(), hit["proof_kind"])
                    for hit in hits for match in re.finditer(re.escape(hit["term"]), text, re.I)},
                   key=lambda item: (item[0], -item[1]))
    distinct = []
    for span in spans:
        if distinct and span[0] < distinct[-1][1]:
            continue  # Nested equivalent labels are one document mention.
        distinct.append(span)
    operators = []
    explicit_and = False
    for left, right in zip(distinct, distinct[1:]):
        if left[2] == right[2]:
            continue
        connector = text[left[1]:right[0]]
        tokens = re.findall(r"或者|并且|以及|及(?!以上|以下)|且|和|与|或|之一|同时", connector)
        explicit_and = explicit_and or any(token in {"并且", "以及", "及", "且", "和", "与", "同时"} for token in tokens)
        operators.extend(tokens or (["及"] if re.search(r"[、，,；;]", connector) else []))
    if len({span[2] for span in distinct}) > 1:
        tail = text[distinct[-1][1]:]
        if re.match(r"(?:等(?:相关)?(?:证明|材料|文件))?(?:之一|[，,；;]?(?:上述|以上)(?:两|二|2)种(?:证明|材料|文件)任选其一)", tail):
            # A trailing alternative qualifies the listed proofs, not their
            # separator. Contradictory explicit AND wording remains mixed.
            operators = [*operators, "或"] if explicit_and else ["或"]
    return operators


def _role_exclusions(value: str, *, document_label: bool = False) -> list[str]:
    """Exclude proof mentions in applications, instructions and absence notices."""
    text = _normal(value)
    reasons = []
    role_pattern = r"申请(?:说明|流程|表|指南|材料|办理|记录|文件)|办理(?:申请|说明|流程|指南|记录)|办证指南|证书申请|样本|模板"
    # A label can establish the document role. In read certificate contents,
    # application notes and references to forms do not change its role.
    declared_role = re.search(r"(?:本文件|本材料|本页|本文|该文件|此件)(?:仅|只)?(?:是|为|属于)[^。；;，,]{0,20}(?:" + role_pattern + r")", text)
    if ((document_label and re.search(role_pattern, text)) or declared_role or
            re.search(r"(?:并非|不是|非)(?:证照|证书|证明|原件)|仅供(?:参考|申请)", text)):
        reasons.append("application_explanation_or_non_proof_role")
    if re.search(r"(?:尚未|未曾|未能|没有|未|暂未)(?:取得|获得|获取|提供|提交|上传|附上|持有|出具)[^。；;，,]{0,16}(?:证书|执照|安许证|许可证|证明|合同)|(?:尚无|暂无|缺少|待补)[^。；;，,]{0,12}(?:证书|执照|安许证|许可证|证明|合同)|(?:证书|执照|许可证|证明|合同)[^。；;，,]{0,8}(?:待补|未提供|未取得|缺失)", text):
        reasons.append("proof_not_provided_or_not_obtained")
    return reasons


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


def _direct_image_origin(group_kind: str, pages: list[dict], refs: list[str], source_map: dict) -> dict | None:
    """A standalone original image needs no synthetic page-selection record.

    A cropped/rendered page differs from the original file path or hash and
    cannot use this whole-file binding to borrow its parent document's scope.
    """
    if group_kind != "single" or len(pages) != 1 or len(refs) != 1:
        return None
    page, original = pages[0], source_map.get(refs[0], {})
    if not (original.get("kind") in {"company_material", "attachment"} and page.get("page_count", 1) == 1 and
            page.get("page_index", 1) == 1 and page.get("path") and original.get("path") and
            _sha(page.get("sha256")) and page["sha256"] == original.get("sha256")):
        return None
    path, original_path = PureWindowsPath(str(page["path"])), PureWindowsPath(str(original["path"]))
    if path != original_path or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
        return None
    return {"source_id": refs[0], "source_sha256": original["sha256"], "format": "whole_image",
            "binding": "standalone_complete_original_image_same_path_and_hash", "pages": [1], "package_parts": []}


def _proof_groups(attachments: list[dict], records: list[dict], sources: list[dict] | None,
                  selections: list[dict] | None, derivations: list[dict] | None = None) -> list[dict]:
    grouped = {}
    for attachment in attachments:
        key = ("bundle", attachment["bundle_id"]) if attachment.get("bundle_id") else ("single", attachment["attachment_id"])
        grouped.setdefault(key, []).append(attachment)
    native_counts = Counter(key[1] for key in grouped)
    reserved_ids = {key[1] for key in grouped}
    used_ids = set()
    source_map = {item["source_id"]: item for item in sources or []}
    selection_map = {item["selection_id"]: item for item in selections or []}
    output = []
    for (group_kind, native_key), pages in sorted(grouped.items()):
        key = native_key if native_counts[native_key] == 1 else f"{group_kind}:{native_key}"
        if native_counts[native_key] > 1:
            while key in reserved_ids or key in used_ids:
                key = ":" + key
        used_ids.add(key)
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
        # A selection id names a bundle. A colliding standalone attachment
        # cannot borrow that bundle's selected original pages.
        selection = selection_map.get(native_key) if group_kind == "bundle" or native_counts[native_key] == 1 else None
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
        elif native_key not in selection_map and (direct_origin := _direct_image_origin(group_kind, pages, refs, source_map)):
            origin = direct_origin
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
        proof_kind = _TERMS.get(_caption(title)) or _PERFORMANCE_TYPES.get(_caption(title), "unclassified_proof")
        if linked and all(link["kind"] == "personnel_declaration" for link in linked):
            proof_kind = "personnel_proof_package"
        elif history_kind:
            proof_kind = history_kind
        declared_derivations = [item for item in derivations or [] if item.get("selection_id") == native_key and selection]
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
        role_exclusions = list(dict.fromkeys(reason for page in pages for reason in
            _role_exclusions(" ".join(str(page.get(field, "")) for field in ("title", "description", "document_role")), document_label=True)))
        if role_exclusions:
            problems.append("资料包含申请、说明或未提供声明，不能作为所请求证明装配。")
        pending = (_PERSON_CHECKS if proof_kind == "personnel_proof_package" else _HISTORY_CHECKS if
                   proof_kind.startswith("performance_") or proof_kind == "historical_performance_package" else _COMMON_CHECKS)
        if proof_kind == "historical_performance_package":
            pending = [*pending, "本包是人工标书选取的历史证明；未认定合同全文或本次所需主要页逐页完整。",
                       "本次agent视觉查看不是用户核验；父文档页范围仍为未核实的登记关系。"]
        output.append({"group_id": key, "group_kind": group_kind, "native_group_id": native_key,
                       "title": title, "proof_kind": proof_kind,
                       "attachment_ids": [page["attachment_id"] for page in pages], "page_count": len(pages),
                       "source_refs": refs, "origin": origin, "record_links": linked,
                       "bundle_complete": complete, "source_bound": bound,
                       "can_assemble": complete and bound and assets_complete and proof_kind != "unclassified_proof" and len(linked) <= 1
                       and (not declared_derivations or derivation_valid) and not role_exclusions,
                       "status": "assemblable" if not problems else "applicability_review",
                       "derivation_proofs": declared_derivations,
                       "role_exclusions": role_exclusions,
                       "pending_checks": [*problems, *pending], "qualification_result": "not_determined",
                       "project_assignment": "not_made", "asset_validation": "frozen_manifest_metadata_only"})
    return output


def _scope_group_read_text(group: dict, parsed: dict | None) -> None:
    """Read only the selected original PDF pages or DOCX image/OCR descendants.

    A historical bid's nearby paragraphs, tables and other images never become
    text for a selected scan. Reader-owned image locators bind OCR descendants
    to a selected package part; a filename or whole-document hash is not scope.
    """
    origin = group.get("origin") or {}
    rows = []
    expected = set(origin.get("pages", [])) if origin.get("format") == "pdf_pages" else set(origin.get("package_parts", []))
    observed, unread = set(), set()
    if parsed is not None and group["source_bound"] and origin and expected:
        for doc in parsed.get("documents", []):
            if (doc.get("source_id") != origin.get("source_id") or doc.get("sha256") != origin.get("source_sha256")
                    or doc.get("role") == "tender"):
                continue
            for unit in doc.get("units", []):
                if unit.get("source_id") != origin["source_id"]:
                    continue
                blocks = unit.get("blocks", [])
                if origin.get("format") == "pdf_pages":
                    keys = {unit.get("page")} & expected
                    selected = blocks if keys else []
                elif origin.get("format") == "docx_images":
                    images = [(block.get("locator"), block.get("image_ref", {}).get("package_part"))
                              for block in blocks if block.get("kind") == "image" and block.get("image_ref")
                              and block.get("source_id") == origin["source_id"]]
                    images = [(locator, part) for locator, part in images if part in expected and locator]
                    keys = {part for _, part in images}
                    selected = [block for block in blocks if any(
                        block.get("locator", "").startswith(locator + "/ocr/") for locator, _ in images)]
                else:
                    continue
                if not keys:
                    continue
                if unit.get("coverage", {}).get("status") not in {"read", "empty"}:
                    unread.update(keys)
                    continue
                observed.update(keys)
                for block in selected:
                    if block.get("source_id") != origin["source_id"]:
                        continue
                    text = "\n".join([block.get("text", ""), *[" | ".join(row) for row in block.get("rows", [])]]).strip()
                    if not text:
                        continue
                    hits = _term_hits(text)
                    hits.extend({"term": term, "proof_kind": kind, "relation": "proof_family_text"}
                                for term, kind in _PERFORMANCE_TYPES.items() if term in _normal(text))
                    rows.append({"source_id": origin["source_id"], "source_sha256": origin["source_sha256"],
                                 "unit_id": unit.get("unit_id"), "block_id": block.get("block_id"),
                                 "locator": block.get("locator"), "page": unit.get("page"),
                                 "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                 "hits": hits, "role_exclusions": _role_exclusions(text)})
    group["read_text_basis"] = rows
    group["read_scope_status"] = ("not_supplied" if parsed is None else "unbound" if not origin or not group["source_bound"]
                                  else "read" if expected and expected <= observed and not unread else "unread_or_incomplete")
    group["read_scope_unread_count"] = len((expected - observed) | unread)
    body_exclusions = list(dict.fromkeys(reason for row in rows for reason in row["role_exclusions"]))
    # A maintained label may be assembled as a candidate with unknown fields.
    # Explicit contrary read evidence must still prevent assembly.
    body_kinds = {hit["proof_kind"] for row in rows for hit in row["hits"]}
    if group["proof_kind"] in set(_TERMS.values()) and body_kinds and group["proof_kind"] not in body_kinds:
        body_exclusions.append("conflicting_read_document_type")
    if body_exclusions:
        group["role_exclusions"] = list(dict.fromkeys([*group["role_exclusions"], *body_exclusions]))
        group["can_assemble"] = False
        group["status"] = "applicability_review"
        group["pending_checks"].append("明确读取内容属于其他证明类别、申请说明或未提供声明，禁止作为本条证明装配。")


def _requested_person_identity(text: str) -> dict:
    """Recognize bounded explicit name syntax, never infer a name from a role."""
    value = _caption(text)
    value = re.sub(r"^(?:人员要求|人员资格要求|人员资质要求)[：:]?", "", value)
    # Source extraction can include a section heading, a role subheading and
    # an enumerated rule. Remove only these structural prefixes, preserving a
    # literal name following a role (项目负责人张三须...) for exact comparison.
    for _ in range(3):
        previous = value
        value = re.sub(r"^(?:拟投入人员|拟派人员|指定人员|项目技术负责人|项目负责人|项目经理|人员)[：:]?", "", value)
        value = re.sub(r"^[（(]\d+[）)]", "", value)
        if value == previous:
            break
    match = re.match(r"[（(]?([\u4e00-\u9fff]{2,4})[）)]?(?:必须|应当|须|应|需)(?:提供|提交|具有|具备|持有|出具)", value)
    if not match:
        match = re.match(r"[（(]?([\u4e00-\u9fff]{2,4})[）)]?(?:提供|提交|具有|具备|持有|出具)", value)
    if match and match[1] not in {"响应人", "投标人", "供应商", "企业", "公司", "负责人", "工程师", "所有人员"}:
        return {"status": "explicit_single_person", "names": [match[1]], "basis": "literal_name_before_proof_instruction"}
    if re.match(r"[\u4e00-\u9fff]{2,4}(?:[、和与及][\u4e00-\u9fff]{2,4})+(?:均?须|应|需)", value):
        return {"status": "unsupported_named_expression", "names": [], "basis": "multiple_literal_names_need_separate_requirements"}
    actor = re.match(r"(.{1,40}?)(?:必须|应当|须|应|需)?(?:提供|提交|具有|具备|持有|出具)", value)
    if actor and not re.match(r"^(?:响应人|投标人|供应商|企业|公司|负责人|工程师|所有人员|项目组人员|技术人员|拟派|拟投入|请|均|须|应|需|必须)", actor[1]):
        return {"status": "unsupported_named_expression", "names": [], "basis": "unrecognized_literal_proof_subject_requires_review"}
    return {"status": "generic_role_or_unrecognized", "names": [], "basis": "no_name_inferred"}


def _proof_request(requirement: dict) -> dict:
    text = _normal(requirement["text"])
    heading = _caption(requirement["text"])
    clauses = re.split(r"[。；;，,\n]|(?:但是|但)(?=(?:仍|应|须|需|必须|应当)?(?:提供|提交|附|具备|具有))", text)
    negative_pattern = r"(?:无需|无须|不得|禁止|不需|不必|不要求|不)(?:再|另行|重复)?(?:提供|提交|附)"
    negative_clauses = [clause for clause in clauses if re.search(negative_pattern, clause)]
    positive_text = "；".join(clause for clause in clauses if not re.search(negative_pattern, clause))
    result = {"rule": "explicit_proof_type_search", "kinds": [], "alternatives": [],
              "original_text": requirement["text"], "query_terms": [], "negative_clauses": negative_clauses,
              "conditions": {"raw_text": requirement["text"], "validation": "pending_original_evidence",
                             "boolean_tokens": re.findall(r"或者|并且|以及|及(?!以上|以下)|且|和|与|或|之一|同时", text),
                             "negation_tokens": re.findall(r"无需|无须|不得|禁止|不需|不必|不要求|未被|不得低于|不低于", text),
                             "dates_levels_and_standards": re.findall(r"(?:19|20)\d{2}年?|\d+日内|[一二三四五贰叁壹肆伍\d]+级(?:及以上)?|ISO\s*\d+|CP[234]|有效(?:期)?", requirement["text"], re.I)}}
    if re.search(r"不得不|并非(?:无需|无须|不需)|不是(?:无需|无须|不需)", text):
        result["rule"] = "ambiguous_negation_requires_review"
        return result
    if negative_clauses and re.search(r"将|否则|视为|无效|否决|不予|后果|不合格|无资格", text):
        result["rule"] = "negative_condition_consequence_requires_review"
        return result
    if re.search(r"(?:提供|提交|附上?|出具)[^。；;，,]{0,40}(?:申请说明|办理流程|申请表|办证指南|申请流程|办理申请说明)", positive_text):
        result["rule"] = "non_proof_document_role_not_searched"
        return result
    if negative_clauses and not any(re.search(r"提供|提交|具有|具备|持有|出具", clause) for clause in clauses if clause not in negative_clauses):
        result["rule"] = "negative_proof_instruction_not_searched"
        return result
    search_text = positive_text
    broad_heading = heading in {"资质材料", "公司资质材料", "企业资质材料", "人员要求材料", "人员材料", "项目人员资料"}
    if not broad_heading and requirement.get("response_mode") not in {"attachment", "factual_response", "table"}:
        result["rule"] = "unsupported_response_mode_for_proof_search"
        return result
    if any(re.search(r"^(?:由)?(?:采购人|招标人|采购方|招标方|发包人|业主)(?:负责)?(?:必须|应当|须|应|需|将)?(?:提供|提交|出具)|由(?:采购人|招标人|采购方|招标方|发包人|业主)(?:负责)?(?:提供|提交|出具)", clause)
           for clause in re.split(r"[；;，,]", search_text)):
        result["rule"] = "non_bidder_proof_provider_requires_review"
        return result
    if heading in {"资质材料", "公司资质材料", "企业资质材料"}:
        result.update(rule="explicit_qualification_materials_heading", kinds=sorted(set(_CAPTIONS.values())))
    elif heading in {"人员要求材料", "人员材料", "项目人员资料"}:
        result.update(rule="explicit_personnel_materials_heading", kinds=["personnel_proof_package"])
    elif "社会保险" in search_text or "社保证明" in search_text:
        result["kinds"] = ["social_insurance_proof"]
    elif "裁判文书网" in search_text:
        result["kinds"] = ["court_query_screenshot"]
    elif re.search(r"业绩|类似项目|同类项目|类似合同|同类合同|已完成项目|历史项目", search_text) and requirement.get("response_mode") in {"attachment", "table", "factual_response"}:
        result["rule"] = "performance_proof_paths_require_separate_review"
        paths = []
        if "合同" in search_text:
            paths.append({"proof_kind": "performance_contract", "date_basis": "contract_signature_date",
                          "required_contents": ["合同首页", "服务或承包范围页", "合同签署页"]})
        if "中标通知书" in search_text:
            paths.append({"proof_kind": "performance_award_notice", "date_basis": "award_issue_date"})
        if re.search(r"竣工|交工|验收", search_text):
            paths.append({"proof_kind": "performance_acceptance", "date_basis": "acceptance_signature_date",
                          "required_contents": ["验收单位及人员", "验收内容", "验收结论", "验收时间"]})
        for token, kind in (("订单", "performance_order"), ("用户评价", "performance_user_evaluation")):
            if token in search_text:
                paths.append({"proof_kind": kind, "qualification_use": "not_established_by_compilation_heading"})
        result.update(kinds=[item["proof_kind"] for item in paths], alternatives=paths,
                      historical_package_candidates=True)
        if (re.match(r"(?:\d+(?:\.\d+)*[、.．]?)?业绩材料[:：]", search_text) or
                re.match(r"(?:[（(]\d+[）)])?业绩[（(]包含有效业绩证明文件汇总", search_text)):
            result["rule"] = "explicit_historical_performance_compilation"
    elif re.search(r"项目负责人|项目技术负责人|项目组人员|项目经理|职称|工程师|高工|AMPP|CP[234]", search_text, re.I) and (
            re.search(r"证书|工程师|高工|人员要求", search_text) and requirement.get("response_mode") in {"attachment", "factual_response"}):
        result["kinds"] = ["personnel_proof_package"]
        result["requested_person_identity"] = _requested_person_identity(search_text)
        result["query_terms"] = [{"term": "高工", "expanded_term": "高级工程师", "relation": "equivalent_title_alias"}] if "高工" in search_text else []
    else:
        hits = []
        for clause in re.split(r"[；;，,]", search_text):
            document_list = any(_caption(clause).upper().startswith(term.upper()) for term in {**_TERMS, **_STANDARD_TERMS})
            for hit in _term_hits(clause):
                prefix, _, suffix = clause.upper().partition(hit["term"].upper())
                # Local proof request or a standalone document list is allowed;
                # a licence mentioned in a financial/status clause is not.
                if (re.search(r"提供|提交|附上?|具有|具备|持有|出具|上传", prefix) or
                    re.search(r"(?:须|应|需|必须)?(?:提供|提交|附上?)(?:原件|扫描件|复印件)", suffix) or
                    document_list):
                    # Bare status clauses do not become document lists.
                    if re.search(r"未被吊销|未被注销|不存在|未发生", suffix) and not re.search(r"提供|提交|附", prefix):
                        continue
                    hits.append(hit)
        result["query_terms"] = hits
        result["kinds"] = list(dict.fromkeys(hit["proof_kind"] for hit in hits))
        result["required_standards"] = list(dict.fromkeys(hit["term"] for hit in hits if hit["relation"] == "related_standard_requires_validation"))
        if not hits and re.search(r"承诺|说明.*关系", search_text) and requirement.get("response_mode") == "attachment":
            result["kinds"] = ["current_signed_commitment"]
        elif not hits:
            result["rule"] = "unsupported_proof_expression_not_searched"
    operators = (_proof_type_operators(search_text, result["query_terms"]) if result["query_terms"] and
                 all("proof_kind" in hit for hit in result["query_terms"]) else result["conditions"]["boolean_tokens"])
    result["conditions"]["proof_boolean_tokens"] = operators
    has_or = any(token in {"或", "或者", "之一"} for token in operators)
    has_and = any(token in {"和", "与", "及", "且", "并且", "以及", "同时"} for token in operators)
    result["alternatives_operator"] = "mixed" if has_or and has_and else "or" if has_or else "and" if has_and else "not_determined"
    if not result["alternatives"] and len(result["kinds"]) > 1:
        result["alternatives"] = [{"proof_kind": kind} for kind in result["kinds"]]
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
    text = text.replace("高工", "高级工程师")
    names = [name.replace("高工", "高级工程师") for name in names]
    if re.search(r"CP[234]", text, re.I):
        return any(re.search(r"CP[234]", name, re.I) for name in names)
    if "高级工程师" in text or "中级工程师" in text:
        return any(term in text and any(term in name for name in names) for term in ("高级工程师", "中级工程师"))
    return False


def _person_identity_exclusion(group: dict, request: dict, records: list[dict]) -> str | None:
    identity = request.get("requested_person_identity", {})
    if identity.get("status") == "unsupported_named_expression":
        return "requested_person_identity_requires_review"
    if group["proof_kind"] != "personnel_proof_package" or identity.get("status") != "explicit_single_person":
        return None
    linked = {link["record_id"] for link in group["record_links"]}
    names = {_field(record.get("fields", {}).get("name")) for record in records if record["record_id"] in linked}
    if names != set(identity["names"]):
        return "requested_person_identity_mismatch"
    return None


def _assembly_selection(request: dict, selected: list[dict], potential: list[dict]) -> tuple[set[str], dict]:
    """Choose distinct requested types; uncertain alternatives remain explicit."""
    kinds = request["kinds"]
    broad = request["rule"] in _BROAD_ASSEMBLY_RULES
    potential_ids = {group["group_id"] for group in potential}
    rows = []
    for kind in kinds:
        candidates = [group["group_id"] for group in selected if group["proof_kind"] == kind or
                      any(hit["proof_kind"] == kind for hit in _term_hits(group["title"])) or
                      any(hit["proof_kind"] == kind for row in group["read_text_basis"] for hit in row["hits"])]
        allowed = [group["group_id"] for group in potential if group["proof_kind"] == kind]
        rows.append({"proof_kind": kind, "label": _PROOF_LABELS.get(kind, kind),
                     "candidate_group_ids": candidates, "assemblable_group_ids": allowed,
                     "selection_status": "missing" if not candidates else "ambiguous" if len(candidates) > 1 and not broad
                     else "candidate_assembly_pending_checks" if allowed else "candidate_requires_review"})
    ambiguous = [row["proof_kind"] for row in rows if row["selection_status"] == "ambiguous"]
    missing = [row["proof_kind"] for row in rows if row["selection_status"] == "missing"]
    unavailable = [row["proof_kind"] for row in rows if row["selection_status"] == "candidate_requires_review"]
    operator = request.get("alternatives_operator", "not_determined")
    if broad:
        chosen, missing, ambiguous, unavailable = potential_ids, [], [], []
    elif operator == "or":
        all_candidates = {gid for row in rows for gid in row["candidate_group_ids"]}
        chosen = potential_ids & all_candidates if len(all_candidates) == 1 else set()
        if all_candidates:
            missing = []
        if len(all_candidates) > 1:
            ambiguous = [row["proof_kind"] for row in rows if row["candidate_group_ids"]]
    elif len(kinds) > 1 and operator in {"mixed", "not_determined"}:
        chosen = set()
        ambiguous = list(dict.fromkeys([*ambiguous, *[row["proof_kind"] for row in rows if row["candidate_group_ids"]]]))
    else:
        chosen = {gid for row in rows if len(row["candidate_group_ids"]) == 1
                  for gid in row["assemblable_group_ids"]}
    for row in rows:
        row["potential_assemblable_group_ids"] = row["assemblable_group_ids"]
        row["assemblable_group_ids"] = [gid for gid in row["assemblable_group_ids"] if gid in chosen]
    unavailable_details = [{"proof_kind": row["proof_kind"], "label": row["label"],
        "candidate_group_ids": row["candidate_group_ids"],
        "reasons": list(dict.fromkeys(reason for group in selected if group["group_id"] in row["candidate_group_ids"]
            for reason in group["pending_checks"][:1]))}
        for row in rows if row["proof_kind"] in unavailable]
    return chosen, {"operator": operator, "broad_compilation": broad, "required_kind_groups": rows,
                    "approved_group_ids": sorted(chosen), "missing_required_kinds": missing,
                    "selection_status": "partial" if chosen and (missing or ambiguous or unavailable) else "candidate_assembly_pending_checks" if chosen
                    else "ambiguous" if ambiguous else "pending_review" if selected else "no_candidate",
                    "missing_required_labels": [_PROOF_LABELS.get(kind, kind) for kind in missing],
                    "unavailable_required_kinds": unavailable,
                    "unavailable_required_labels": [_PROOF_LABELS.get(kind, kind) for kind in unavailable],
                    "unavailable_required_details": unavailable_details,
                    "ambiguous_candidate_kinds": ambiguous,
                    "ambiguous_required_labels": [_PROOF_LABELS.get(kind, kind) for kind in ambiguous]}


def material_candidate_report(requirements: list[dict], records: list[dict], *,
                              attachments: list[dict] | None = None, sources: list[dict] | None = None,
                              parsed: dict | None = None, evidence_selections: list[dict] | None = None,
                              proof_derivations: list[dict] | None = None) -> dict:
    """The search scope is the frozen prepared records, never the entire company."""
    queries = []
    for requirement in requirements:
        text = requirement["text"]
        kinds = []
        if re.search(r"人员|项目负责人|项目经理|职称|检测员|工程师|高工", text):
            kinds.append("personnel_declaration")
        if re.search(r"业绩|类似项目|同类项目|类似合同|同类合同|已完成项目|历史项目", text):
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
    for group in groups:
        _scope_group_read_text(group, parsed)
    reading = _reading_scope(parsed)
    proof_queries = []
    assembly = []
    for requirement in requirements:
        request = _proof_request(requirement)
        selected, evaluations = [], []
        for group in groups:
            type_hit = (group["proof_kind"] in request["kinds"] or
                        (request.get("historical_package_candidates") and group["proof_kind"] == "historical_performance_package"))
            body_hits = [{**row, "hits": [hit for hit in row["hits"] if hit["proof_kind"] in request["kinds"]]}
                         for row in group["read_text_basis"] if any(hit["proof_kind"] in request["kinds"] for hit in row["hits"])]
            caption_hits = [hit for hit in _term_hits(group["title"]) if hit["proof_kind"] in request["kinds"]]
            related = bool(type_hit or body_hits or caption_hits)
            family_ok = _person_family_candidate(group, request, requirement["text"], records)
            excluded = group["role_exclusions"][:] if related else []
            if related and not family_ok:
                excluded.append("declared_person_certificate_family_differs")
            identity_exclusion = _person_identity_exclusion(group, request, records)
            if related and identity_exclusion:
                excluded.append(identity_exclusion)
            blockers = excluded[:]
            if related and not type_hit:
                blockers.append("related_text_does_not_establish_requested_proof_type")
            if related and group["proof_kind"] == "unclassified_proof":
                blockers.append("unclassified_proof_role_requires_review")
            if related and not group["source_bound"]:
                blockers.append("frozen_source_scope_not_bound")
            if related and not group["bundle_complete"]:
                blockers.append("incomplete_proof_package")
            if related and (group["proof_kind"].startswith("performance_") or (
                    group["proof_kind"] == "historical_performance_package" and
                    request["rule"] != "explicit_historical_performance_compilation")):
                blockers.append("performance_applicability_not_established")
            if related and not group["can_assemble"] and not blockers:
                blockers.append("proof_group_integrity_or_provenance_incomplete")
            if related and not excluded:
                selected.append(group)
            evaluations.append({"group_id": group["group_id"], "relation": "excluded" if excluded else "candidate" if related else "unrelated",
                                "channels": [*(["maintained_type"] if type_hit else []),
                                             *(["controlled_caption_term"] if caption_hits else []),
                                             *(["selected_read_text"] if body_hits else [])],
                                "caption_terms": caption_hits, "read_text_hits": body_hits,
                                "negative_reasons": excluded, "assembly_blockers": blockers,
                                "matches_requested_type": type_hit,
                                "qualification_result": "not_determined", "project_assignment": "not_made"})
        type_matched_ids = {item["group_id"] for item in evaluations if item["matches_requested_type"]}
        potential_assembly = [group for group in selected if group["can_assemble"] and group["group_id"] in type_matched_ids and
                        not group["proof_kind"].startswith("performance_") and
                        (group["proof_kind"] != "historical_performance_package" or
                         request["rule"] == "explicit_historical_performance_compilation")]
        approved_ids, selection = _assembly_selection(request, selected, potential_assembly)
        can_assemble = [group for group in potential_assembly if group["group_id"] in approved_ids]
        supported = bool(request["kinds"] or request.get("historical_package_candidates"))
        status = ("not_searched" if attachments is None or not supported else
                  "assemblable" if can_assemble else "applicability_review" if selected else "no_candidate")
        pending = list(_PERSON_CHECKS if "personnel_proof_package" in request["kinds"] else
                   _HISTORY_CHECKS if request.get("historical_package_candidates") else _COMMON_CHECKS)
        if request.get("required_standards"):
            pending.append("相关体系类型仅作候选；逐页核对所请求标准及版本：" + "、".join(request["required_standards"]) + "。")
        pending.append("核对本条与/或、否定、有效期、等级和范围条件；不能由候选命中推定满足。")
        scope_incomplete = bool(reading["unread_unit_count"] or any(
            group["read_scope_status"] in {"unread_or_incomplete", "not_supplied", "unbound"} for group in groups))
        decision = ("negative_instruction" if request["rule"] == "negative_proof_instruction_not_searched" else
                    "unsupported_expression_semantic_recall_needed" if not supported else
                    "not_searched" if attachments is None else
                    "candidate_assembly_pending_checks" if can_assemble else
                    "related_candidate_needs_review" if selected else
                    "scope_incomplete" if scope_incomplete else "no_candidate_in_frozen_scope")
        row = {"requirement_id": requirement["requirement_id"], "status": status, "request": request,
               "candidate_group_ids": [group["group_id"] for group in selected],
               "assemblable_group_ids": [group["group_id"] for group in can_assemble],
               "search_status": "searched_frozen_prepared_attachments" if attachments is not None and supported else "not_searched",
               "catalog_status": reading["status"], "unread_scope_present": scope_incomplete,
               "pending_checks": pending[:], "qualification_result": "not_determined", "project_assignment": "not_made",
               "absence_conclusion": "not_established"}
        row.update(decision=decision, assembly_selection=selection,
                   missing_required_kinds=selection["missing_required_kinds"],
                   missing_required_labels=selection["missing_required_labels"],
                   unavailable_required_kinds=selection["unavailable_required_kinds"],
                   unavailable_required_labels=selection["unavailable_required_labels"],
                   unavailable_required_details=selection["unavailable_required_details"],
                   ambiguous_candidate_kinds=selection["ambiguous_candidate_kinds"],
                   ambiguous_required_labels=selection["ambiguous_required_labels"],
                   required_kind_groups=selection["required_kind_groups"],
                   decision_basis={"matching_version": MATERIAL_MATCHING_VERSION,
                   "request_rule": request["rule"], "frozen_attachment_inventory_supplied": attachments is not None,
                   "supported_controlled_terms": supported, "conditions_validation": "pending_original_evidence"},
                   candidate_evaluations=evaluations,
                   negative_reasons=list(dict.fromkeys([reason for item in evaluations for reason in item["negative_reasons"]] +
                       (["negative_proof_instruction"] if request["rule"] == "negative_proof_instruction_not_searched" else []) +
                       (["unsupported_controlled_expression"] if not supported and request["rule"] != "negative_proof_instruction_not_searched" else []) +
                       (["explicitly_negated_clauses_not_searched"] if request["negative_clauses"] else []))),
                   excluded_group_ids=[item["group_id"] for item in evaluations if item["relation"] == "excluded"],
                   read_scope_status="incomplete" if scope_incomplete else "no_unread_selected_units_reported",
                   semantic_recall_status="not_applicable_negative_instruction" if request["rule"] == "negative_proof_instruction_not_searched"
                   else "needed_but_not_implemented" if not supported or not selected else "not_performed")
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
                                       "matching_version": MATERIAL_MATCHING_VERSION,
                                       "proof_kind": group["proof_kind"], "origin": group["origin"],
                                       "record_links": group["record_links"], "proof_derivations": group["derivation_proofs"]},
                             "pending_checks": list(dict.fromkeys([*group["pending_checks"], *pending])),
                             "qualification_result": "not_determined", "project_assignment": "not_made",
                             "presentation": "historical_proof_candidate_pending_review",
                             "assembly_scope": "historical_supplement" if request["rule"] in {
                                 "explicit_qualification_materials_heading", "explicit_personnel_materials_heading",
                                 "explicit_historical_performance_compilation"}
                             else "requested_proof_candidate"})
    report.update(schema_version="material.candidates.v3", matching_version=MATERIAL_MATCHING_VERSION,
                  scope="frozen_prepared_declarations_and_proof_packages_only",
                  reading_scope=reading, record_candidates=[_record_view(record) for record in records],
                  proof_groups=groups, proof_queries=proof_queries, assembly_candidates=assembly,
                  proof_counts={"pages": len(attachments or []), "groups": len(groups),
                                "assemblable_groups": sum(group["can_assemble"] for group in groups)})
    return report
