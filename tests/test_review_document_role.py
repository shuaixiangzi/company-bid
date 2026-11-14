"""Response coverage and companion review content share one offline executor."""
from __future__ import annotations

from copy import deepcopy
from zipfile import ZipFile

import pytest
from docx import Document
from pydantic import ValidationError

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import DocumentPackage, PreparedBid, Section, SectionPlan
from company_bid.pipeline import build_executor
from company_bid.rendering import DIAGNOSTIC_MARKER, render_package
from company_bid.runtime import Executor, NodeSpec
from company_bid.sample import create_synthetic_sample


ORIGINAL = "合同原文：除非另有书面约定，履约期限由双方核对；第0条的回指尚未澄清。"
REVIEW_TITLE = "合同原文与内控核对"
REVIEW_CELL = "原表完整续文，未经核对不得视为投标承诺。"
PROPOSAL = {"paragraphs": ["拟核对许可与隔离状态，按测点编号记录数据，复测异常并交付完整记录。"]}


def prepared_data(tmp_path):
    data = create_synthetic_sample(tmp_path / "input").model_dump(mode="json")
    data["sections"].insert(0, {"section_id": "S-REVIEW", "title": REVIEW_TITLE,
        "document_role": "review", "kind": "commercial", "requirement_refs": ["R-004"],
        "source_refs": ["S-TENDER"], "table_refs": ["T-REVIEW"], "attachment_refs": ["A-REVIEW"],
        "fixed_content": [{"kind": "paragraph", "text": ORIGINAL, "source_refs": ["S-TENDER"]},
                          {"kind": "table_ref", "table_ref": "T-REVIEW", "source_refs": ["S-TENDER"]}]})
    data["tables"].append({"table_id": "T-REVIEW", "section_id": "S-REVIEW", "title": "原文核对表",
        "columns": ["条款", "未定值"], "rows": [[{"text": REVIEW_CELL}, {"issue_refs": ["M-002"]}]],
        "source_refs": ["S-TENDER"], "requirement_refs": ["R-004"]})
    data["attachments"].append({**data["attachments"][0], "attachment_id": "A-REVIEW",
        "section_id": "S-REVIEW", "title": "待核对的原图", "requirement_refs": ["R-004"]})
    data["issues"].append({"issue_id": "M-002", "title": "合同字段待核", "section_id": "S-REVIEW",
        "requirement_refs": ["R-004"], "reason": "原文条件尚未核定。", "action": "线下核对原文并补充约定。",
        "source_requirement": ORIGINAL})
    return data


def execute_prepared(tmp_path, *, diagnostic=False, debug=True, mutate_output=None):
    data = prepared_data(tmp_path)
    config = {"provider": {"mode": "prepared"}, "debug_capture": debug}
    if diagnostic:
        config["run_purpose"] = "diagnostic_replay"
    production = build_executor(tmp_path / "runtime", config)
    if mutate_output:
        nodes = []
        for node in production.nodes:
            if node.key != "N4":
                nodes.append(node)
                continue
            def changed(ctx, value, handler=node.handler):
                result = handler(ctx, value)
                mutate_output(result)
                return result
            nodes.append(NodeSpec("N4", "synthetic-role-output-mutation", changed, node.config_keys))
        production = Executor(production.store.root, nodes, config)
    run = production.create_run({"prepared": data, "proposals": {"S2": PROPOSAL}})
    done = production.execute(run)
    return production, run, done


def document_text(path):
    document = Document(path)
    return document, "\n".join(paragraph.text for paragraph in document.paragraphs)


def package_data(tmp_path):
    executor, run, done = execute_prepared(tmp_path)
    assert done["status"] == "succeeded", done.get("error_code")
    return executor.output(run, "N4")["package"]


@pytest.mark.parametrize("model", [SectionPlan, Section])
def test_legacy_section_defaults_to_response_and_role_values_are_strict(model):
    data = {"section_id": "S", "title": "合成位置", "requirement_refs": ["R"]}
    data.update({"kind": "commercial"} if model is SectionPlan else
                {"blocks": [{"kind": "paragraph", "text": "合成响应"}]})
    assert model.model_validate(data).document_role == "response"
    with pytest.raises(ValidationError, match="document_role"):
        model.model_validate({**data, "document_role": "fulfilled"})


def test_legacy_prepared_without_roles_still_runs_through_shared_executor(tmp_path):
    data = create_synthetic_sample(tmp_path / "legacy").model_dump(mode="json")
    for section in data["sections"]:
        section.pop("document_role")
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}})
    run = executor.create_run({"prepared": data, "proposals": {"S2": PROPOSAL}})
    done = executor.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    assert set(executor.output(run, "N4")["expected_section_document_roles"].values()) == {"response"}
    result = executor.output(run)
    assert result["validation"]["review_section_count"] == 0
    bid, _ = document_text(executor.store.root / result["files"]["bid_docx"]["path"])
    assert len(bid.tables) == 2


def test_procedure_review_remains_non_body_and_keeps_source_without_false_all_clear(tmp_path):
    data = create_synthetic_sample(tmp_path / "procedure").model_dump(mode="json")
    data.update(requirements=[{"requirement_id": "R-PROCEDURE", "text": ORIGINAL,
        "category": "procedure", "response_mode": "procedure", "source_refs": ["S-TENDER"]}],
        attachments=[], tables=[], issues=[], sections=[{"section_id": "REVIEW-PROCEDURE", "title": REVIEW_TITLE,
        "kind": "commercial", "document_role": "review", "requirement_refs": ["R-PROCEDURE"],
        "fixed_content": [{"kind": "paragraph", "text": ORIGINAL, "source_refs": ["S-TENDER"]}]}])
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}})
    run = executor.create_run({"prepared": data})
    done = executor.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    result = executor.output(run)
    row, = result["validation"]["requirement_coverage"]
    assert row["status"] == "no_body_required" and row["body_status"] == "not_required"
    assert row["response_section_ids"] == [] and row["review_section_ids"] == ["REVIEW-PROCEDURE"]
    assert row["satisfaction_status"] == "not_determined"
    _, text = document_text(executor.store.root / result["files"]["issues_docx"]["path"])
    assert ORIGINAL in text and "本次未自动检出待补项" not in text


@pytest.mark.parametrize("target", ["prepared", "package"])
def test_review_cannot_replace_required_response_section(tmp_path, target):
    data = prepared_data(tmp_path) if target == "prepared" else package_data(tmp_path)
    next(item for item in data["sections"] if item["section_id"] == "S3")["document_role"] = "review"
    with pytest.raises(ValidationError, match="cannot replace a required response"):
        (PreparedBid if target == "prepared" else DocumentPackage).model_validate(data)


@pytest.mark.parametrize("target", ["prepared", "package"])
def test_review_requires_same_requirement_pending_marker_in_response(tmp_path, target):
    data = prepared_data(tmp_path) if target == "prepared" else package_data(tmp_path)
    # The full source and its review-side issue survive; they do not supply the
    # missing response marker, even though the response table is still present.
    data["issues"] = [issue for issue in data["issues"] if issue["issue_id"] != "M-001"]
    if target == "prepared":
        next(table for table in data["tables"] if table["table_id"] == "T-PRICE")["rows"][0][1]["issue_refs"] = []
    else:
        response = next(section for section in data["sections"] if section["section_id"] == "S3")
        response["blocks"] = [block for block in response["blocks"] if block.get("issue_id") != "M-001"]
        next(block["table"] for block in response["blocks"] if block["kind"] == "table")["rows"][0][1]["issue_refs"] = []
    with pytest.raises(ValidationError, match="own pending issue in the response"):
        (PreparedBid if target == "prepared" else DocumentPackage).model_validate(data)


def test_issue_assigned_to_response_still_needs_actual_body_marker(tmp_path):
    data = package_data(tmp_path)
    response = next(section for section in data["sections"] if section["section_id"] == "S3")
    response["blocks"] = [{"kind": "paragraph", "text": "请核对配套清单。", "requirement_refs": ["R-004"]}]
    with pytest.raises(ValidationError, match="matching body marker"):
        DocumentPackage.model_validate(data)


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("debug", [False, True])
def test_one_executor_routes_all_review_content_without_claiming_response(tmp_path, diagnostic, debug):
    executor, run, done = execute_prepared(tmp_path, diagnostic=diagnostic, debug=debug)
    assert done["status"] == "succeeded", done.get("error_code")
    result = executor.output(run)
    if diagnostic:
        assert result["publication_status"] == "diagnostic_only" and "files" not in result
    files = result["diagnostic_files" if diagnostic else "files"]
    bid, bid_text = document_text(executor.store.root / files["bid_docx"]["path"])
    review, review_text = document_text(executor.store.root / files["issues_docx"]["path"])
    assert ORIGINAL not in bid_text and REVIEW_TITLE not in bid_text and "M-002" not in bid_text
    assert "【待补 M-001】" in bid.tables[1].cell(1, 1).text
    assert ORIGINAL in review_text and REVIEW_TITLE in review_text
    assert "配套清单位置：" in review_text and "尚未认定满足" in review_text
    assert any(REVIEW_CELL in cell.text and "完整续文" in cell.text for table in review.tables for row in table.rows for cell in row.cells)
    assert any("【待补 M-002】" in cell.text for table in review.tables for row in table.rows for cell in row.cells)
    assert len(bid.tables) == 2 and len(review.tables) == 1
    for name in ("bid_docx", "issues_docx"):
        with ZipFile(executor.store.root / files[name]["path"]) as archive:
            assert any(path.startswith("word/media/") for path in archive.namelist())
    if diagnostic:
        assert DIAGNOSTIC_MARKER in bid_text and DIAGNOSTIC_MARKER in review_text
    output = executor.output(run, "N4")
    assert output["expected_section_document_roles"]["S-REVIEW"] == "review"
    section = next(section for section in output["package"]["sections"] if section["section_id"] == "S-REVIEW")
    assert section["document_role"] == "review"
    row = next(row for row in result["validation"]["requirement_coverage"] if row["requirement_id"] == "R-004")
    assert row["status"] == row["body_status"] == "pending_manual"
    assert row["response_section_ids"] == ["S3"] and row["review_section_ids"] == ["S-REVIEW"]
    assert row["response_issue_ids"] == ["M-001"] and row["issue_ids"] == ["M-001", "M-002"]
    assert row["review_status"] == "not_checked" and row["satisfaction_status"] == "not_determined"
    checks = result["validation"]
    assert checks["response_table_count"] == 2 and checks["review_table_count"] == 1 and checks["table_count"] == 3
    assert checks["response_section_count"] == 4 and checks["review_section_count"] == 1
    assert checks["response_attachment_count"] == checks["review_attachment_count"] == 1
    assert checks["response_issue_count"] == checks["review_issue_count"] == 1
    assert next(link for link in checks["table_requirement_links"] if link["table_id"] == "T-REVIEW")["document_role"] == "review"
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


@pytest.mark.parametrize("target", ["prepared", "package"])
def test_source_context_writing_cannot_be_moved_to_review_and_replaced_with_warning(tmp_path, monkeypatch, target):
    from test_parent_context_pipeline import planned, runner
    if target == "prepared":
        data, _ = planned(tmp_path)
    else:
        executor, run, _ = runner(tmp_path, monkeypatch)
        done = executor.execute(run)
        assert done["status"] == "succeeded", done.get("error_code")
        data = executor.output(run, "N4")["package"]
    moved = [section for section in data["sections"]
             if set(section["requirement_refs"]) & {item["requirement_id"] for item in data["requirements"]
                                                    if item["response_mode"] == "writing"}]
    for section in moved:
        section["document_role"] = "review"
    refs = list(dict.fromkeys(ref for section in moved for ref in section["requirement_refs"]))
    pending = {"section_id": "RESPONSE-PENDING", "title": "技术方案待补", "requirement_refs": refs}
    if target == "prepared":
        pending["kind"] = "facts"
    else:
        pending["blocks"] = []
    for index, ref in enumerate(refs, 900):
        issue = {"issue_id": f"M-{index}", "title": "技术方案待补", "section_id": pending["section_id"],
                 "requirement_refs": [ref], "reason": "尚未响应。", "action": "补充技术方案。", "source_requirement": "合成技术要求"}
        data["issues"].append(issue)
        if target == "package":
            pending["blocks"].append({"kind": "issue", "issue_id": issue["issue_id"], "requirement_refs": [ref]})
    data["sections"].append(pending)
    message = "writing.*task" if target == "prepared" else "cannot replace technical proposal"
    with pytest.raises(ValidationError, match=message):
        (PreparedBid if target == "prepared" else DocumentPackage).model_validate(data)


@pytest.mark.parametrize("damage", ["role", "drop_expected", "expected"])
def test_n5_rejects_role_changes_after_n4(tmp_path, damage):
    def mutate(output):
        if damage == "role":
            # This is a schema-valid relocation; the N4 declaration must still
            # prevent a later node from quietly publishing the quoted source.
            output["package"]["sections"][0]["document_role"] = "response"
        elif damage == "drop_expected":
            output.pop("expected_section_document_roles")
        else:
            output["expected_section_document_roles"]["S-REVIEW"] = "response"
    executor, run, done = execute_prepared(tmp_path, mutate_output=mutate)
    assert done["status"] == "failed" and done["error_code"] == "SECTION_DOCUMENT_ROLE_MISMATCH"
    assert done["nodes"][-1]["status"] == "pending"
    assert not list((executor.store.root / "runs" / run).rglob("*.docx"))


def test_rendering_keeps_heading_list_refs_and_source_text_in_companion(tmp_path):
    data = package_data(tmp_path)
    section = data["sections"][0]
    section["blocks"].insert(0, {"kind": "heading", "text": "完整核对分项", "requirement_refs": ["R-004"]})
    section["blocks"].insert(1, {"kind": "list", "items": ["原始条件甲", "原始例外乙"], "ordered": False,
                                 "content_role": "neutral", "source_refs": ["S-TENDER"], "requirement_refs": ["R-004"]})
    output = render_package(DocumentPackage.model_validate(data), tmp_path / "extra-blocks")
    _, bid = document_text(output["bid_docx"]["path"])
    _, companion = document_text(output["issues_docx"]["path"])
    assert all(text not in bid and text in companion for text in ("完整核对分项", "原始条件甲", "原始例外乙"))
    assert "S-TENDER" in companion and data["sources"][0]["sha256"] in companion


def test_prescribed_cover_cannot_be_moved_to_review_even_with_other_response_marker(tmp_path):
    from test_prescribed_cover_rendering import prepared_data as cover_prepared, package_data as cover_package
    data = cover_prepared(tmp_path)
    cover = data["sections"][0]
    cover["document_role"] = "review"
    data["sections"].append({"section_id": "COVER-PENDING", "title": "封面待补", "kind": "facts",
                             "requirement_refs": ["R-COVER"]})
    data["issues"].append({**data["issues"][0], "issue_id": "M-099", "section_id": "COVER-PENDING"})
    with pytest.raises(ValidationError, match="prescribed cover must be visible"):
        PreparedBid.model_validate(data)
    # package_data intentionally predates roles; apply role and pending marker
    # after conversion to exercise the rendered-package boundary independently.
    other = tmp_path / "other"
    other.mkdir()
    original = cover_prepared(other)
    package = cover_package(original)
    package["sections"][0]["document_role"] = "review"
    package["sections"].append({"section_id": "COVER-PENDING", "title": "封面待补", "requirement_refs": ["R-COVER"],
                               "blocks": [{"kind": "issue", "issue_id": "M-099", "requirement_refs": ["R-COVER"]}]})
    package["issues"].append({**package["issues"][0], "issue_id": "M-099", "section_id": "COVER-PENDING"})
    with pytest.raises(ValidationError, match="prescribed cover must be visible"):
        DocumentPackage.model_validate(package)


def test_production_n2_n6_contract_planning_uses_review_role_and_pending_response(tmp_path, monkeypatch):
    from test_contract_source_review_plan import runtime
    executor, run = runtime(tmp_path, monkeypatch, partial=True, old_pending=True)
    done = executor.execute(run, stop_after="N2")
    assert done["status"] == "paused_debug", done.get("error_code")
    original = deepcopy(executor.output(run, "N2"))
    parent_run = run
    config = deepcopy(executor.execution_config)
    config["planning"]["organize_response"] = True
    run = executor.fork(parent_run, "N3", execution_config=config)
    executor = Executor(executor.store.root, executor.nodes, config)
    done = executor.resume(run)
    assert done["status"] == "succeeded", done.get("error_code")
    assert executor.output(run, "N2") == original
    assert executor.output(parent_run, "N2") == original
    assert executor.inspect(parent_run)["status"] == "paused_debug"
    package = executor.output(run, "N4")["package"]
    review_sections = [section for section in package["sections"] if section["document_role"] == "review"]
    assert review_sections
    rids = {rid for section in review_sections for rid in section["requirement_refs"]}
    requirements = {item["requirement_id"]: item for item in package["requirements"]}
    rows = executor.output(run, "N5")["validation"]["requirement_coverage"]
    for row in rows:
        if row["requirement_id"] in rids:
            assert row["status"] == "pending_manual" and row["satisfaction_status"] == "not_determined"
            if requirements[row["requirement_id"]]["response_mode"] != "procedure":
                assert row["response_issue_ids"]
    files = executor.output(run)["files"]
    _, bid = document_text(executor.store.root / files["bid_docx"]["path"])
    _, companion = document_text(executor.store.root / files["issues_docx"]["path"])
    for section in review_sections:
        assert section["title"] not in bid and section["title"] in companion
        for block in section["blocks"]:
            if block["kind"] == "paragraph":
                assert block["text"] in companion
    assert "第0条" in companion and "待补" in bid
    assert inspect_diagnostics(executor.store.root, run)["actual_call_attempts"] == 0


def test_organizing_proof_assembly_retains_content_at_original_review_location():
    from company_bid.extraction import _id
    from company_bid.response_organization import organize_response
    requirement = {"requirement_id": "R-PROOF", "text": "提供项目证明材料。", "category": "qualification",
                   "response_mode": "attachment", "source_refs": ["S-PROOF"]}
    def section(identity, **content):
        return {"section_id": identity, "title": "原资料落点", "kind": "facts", "requirement_refs": ["R-PROOF"],
                "source_refs": ["S-PROOF"], "fact_refs": [], "table_refs": [], "attachment_refs": [], "writing_tasks": [],
                "fixed_content": [], "nonwriting_requirement_refs": [], "level": 1, "start_on_new_page": False,
                "render_body": True, **content}
    source_section = section("ORIGINAL-PROOF", attachment_refs=["A-PROOF"])
    family = section(_id("SEC-", "FORMAT-FAMILY"), fixed_content=[{"kind": "paragraph", "text": "材料格式正文。",
                                                                     "source_refs": ["S-PROOF"]}])
    sections, issues = [family, source_section], []
    original = {"attachment_id": "A-PROOF", "source_refs": ["S-PROOF"]}
    attachments = [{**original, "section_id": source_section["section_id"], "requirement_refs": ["R-PROOF"]}]
    report = organize_response(sections, [requirement], {"R-PROOF": requirement}, [{"format_id": "FORMAT-FAMILY",
        "title": "资质材料", "source_derivation": {"kind": "native_material_family_prefix", "sibling_block_ids": []}}],
        {}, issues, attachments, {"attachments": [original]}, {"assembly_candidates": [{"requirement_id": "R-PROOF",
        "attachment_ids": ["A-PROOF"], "pending_checks": ["核对本次适用性。"]}]})
    assert report["proof_assembly"] and source_section["document_role"] == "review"
    assert source_section in sections and source_section["render_body"]
    assert any(source_section[key] for key in ("fact_refs", "table_refs", "attachment_refs", "writing_tasks", "fixed_content")) or any(
        issue["section_id"] == source_section["section_id"] for issue in issues), "Proof relocation left an empty visible review section"


def test_organizing_two_response_requirements_embeds_shared_proof_once():
    from company_bid.extraction import _id
    from company_bid.response_organization import organize_response
    requirements = [{"requirement_id": f"R-{index}", "text": f"提供资质证明材料{index}。", "category": "qualification",
                     "response_mode": "attachment", "source_refs": ["S-PROOF"]} for index in (1, 2)]
    refs = [item["requirement_id"] for item in requirements]
    sections = [{"section_id": _id("SEC-", "FORMAT-FAMILY"), "title": "资格格式", "kind": "facts",
        "requirement_refs": refs, "source_refs": ["S-PROOF"], "fact_refs": [], "table_refs": [], "attachment_refs": [],
        "writing_tasks": [], "fixed_content": [{"kind": "paragraph", "text": "材料格式正文。", "source_refs": ["S-PROOF"]}],
        "nonwriting_requirement_refs": refs, "level": 1, "start_on_new_page": False, "render_body": True}]
    attachments, issues = [], []
    original = {"attachment_id": "A-SHARED", "source_refs": ["S-PROOF"]}
    result = organize_response(sections, requirements, {item["requirement_id"]: item for item in requirements},
        [{"format_id": "FORMAT-FAMILY", "title": "资格格式", "source_derivation": {"kind": "native_material_family_prefix",
         "sibling_block_ids": ["NATIVE-QUALIFICATION"]}}], {"NATIVE-QUALIFICATION": {"text": "1.1资质材料"}},
        issues, attachments, {"attachments": [original]}, {"assembly_candidates": [
            {"requirement_id": rid, "attachment_ids": ["A-SHARED"], "pending_checks": ["核对本条适用条件。"]} for rid in refs]})
    assert len(result["proof_assembly"]) == 2
    assert len(attachments) == 1 and set(attachments[0]["requirement_refs"]) == set(refs)
    assert [aid for section in sections for aid in section["attachment_refs"]] == ["A-SHARED"]
    assert all(any(rid in issue["requirement_refs"] for issue in issues) for rid in refs)
