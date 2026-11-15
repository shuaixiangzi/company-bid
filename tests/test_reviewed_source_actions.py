from copy import deepcopy
import hashlib

import pytest

from company_bid.extraction import PlanningError
from company_bid.reviewed_source_actions import VERSION, reviewed_source_actions


def fixture():
    text = ["12.2 通知应送达约定地点，", "可以通过专人或双方确认的方式送达。"]
    blocks = {f"B{i}": {"block_id": f"B{i}", "source_id": "S", "kind": "paragraph", "origin": "native",
                        "text": t, "locator": f"pdf/page[1]/line[{i}]", "ordinal": i, "page": 1, "unit_id": "U"}
              for i, t in enumerate(text)}
    documents = [{"source_id": "S", "sha256": "a" * 64, "role": "tender"}]
    config = {"version": VERSION, "review_sha256": "b" * 64, "reviewer": "assistant_source_review",
              "items": [{"source_id": "S", "source_sha256": "a" * 64, "block_ids": ["B0", "B1"],
                         "quote_sha256": hashlib.sha256("\n".join(text).encode()).hexdigest(),
                         "category": "commercial", "instruction": "确认通知渠道并按原条款送达。",
                         "review_reason": "已有联系字段待填，但未单独保留送达渠道条件。"}]}
    return config, blocks, documents, []


def test_explicit_review_reconstructs_full_original_and_stays_pending():
    args = fixture(); before = deepcopy(args)
    requirements, report = reviewed_source_actions(*args)
    assert args == before and report["n2_candidate_mutation"] is False
    assert requirements[0]["text"] == "\n".join(b["text"] for b in args[1].values())
    assert requirements[0]["response_mode"] == "procedure" and requirements[0]["window_ids"] == []
    assert report["items"][0]["satisfaction_status"] == "not_determined"


@pytest.mark.parametrize("change", ["source", "hash", "quote", "order", "gap", "duplicate", "non_tender", "ocr", "classification"])
def test_changed_or_unsupported_review_cannot_silently_add_requirements(change):
    config, blocks, documents, existing = fixture()
    item = config["items"][0]
    if change == "source": item["source_id"] = "UNKNOWN"
    if change == "hash": item["source_sha256"] = "c" * 64
    if change == "quote": item["quote_sha256"] = "c" * 64
    if change == "order": item["block_ids"].reverse()
    if change == "gap": blocks["B1"]["ordinal"] = 2
    if change == "duplicate": existing.append({"text": "\n".join(b["text"] for b in blocks.values())})
    if change == "non_tender": documents[0]["role"] = "company_material"
    if change == "ocr": blocks["B0"]["origin"] = "ocr"
    if change == "classification": item["category"] = "technical"
    with pytest.raises(PlanningError):
        reviewed_source_actions(config, blocks, documents, existing)


def test_no_review_does_not_generate_rules_from_familiar_words():
    _, blocks, documents, existing = fixture()
    assert reviewed_source_actions(None, blocks, documents, existing)[0] == []


def runtime_fixture():
    from company_bid.extraction import _id
    config, blocks, documents, existing = fixture()
    additions, report = reviewed_source_actions(config, blocks, documents, existing)
    req = additions[0]; citation = req["citations"][0]; ref = _id("SCOPE-", citation)
    source = {"source_id": "S", "sha256": "a" * 64, "kind": "tender"}
    n1 = {"manifest": {"sources": [source]}, "parsed": {"documents": [{**documents[0], "format": "pdf",
        "units": [{"unit_id": "U", "source_id": "S", "page": 1, "locator": "pdf/page[1]",
                   "coverage": {"status": "read"}, "blocks": list(blocks.values())}]}]}}
    locator = (f"{citation['locator']}; basis={citation['basis']}; chars[{citation['start']}:{citation['end']}]; "
               f"quote_sha256={citation['quote_sha256']}; through={citation['end_locator']}; blocks=" + ",".join(citation["block_ids"]))
    prepared = {"sources": [{**source, "source_id": ref, "locator": locator}],
        "requirements": [{**{key: req[key] for key in ("requirement_id", "text", "category", "mandatory", "response_mode")},
                          "source_refs": [ref], "source_contexts": []}],
        "sections": [{"writing_tasks": []}],
        "review_actions": [{"action_id": _id("RA-", req["requirement_id"]), "kind": "commercial_constraint",
            "status": "not_checked", "requirement_refs": [req["requirement_id"]], "source_refs": [ref],
            "source_requirement": req["text"], "instruction": config["items"][0]["instruction"],
            "source_scope_status": "not_applicable", "source_scope_contexts": []}]}
    return {"reviewed_source_actions": report}, prepared, n1, {"reviewed_source_actions": config}, []


def test_runtime_independently_rebuilds_source_and_actual_pending_action():
    from company_bid.project_constraint_runtime import _reviewed_action_requirements
    result = _reviewed_action_requirements(*runtime_fixture())
    assert len(result) == 1 and result[0]["response_mode"] == "procedure"


@pytest.mark.parametrize("change", ["text", "source", "action", "status", "writing", "report"])
def test_runtime_rejects_self_reported_or_adopted_review(change):
    from company_bid.project_constraint_runtime import _reviewed_action_requirements
    from company_bid.project_constraints import ProjectConstraintError
    report, prepared, n1, config, existing = runtime_fixture()
    if change == "text": prepared["requirements"][0]["text"] = "公司已完成全部事项。"
    if change == "source": prepared["sources"][0]["sha256"] = "c" * 64
    if change == "action": prepared["review_actions"][0]["instruction"] = "无需核对。"
    if change == "status": prepared["review_actions"][0]["status"] = "complete"
    if change == "writing": prepared["sections"][0]["writing_tasks"].append({"requirement_refs": [prepared["requirements"][0]["requirement_id"]]})
    if change == "report": report["reviewed_source_actions"]["items"][0]["review_reason"] = "不同的审查记录"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_SOURCE_REVIEW_CHANGED"):
        _reviewed_action_requirements(report, prepared, n1, config, existing)
