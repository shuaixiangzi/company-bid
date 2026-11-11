"""Source rules stay in a pending checklist, outside the bid's body and facts."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import DocumentPackage, PreparedBid, file_sha256
from company_bid.pipeline import build_executor
from company_bid.runtime import Executor, NodeSpec
from company_bid.structured import JsonTaskGateway


LIMIT = "最高限价为999999元，实际报价须由投标人明确输入。"
TERM = "服务期限为合同生效后30个日历日。"
SUBMIT = "响应文件须线下核对签章后按截止时间递交。"
EXPLANATION = "应说明商务偏离与报价构成。"
PRICE = "12345.67"


def automatic(tmp_path, monkeypatch, *, explanation=False, unresolved_format=False, linked_constraint=False):
    tender = tmp_path / "synthetic-tender.docx"
    document = Document()
    document.add_paragraph("第一章 商务与递交条件")
    for text in (LIMIT, TERM, SUBMIT):
        document.add_paragraph(text)
    if explanation:
        document.add_paragraph(EXPLANATION)
    document.add_paragraph("第二章 报价表")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "投标总价（元）"
    document.save(tender)
    explicit = tmp_path / "synthetic-project-input.docx"
    document = Document()
    document.add_paragraph("本项目明确输入的投标总价（元）：" + PRICE)
    document.save(explicit)
    def source(identity, path, kind, provenance):
        return {"source_id": identity, "title": "合成来源", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": provenance, "synthetic": True}
    manifest = {"project": {"project_id": "P", "name": "合成商务合同验证", "source_refs": ["T"]},
                "company": {"company_id": "C", "name": "合成测试公司", "source_refs": ["F"]},
                "sources": [source("T", tender, "tender", "synthetic"), source("F", explicit, "manual_input", "manual_input")],
                "source_roles": {"T": "tender", "F": "project_supplement"},
                "facts": [{"fact_id": "F-PRICE", "label": "投标总价（元）", "value": PRICE, "source_refs": ["F"]}],
                "synthetic": True, "preparation_note": "仅供合成验证，不可用于实际投标。"}
    def candidates(self, ctx, task_type, unit_id, payload, validate):
        assert task_type == "extract_requirements", "No mapping or writing model is needed"
        blocks = payload["window"]["blocks"]
        requirements = []
        for block in blocks:
            text = block.get("text", "")
            if text in (LIMIT, TERM, SUBMIT, EXPLANATION):
                mode = "procedure" if text != EXPLANATION else "writing"
                if unresolved_format and text == LIMIT:
                    mode = "fixed_text"
                item = {"citation": {key: block[key] for key in ("source_id", "block_id", "locator")},
                    "category": "procedure" if text == SUBMIT else "commercial", "response_mode": mode,
                    "format_status": "prescribed" if (unresolved_format or linked_constraint) and text == LIMIT else "not_prescribed"}
                if linked_constraint and text == LIMIT:
                    item["format_title"] = "第二章 报价表"
                requirements.append(item)
        heading = next(block for block in blocks if block.get("text") == "第二章 报价表")
        grid = next(block for block in blocks if block["kind"] == "table")
        return validate({"window_id": payload["window"]["window_id"], "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": requirements,
            "format_status": "specified", "format_chapters": [{"title": heading["text"], "level": 1,
                "citation": {key: heading[key] for key in ("source_id", "block_id", "locator")},
                "content_block_ids": [grid["block_id"]]}]})
    monkeypatch.setattr(JsonTaskGateway, "__call__", candidates)
    config = {"pipeline": "automatic", "provider": {"mode": "prepared"}, "planning": {"map_table_fields": False}}
    executor = build_executor(tmp_path / "runtime", config)
    return executor, {"manifest": manifest}


def planned(tmp_path, monkeypatch, **options):
    executor, initial = automatic(tmp_path, monkeypatch, **options)
    run = executor.create_run(initial)
    state = executor.execute(run, stop_after="N3")
    assert state["status"] == "paused_debug", state.get("error_code")
    return executor, run, executor.output(run, "N3")["prepared"]


def test_real_executor_keeps_pending_rules_outside_body_and_current_price(tmp_path, monkeypatch):
    executor, run, prepared = planned(tmp_path, monkeypatch)
    assert len(prepared["review_actions"]) == 3
    assert sorted(action["status"] for action in prepared["review_actions"]) == ["not_checked", "not_checked", "pending_manual"]
    assert len(prepared["facts"]) == 3  # explicit price plus current project/company names
    assert all(fact["value"] not in (LIMIT, TERM, SUBMIT, "999999") for fact in prepared["facts"])
    assert prepared["tables"][0]["rows"][0][1]["text"] == PRICE
    assert prepared["tables"][0]["rows"][0][1]["fact_refs"] == ["F-PRICE"]
    assert not prepared["issues"]
    assert sum(not section["render_body"] for section in prepared["sections"]) == 3
    assert executor.resume(run)["status"] == "succeeded"
    result = executor.output(run)
    package = executor.output(run, "N4")["package"]
    assert package["review_actions"] == prepared["review_actions"]
    rows = result["validation"]["requirement_coverage"]
    actions = {ref: action for action in prepared["review_actions"] for ref in action["requirement_refs"]}
    for row in rows:
        if row["requirement_id"] in actions:
            assert row["status"] == "no_body_required" and row["body_status"] == "not_required"
            assert row["review_status"] == actions[row["requirement_id"]]["status"]
            assert row["satisfaction_status"] == "not_determined"
    root = executor.store.root
    bid = Document(root / result["files"]["bid_docx"]["path"])
    checklist = Document(root / result["files"]["issues_docx"]["path"])
    body = "\n".join(paragraph.text for paragraph in bid.paragraphs)
    checklist_text = "\n".join(paragraph.text for paragraph in checklist.paragraphs)
    assert all(text not in body for text in (LIMIT, TERM, SUBMIT, "RA-"))
    assert bid.tables[0].cell(0, 1).text == PRICE
    assert "999999" not in "\n".join(cell.text for table in bid.tables for row in table.rows for cell in row.cells)
    assert all(text in checklist_text for text in (LIMIT, TERM, SUBMIT, "尚未核验", "待线下办理", "来源定位", "尚未认定满足"))
    assert "本次未自动检出待补项" not in checklist_text
    assert result["review_action_ids"] == [action["action_id"] for action in prepared["review_actions"]]
    assert inspect_diagnostics(root, run)["actual_call_attempts"] == 0
    state = executor.inspect(run)
    capture = next(capture for capture in state["nodes"][3]["attempts"][0]["captures"] if capture["name"] == "n3-planning-report")
    report = executor.store.read_json(capture["ref"])
    assert report["status"] == "ok_with_issues" and report["counts"]["pending_review"] == 3
    assert report["counts"]["commercial_constraints"] == 2 and report["counts"]["procedure_actions"] == 1


@pytest.mark.parametrize("failure", ["empty_requirements", "empty_sources", "unknown_requirement", "wrong_requirement",
                                     "unknown_source", "wrong_source", "changed_rule", "fulfilled_status", "duplicate_action"])
def test_review_action_references_and_rule_cannot_drift(tmp_path, monkeypatch, failure):
    _, _, value = planned(tmp_path, monkeypatch)
    action = value["review_actions"][0]
    if failure == "empty_requirements":
        action["requirement_refs"] = []
    elif failure == "empty_sources":
        action["source_refs"] = []
    elif failure == "unknown_requirement":
        action["requirement_refs"] = ["UNKNOWN"]
    elif failure == "wrong_requirement":
        action["requirement_refs"] = [next(item["requirement_id"] for item in value["requirements"] if item["response_mode"] == "fixed_text")]
    elif failure == "unknown_source":
        action["source_refs"] = ["UNKNOWN"]
    elif failure == "wrong_source":
        action["source_refs"] = ["F"]
    elif failure == "changed_rule":
        action["source_requirement"] = "我方报价为999999元。"
    elif failure == "fulfilled_status":
        action["status"] = "satisfied"
    else:
        value["review_actions"].append(copy.deepcopy(action))
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(value)


def test_legacy_prepared_and_package_default_to_no_review_actions(tmp_path, monkeypatch):
    executor, run, prepared = planned(tmp_path, monkeypatch)
    legacy = copy.deepcopy(prepared)
    legacy.pop("review_actions")
    legacy["input_kind"] = "manual_prepared"
    assert PreparedBid.model_validate(legacy).review_actions == []
    legacy["input_kind"] = "automatic"
    with pytest.raises(ValidationError, match="automatic non-body requirements"):
        PreparedBid.model_validate(legacy)
    assert executor.resume(run)["status"] == "succeeded"
    package = executor.output(run, "N4")["package"]
    package.pop("review_actions")
    assert DocumentPackage.model_validate(package).review_actions == []


def test_missing_actions_stop_automatic_pipeline_instead_of_claiming_no_body_success(tmp_path, monkeypatch):
    original, initial = automatic(tmp_path, monkeypatch)
    nodes = []
    for node in original.nodes:
        if node.key != "N4":
            nodes.append(node)
            continue
        def drop_actions(ctx, value, handler=node.handler):
            output = handler(ctx, value)
            output["package"]["review_actions"] = []
            return output
        nodes.append(NodeSpec(node.key, "synthetic-drop-actions", drop_actions, node.config_keys))
    executor = Executor(original.store.root, nodes, original.execution_config)
    state = executor.execute(executor.create_run(initial))
    assert state["status"] == "failed" and state["error_code"] == "NON_BODY_REVIEW_ACTION_MISSING"
    assert state["nodes"][-1]["status"] == "pending"


def test_non_body_requirement_cannot_hide_body_content(tmp_path, monkeypatch):
    executor, run, prepared = planned(tmp_path, monkeypatch)
    hidden = next(section for section in prepared["sections"] if not section["render_body"])
    hidden["fixed_content"] = [{"kind": "paragraph", "text": "不能隐藏的内容", "source_refs": ["T"]}]
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(prepared)
    assert executor.resume(run)["status"] == "succeeded"
    package = executor.output(run, "N4")["package"]
    hidden = next(section for section in package["sections"] if not section["render_body"])
    hidden["blocks"] = [{"kind": "paragraph", "text": "不能隐藏的内容", "content_role": "neutral"}]
    with pytest.raises(ValidationError):
        DocumentPackage.model_validate(package)


def test_required_commercial_explanation_is_not_silently_a_hidden_action(tmp_path, monkeypatch):
    _, _, prepared = planned(tmp_path, monkeypatch, explanation=True)
    requirement = next(item for item in prepared["requirements"] if item["text"] == EXPLANATION)
    section = next(section for section in prepared["sections"] if requirement["requirement_id"] in section["requirement_refs"])
    assert section["render_body"] and any(issue["section_id"] == section["section_id"] for issue in prepared["issues"])
    assert all(requirement["requirement_id"] not in action["requirement_refs"] for action in prepared["review_actions"])


def test_fixed_text_still_requires_a_grounded_prescribed_format(tmp_path, monkeypatch):
    executor, initial = automatic(tmp_path, monkeypatch, unresolved_format=True)
    state = executor.execute(executor.create_run(initial), stop_after="N3")
    assert state["status"] == "failed" and state["error_code"] == "REQUIRED_FORMAT_UNRESOLVED"


def test_commercial_action_can_share_a_prescribed_structural_section_without_rendering_rule(tmp_path, monkeypatch):
    executor, run, prepared = planned(tmp_path, monkeypatch, linked_constraint=True)
    limit = next(item for item in prepared["requirements"] if item["text"] == LIMIT)
    section = next(item for item in prepared["sections"] if limit["requirement_id"] in item["requirement_refs"])
    assert section["render_body"] and section["table_refs"]
    assert any(ref.startswith("R-FORM-") for ref in section["requirement_refs"])
    assert [item["kind"] for item in section["fixed_content"]] == ["table_ref"]
    action = next(item for item in prepared["review_actions"] if limit["requirement_id"] in item["requirement_refs"])
    assert action["status"] == "not_checked" and action["source_requirement"] == LIMIT
    assert executor.resume(run)["status"] == "succeeded"
    result = executor.output(run)
    bid = Document(executor.store.root / result["files"]["bid_docx"]["path"])
    checklist = Document(executor.store.root / result["files"]["issues_docx"]["path"])
    assert bid.tables[0].cell(0, 1).text == PRICE
    assert LIMIT not in "\n".join(paragraph.text for paragraph in bid.paragraphs)
    assert LIMIT in "\n".join(paragraph.text for paragraph in checklist.paragraphs)
