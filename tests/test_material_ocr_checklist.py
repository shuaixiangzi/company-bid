"""OCR numeric review follows each requirement's material groups, not recall route."""
import copy

import pytest

from company_bid.material_recall import annotate_material_issues
from company_bid.storage import digest


def _unit(name, codes=(), status="pending_source_check"):
    return {"source_id": "SOURCE-" + name, "unit_id": "UNIT-" + name, "target": 3,
            "ocr_unit_id": "OCR-" + name, "ocr_image_sha256": digest(name),
            "ocr_output_sha256": digest([name, "output"]),
            "numeric_validation": {"status": status,
                "findings": [{"code": code, "region_index": n} for n, code in enumerate(codes)]}}


def _case(*, route="deterministic_candidate_available", candidate_ids=None, units=None):
    candidate_ids = ["G1"] if candidate_ids is None else candidate_ids
    units = [_unit("A", ["OCR_DATE_INVALID"], "review_required")] if units is None else units
    row = {"requirement_id": "R1", "status": route, "candidates": [], "condition_findings": []}
    report = {"proof_groups": [{"group_id": "G1", "title": "营业执照"},
                                {"group_id": "G2", "title": "其他证明"}],
        "proof_queries": [{"requirement_id": "R1", "candidate_group_ids": candidate_ids,
            "request": {"original_text": "须提供有效营业执照。", "conditions": {"validity": "原要求"}},
            "search_status": "searched_frozen_prepared_attachments"}],
        "semantic_recall": {"queries": [row]},
        "original_content_index": {"groups": [{"group_id": "G1", "units": units}],
            "coverage": {"selected_unit_count": 2, "status_counts": {"read": 0, "empty": 0},
                "unread_unit_count": 2, "ocr_processed_unit_count": 2,
                "ocr_transcribed_unverified_count": 2, "pending_ocr_unit_count": 0,
                "ocr_transcribed_crop_count": 0, "ocr_engine": {"engine": "deepseek_ocr"},
                # These are global totals and must never become per-issue warnings.
                "ocr_numeric_pending_unit_count": 100, "ocr_numeric_issue_count": 100,
                "ocr_numeric_issue_codes": {"OCR_TABLE_ARITHMETIC_INCONSISTENT": 100}}}}
    issue = {"issue_id": "M1", "requirement_refs": ["R1"], "reason": "原有效期条件待核。",
             "action": "补齐并核验原件。", "source_requirement": "原文不改。"}
    return report, issue


def test_dictionary_candidate_still_surfaces_numeric_finding_and_bound_provenance():
    report, issue = _case()
    before_report, before_issue = copy.deepcopy((report, issue))
    change, = annotate_material_issues([issue], report)
    assert "词典已有候选，未另补查" in issue["reason"]
    assert "已关联1页/图，1项的数字、日期或单位仍需原图核对" in issue["reason"]
    assert "日期不合法1处" in issue["reason"]
    assert "表内数量" not in issue["reason"] and "100" not in issue["reason"]
    assert issue["reason"].startswith(before_issue["reason"] + "\n")
    assert all(issue[key] == value for key, value in before_issue.items() if key != "reason")
    context, = change["requirement_contexts"]
    scope = context["ocr_numeric_review"]
    assert scope["group_ids"] == ["G1"] and scope["finding_count"] == 1
    assert scope["automatic_numeric_adoption"] is False
    unit, = scope["units"]
    assert unit["source_id"] == "SOURCE-A" and unit["target"] == 3
    assert unit["numeric_validation_sha256"] == digest(
        report["original_content_index"]["groups"][0]["units"][0]["numeric_validation"])
    assert report == before_report


def test_numeric_pending_alone_is_visible_without_claiming_an_error():
    report, issue = _case(units=[_unit("A")])
    change, = annotate_material_issues([issue], report)
    assert "1项的数字、日期或单位仍需原图核对" in issue["reason"]
    assert "数字校验发现" not in issue["reason"]
    assert change["requirement_contexts"][0]["ocr_numeric_review"]["finding_count"] == 0


@pytest.mark.parametrize("candidate_ids,route", [(["G2"], "deterministic_candidate_available"),
                                               ([], "no_candidate_in_frozen_catalog")])
def test_unrelated_or_unmatched_library_warnings_do_not_attach(candidate_ids, route):
    report, issue = _case(candidate_ids=candidate_ids, route=route)
    changes = annotate_material_issues([issue], report)
    assert "日期不合法" not in issue["reason"] and "表内数量" not in issue["reason"]
    assert "本条候选证明的 OCR" not in issue["reason"] and "100" not in issue["reason"]
    if changes:
        assert changes[0]["requirement_contexts"][0]["ocr_numeric_review"]["units"] == []
    else:
        assert issue["reason"] == "原有效期条件待核。"


def test_shared_selected_unit_is_counted_once_across_two_candidate_groups():
    report, issue = _case(candidate_ids=["G1", "G2"])
    report["original_content_index"]["groups"].append({"group_id": "G2",
        "units": copy.deepcopy(report["original_content_index"]["groups"][0]["units"])})
    change, = annotate_material_issues([issue], report)
    scope = change["requirement_contexts"][0]["ocr_numeric_review"]
    assert scope["group_ids"] == ["G1", "G2"] and scope["selected_unit_count"] == 1
    assert scope["finding_count"] == 1 and "日期不合法1处" in issue["reason"]


def test_each_requirement_keeps_its_own_findings_in_merged_issue():
    report, issue = _case()
    row2 = copy.deepcopy(report["semantic_recall"]["queries"][0]); row2["requirement_id"] = "R2"
    query2 = copy.deepcopy(report["proof_queries"][0])
    query2.update(requirement_id="R2", candidate_group_ids=["G2"])
    report["semantic_recall"]["queries"].append(row2)
    report["proof_queries"].append(query2)
    report["original_content_index"]["groups"].append({"group_id": "G2",
        "units": [_unit("B", ["OCR_TABLE_ARITHMETIC_INCONSISTENT"], "review_required")]})
    issue["requirement_refs"].append("R2")
    change, = annotate_material_issues([issue], report)
    contexts = {c["requirement_id"]: c["ocr_numeric_review"] for c in change["requirement_contexts"]}
    assert contexts["R1"]["finding_codes"] == {"OCR_DATE_INVALID": 1}
    assert contexts["R2"]["finding_codes"] == {"OCR_TABLE_ARITHMETIC_INCONSISTENT": 1}
    before_r2, after_r2 = issue["reason"].split("【R2｜", 1)
    assert "日期不合法1处" in before_r2 and "表内数量" not in before_r2
    assert "日期不合法" not in after_r2 and "表内数量、单价与金额不一致1处" in after_r2


@pytest.mark.parametrize("binding", ["semantic_candidate", "historical_dictionary", "deterministic_group_ids"])
def test_bound_prior_candidates_are_reviewed_even_after_current_match_changes(binding):
    report, issue = _case(candidate_ids=[])
    row = report["semantic_recall"]["queries"][0]
    if binding == "semantic_candidate":
        row.update(status="all_candidates_blocked", candidates=[{"group_id": "G1", "decision": "blocked_candidate"}])
    elif binding == "historical_dictionary":
        row["catalog_stage_dictionary_candidate_group_ids"] = ["G1"]
    else:
        row["deterministic_group_ids"] = ["G1"]
    change, = annotate_material_issues([issue], report)
    assert "日期不合法1处" in issue["reason"]
    assert change["requirement_contexts"][0]["ocr_numeric_review"]["group_ids"] == ["G1"]


def test_no_numeric_result_and_unrelated_issue_do_not_force_annotations():
    report, issue = _case(units=[_unit("A", status="no_numeric_content")])
    other = {"issue_id": "M2", "requirement_refs": ["OTHER"], "reason": "其他缺项。"}
    before = copy.deepcopy([issue, other])
    assert annotate_material_issues([issue, other], report) == []
    assert [issue, other] == before
