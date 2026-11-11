"""Factual qualifications stay source-backed or visibly missing; no model IO."""
from __future__ import annotations

from copy import deepcopy

from docx import Document
from PIL import Image
import pytest
from pydantic import ValidationError

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import DocumentPackage, PreparedBid, Requirement, file_sha256
from company_bid.pipeline import build_executor
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.structured import JsonTaskGateway


CONDITIONS = (
    "合成信用资格甲：响应人不得被列入经营异常名录；在本次响应截止日前已移出的，按移出记录处理。经营异常名录的状态核对时间应在截止日前三个工作日内。",
    "合成信用资格乙：响应人不得被列入失信被执行人名单；已于本次响应截止日前解除的，按解除记录处理。失信被执行人名单的状态应以本次响应截止日为准。",
)
LABELS = ("经营异常名录", "失信被执行人名单")
VALUES = ("合成明确输入甲：本项目指定时点未列入经营异常名录。", "合成明确输入乙：本项目指定时点未列入失信被执行人名单。")
FORMAT_BODY = "合成指定格式正文：请逐项列示经营异常名录状态、核对时点及移出记录说明。"


def setup_chain(tmp_path, monkeypatch, *, facts="missing", labels=True, category="qualification", format_case=None):
    conditions = list(CONDITIONS)
    if format_case in {"title_missing", "header_only", "numbered_header_only", "body"}:
        conditions[0] += "应按《信用声明书》格式响应。"
    tender = tmp_path / "synthetic-conditions.docx"
    document = Document()
    for condition in conditions:
        document.add_paragraph(condition)
    if format_case in {"header_only", "numbered_header_only", "body"}:
        document.add_paragraph("一、信用声明书" if format_case == "numbered_header_only" else "信用声明书", "Heading 1")
        if format_case == "body":
            document.add_paragraph(FORMAT_BODY)
    document.save(tender)
    fact_source = tmp_path / "synthetic-explicit-facts.docx"
    document = Document()
    document.add_paragraph("合成测试公司；只用于本项目的明确事实输入。")
    facts_data = []
    if facts in {"valid", "conflict", "historical_bid", "historical_material"}:
        for index, (label, value) in enumerate(zip(LABELS, VALUES)):
            document.add_paragraph(label + "：" + value)
            facts_data.append({"fact_id": f"F-{index}", "label": label, "value": value, "source_refs": ["F"]})
        if facts == "conflict":
            document.add_paragraph("合成冲突记录：经营异常名录状态待重新明确。")
            facts_data.append({"fact_id": "F-CONFLICT", "label": LABELS[0], "value": "合成冲突记录：状态待明确。", "source_refs": ["F"]})
    document.save(fact_source)
    proof = tmp_path / "synthetic-proof.png"
    Image.new("RGB", (40, 30), "white").save(proof)

    def source(identity, path, kind, provenance="synthetic"):
        return {"source_id": identity, "title": "合成事实响应来源", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": provenance, "synthetic": True}
    manifest = {"project": {"project_id": "P", "name": "合成事实响应验证", "source_refs": ["T"]},
                "company": {"company_id": "C", "name": "合成测试公司", "source_refs": ["F"]},
                "sources": [source("T", tender, "tender"), source("F", fact_source, "manual_input", "manual_input"),
                            source("A", proof, "attachment")],
                "source_roles": {"T": "tender", "F": "project_supplement", "A": "provenance_only"},
                "facts": facts_data,
                "attachments": [{"attachment_id": "UNSELECTED-PROOF", "section_id": "unassigned", "title": "经营异常名录证明", "description": "合成证明候选",
                                 "path": str(proof), "sha256": file_sha256(proof), "source_refs": ["A"],
                                 "requirement_refs": ["unassigned"], "synthetic": True}],
                "synthetic": True, "preparation_note": "仅供本地合成验证，不可投标。"}
    if facts in {"historical_bid", "historical_material"}:
        historical = next(item for item in manifest["sources"] if item["source_id"] == "F")
        historical.update(kind="company_material", provenance="derived_from_bid" if facts == "historical_bid" else "user_authorized_fact")
        manifest["source_roles"]["F"] = "reference_bid" if facts == "historical_bid" else "company_material"
    gateway_calls = []
    def candidate(self, ctx, task_type, unit_id, payload, validate):
        gateway_calls.append(task_type)
        assert task_type == "extract_requirements", "Factual responses must not request mapping or writing"
        selected = [block for block in payload["window"]["blocks"] if block.get("text") in conditions]
        requirements = []
        for block in selected:
            index = conditions.index(block["text"])
            item = {"citation": {key: block[key] for key in ("source_id", "block_id", "locator")},
                    "category": category, "response_mode": "factual_response", "mandatory": True,
                    "format_status": format_case if format_case in {"unread", "conflict", "prescribed"} else "not_prescribed",
                    "required_fact_labels": [LABELS[index]] if labels else [],
                    "attachment_keywords": [LABELS[index]]}
            if format_case == "title_missing" and index == 0:
                item["format_title"] = "信用声明书"
            if format_case in {"header_only", "numbered_header_only", "body"} and index == 0:
                item.update(format_status="prescribed", format_title="信用声明书")
            requirements.append(item)
        formats = []
        if format_case in {"header_only", "numbered_header_only", "body"}:
            heading = next(block for block in payload["window"]["blocks"] if block.get("text") in {"信用声明书", "一、信用声明书"})
            content_ids = [heading["block_id"]]
            if format_case == "body":
                body = next(block for block in payload["window"]["blocks"] if block.get("text") == FORMAT_BODY)
                content_ids.append(body["block_id"])
            formats.append({"title": "信用声明书", "level": 1, "status": "prescribed",
                            "citation": {key: heading[key] for key in ("source_id", "block_id", "locator")},
                            "content_block_ids": content_ids, "requirement_indices": [0]})
        return validate({"window_id": payload["window"]["window_id"], "processing_declaration": "processed_not_semantically_accepted",
                         "unresolved_block_ids": [], "requirements": requirements, "format_chapters": formats,
                         "format_status": "specified" if formats else "none_verified"})
    def no_generation(*args, **kwargs):
        pytest.fail("A factual response cannot call a proposal/model provider")
    monkeypatch.setattr(JsonTaskGateway, "__call__", candidate)
    monkeypatch.setattr(PreparedProposalProvider, "generate", no_generation)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", no_generation)
    config = {"pipeline": "automatic", "debug_capture": True, "provider": {"mode": "prepared"},
              "planning": {"map_table_fields": False}}
    executor = build_executor(tmp_path / "runtime", config)
    run = executor.create_run({"manifest": manifest})
    return executor, run, conditions, gateway_calls


def assert_factual_route(executor, run, conditions):
    prepared = executor.output(run, "N3")["prepared"]
    assert len(prepared["requirements"]) == 2
    assert {item["text"] for item in prepared["requirements"]} == set(conditions)
    assert all(item["response_mode"] == "factual_response" for item in prepared["requirements"])
    assert not prepared["attachments"] and not prepared["review_actions"]
    assert all(section["render_body"] and section["kind"] != "technical" and not section["writing_tasks"] for section in prepared["sections"])
    assert all(any(block["text"] == "响应要求（招标原文）：\n" + requirement["text"] for block in section["fixed_content"])
               for requirement in prepared["requirements"] for section in prepared["sections"]
               if requirement["requirement_id"] in section["requirement_refs"])
    result = executor.output(run)
    assert result["validation"]["writing_units"] == []
    assert all(row["status"] != "no_body_required" and row.get("satisfaction_status") != "satisfied"
               for row in result["validation"]["requirement_coverage"])
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0
    document = Document(executor.store.root / result["files"]["bid_docx"]["path"])
    body = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert all(condition in body for condition in conditions)
    assert "截图" not in body and "我方承诺" not in body
    return prepared, result, body


@pytest.mark.parametrize("category", ["qualification", "commercial"])
def test_full_n1_n6_missing_facts_keep_conditions_and_visible_issues(tmp_path, monkeypatch, category):
    executor, run, conditions, calls = setup_chain(tmp_path, monkeypatch, category=category)
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert all(node["status"] == "succeeded" for node in state["nodes"])
    assert [node["key"] for node in state["nodes"]] == [f"N{index}" for index in range(7)]
    prepared, result, body = assert_factual_route(executor, run, conditions)
    assert len(prepared["issues"]) == 2 and all(not section["fact_refs"] for section in prepared["sections"])
    assert {item["source_requirement"] for item in prepared["issues"]} == set(conditions)
    assert all(row["status"] == "pending_manual" for row in result["validation"]["requirement_coverage"])
    assert "【待补 M-" in body and calls == ["extract_requirements"]
    assert all(value not in body for value in VALUES)


def test_explicit_source_facts_are_adopted_without_generating_credit_results(tmp_path, monkeypatch):
    executor, run, conditions, calls = setup_chain(tmp_path, monkeypatch, facts="valid")
    assert executor.execute(run)["status"] == "succeeded"
    prepared, result, body = assert_factual_route(executor, run, conditions)
    assert not prepared["issues"]
    assert {ref for section in prepared["sections"] for ref in section["fact_refs"]} == {"F-0", "F-1"}
    assert all(value in body for value in VALUES)
    package = executor.output(run, "N4")["package"]
    adopted = [block for section in package["sections"] for block in section["blocks"] if block.get("content_role") == "fact"]
    assert {tuple(block["fact_refs"]) for block in adopted} == {("F-0",), ("F-1",)}
    assert all(block["source_refs"] == ["F"] for block in adopted)
    assert all(row["status"] == "drafted_for_review" for row in result["validation"]["requirement_coverage"])
    assert calls == ["extract_requirements"]


def test_conflicting_facts_are_missing_instead_of_arbitrarily_selected(tmp_path, monkeypatch):
    executor, run, conditions, _ = setup_chain(tmp_path, monkeypatch, facts="conflict")
    assert executor.execute(run)["status"] == "succeeded"
    prepared, result, body = assert_factual_route(executor, run, conditions)
    assert len(prepared["issues"]) == 1
    assert {ref for section in prepared["sections"] for ref in section["fact_refs"]} == {"F-1"}
    assert VALUES[0] not in body and VALUES[1] in body
    assert "合成冲突记录：状态待明确。" not in body
    assert sorted(row["status"] for row in result["validation"]["requirement_coverage"]) == ["drafted_for_review", "pending_manual"]


@pytest.mark.parametrize("historical", ["historical_bid", "historical_material"])
def test_historical_credit_values_are_not_current_project_facts(tmp_path, monkeypatch, historical):
    executor, run, conditions, calls = setup_chain(tmp_path, monkeypatch, facts=historical)
    assert executor.execute(run)["status"] == "succeeded"
    prepared, result, body = assert_factual_route(executor, run, conditions)
    # The exact same label/value pairs remain available in the frozen inputs;
    # their historical source identity disqualifies them for this response.
    frozen = executor.output(run, "N2")["manifest"]
    assert {fact["value"] for fact in frozen["facts"]} == set(VALUES)
    historical_source = next(item for item in frozen["sources"] if item["source_id"] == "F")
    assert historical_source["kind"] == "company_material"
    assert historical_source["provenance"] == ("derived_from_bid" if historical == "historical_bid" else "user_authorized_fact")
    assert frozen["source_roles"]["F"] == ("reference_bid" if historical == "historical_bid" else "company_material")
    assert len(prepared["issues"]) == 2 and all(not section["fact_refs"] for section in prepared["sections"])
    assert all(row["status"] == "pending_manual" for row in result["validation"]["requirement_coverage"])
    assert all(value not in body for value in VALUES) and all(condition in body for condition in conditions)
    checklist = Document(executor.store.root / result["files"]["issues_docx"]["path"])
    checklist_text = "\n".join(paragraph.text for paragraph in checklist.paragraphs)
    assert all(value not in checklist_text for value in VALUES)
    assert calls == ["extract_requirements"]


def test_no_fact_label_is_an_issue_even_when_library_has_related_values(tmp_path, monkeypatch):
    executor, run, conditions, _ = setup_chain(tmp_path, monkeypatch, facts="valid", labels=False)
    assert executor.execute(run)["status"] == "succeeded"
    prepared, _, body = assert_factual_route(executor, run, conditions)
    assert len(prepared["issues"]) == 2 and all(not section["fact_refs"] for section in prepared["sections"])
    assert all(value not in body for value in VALUES)


@pytest.mark.parametrize("format_case", ["unread", "conflict", "prescribed", "title_missing"])
def test_factual_mode_never_grants_permission_to_bypass_original_format(tmp_path, monkeypatch, format_case):
    executor, run, _, calls = setup_chain(tmp_path, monkeypatch, format_case=format_case)
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "REQUIRED_FORMAT_UNRESOLVED"
    assert state["nodes"][3]["status"] == "failed"
    assert all(node["status"] == "pending" for node in state["nodes"][4:])
    assert calls == ["extract_requirements"]
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("format_case", ["header_only", "numbered_header_only"])
def test_header_only_prescribed_format_is_unresolved_even_with_all_facts(tmp_path, monkeypatch, format_case):
    executor, run, _, calls = setup_chain(tmp_path, monkeypatch, facts="valid", format_case=format_case)
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "REQUIRED_FORMAT_UNRESOLVED"
    assert state["nodes"][2]["status"] == "succeeded" and state["nodes"][3]["status"] == "failed"
    assert all(node["status"] == "pending" for node in state["nodes"][4:])
    extracted = executor.output(run, "N2")["extracted"]
    chapter = extracted["format_chapters"][0]
    assert chapter["title"] == "信用声明书" and len(chapter["content_block_ids"]) == 1
    assert chapter["content_block_ids"] == [chapter["citation"]["block_id"]]
    parsed = executor.output(run, "N1")["parsed"]
    heading = next(block for document in parsed["documents"] for unit in document["units"] for block in unit["blocks"]
                   if block["block_id"] == chapter["citation"]["block_id"])
    assert heading["text"] == ("一、信用声明书" if format_case == "numbered_header_only" else "信用声明书")
    assert {fact["fact_id"] for fact in executor.output(run, "N2")["manifest"]["facts"]} == {"F-0", "F-1"}
    assert calls == ["extract_requirements"]
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


def test_substantive_prescribed_paragraph_survives_and_source_facts_are_adopted(tmp_path, monkeypatch):
    executor, run, conditions, calls = setup_chain(tmp_path, monkeypatch, facts="valid", format_case="body")
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    section = next(item for item in prepared["sections"] if item["title"] == "信用声明书")
    paragraphs = [item for item in section["fixed_content"] if item["kind"] == "paragraph"]
    original_body = next(item for item in paragraphs if item["text"] == FORMAT_BODY)
    source_rule = next(item for item in paragraphs if item["text"] == "响应要求（招标原文）：\n" + conditions[0])
    assert original_body["source_refs"] and source_rule["source_refs"]
    assert original_body["source_refs"] != source_rule["source_refs"]
    assert "F-0" in section["fact_refs"] and not section["writing_tasks"]
    assert not prepared["issues"] and not prepared["review_actions"] and not prepared["attachments"]
    extracted = executor.output(run, "N2")["extracted"]
    chapter = extracted["format_chapters"][0]
    assert len(chapter["content_block_ids"]) == 2
    parsed = executor.output(run, "N1")["parsed"]
    original_blocks = {block["block_id"]: block for document in parsed["documents"] for unit in document["units"] for block in unit["blocks"]}
    assert [original_blocks[key]["text"] for key in chapter["content_block_ids"]] == ["信用声明书", FORMAT_BODY]
    assert all(not original_blocks[key]["text"].startswith("响应要求（招标原文）") for key in chapter["content_block_ids"])
    result = executor.output(run)
    document = Document(executor.store.root / result["files"]["bid_docx"]["path"])
    body = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert FORMAT_BODY in body and all(text in body for text in (*conditions, *VALUES))
    assert all(row["status"] != "no_body_required" and row.get("satisfaction_status") != "satisfied"
               for row in result["validation"]["requirement_coverage"])
    assert calls == ["extract_requirements"] and inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("category", ["technical", "fixed_table", "procedure"])
def test_requirement_contract_rejects_nonfactual_categories(category):
    with pytest.raises(ValidationError, match="qualification or commercial"):
        Requirement(requirement_id="R", text="合成条件", category=category, response_mode="factual_response", source_refs=["T"])


def test_factual_requirement_cannot_be_hidden_or_relabelled_as_review_action(tmp_path, monkeypatch):
    executor, run, _, _ = setup_chain(tmp_path, monkeypatch)
    assert executor.execute(run)["status"] == "succeeded"
    original = executor.output(run, "N3")["prepared"]
    hidden = deepcopy(original)
    hidden["sections"][0].update(render_body=False, fixed_content=[])
    with pytest.raises(ValidationError, match="only procedural requirements"):
        PreparedBid.model_validate(hidden)
    actions = deepcopy(original)
    requirement = actions["requirements"][0]
    actions["review_actions"] = [{"action_id": "RA-INVALID", "kind": "commercial_constraint", "status": "not_checked",
                                  "requirement_refs": [requirement["requirement_id"]], "source_refs": requirement["source_refs"],
                                  "source_requirement": requirement["text"], "instruction": "合成待核对"}]
    with pytest.raises(ValidationError, match="compatible non-body"):
        PreparedBid.model_validate(actions)
    package = executor.output(run, "N4")["package"]
    package["sections"][0].update(render_body=False, blocks=[])
    with pytest.raises(ValidationError, match="non-procedure requirement hidden"):
        DocumentPackage.model_validate(package)
