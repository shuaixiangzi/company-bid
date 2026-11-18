"""The same proof can have a successful historical call and a failed current check."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from company_bid.material_recall import _ocr_failure_notes, annotate_material_issues
from company_bid.material_source_review import build_material_source_review
from company_bid.models import DocumentPackage
from company_bid.review_document import (
    _LINKED_OCR_SUCCESS,
    _NUMERIC_CONFLICT,
    _display_source_review_status,
    material_review_report,
    render_task_list,
)
from company_bid.storage import digest
from test_material_ocr_checklist import _case
from test_material_ocr_failure_checklist import failed_unit
from test_review_document import review_package


def _material_report(issue=None, requirement_text=None):
    unit = failed_unit(review="machine_error", error=_NUMERIC_CONFLICT)
    disposition = unit["ocr_failure_disposition"]
    former_review = disposition["source_review"]
    ocr = {**former_review["ocr"], "status": "succeeded", "error_code": None}
    review = build_material_source_review({key: ocr if key == "ocr" else former_review[key]
                                           for key in ("identity", "ocr", "reviewer", "field")})
    identity = {**review["identity"], "format": unit["format"], "target": unit["target"],
                "group_bindings": [{"group_id": "G1"}]}
    disposition.update(schema_version="material-ocr-disposition.v1", source_review=review, identity=identity,
                       identity_sha256=digest(identity), validation_run_id="f" * 32)
    disposition["provenance"] = {"origin_run_id": ocr["run_id"], "origin_node_key": ocr["node_key"],
                                 "origin_unit_id": ocr["unit_id"], "raw_response_sha256": ocr["response_sha256"],
                                 "raw_response_snapshot": {"sha256": ocr["response_sha256"]}}
    unit_id = "material-ocr-validation-" + disposition["identity_sha256"][:24]
    failed = {unit_id: {"run_id": "f" * 32, "unit_id": unit_id, "attempt_id": "c" * 32,
                        "status": "failed", "error_code": _NUMERIC_CONFLICT}}
    report, fallback_issue = _case(units=[unit])
    if issue is None:
        issue = fallback_issue
    rid, = issue["requirement_refs"]
    report["proof_queries"][0]["requirement_id"] = report["semantic_recall"]["queries"][0]["requirement_id"] = rid
    if requirement_text is not None:
        report["proof_queries"][0]["request"]["original_text"] = requirement_text
    change, = annotate_material_issues([issue], report)
    groups = {group["group_id"]: group for group in report["proof_groups"]}
    sources = {unit["source_id"]: SimpleNamespace(kind="company_material", sha256=unit["source_sha256"])}
    return issue, change, groups, failed, sources


def test_status_projection_preserves_frozen_issue_values_and_exact_edits():
    arguments = _material_report()
    frozen = deepcopy(arguments)
    issue, change, groups, failed, sources = arguments
    display, (edit,) = _display_source_review_status(*arguments)
    assert "740 m²" in display and "740 m³" in display and "原响应保留" in display
    assert "读取失败状态保留" in display and "资格仍待核" in display
    assert "历史 OCR 执行成功" in display and "该页当前原图字段校验失败" in display
    assert _LINKED_OCR_SUCCESS not in display
    left, right = edit["original_start"], edit["original_end"]
    assert issue["reason"][left:right] == edit["original_text"] == _LINKED_OCR_SUCCESS
    assert display == issue["reason"][:left] + edit["display_text"] + issue["reason"][right:]
    assert edit["source_identity"]["target"] == 4 and edit["requirement_id"] == "R1"
    assert edit["historical_ocr"]["status"] == "succeeded"
    assert edit["current_validation"]["machine_text_adopted"] is False
    assert edit["current_validation"]["error_code"] == _NUMERIC_CONFLICT
    assert arguments == frozen


@pytest.mark.parametrize("bad_binding", ["wrong_page", "wrong_image", "wrong_source", "wrong_response",
                                        "wrong_current_run", "wrong_current_status", "wrong_error", "missing_failure",
                                        "wrong_group", "wrong_requirement", "different_issue", "different_failure_line",
                                        "repeated_pair"])
def test_status_does_not_follow_equal_text_or_unrelated_evidence(bad_binding):
    arguments = _material_report()
    issue, change, groups, failed, sources = arguments
    context, = change["requirement_contexts"]
    unit, = context["ocr_failure_review"]["units"]
    current, = failed.values()
    if bad_binding == "wrong_page":
        unit["target"] += 1
    elif bad_binding == "wrong_image":
        unit["ocr_image_sha256"] = "0" * 64
    elif bad_binding == "wrong_source":
        sources[unit["source_id"]].sha256 = "0" * 64
    elif bad_binding == "wrong_response":
        unit["disposition"]["provenance"]["raw_response_sha256"] = "0" * 64
        unit["disposition_sha256"] = digest(unit["disposition"])
    elif bad_binding == "wrong_current_run":
        current["run_id"] = "0" * 32
    elif bad_binding == "wrong_current_status":
        current["status"] = "succeeded"
    elif bad_binding == "wrong_error":
        current["error_code"] = "MATERIAL_OCR_OUTPUT_INCOMPLETE"
    elif bad_binding == "missing_failure":
        failed.clear()
    elif bad_binding == "wrong_group":
        unit["group_ids"] = ["G2"]
    elif bad_binding == "wrong_requirement":
        context["requirement_id"] = "R2"
    elif bad_binding == "different_issue":
        issue["action"] = "其他处置。"
    elif bad_binding == "different_failure_line":
        issue["reason"] = issue["reason"].replace("原件第4页：", "原件第5页：")
        change["after"] = deepcopy(issue)
    elif bad_binding == "repeated_pair":
        pair = "\n".join(_ocr_failure_notes({"failed_unit_count": 0, "units": [unit]}, groups))
        issue["reason"] = issue["reason"].replace(pair, pair + "\n" + pair)
        change["after"] = deepcopy(issue)
    assert _display_source_review_status(*arguments) == (issue["reason"], [])


def test_original_reason_can_quote_same_note_with_no_status_rewrite():
    issue, change, groups, failed, sources = _material_report()
    unit, = change["requirement_contexts"][0]["ocr_failure_review"]["units"]
    quoted = "\n".join(_ocr_failure_notes({"failed_unit_count": 0, "units": [unit]}, groups))
    issue["reason"] = quoted + "\n" + issue["reason"]
    change["before"]["reason"] = quoted + "\n" + change["before"]["reason"]
    change["after"] = deepcopy(issue)
    display, changes = _display_source_review_status(issue, change, groups, failed, sources)
    assert display.startswith(quoted + "\n") and len(changes) == 1


def test_rendered_index_retains_source_reason_and_makes_both_statuses_visible(tmp_path):
    data = review_package(tmp_path).model_dump(mode="json")
    issue = data["issues"][0]
    requirement = next(r for r in data["requirements"] if r["requirement_id"] in issue["requirement_refs"])
    issue, change, groups, failed, sources = _material_report(issue, requirement["text"])
    source_id, = sources
    data["sources"].append({**data["sources"][0], "source_id": source_id, "kind": "company_material",
                            "sha256": sources[source_id].sha256, "title": "合成材料"})
    package = DocumentPackage.model_validate(data)
    report = {"changes": [change], "proof_groups": list(groups.values()), "validation_failures": failed}
    document, index = render_task_list(package, material_review_report=report)
    _, old_index = render_task_list(package)
    paragraphs = [p.text for p in document.paragraphs]
    new, old = index["issues"][0], old_index["issues"][0]
    assert new["original_issue"] == old["original_issue"] == issue
    assert new["source_refs"] == old["source_refs"]
    assert _LINKED_OCR_SUCCESS in old["display_reason"]
    assert any("740 m²" in p and "740 m³" in p and "历史 OCR 执行成功" in p and "校验失败" in p for p in paragraphs)
    assert index["source_quotes"] == old_index["source_quotes"]


def test_n3_report_reads_only_the_inherited_successful_assembly_and_latest_failure():
    _, change, groups, failed, _ = _material_report()
    unit_id, = failed
    current = failed[unit_id]
    origin = current["run_id"]
    report = {"material_recall_issue_changes": [change], "material_matching": {"proof_groups": list(groups.values())}}
    read_checkpoint = Mock(return_value={"report": report})
    assembly = {"status": "succeeded", "output_ref": {"path": f"runs/{origin}/artifacts/checkpoint.json"}}
    n3 = {"key": "N3", "status": "succeeded", "units": {"plan-assemble": assembly,
          unit_id: {"status": "failed", "attempts": [{"status": "running"}, current]}}}
    ctx = SimpleNamespace(run_id="1" * 32, _state={"nodes": [n3]},
                          executor=SimpleNamespace(_checkpoint_data=read_checkpoint))
    assert material_review_report(ctx) == {"changes": [change], "proof_groups": list(groups.values()),
                                          "validation_failures": failed}
    read_checkpoint.assert_called_once_with(assembly)
    n3["status"] = "paused"
    assert material_review_report(ctx) is None
    read_checkpoint.assert_called_once()
