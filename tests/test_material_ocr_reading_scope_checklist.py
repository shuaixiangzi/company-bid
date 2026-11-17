"""Selected regions and no-text OCR remain requirement-scoped reading limits."""
import copy

import pytest

from company_bid.material_content_index import MaterialContentFailure
from company_bid.material_recall import annotate_material_issues
from company_bid.storage import digest
from test_material_ocr_checklist import _case, _unit


def _reading_unit(name="A", *, scope="partial_selected_region", status="ocr_transcribed_unverified", projection=True):
    unit = {"source_id":"SOURCE-"+name,"source_sha256":digest([name,"source"]),"unit_id":"UNIT-"+name,
            "target":3,"format":"pdf_pages","ocr_image_sha256":digest([name,"image"]),
            "ocr_unit_id":"OCR-"+name,"ocr_output_sha256":digest([name,"output"]),
            "transcription_status":status,"transcription_scope":scope,"status":"needs_ocr"}
    if projection:
        unit["reading_region_projection"]={"whole_image_read":False,"original_appendix_replaced":False,
            "scope":"partial_selected_region","parent_identity":{"image_sha256":digest([name,"parent"])},
            "child_identity":{"image_sha256":unit["ocr_image_sha256"]}}
    return unit


def test_region_is_visible_for_dictionary_candidate_with_frozen_image_and_no_business_changes():
    unit=_reading_unit();report,issue=_case(units=[unit])
    report["assembly_candidates"]=[{"group_id":"G1","requirement_id":"R1"}]
    before_report,before_issue=copy.deepcopy((report,issue))
    change,=annotate_material_issues([issue],report)
    assert "仅所选区域已转录，未读取全页。完整附件保留。" in issue["reason"]
    assert "机器文字、字段准确性和表格关系仍待核" in issue["reason"]
    assert issue["reason"].startswith(before_issue["reason"]+"\n")
    assert all(issue[k]==v for k,v in before_issue.items() if k!="reason")
    assert report==before_report
    scope=change["requirement_contexts"][0]["ocr_reading_scope"]
    assert scope["group_ids"]==["G1"] and scope["partial_transcribed_unit_count"]==1
    assert scope["no_text_unit_count"]==0 and scope["automatic_fact_adoption"] is False
    bound=scope["units"][0]
    for key in ("source_id","unit_id","ocr_image_sha256","ocr_output_sha256","reading_region_projection"):
        assert bound[key]==unit[key]


def test_existing_selected_crop_does_not_invent_complete_appendix_presence():
    report,issue=_case(units=[_reading_unit(scope="selected_crop",projection=False)])
    annotate_material_issues([issue],report)
    assert "仅所选区域已转录，未读取全页。" in issue["reason"]
    assert "完整附件保留" not in issue["reason"]


@pytest.mark.parametrize("scope",["selected_image","selected_crop","partial_selected_region",None])
def test_no_text_is_neither_successful_transcription_source_blank_nor_material_missing(scope):
    report,issue=_case(units=[_reading_unit(status="ocr_no_text_unresolved",scope=scope)])
    change,=annotate_material_issues([issue],report)
    assert "未取得可用识别文字，需核对原图；不代表原文留空或资料缺失" in issue["reason"]
    assert "仅所选区域已转录" not in issue["reason"]
    assert "完整附件保留" not in issue["reason"]
    reading=change["requirement_contexts"][0]["ocr_reading_scope"]
    assert reading["no_text_unit_count"]==1 and reading["partial_transcribed_unit_count"]==0


@pytest.mark.parametrize("status",["ocr_transcribed_unverified","ocr_no_text_unresolved"])
def test_dictionary_candidate_with_no_semantic_row_still_displays_reading_limit(status):
    report,issue=_case(units=[_reading_unit(status=status)]);del report["semantic_recall"]
    before=copy.deepcopy(report)
    change,=annotate_material_issues([issue],report)
    assert report==before
    assert change["requirement_contexts"][0]["ocr_reading_scope"]["selected_unit_count"]==1
    assert change["requirement_contexts"][0]["semantic_recall"]["annotation_only_no_semantic_record"] is True


def test_unrelated_groups_and_global_crop_counts_do_not_attach_reading_limit():
    report,issue=_case(units=[_reading_unit()],candidate_ids=["G2"])
    report["original_content_index"]["coverage"]["ocr_transcribed_crop_count"]=99
    report["original_content_index"]["coverage"]["ocr_no_text_count"]=99
    before=copy.deepcopy(issue)
    assert annotate_material_issues([issue],report)==[]
    assert issue==before


def test_shared_unit_deduplicates_but_retains_both_candidate_groups():
    report,issue=_case(units=[_reading_unit()],candidate_ids=["G1","G2"])
    report["original_content_index"]["groups"].append({"group_id":"G2","units":[_reading_unit()]})
    change,=annotate_material_issues([issue],report)
    scope=change["requirement_contexts"][0]["ocr_reading_scope"]
    assert scope["selected_unit_count"]==1 and scope["group_ids"]==["G1","G2"]
    assert scope["units"][0]["group_ids"]==["G1","G2"]
    assert issue["reason"].count("仅所选区域已转录")==1


def test_different_selected_image_hashes_are_not_collapsed_as_same_unit():
    first=_reading_unit();second=copy.deepcopy(first)
    second["ocr_image_sha256"]=digest("another crop")
    report,issue=_case(units=[first,second])
    change,=annotate_material_issues([issue],report)
    assert change["requirement_contexts"][0]["ocr_reading_scope"]["selected_unit_count"]==2


def test_two_requirements_keep_partial_and_no_text_scopes_separate():
    report,issue=_case(units=[_reading_unit()])
    query=copy.deepcopy(report["proof_queries"][0]);query.update(requirement_id="R2",candidate_group_ids=["G2"])
    row=copy.deepcopy(report["semantic_recall"]["queries"][0]);row["requirement_id"]="R2"
    report["proof_queries"].append(query);report["semantic_recall"]["queries"].append(row)
    report["original_content_index"]["groups"].append({"group_id":"G2","units":[_reading_unit("B",status="ocr_no_text_unresolved")]})
    issue["requirement_refs"].append("R2")
    change,=annotate_material_issues([issue],report)
    first,second=issue["reason"].split("【R2｜",1)
    assert "仅所选区域已转录" in first and "不代表原文留空或资料缺失" not in first
    assert "仅所选区域已转录" not in second and "不代表原文留空或资料缺失" in second
    contexts={c["requirement_id"]:c["ocr_reading_scope"] for c in change["requirement_contexts"]}
    assert contexts["R1"]["units"][0]["source_id"]=="SOURCE-A"
    assert contexts["R2"]["units"][0]["source_id"]=="SOURCE-B"


@pytest.mark.parametrize("binding",["historical_dictionary","deterministic_group_ids","semantic_candidate"])
def test_prior_or_semantic_candidate_bindings_retain_reading_limit(binding):
    report,issue=_case(units=[_reading_unit()],candidate_ids=[])
    row=report["semantic_recall"]["queries"][0]
    if binding=="historical_dictionary":row["catalog_stage_dictionary_candidate_group_ids"]=["G1"]
    elif binding=="deterministic_group_ids":row["deterministic_group_ids"]=["G1"]
    else:row["candidates"]=[{"group_id":"G1","decision":"blocked_candidate"}]
    change,=annotate_material_issues([issue],report)
    assert change["requirement_contexts"][0]["ocr_reading_scope"]["group_ids"]==["G1"]


def test_legacy_index_without_new_metadata_keeps_prior_numeric_behavior():
    report,issue=_case(units=[_unit("A",["OCR_DATE_INVALID"])])
    change,=annotate_material_issues([issue],report)
    assert "日期不合法" in issue["reason"]
    assert "仅所选区域已转录" not in issue["reason"] and "不代表原文留空或资料缺失" not in issue["reason"]
    assert change["requirement_contexts"][0]["ocr_reading_scope"]["units"]==[]


def test_legacy_read_scope_or_unknown_transcription_status_does_not_invent_limits():
    report,issue=_case(units=[{"source_id":"S","unit_id":"U","status":"needs_ocr"},
                             _reading_unit(status="future_unknown_status")])
    before=copy.deepcopy(issue)
    assert annotate_material_issues([issue],report)==[] and issue==before


def test_conflicting_scope_copies_of_same_image_fail_instead_of_silently_losing_state():
    unit=_reading_unit();other=copy.deepcopy(unit);other["transcription_status"]="ocr_no_text_unresolved"
    report,issue=_case(units=[unit,other])
    with pytest.raises(MaterialContentFailure,match="MATERIAL_OCR_READING_SCOPE_DISPLAY_AMBIGUOUS"):
        annotate_material_issues([issue],report)
