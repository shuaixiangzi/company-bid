"""Synthetic proof packages: scope and provenance, never eligibility verdicts."""
import copy

import pytest

from company_bid.material_matching import material_candidate_report


SHA = "a" * 64
OTHER_SHA = "b" * 64


def requirement(text, rid="R1", mode="attachment"):
    return {"requirement_id": rid, "text": text, "response_mode": mode}


def personnel(name="合成人甲", record_id="P1", sha=SHA, certificate="AMPP CP3", status="usable_provisional"):
    return {"record_id": record_id, "kind": "personnel_declaration", "status": status,
            "citations": [{"source_id": "ORIGINAL", "source_sha256": sha, "locator": "body/table[1]", "row": 1}],
            "fields": {"name": {"value": name}, "roles": [{"value": "历史项目技术负责人"}],
                       "titles": [{"value": "高级工程师"}], "certificate_declarations": [
                           {"declaration_id": "C1", "certificate_name": {"value": certificate},
                            "level": {"value": "三级"}, "status": "usable_provisional"}]}}


def package(title="合成人甲", count=8, group_id="GROUP"):
    sources = [{"source_id": "ORIGINAL", "sha256": SHA, "kind": "company_material"}]
    pages = []
    for index in range(1, count + 1):
        sid = f"PAGE-{index}"
        sources.append({"source_id": sid, "sha256": SHA, "kind": "company_material"})
        pages.append({"attachment_id": f"A-{index}", "bundle_id": group_id, "title": title,
                      "source_refs": [sid], "path": f"synthetic/proof-{index}.png", "sha256": OTHER_SHA,
                      "page_count": count, "page_index": index})
    selections = [{"selection_id": group_id, "title": title, "source_id": "ORIGINAL", "source_sha256": SHA,
                   "format": "docx_images", "pages": [],
                   "package_parts": [f"word/media/image{index}.png" for index in range(1, count + 1)]}]
    return {"attachments": pages, "sources": sources, "evidence_selections": selections}


def test_eight_page_person_package_has_exact_source_bound_record_link_and_no_assignment():
    inputs = package()
    before = copy.deepcopy(inputs)
    report = material_candidate_report([requirement("7.5、人员要求材料：", mode="factual_response")],
                                       [personnel()], **inputs)
    group = report["proof_groups"][0]
    candidate = report["assembly_candidates"][0]
    assert group["record_links"][0]["record_id"] == "P1"
    assert group["record_links"][0]["citations"][0]["source_sha256"] == SHA
    assert candidate["attachment_ids"] == [f"A-{n}" for n in range(1, 9)]
    assert candidate["basis"]["origin"]["source_id"] == "ORIGINAL"
    assert len(candidate["source_refs"]) == 8
    assert candidate["assembly_scope"] == "historical_supplement"
    assert candidate["qualification_result"] == "not_determined"
    assert candidate["project_assignment"] == "not_made"
    assert any("派任" in text for text in candidate["pending_checks"])
    assert inputs == before


@pytest.mark.parametrize("damage", ["missing_page", "duplicate_index", "wrong_count", "missing_asset", "missing_hash", "missing_page_ref"])
def test_incomplete_or_unfrozen_package_cannot_be_assembled(damage):
    inputs = package()
    if damage == "missing_page":
        inputs["attachments"].pop()
    elif damage == "duplicate_index":
        inputs["attachments"][-1]["page_index"] = 1
    elif damage == "wrong_count":
        inputs["attachments"][-1]["page_count"] = 7
    elif damage == "missing_asset":
        inputs["attachments"][0].pop("path")
    elif damage == "missing_hash":
        inputs["attachments"][0].pop("sha256")
    else:
        inputs["attachments"][0]["source_refs"] = []
    report = material_candidate_report([requirement("7.5、人员要求材料：")], [personnel()], **inputs)
    assert not report["assembly_candidates"]
    assert not report["proof_groups"][0]["can_assemble"]


@pytest.mark.parametrize("records", [[personnel(sha=OTHER_SHA)], [personnel(status="conflict")],
                                   [personnel(), personnel(record_id="P2")]])
def test_person_name_alone_or_ambiguous_identity_is_not_enough(records):
    report = material_candidate_report([requirement("7.5、人员要求材料：")], records, **package())
    assert not report["assembly_candidates"]


def test_alternative_legal_credential_is_or_not_all_words_and_not_qualification():
    report = material_candidate_report([requirement("（5）企业营业执照或事业单位法人证书等相关资质证明文件。")],
                                       [], **package("9.1 营业执照复印件", count=1))
    query = report["proof_queries"][0]
    assert query["request"]["alternatives_operator"] == "or"
    assert query["status"] == "assemblable"
    assert report["assembly_candidates"][0]["qualification_result"] == "not_determined"


def test_design_and_construction_certificates_do_not_substitute_for_control_qualification():
    report = material_candidate_report([
        requirement("须具备中国腐蚀控制资质证书贰级及以上资质。"),
        requirement("7.1、资质材料：", rid="R2", mode="factual_response")], [],
        **package("（2）中国腐蚀控制设计资格证书", count=1))
    specific, heading = report["proof_queries"]
    assert specific["status"] == "no_candidate"
    assert specific["distinct_related_group_ids"] == ["GROUP"]
    assert heading["status"] == "assemblable"
    assert [(item["requirement_id"], item["assembly_scope"]) for item in report["assembly_candidates"]] == [
        ("R2", "historical_supplement")]


def test_personnel_family_is_only_candidate_and_social_insurance_is_separate():
    report = material_candidate_report([
        requirement("项目负责人具有AMPP协会颁发的CP2及以上证书。", mode="factual_response"),
        requirement("项目负责人须提供截止日前120日内社会保险证明。", rid="R2")],
        [personnel()], **package())
    cp, insurance = report["proof_queries"]
    assert cp["status"] == "assemblable" and cp["qualification_result"] == "not_determined"
    assert insurance["status"] == "no_candidate" and insurance["candidate_group_ids"] == []
    assert all("等级" in "".join(item["pending_checks"]) for item in report["assembly_candidates"])


def test_unrelated_declared_certificate_is_not_reused_as_cp_proof():
    person = personnel(certificate="注册会计师")
    report = material_candidate_report([requirement("项目负责人具有AMPP协会颁发的CP2及以上证书。")],
                                       [person], **package())
    assert report["proof_queries"][0]["candidate_group_ids"] == []
    assert not report["assembly_candidates"]


def test_performance_qualification_and_scoring_keep_their_distinct_or_paths():
    report = material_candidate_report([
        requirement("2023年以来至少1项检测业绩，提供合同主要页或中标通知书；以合同签订或中标签发时间为准。"),
        requirement("业绩评分提供以下证明文件之一：合同主要页；竣工（交工）验收文件。", rid="R2"),
        requirement("业绩（包含有效业绩证明汇总，如合同、订单、用户评价材料等复印件）（如有）", rid="R3")],
        [], **package("订单", count=1))
    qualification, scoring, compilation = report["proof_queries"]
    assert qualification["request"]["kinds"] == ["performance_contract", "performance_award_notice"]
    assert scoring["request"]["kinds"] == ["performance_contract", "performance_acceptance"]
    assert qualification["request"]["alternatives_operator"] == scoring["request"]["alternatives_operator"] == "or"
    assert qualification["candidate_group_ids"] == scoring["candidate_group_ids"] == []
    assert compilation["candidate_group_ids"] == ["GROUP"]
    assert compilation["status"] == "applicability_review"
    assert not report["assembly_candidates"]
    assert "服务日期" in "".join(qualification["pending_checks"])


def test_historical_declaration_keeps_dates_amount_and_staff_historical():
    record = {"record_id": "H1", "kind": "historical_performance", "status": "usable_provisional",
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA, "row": 1}],
              "fields": {"project_name": {"value": "合成历史检测项目"}, "service_date": {"value": "2024年"},
                         "contract_amount": {"value": "12"}, "historical_bid_staff_participation": {"value": "是（合成人甲）"}}}
    report = material_candidate_report([requirement("须提供业绩合同主要页。")], [record], attachments=[])
    assert report["proof_queries"][0]["status"] == "no_candidate"
    row = report["record_candidates"][0]
    assert row["declared_fields"]["service_date"] == "2024年"
    assert row["qualification_result"] == "not_determined" and row["project_assignment"] == "not_made"
    assert any("报价" in item for item in row["pending_checks"])


def test_not_searched_no_candidate_and_unparsed_inventory_are_distinct():
    req = [requirement("企业营业执照或事业单位法人证书。")]
    not_searched = material_candidate_report(req, [], sources=[])
    no_candidate = material_candidate_report(req, [], attachments=[], parsed={"documents": []})
    assert not_searched["proof_queries"][0]["status"] == "not_searched"
    assert no_candidate["proof_queries"][0]["status"] == "no_candidate"
    assert no_candidate["reading_scope"]["status"] == "unparsed"
    assert no_candidate["proof_queries"][0]["absence_conclusion"] == "not_established"
    assert no_candidate["unparsed_catalog_is_not_evidence_of_company_absence"] is True


def test_unsupported_expression_does_not_report_a_completed_search():
    report = material_candidate_report([requirement("所有条款均须提供相关证明。")], [], attachments=[])
    assert report["proof_queries"][0]["status"] == "not_searched"


def test_selected_origin_hash_mismatch_prevents_assembly():
    inputs = package("营业执照", count=1)
    inputs["evidence_selections"][0]["source_sha256"] = OTHER_SHA
    report = material_candidate_report([requirement("企业营业执照或事业单位法人证书。")], [], **inputs)
    assert not report["assembly_candidates"]
    assert report["proof_queries"][0]["status"] == "applicability_review"


def test_broad_caption_keyword_is_not_a_document_type():
    report = material_candidate_report([requirement("企业营业执照或事业单位法人证书。")], [],
                                       **package("营业执照办理申请说明", count=1))
    assert report["proof_groups"][0]["proof_kind"] == "unclassified_proof"
    assert not report["assembly_candidates"]


def test_negative_proof_instruction_never_assembles_a_positive_keyword_match():
    report = material_candidate_report([requirement("无需提供企业营业执照或事业单位法人证书。")], [],
                                       **package("营业执照", count=1))
    assert report["proof_queries"][0]["status"] == "not_searched"
    assert not report["assembly_candidates"]


def test_project_title_link_keeps_contract_main_page_review_unresolved():
    record = {"record_id": "H1", "kind": "historical_performance", "status": "usable_provisional",
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA, "row": 2}],
              "fields": {"project_name": {"value": "合成检测项目"}}}
    report = material_candidate_report([requirement("业绩须提供合同主要页或中标通知书。")], [record],
                                       **package("合成检测项目合同主要页", count=3))
    group = report["proof_groups"][0]
    assert group["proof_kind"] == "performance_contract"
    assert group["record_links"][0]["record_id"] == "H1"
    assert report["proof_queries"][0]["status"] == "applicability_review"
    assert not report["assembly_candidates"]


def test_closed_single_certificate_instruction_does_not_match_composite_financial_rule():
    report = material_candidate_report([
        requirement("响应人须提供有效安全生产许可证扫描件。"),
        requirement("响应人营业执照未被吊销，且须提供年度财务报告。", rid="R2")], [],
        **package("安全生产许可证", count=1))
    assert report["proof_queries"][0]["status"] == "assemblable"
    assert report["proof_queries"][1]["status"] == "not_searched"
    assert [item["requirement_id"] for item in report["assembly_candidates"]] == ["R1"]


def derived_history_package():
    title = "合成检测项目"
    inputs = package(title, count=3)
    for source in inputs["sources"][1:]:
        source["sha256"] = OTHER_SHA
    inputs["sources"].append({"source_id": "CHILD", "sha256": OTHER_SHA, "kind": "company_material",
                              "provenance": "derived_from_bid", "title": title})
    selection = inputs["evidence_selections"][0]
    selection.update(source_id="CHILD", source_sha256=OTHER_SHA, format="pdf_pages", pages=[1, 2, 3], package_parts=[])
    record = {"record_id": "H1", "kind": "historical_performance", "status": "usable_provisional",
              "fields": {"project_name": {"value": title}},
              "citations": [{"source_id": "ORIGINAL", "source_sha256": SHA, "locator": "body/table[2]", "row": 1}]}
    binding = {"derivation_id": "D1", "relation": "user_authorized_bid_split", "selection_id": "GROUP",
               "child_source_id": "CHILD", "child_source_sha256": OTHER_SHA,
               "parent_source_id": "ORIGINAL", "parent_source_sha256": SHA, "record_id": "H1",
               "physical_pages": [1, 2, 3],
               "parent_locator": {"text": "declared parent pages 10-12", "status": "declared_unverified_parent_page_range"},
               "visual_review": {"method": "agent_visual_review_of_local_render", "status": "candidate_identity_and_boundary_reviewed",
                                 "project_title": title, "physical_pages": [1, 3],
                                 "observations": ["Cover title matches; last supplied page is blank; keep all physical pages."]},
               "qualification_result": "not_determined", "project_assignment": "not_made"}
    inputs["proof_derivations"] = [binding]
    return [record], inputs


def test_source_bound_bid_split_can_display_whole_historical_package_only_at_compilation():
    records, inputs = derived_history_package()
    report = material_candidate_report([
        requirement("至少一项检测业绩，提供合同主要页或中标通知书。"),
        requirement("7.2、业绩材料：响应人业绩统计表（后附业绩材料）", rid="R2", mode="table")], records, **inputs)
    direct, compilation = report["proof_queries"]
    assert direct["status"] == "applicability_review"
    assert direct["candidate_group_ids"] == ["GROUP"]
    assert compilation["status"] == "assemblable"
    group = report["proof_groups"][0]
    assert group["proof_kind"] == "historical_performance_package"
    assert group["record_links"][0]["child_source_sha256"] == OTHER_SHA
    assert group["record_links"][0]["citations"][0]["source_sha256"] == SHA
    candidate, = report["assembly_candidates"]
    assert candidate["requirement_id"] == "R2" and candidate["attachment_ids"] == ["A-1", "A-2", "A-3"]
    assert candidate["basis"]["proof_derivations"][0]["parent_locator"]["status"] == "declared_unverified_parent_page_range"
    assert candidate["qualification_result"] == "not_determined" and candidate["project_assignment"] == "not_made"
    assert any("agent视觉查看不是用户核验" in text for text in candidate["pending_checks"])


@pytest.mark.parametrize("damage", ["parent_hash", "child_hash", "parent_id", "record_citation", "wrong_title",
                                   "source_not_derived", "review_unbound", "blank_page_omitted", "end_not_reviewed",
                                   "locator_promoted", "record_conflict", "duplicate_binding"])
def test_derivative_package_needs_every_frozen_provenance_and_review_binding(damage):
    records, inputs = derived_history_package()
    binding = inputs["proof_derivations"][0]
    if damage == "parent_hash":
        binding["parent_source_sha256"] = OTHER_SHA
    elif damage == "child_hash":
        binding["child_source_sha256"] = SHA
    elif damage == "parent_id":
        binding["parent_source_id"] = "MISSING"
    elif damage == "record_citation":
        records[0]["citations"][0]["source_id"] = "UNRELATED"
    elif damage == "wrong_title":
        binding["visual_review"]["project_title"] = "不同的合成项目"
    elif damage == "source_not_derived":
        inputs["sources"][-1]["provenance"] = "original"
    elif damage == "review_unbound":
        binding["visual_review"]["status"] = "user_verified"
    elif damage == "blank_page_omitted":
        binding["physical_pages"] = [1, 2]
    elif damage == "end_not_reviewed":
        binding["visual_review"]["physical_pages"] = [1, 2]
    elif damage == "locator_promoted":
        binding["parent_locator"]["status"] = "verified"
    elif damage == "record_conflict":
        records[0]["status"] = "conflict"
    else:
        inputs["proof_derivations"].append(copy.deepcopy(binding))
    report = material_candidate_report([requirement("7.2、业绩材料：", mode="table")], records, **inputs)
    assert not report["assembly_candidates"]
    assert not report["proof_groups"][0]["can_assemble"]
