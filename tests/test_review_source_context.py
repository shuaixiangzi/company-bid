import copy
from types import SimpleNamespace

import pytest

from company_bid.observability import redact_payload
from company_bid.review_source_context import material_stage_display


def stage_inputs():
    rid = "REQ"
    text = "合成要求：查询指定系统（https://long-test-host.invalid/check）并提供结果。"
    requirement = SimpleNamespace(requirement_id=rid, text=text, source_refs=["SRC"])
    catalog = {"requirements": {rid: requirement}, "requirement_quote_ids": {rid: "原文010"}}
    heading = f"【{rid}｜{text[:36]}…】"
    suffix = ("词典阶段：表达未支持，未检索；语义目录补查：已补查冻结目录，未找到可用候选（复用历史响应）；"
              "原件读取后匹配：未形成候选。")
    old_header = redact_payload(heading + suffix)
    assert old_header != heading + suffix
    assert old_header.endswith("[REDACTED_URL]")
    reason = "\n".join(["初始办理说明。", old_header, "原件读取：补充说明。"])
    after = {"reason": reason, "issue_id": "M-test"}
    issue = SimpleNamespace(issue_id="M-test", reason=reason, requirement_refs=[rid],
                            model_dump=lambda **kwargs: after)
    context = {"original_requirement": text, "deterministic_search_status": "not_searched",
               "requirement_id": rid, "current_content_matching": {"candidate_group_ids": []},
               "semantic_recall": {"requirement_id": rid, "catalog_stage_dictionary_search_status": "not_searched",
                                   "catalog_stage_dictionary_candidate_group_ids": [],
                                   "catalog_stage_status": "no_candidate_in_frozen_catalog",
                                   "status": "no_candidate_in_frozen_catalog", "historical_response_reused": True,
                                   "model_called": False, "candidates": [], "qualification_result": "not_determined",
                                   "absence_conclusion": "not_established"}}
    report = {"issue_id": "M-test", "after": copy.deepcopy(after), "before": {"reason": "初始办理说明。"},
              "rule": "append_scoped_stage_context_preserve_missing_conditions_and_provenance",
              "requirement_refs": [rid], "requirement_contexts": [context]}
    return issue, catalog, report, suffix


def test_recovers_scoped_stage_status_without_exposing_url_or_rewriting_original():
    issue, catalog, report, suffix = stage_inputs()
    retained = copy.deepcopy(report)
    reason, changes, ids = material_stage_display(issue.reason, issue, catalog, [report])
    change, = changes
    assert ids == ["原文010"]
    assert change["source_line_index"] == 1
    assert change["requirement_id"] == "REQ"
    assert change["source_refs"] == ["SRC"]
    assert change["basis"] == "n3_material_stage_line_redaction_recovery_v1"
    assert reason == f"初始办理说明。\n【原文010｜合成要求：查询指定系统…】{suffix}\n原件读取：补充说明。"
    assert issue.reason == retained["after"]["reason"]
    assert report == retained


@pytest.mark.parametrize("change", ["no_report", "wrong_after", "changed_state", "duplicated_context", "still_has_url"])
def test_requires_same_frozen_issue_line_and_exact_known_n3_outcome(change):
    issue, catalog, report, _ = stage_inputs()
    records = [report]
    display = issue.reason
    if change == "no_report":
        records = []
    if change == "wrong_after":
        report["after"]["reason"] += "追加。"
    if change == "changed_state":
        report["requirement_contexts"][0]["current_content_matching"]["candidate_group_ids"] = ["another"]
    if change == "duplicated_context":
        report["requirement_contexts"] *= 2
    if change == "still_has_url":
        display = display.replace("[REDACTED_URL]", "https://another.invalid")
    assert material_stage_display(display, issue, catalog, records) == (display, [], [])
