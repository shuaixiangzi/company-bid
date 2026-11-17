"""Proof reading failures stay scoped and cannot become missing-material facts."""
import copy

import pytest

from company_bid.material_content_index import MaterialContentFailure
from company_bid.material_recall import annotate_material_issues
from company_bid.material_source_review import build_material_source_review, MaterialSourceReviewFailure
from company_bid.storage import digest
from test_material_ocr_checklist import _case, _unit
from test_material_source_review import example


def failed_unit(*, review=None, name="A", error="MATERIAL_OCR_OUTPUT_INCOMPLETE"):
    unit = {"source_id": "SOURCE-"+name, "source_sha256": digest([name,"source"]),
            "unit_id": "UNIT-"+name, "target": 4, "format": "pdf_pages", "ocr_image_sha256": digest(name),
            "ocr_unit_id": "OCR-"+name, "status": "needs_ocr", "transcription_status": "ocr_failed_pending_review",
            "ocr_failure_disposition": {"schema_version": "material-ocr-failure-disposition.v1",
                "error_code": error, "origin_run_id": "a"*32, "origin_unit_id": "material-ocr-origin",
                "identity_sha256": digest([name,"identity"]), "status": "reading_failed_pending_review",
                "machine_text_adopted": False, "material_presence": "present_but_unread_or_conflicting",
                "qualification_result": "not_determined"}}
    if review is not None:
        value = example()
        value["identity"].update(source_id=unit["source_id"], source_sha256=unit["source_sha256"],
                                 image_sha256=unit["ocr_image_sha256"])
        value["ocr"].update(run_id=unit["ocr_failure_disposition"]["origin_run_id"],
                            unit_id=unit["ocr_failure_disposition"]["origin_unit_id"])
        if review == "source_ambiguous":
            value["field"].update(disposition=review,source_observation="ambiguous_or_unreadable",source_text=None)
        elif review == "source_blank":
            value["field"].update(disposition=review,source_observation="independently_confirmed_blank",source_text="")
        unit["ocr_failure_disposition"]["source_review"] = build_material_source_review(value)
        unit["ocr_failure_disposition"].update(source_review_authenticated=True,
            identity=copy.deepcopy(value["identity"]),identity_sha256=digest(value["identity"]))
    return unit


def test_failed_dictionary_candidate_is_visible_without_ocr_numbers_and_preserves_business_inputs():
    report, issue = _case(units=[failed_unit()])
    report["assembly_candidates"] = [{"requirement_id":"R1","group_id":"G1"}]
    before_report, before_issue = copy.deepcopy((report,issue))
    changes = annotate_material_issues([issue],report)
    assert "本条候选证明有1页/图读取失败待核" in issue["reason"]
    assert "营业执照" in issue["reason"] and "原件第4页" in issue["reason"]
    assert "接口输出被截断" in issue["reason"] and "失败转录未采用" in issue["reason"]
    assert "不能据此认定原文留空、公司缺资料或资格满足" in issue["reason"]
    assert issue["reason"].startswith(before_issue["reason"]+"\n")
    assert report == before_report
    assert {k:v for k,v in issue.items() if k!="reason"} == {k:v for k,v in before_issue.items() if k!="reason"}
    scope = changes[0]["requirement_contexts"][0]["ocr_failure_review"]
    assert scope["failed_unit_count"] == 1 and scope["machine_text_adopted"] is False
    assert scope["units"][0]["disposition_sha256"] == digest(failed_unit()["ocr_failure_disposition"])


@pytest.mark.parametrize("review,phrase", [("machine_error","机器识别错误"),
    ("source_ambiguous","来源模糊"),("source_blank","原文留空：独立看图已确认")])
def test_only_independent_review_supplies_specific_source_disposition(review,phrase):
    report,issue = _case(units=[failed_unit(review=review,error="MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH")])
    annotate_material_issues([issue],report)
    assert phrase in issue["reason"]
    assert "读取失败状态保留" in issue["reason"] and "资格仍待核" in issue["reason"]
    if review == "machine_error":
        assert "740 m²" in issue["reason"] and "740 m³" in issue["reason"]
    elif review == "source_ambiguous":
        assert "740 m²" not in issue["reason"] and "不猜填" in issue["reason"]


def test_no_review_does_not_invent_field_values_or_source_blank():
    report,issue = _case(units=[failed_unit(error="MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH")])
    annotate_material_issues([issue],report)
    assert "转录与已审原图字段冲突" in issue["reason"]
    assert "740" not in issue["reason"] and "机器识别错误：" not in issue["reason"]
    assert "原文留空：" not in issue["reason"]


def test_unrelated_failures_and_global_counts_do_not_attach():
    report,issue = _case(units=[failed_unit()],candidate_ids=["G2"])
    report["original_content_index"]["coverage"]["ocr_failed_pending_review_count"]=999
    before=copy.deepcopy(issue)
    assert annotate_material_issues([issue],report)==[]
    assert issue==before


def test_shared_page_failure_is_counted_once_with_both_group_links():
    report,issue = _case(units=[failed_unit()],candidate_ids=["G1","G2"])
    report["original_content_index"]["groups"].append({"group_id":"G2","units":[failed_unit()]})
    change,=annotate_material_issues([issue],report)
    scope=change["requirement_contexts"][0]["ocr_failure_review"]
    assert scope["failed_unit_count"]==1 and scope["group_ids"]==["G1","G2"]
    assert issue["reason"].count("接口输出被截断")==1


def test_merged_issue_keeps_failure_and_numeric_findings_with_their_requirements():
    report,issue=_case(units=[failed_unit()])
    query=copy.deepcopy(report["proof_queries"][0]);query.update(requirement_id="R2",candidate_group_ids=["G2"])
    row=copy.deepcopy(report["semantic_recall"]["queries"][0]);row["requirement_id"]="R2"
    report["proof_queries"].append(query);report["semantic_recall"]["queries"].append(row)
    report["original_content_index"]["groups"].append({"group_id":"G2","units":[_unit("B",["OCR_DATE_INVALID"])]})
    issue["requirement_refs"].append("R2")
    annotate_material_issues([issue],report)
    first,second=issue["reason"].split("【R2｜",1)
    assert "接口输出被截断" in first and "日期不合法" not in first
    assert "日期不合法" in second and "接口输出被截断" not in second


def test_absent_semantic_row_does_not_hide_dictionary_linked_read_failure():
    report,issue=_case(units=[failed_unit()]);del report["semantic_recall"]
    before=copy.deepcopy(report)
    change,=annotate_material_issues([issue],report)
    assert "读取失败待核" in issue["reason"]
    assert report==before
    assert change["requirement_contexts"][0]["semantic_recall"]["annotation_only_no_semantic_record"] is True


@pytest.mark.parametrize("key,value", [("machine_text_adopted",True), ("material_presence","missing"),
    ("qualification_result","satisfied"),("origin_run_id","invalid"),("identity_sha256",None),
    ("status","succeeded"),("error_code","confidential original text")])
def test_invalid_disposition_fails_instead_of_hiding_or_relabeling_failure(key,value):
    unit=failed_unit();unit["ocr_failure_disposition"][key]=value
    report,issue=_case(units=[unit]);before=copy.deepcopy(issue)
    with pytest.raises(MaterialContentFailure,match="MATERIAL_OCR_FAILURE_DISPLAY_INVALID"):
        annotate_material_issues([issue],report)
    assert issue==before


@pytest.mark.parametrize("changed",["source_id","source_sha256","image_sha256"])
def test_review_for_another_source_or_image_is_not_rendered(changed):
    unit=failed_unit(review="machine_error")
    record=unit["ocr_failure_disposition"]["source_review"]
    value={key:copy.deepcopy(record[key]) for key in ("identity","ocr","reviewer","field")}
    value["identity"][changed]="OTHER" if changed=="source_id" else "f"*64
    unit["ocr_failure_disposition"]["source_review"]=build_material_source_review(value)
    report,issue=_case(units=[unit])
    with pytest.raises(MaterialContentFailure,match="MATERIAL_OCR_FAILURE_REVIEW_SOURCE_CHANGED"):
        annotate_material_issues([issue],report)


def test_tampered_review_cannot_put_values_into_checklist():
    unit=failed_unit(review="machine_error")
    unit["ocr_failure_disposition"]["source_review"]["field"]["source_text"]="invented value"
    report,issue=_case(units=[unit])
    with pytest.raises(MaterialSourceReviewFailure):
        annotate_material_issues([issue],report)
    assert "invented" not in issue["reason"]


@pytest.mark.parametrize("authenticated",[None,False,1,"true"])
def test_review_requires_explicit_independent_authentication(authenticated):
    unit=failed_unit(review="machine_error")
    unit["ocr_failure_disposition"]["source_review_authenticated"]=authenticated
    report,issue=_case(units=[unit])
    with pytest.raises(MaterialContentFailure,match="MATERIAL_OCR_FAILURE_REVIEW_NOT_AUTHENTICATED"):
        annotate_material_issues([issue],report)


@pytest.mark.parametrize("changed",["origin_run_id","origin_unit_id","identity_sha256","selection_sha256"])
def test_review_requires_original_call_and_complete_frozen_identity(changed):
    unit=failed_unit(review="machine_error")
    disposition=unit["ocr_failure_disposition"]
    if changed=="selection_sha256":
        disposition["identity"][changed]="f"*64
        disposition["identity_sha256"]=digest(disposition["identity"])
    else:
        disposition[changed]="f"*32 if changed=="origin_run_id" else "f"*64 if changed=="identity_sha256" else "other-unit"
    report,issue=_case(units=[unit])
    with pytest.raises(MaterialContentFailure,match="MATERIAL_OCR_FAILURE_REVIEW_SOURCE_CHANGED"):
        annotate_material_issues([issue],report)
