"""Controlled candidate recall and negative evidence, without qualification claims."""
import copy

import pytest

from company_bid.material_matching import MATERIAL_MATCHING_VERSION, material_candidate_report


SHA = "a" * 64
OTHER_SHA = "b" * 64


def request(text, mode="attachment"):
    return {"requirement_id": "R1", "text": text, "response_mode": mode}


def inventory(title="营业执照", description="", *, selected_pages=None, part=None):
    selection = {"selection_id": "G", "title": title, "source_id": "ORIGINAL", "source_sha256": SHA,
                 "format": "docx_images" if part else "pdf_pages", "pages": [] if part else selected_pages or [2],
                 "package_parts": [part] if part else []}
    return {"attachments": [{"attachment_id": "A", "bundle_id": "G", "title": title, "description": description,
                             "source_refs": ["PAGE"], "path": "synthetic/page.png", "sha256": OTHER_SHA,
                             "page_index": 1, "page_count": 1}],
            "sources": [{"source_id": "ORIGINAL", "sha256": SHA, "kind": "company_material"},
                        {"source_id": "PAGE", "sha256": SHA, "kind": "company_material"}],
            "evidence_selections": [selection]}


def block(text, locator="pdf/page[2]/line[0]", **extras):
    return {"source_id": "ORIGINAL", "block_id": locator, "locator": locator,
            "kind": "paragraph", "text": text, **extras}


def parsed_page(text, *, page=2, sha=SHA, status="read", sid="ORIGINAL"):
    return {"documents": [{"source_id": sid, "sha256": sha, "role": "reference_bid", "units": [
        {"unit_id": "U", "source_id": sid, "page": page, "coverage": {"status": status},
         "blocks": [block(text)]}]}]}


@pytest.mark.parametrize("phrase,title,kind", [
    ("安许证", "安全生产许可证", "safety_production_license"),
    ("安全生产许可证", "安许证", "safety_production_license"),
    ("工商营业执照", "营业执照", "business_license"),
    ("高企证书", "高新技术企业资质", "high_tech_enterprise"),
    ("质量体系证书", "质量管理体系认证证书", "quality_management_certificate"),
    ("质量体系认证证书", "质量体系证书", "quality_management_certificate"),
])
def test_equivalent_aliases_share_maintained_proof_candidate(phrase, title, kind):
    inputs = inventory(title)
    frozen = copy.deepcopy(inputs)
    report = material_candidate_report([request(f"响应人须提供有效的{phrase}扫描件。")], [], **inputs)
    candidate, = report["assembly_candidates"]
    assert candidate["basis"]["proof_kind"] == kind
    assert candidate["qualification_result"] == "not_determined"
    assert any("有效" in check for check in candidate["pending_checks"])
    assert report["matching_version"] == MATERIAL_MATCHING_VERSION
    assert inputs == frozen


def test_iso_is_related_certificate_candidate_with_unresolved_standard():
    report = material_candidate_report([request("须提供ISO 9001:2015认证证书。")], [],
                                       **inventory("质量管理体系认证证书"))
    query, = report["proof_queries"]
    candidate, = report["assembly_candidates"]
    assert query["request"]["required_standards"] == ["ISO9001"]
    assert query["request"]["query_terms"][0]["relation"] == "related_standard_requires_validation"
    assert candidate["qualification_result"] == "not_determined"
    assert any("ISO9001" in check and "标准" in check for check in candidate["pending_checks"])


@pytest.mark.parametrize("text,operator", [
    ("须提供营业执照及安许证，且资质等级不低于二级并在有效期内。", "and"),
    ("须提供营业执照或事业单位法人证书。", "or"),
    ("须提供营业执照或事业单位法人证书，并且提供安许证。", "mixed"),
])
def test_boolean_and_required_conditions_are_preserved_not_satisfied(text, operator):
    report = material_candidate_report([request(text)], [], **inventory())
    query, = report["proof_queries"]
    assert query["request"]["alternatives_operator"] == operator
    assert query["request"]["conditions"]["raw_text"] == text
    assert query["request"]["conditions"]["validation"] == "pending_original_evidence"
    assert all(text not in check for check in query["pending_checks"])
    assert "条件" in query["pending_checks"][-1]
    if operator == "mixed":
        assert not report["assembly_candidates"]
    assert query["qualification_result"] == "not_determined"


@pytest.mark.parametrize("text", ["无需提供营业执照，但须提供有效的安许证。", "无需提供营业执照但须提供有效的安许证。"])
def test_mixed_negative_clause_only_recalls_positive_proof(text):
    report = material_candidate_report([request(text)], [],
                                       **inventory("安全生产许可证"))
    query, = report["proof_queries"]
    assert query["candidate_group_ids"] == ["G"]
    assert query["request"]["kinds"] == ["safety_production_license"]
    assert query["request"]["negative_clauses"] == ["无需提供营业执照"]
    assert "explicitly_negated_clauses_not_searched" in query["negative_reasons"]


@pytest.mark.parametrize("title,description", [
    ("营业执照办理申请说明", ""),
    ("营业执照", "尚未提供营业执照"),
    ("营业执照", "营业执照办理流程介绍，不是证照原件"),
    ("营业执照", "申请办理记录"),
    ("营业执照", "营业执照未提供，待补"),
])
def test_application_and_absence_cannot_masquerade_as_maintained_proof(title, description):
    report = material_candidate_report([request("须提供营业执照。")], [], **inventory(title, description))
    assert not report["assembly_candidates"]
    query, = report["proof_queries"]
    assert query["candidate_group_ids"] == []
    assert query["excluded_group_ids"] == ["G"]
    assert query["negative_reasons"]


def test_selected_read_body_recalls_ambiguous_scan_but_does_not_classify_for_assembly():
    report = material_candidate_report([request("须提供工商营业执照。")], [],
                                       **inventory("扫描资料2"), parsed=parsed_page("营业执照 统一社会信用代码（合成）"))
    query, = report["proof_queries"]
    assert query["status"] == "applicability_review"
    assert query["candidate_group_ids"] == ["G"]
    assert report["proof_groups"][0]["proof_kind"] == "unclassified_proof"
    basis = query["candidate_evaluations"][0]
    assert basis["channels"] == ["selected_read_text"]
    assert basis["read_text_hits"][0]["page"] == 2
    assert basis["read_text_hits"][0]["source_sha256"] == SHA
    assert not report["assembly_candidates"]


@pytest.mark.parametrize("change", ["wrong_page", "wrong_hash", "wrong_source", "unread", "tender", "no_selection", "empty_selection"])
def test_body_text_requires_selected_original_hash_page_and_read_coverage(change):
    inputs = inventory("扫描资料2")
    parsed = parsed_page("营业执照")
    if change == "wrong_page":
        parsed["documents"][0]["units"][0]["page"] = 1
    elif change == "wrong_hash":
        parsed["documents"][0]["sha256"] = OTHER_SHA
    elif change == "wrong_source":
        parsed["documents"][0]["source_id"] = "UNRELATED"
    elif change == "unread":
        parsed["documents"][0]["units"][0]["coverage"]["status"] = "needs_ocr"
    elif change == "tender":
        parsed["documents"][0]["role"] = "tender"
    elif change == "no_selection":
        inputs["evidence_selections"] = None
    else:
        inputs["evidence_selections"] = []
    report = material_candidate_report([request("须提供营业执照。")], [], **inputs, parsed=parsed)
    query, = report["proof_queries"]
    assert query["candidate_group_ids"] == []
    assert not report["assembly_candidates"]
    assert not report["proof_groups"][0]["read_text_basis"]
    if change not in {"no_selection", "empty_selection"}:
        assert query["decision"] == "scope_incomplete"


def test_other_historical_bid_pages_cannot_reclassify_selected_scan():
    parsed = parsed_page("安全生产许可证", page=1)
    parsed["documents"][0]["units"].extend(parsed_page("普通扫描资料", page=2)["documents"][0]["units"])
    report = material_candidate_report([request("须提供安许证。")], [], **inventory("扫描资料2"), parsed=parsed)
    assert report["proof_queries"][0]["candidate_group_ids"] == []
    assert report["proof_groups"][0]["proof_kind"] == "unclassified_proof"


def test_docx_part_recall_reads_own_ocr_only_and_excludes_surrounding_bid_text():
    locator = "body/p[2]/image[0]"
    parsed = {"documents": [{"source_id": "ORIGINAL", "sha256": SHA, "role": "reference_bid", "units": [
        {"source_id": "ORIGINAL", "unit_id": "U", "page": None, "coverage": {"status": "read"}, "blocks": [
            block("营业执照", "body/p[2]"),
            block("", locator, kind="image", image_ref={"package_part": "word/media/image1.png"}),
            block("安全生产许可证", locator + "/ocr/text", origin="ocr"),
            block("营业执照", "body/p[2]/image[1]/ocr/text", origin="ocr")]}]}]}
    inputs = inventory("扫描资料2", part="word/media/image1.png")
    yes = material_candidate_report([request("须提供安许证。")], [], **inputs, parsed=parsed)
    no = material_candidate_report([request("须提供营业执照。")], [], **inputs, parsed=parsed)
    assert yes["proof_queries"][0]["candidate_group_ids"] == ["G"]
    assert no["proof_queries"][0]["candidate_group_ids"] == []
    assert yes["proof_groups"][0]["read_text_basis"][0]["locator"] == locator + "/ocr/text"
    assert not yes["assembly_candidates"]


@pytest.mark.parametrize("text,reason", [
    ("本文件是营业执照申请说明，不是证照原件。", "application_explanation_or_non_proof_role"),
    ("尚未提供营业执照。", "proof_not_provided_or_not_obtained"),
    ("安全生产许可证", "conflicting_read_document_type"),
])
def test_contrary_read_evidence_blocks_even_maintained_title(text, reason):
    report = material_candidate_report([request("须提供营业执照。")], [], **inventory(), parsed=parsed_page(text))
    assert not report["assembly_candidates"]
    assert reason in report["proof_queries"][0]["negative_reasons"]


def test_unsupported_unread_and_completed_empty_scope_have_distinct_decisions():
    unsupported = material_candidate_report([request("请提供一切适用材料。")], [], attachments=[])
    unread = material_candidate_report([request("须提供营业执照。")], [],
                                       **inventory("扫描资料2"), parsed=parsed_page("", status="needs_ocr"))
    empty = material_candidate_report([request("须提供营业执照。")], [], attachments=[], parsed={"documents": []})
    assert unsupported["proof_queries"][0]["decision"] == "unsupported_expression_semantic_recall_needed"
    assert unread["proof_queries"][0]["decision"] == "scope_incomplete"
    assert empty["proof_queries"][0]["decision"] == "no_candidate_in_frozen_scope"
    assert all(r["proof_queries"][0]["absence_conclusion"] == "not_established" for r in (unsupported, unread, empty))


def test_collision_between_bundle_and_single_preserves_both_native_identities():
    inputs = inventory()
    single = {**inputs["attachments"][0], "attachment_id": "G", "title": "高企证书"}
    single.pop("bundle_id")
    inputs["attachments"].append(single)
    report = material_candidate_report([request("须提供营业执照。")], [], **inputs)
    groups = report["proof_groups"]
    assert len(groups) == 2 and len({g["group_id"] for g in groups}) == 2
    assert {(g["group_kind"], g["native_group_id"]) for g in groups} == {("bundle", "G"), ("single", "G")}
    assert report["assembly_candidates"][0]["attachment_ids"] == ["A"]
    assert next(g for g in groups if g["group_kind"] == "single")["source_bound"] is False


def test_frozen_attachment_origins_none_selection_stays_distinct_from_explicit_empty():
    inputs = inventory()
    inputs["evidence_selections"] = None
    allowed = material_candidate_report([request("须提供营业执照。")], [], **inputs)
    inputs["evidence_selections"] = []
    denied = material_candidate_report([request("须提供营业执照。")], [], **inputs)
    assert allowed["assembly_candidates"]
    assert not denied["assembly_candidates"]


def test_same_family_history_contract_recalled_as_lead_without_project_qualification():
    record = {"record_id": "H", "kind": "historical_performance", "status": "usable_provisional",
              "fields": {"project_name": {"value": "合成检测项目"}},
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA}]}
    report = material_candidate_report([request("须提供同类项目合同。")], [record], **inventory("合成检测项目合同主要页"))
    query, = report["proof_queries"]
    assert query["status"] == "applicability_review" and query["candidate_group_ids"] == ["G"]
    assert report["queries"][0]["candidate_record_ids"] == ["H"]
    assert not report["assembly_candidates"]


def test_high_engineer_alias_changes_family_recall_without_fuzzy_person_identity():
    record = {"record_id": "P", "kind": "personnel_declaration", "status": "usable_provisional",
              "fields": {"name": {"value": "合成人甲"}, "titles": [{"value": "高级工程师"}]},
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA}]}
    inputs = inventory("合成人甲")
    yes = material_candidate_report([request("项目负责人须提供高工证书。")], [record], **inputs)
    assert yes["proof_queries"][0]["status"] == "assemblable"
    assert yes["assembly_candidates"][0]["project_assignment"] == "not_made"
    record["fields"]["name"]["value"] = "合成人乙"
    no = material_candidate_report([request("项目负责人须提供高工证书。")], [record], **inputs)
    assert not no["assembly_candidates"]
    assert no["proof_groups"][0]["record_links"] == []


@pytest.mark.parametrize("text", ["未过期的营业执照", "未被吊销的营业执照", "未经批准不得使用本证书。营业执照",
                                 "营业执照。申请办理过程中请使用指定模板，证书申请注意事项见网站。"])
def test_valid_status_and_application_notes_in_actual_proof_body_do_not_mean_absence(text):
    report = material_candidate_report([request("须提供营业执照。")], [], **inventory(), parsed=parsed_page(text))
    assert report["assembly_candidates"]
    assert report["proof_queries"][0]["negative_reasons"] == []


def test_related_body_mention_in_known_other_type_never_assembles_as_requested_certificate():
    report = material_candidate_report([request("须提供营业执照。")], [], **inventory("安全生产许可证"),
                                       parsed=parsed_page("安全生产许可证。办理变更请另附营业执照。"))
    query, = report["proof_queries"]
    assert query["candidate_group_ids"] == ["G"]
    assert query["status"] == "applicability_review"
    assert not report["assembly_candidates"]
    assert "related_text_does_not_establish_requested_proof_type" in query["candidate_evaluations"][0]["assembly_blockers"]


@pytest.mark.parametrize("text,should_assemble", [("张三须提供高工证书。", False), ("李四须提供高工证书。", True),
                                                 ("项目负责人须提供高工证书。", True),
                                                 ("项目负责人张三须提供高工证书。", False),
                                                 ("张三和李四须提供高工证书。", False)])
def test_explicit_requested_person_must_equal_exact_provenance_bound_person(text, should_assemble):
    record = {"record_id": "P", "kind": "personnel_declaration", "status": "usable_provisional",
              "fields": {"name": {"value": "李四"}, "titles": [{"value": "高级工程师"}]},
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA}]}
    report = material_candidate_report([request(text)], [record], **inventory("李四"))
    assert bool(report["assembly_candidates"]) == should_assemble
    assert report["proof_queries"][0]["qualification_result"] == "not_determined"
    if not should_assemble:
        assert any(reason.startswith("requested_person_identity") for reason in report["proof_queries"][0]["negative_reasons"])


@pytest.mark.parametrize("text,rule", [("不提供营业执照。", "negative_proof_instruction_not_searched"),
                                       ("不得不提供营业执照。", "ambiguous_negation_requires_review"),
                                       ("并非无需提供营业执照。", "ambiguous_negation_requires_review"),
                                       ("不提供营业执照将被否决。", "negative_condition_consequence_requires_review"),
                                       ("不提供营业执照视为无效。", "negative_condition_consequence_requires_review")])
def test_negative_instruction_double_negation_and_consequence_are_distinguished(text, rule):
    report = material_candidate_report([request(text)], [], **inventory())
    assert not report["assembly_candidates"]
    assert report["proof_queries"][0]["request"]["rule"] == rule
    assert report["proof_queries"][0]["request"]["original_text"] == text


@pytest.mark.parametrize("text", ["须提供营业执照办理申请说明。", "须提交安全生产许可证办理流程。"])
def test_request_for_application_instructions_does_not_request_the_certificate(text):
    report = material_candidate_report([request(text)], [], **inventory())
    assert report["proof_queries"][0]["request"]["rule"] == "non_proof_document_role_not_searched"
    assert not report["assembly_candidates"]


@pytest.mark.parametrize("text,should_assemble", [("采购人须提供营业执照。", False), ("由招标人提供营业执照。", False),
                                                 ("响应人应向采购人提供营业执照。", True)])
def test_instruction_provider_is_not_confused_with_proof_recipient(text, should_assemble):
    report = material_candidate_report([request(text)], [], **inventory())
    assert bool(report["assembly_candidates"]) == should_assemble


@pytest.mark.parametrize("mode", ["writing", "procedure", "pricing", "narrative"])
def test_non_material_response_modes_do_not_gain_automatic_proof_attachments(mode):
    report = material_candidate_report([request("须提供营业执照。", mode=mode)], [], **inventory())
    assert not report["assembly_candidates"]
    assert report["proof_queries"][0]["request"]["rule"] == "unsupported_response_mode_for_proof_search"


@pytest.mark.parametrize("include_safety", [False, True])
def test_and_selection_tracks_independent_proof_obligations(include_safety):
    inputs = inventory()
    if include_safety:
        second = {**inputs["attachments"][0], "attachment_id": "B", "title": "安全生产许可证"}
        second.pop("bundle_id")
        inputs["attachments"].append(second)
    inputs["evidence_selections"] = None
    report = material_candidate_report([request("须提供营业执照及安许证。")], [], **inputs)
    query, = report["proof_queries"]
    assert {candidate["attachment_ids"][0] for candidate in report["assembly_candidates"]} == ({"A", "B"} if include_safety else {"A"})
    assert query["missing_required_labels"] == ([] if include_safety else ["安全生产许可证"])
    assert query["assembly_selection"]["selection_status"] == ("candidate_assembly_pending_checks" if include_safety else "partial")
    assert len(query["required_kind_groups"]) == 2


def test_competing_packages_of_same_kind_are_ambiguous_but_broad_heading_can_display_both():
    inputs = inventory()
    inputs["evidence_selections"] = None
    second = {**inputs["attachments"][0], "attachment_id": "B", "bundle_id": "G2"}
    inputs["attachments"].append(second)
    narrow = material_candidate_report([request("须提供营业执照。")], [], **inputs)
    broad = material_candidate_report([request("资质材料：")], [], **inputs)
    assert not narrow["assembly_candidates"]
    assert narrow["proof_queries"][0]["ambiguous_required_labels"] == ["营业执照"]
    assert narrow["proof_queries"][0]["required_kind_groups"][0]["assemblable_group_ids"] == []
    assert len(broad["assembly_candidates"]) == 2


def test_collision_encoding_cannot_reuse_an_existing_native_prefixed_id():
    inputs = inventory()
    inputs["evidence_selections"] = None
    single = {**inputs["attachments"][0], "attachment_id": "G", "title": "高企证书"}
    single.pop("bundle_id")
    prefixed = {**single, "attachment_id": "bundle:G", "title": "安全生产许可证"}
    inputs["attachments"].extend([single, prefixed])
    report = material_candidate_report([request("资质材料：")], [], **inputs)
    assert len(report["proof_groups"]) == 3
    assert len({group["group_id"] for group in report["proof_groups"]}) == 3
    assert len(report["assembly_candidates"]) == 3


def test_proof_alternatives_ignore_earlier_organisation_and_capacity_connectors():
    text = ("1 基本要求：\n响应人必须是在中华人民共和国境内注册的具有独立法人资格的企业、事业单位或其他\n"
            "组织，具有独立订立合同的权利和履行合同的能力，具有有效营业执照或事业单位法人证书。")
    report = material_candidate_report([request(text)], [], **inventory())
    query, = report["proof_queries"]
    assert query["request"]["alternatives_operator"] == "or"
    assert "和" in query["request"]["conditions"]["boolean_tokens"]
    assert query["request"]["conditions"]["proof_boolean_tokens"] == ["或"]
    assert query["request"]["conditions"]["raw_text"] == text
    candidate, = report["assembly_candidates"]
    assert candidate["requirement_id"] == "R1" and candidate["source_refs"] == ["PAGE"]
    assert candidate["qualification_result"] == "not_determined"


def test_actual_proof_and_obligation_survives_unrelated_organisation_alternative():
    text = "响应人须为企业或事业单位，具有订立合同的权利和履约能力，须提供营业执照及安许证。"
    report = material_candidate_report([request(text)], [], **inventory())
    query, = report["proof_queries"]
    assert query["request"]["alternatives_operator"] == "and"
    assert query["missing_required_labels"] == ["安全生产许可证"]
    assert query["assembly_selection"]["selection_status"] == "partial"
    assert report["assembly_candidates"]


@pytest.mark.parametrize("named", [False, True])
def test_source_like_person_heading_and_subrule_do_not_become_a_fabricated_name(named):
    subject = "张三须提供" if named else "项目负责人具有"
    text = f"6 人员要求\n项目负责人：\n（1）{subject}AMPP协会颁发的CP2及以上证书。"
    record = {"record_id": "P", "kind": "personnel_declaration", "status": "usable_provisional",
              "fields": {"name": {"value": "李四"}, "certificate_declarations": [
                  {"declaration_id": "CP", "status": "usable_provisional", "certificate_name": {"value": "AMPP CP3"}}]},
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA}]}
    report = material_candidate_report([request(text)], [record], **inventory("李四"))
    query, = report["proof_queries"]
    assert bool(report["assembly_candidates"]) is not named
    if named:
        assert "requested_person_identity_mismatch" in query["negative_reasons"]
    else:
        assert query["request"]["requested_person_identity"]["status"] == "generic_role_or_unrecognized"
        assert report["assembly_candidates"][0]["qualification_result"] == "not_determined"
        assert report["assembly_candidates"][0]["project_assignment"] == "not_made"


def direct_image_inventory():
    inputs = inventory()
    inputs["attachments"][0].pop("bundle_id")
    inputs["sources"] = [{"source_id": "PAGE", "kind": "attachment", "sha256": OTHER_SHA,
                           "path": "synthetic/page.png"}]
    inputs["evidence_selections"] = []
    return inputs


def test_empty_default_selections_allow_a_complete_standalone_original_image():
    report = material_candidate_report([request("须提供营业执照。")], [], **direct_image_inventory())
    candidate, = report["assembly_candidates"]
    assert candidate["attachment_ids"] == ["A"]
    assert candidate["basis"]["origin"]["format"] == "whole_image"
    assert candidate["basis"]["origin"]["source_sha256"] == OTHER_SHA
    assert candidate["qualification_result"] == "not_determined"


@pytest.mark.parametrize("damage", ["different_hash", "different_path", "parent_document", "bundle", "multiple_refs",
                                    "wrong_page_index", "wrong_page_count", "wrong_source_kind", "mismatched_selection"])
def test_direct_image_binding_cannot_replace_split_scope_or_override_selection(damage):
    inputs = direct_image_inventory()
    page, source = inputs["attachments"][0], inputs["sources"][0]
    if damage == "different_hash":
        source["sha256"] = SHA
    elif damage == "different_path":
        source["path"] = "synthetic/other-page.png"
    elif damage == "parent_document":
        source["path"] = "synthetic/historical-bid.docx"
    elif damage == "bundle":
        page["bundle_id"] = "G"
    elif damage == "multiple_refs":
        inputs["sources"].append({**source, "source_id": "OTHER"})
        page["source_refs"].append("OTHER")
    elif damage == "wrong_page_index":
        page["page_index"] = 2
    elif damage == "wrong_page_count":
        page["page_count"] = 2
    elif damage == "wrong_source_kind":
        source["kind"] = "manual_input"
    else:
        inputs["evidence_selections"] = [{"selection_id": "A", "title": "营业执照", "source_id": "PAGE",
            "source_sha256": SHA, "format": "pdf_pages", "pages": [1], "package_parts": []}]
    report = material_candidate_report([request("须提供营业执照。")], [], **inputs)
    assert not report["assembly_candidates"]
    assert not report["proof_groups"][0]["source_bound"]


@pytest.mark.parametrize("suffix", ["之一。", "，上述两种证明任选其一。"])
@pytest.mark.parametrize("both_available", [False, True])
def test_trailing_alternative_applies_to_the_proof_list_without_requiring_both(suffix, both_available):
    inputs = inventory()
    inputs["evidence_selections"] = None
    if both_available:
        second = {**inputs["attachments"][0], "attachment_id": "LEGAL", "title": "事业单位法人证书"}
        second.pop("bundle_id")
        inputs["attachments"].append(second)
    report = material_candidate_report([request("须提供营业执照、事业单位法人证书" + suffix)], [], **inputs)
    query, = report["proof_queries"]
    assert query["request"]["alternatives_operator"] == "or"
    assert query["missing_required_labels"] == []
    if both_available:
        assert not report["assembly_candidates"]
        assert query["ambiguous_required_labels"]
    else:
        candidate, = report["assembly_candidates"]
        assert candidate["attachment_ids"] == ["A"]
        assert candidate["qualification_result"] == "not_determined"
