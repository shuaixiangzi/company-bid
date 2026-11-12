"""Read-clear missing responses remain visible without inventing declarations."""
import copy
import hashlib

import pytest
from docx import Document

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import PlanningError, extract_requirements
from company_bid.models import file_sha256
from company_bid.pipeline import build_executor
from company_bid.planning import plan_bid
from company_bid.provider import ChatProposalProvider, PreparedProposalProvider
from company_bid.structured import JsonTaskGateway
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, no_call


DECLARATION = (
    "应提交合成关联情况的完整书面说明，",
    "信息变化应主动说明，并如实申报合成管理人员身份；声明结论须由授权人员核对。",
)
SINGLE_DECLARATION = "应提交合成检查期间质量与安全记录的书面声明（格式自定）。"
FORM_TITLE = "一、合成售后服务承诺"
PURE_INSTRUCTION = "格式自拟，须有法定代表人或授权代表签字并加盖单位公章。"
FIXED_BODY = "本单位按合成边界提供固定说明，未经授权不得扩大义务。"


def raw_for(window, *, category="qualification", mode="fixed_text", status="not_prescribed",
            format_title=None, supplied=False, structural_only=False, chapter_status="prescribed",
            root_status=None):
    targets = {value["block_id"]: value for value in window["blocks"]}
    requirements = []
    ids = [identity for identity in ("R0", "R1") if identity in targets]
    if ids and not structural_only:
        item = {"citation": {"block_ids": ids}, "category": category, "response_mode": mode,
                "format_status": status, "required_fact_labels": []}
        if format_title is not None:
            item["format_title"] = format_title
        requirements.append(item)
    formats = []
    if supplied and "FORM" in targets:
        formats.append({"title": FORM_TITLE, "level": 1, "citation": {"block_ids": ["FORM"]},
                        "content_block_ids": ["BODY"], "requirement_indices": [], "status": chapter_status})
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": requirements, "format_chapters": formats,
            "format_status": root_status or ("specified" if formats else "none_verified")}


def case(tmp_path, *, category="qualification", mode="fixed_text", status="not_prescribed",
         format_title=None, template_body=None, structural_only=False,
         chapter_status="prescribed", root_status=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    if template_body is None:
        for index, text in enumerate(DECLARATION):
            if index == len(DECLARATION) - 1 and format_title is not None:
                text += f"应按《{format_title}》原格式提交。"
            line(parsed, f"R{index}", text)
    else:
        if not structural_only:
            line(parsed, "R0", "应提交《合成售后服务承诺》作为合成响应材料。")
        line(parsed, "REGION", "第六章 投标文件格式", page=2)
        line(parsed, "FORM", FORM_TITLE, page=2)
        line(parsed, "BODY", template_body, page=2)
        line(parsed, "END", "第七章 合成合同事项", page=3)
    path = tmp_path / "synthetic-pending-response-source.txt"
    path.write_text("\n".join(value["text"] for unit in parsed["documents"][0]["units"]
                              for value in unit["blocks"]), encoding="utf-8")
    digest = file_sha256(path)
    manifest["sources"][0].update(path=str(path), sha256=digest)
    parsed["documents"][0]["sha256"] = digest

    def candidate(ctx, task, unit, payload, validate):
        return validate(raw_for(payload["window"], category=category, mode=mode, status=status,
            format_title=format_title, supplied=template_body is not None,
            structural_only=structural_only, chapter_status=chapter_status, root_status=root_status))

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {})
    return manifest, parsed, extracted


def missing_body_issues(prepared, rid):
    return [issue for issue in prepared["issues"] if rid in issue["requirement_refs"] and
            issue["severity"] == "missing" and
            any(word in " ".join((issue["title"], issue["reason"], issue["action"]))
                for word in ("正文", "全文响应", "完整响应"))]


def doc_text(document):
    values = [paragraph.text for paragraph in document.paragraphs]
    values.extend(cell.text for table in document.tables for row in table.rows for cell in row.cells)
    return "\n".join(values)


@pytest.mark.parametrize("category", ["qualification", "commercial"])
def test_full_unprovided_response_is_source_backed_pending_not_procedure(tmp_path, category):
    manifest, parsed, extracted = case(tmp_path, category=category)
    original = copy.deepcopy((manifest, parsed, extracted))
    prepared = plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    requirement, = prepared["requirements"]
    expected = "\n".join(DECLARATION)
    assert requirement["text"] == expected and requirement["category"] == category
    assert requirement["response_mode"] == "fixed_text"
    section, = prepared["sections"]
    assert section["render_body"] and not section["writing_tasks"] and not section["fact_refs"]
    assert any(value["kind"] == "paragraph" and value["text"] == "响应要求（招标原文）：\n" + expected
               and value["source_refs"] == requirement["source_refs"] for value in section["fixed_content"])
    assert missing_body_issues(prepared, requirement["requirement_id"])
    assert all(issue["source_requirement"] == expected for issue in prepared["issues"])
    assert not prepared["review_actions"] and not prepared["attachments"] and not prepared["tables"]
    assert (manifest, parsed, extracted) == original


@pytest.mark.parametrize("options", [
    {"status": "unread"}, {"status": "conflict"}, {"status": "prescribed"},
    {"mode": "table"}, {"format_title": "明确指定但未读取的合成声明书"},
    {"category": "technical"}, {"root_status": "unread"}, {"root_status": "conflict"},
])
def test_pending_response_cannot_bypass_real_format_or_category_gates(tmp_path, options):
    manifest, parsed, extracted = case(tmp_path, **options)
    original = copy.deepcopy(extracted)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert extracted == original


@pytest.mark.parametrize("structural_only", [False, True])
def test_instruction_only_declared_chapter_needs_actual_body_and_keeps_signing_pending(tmp_path, structural_only):
    manifest, parsed, extracted = case(tmp_path, category="commercial", status="prescribed",
        format_title=FORM_TITLE, template_body=PURE_INSTRUCTION, structural_only=structural_only)
    original = copy.deepcopy(extracted)
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    section, = prepared["sections"]
    assert section["title"] == FORM_TITLE and section["render_body"]
    assert not section["writing_tasks"] and not section["fact_refs"]
    assert all(value.get("text") != PURE_INSTRUCTION for value in section["fixed_content"])
    assert not any(value.get("text", "").startswith(("我方承诺", "本单位承诺", "本公司保证"))
                   for value in section["fixed_content"])
    assert any(missing_body_issues(prepared, rid) for rid in section["requirement_refs"])
    signed = [issue for issue in prepared["issues"] if issue["section_id"] == section["section_id"] and
              any(word in " ".join((issue["title"], issue["reason"], issue["action"]))
                  for word in ("签字", "签章", "盖章", "公章"))]
    assert signed, "Removing the source instruction cannot remove its signing obligation"
    assert PURE_INSTRUCTION in repr(prepared["issues"]) or PURE_INSTRUCTION in repr(ctx.captures)
    assert extracted == original


@pytest.mark.parametrize("body", [
    "本单位声明合成材料保持原样；格式自拟，须有法定代表人签字并加盖单位公章。",
    "不得自拟格式，须有法定代表人或授权代表签字并加盖单位公章。",
    "仅经合成书面同意后才可自拟格式，并须由授权代表签字。",
    "采购人可自拟格式，要求响应人法人代表或授权代表签字并加盖公章。",
])
def test_mixed_fixed_text_negation_condition_and_other_actor_are_not_stripped(tmp_path, body):
    manifest, parsed, extracted = case(tmp_path, category="commercial", status="prescribed",
        format_title=FORM_TITLE, template_body=body)
    original = copy.deepcopy(extracted)
    prepared = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    fixed = [value["text"] for section in prepared["sections"] for value in section["fixed_content"]
             if value["kind"] == "paragraph"]
    assert body in fixed
    assert extracted == original


def test_real_fixed_template_keeps_normal_prescribed_body_path(tmp_path):
    manifest, parsed, extracted = case(tmp_path, category="commercial", status="prescribed",
        format_title=FORM_TITLE, template_body=FIXED_BODY)
    prepared = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    section, = prepared["sections"]
    assert [value["text"] for value in section["fixed_content"]] == [FIXED_BODY]
    assert all(item["response_mode"] == "fixed_text" for item in prepared["requirements"])
    assert not any(missing_body_issues(prepared, rid) for rid in section["requirement_refs"])


@pytest.mark.parametrize("chapter_status", ["unread", "conflict"])
def test_instruction_only_does_not_rescue_unread_or_conflicting_format_chapter(tmp_path, chapter_status):
    manifest, parsed, extracted = case(tmp_path, category="commercial", status="prescribed",
        format_title=FORM_TITLE, template_body=PURE_INSTRUCTION, chapter_status=chapter_status)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})


def test_native_document_through_production_n3_n6_exports_pending_body_with_zero_calls(tmp_path, monkeypatch):
    tender = tmp_path / "synthetic-pending-declaration.docx"
    doc = Document()
    doc.add_paragraph(SINGLE_DECLARATION)
    doc.save(tender)
    manifest, _ = fixture(tmp_path)
    manifest["sources"][0].update(path=str(tender), sha256=file_sha256(tender))
    gateway_calls = []

    def candidate(self, ctx, task_type, unit_id, payload, validate):
        gateway_calls.append(task_type)
        assert task_type == "extract_requirements"
        selected = [value for value in payload["window"]["blocks"] if value.get("text") == SINGLE_DECLARATION]
        assert len(selected) == 1
        return validate({"window_id": payload["window"]["window_id"],
            "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": [{"citation": {"block_ids": [selected[0]["block_id"]]}, "category": "qualification",
                "response_mode": "fixed_text", "format_status": "not_prescribed", "required_fact_labels": []}],
            "format_chapters": [], "format_status": "none_verified"})

    def forbidden_provider(*args, **kwargs):
        pytest.fail("A missing declaration must not call writing/mapping/model transport")

    monkeypatch.setattr(JsonTaskGateway, "__call__", candidate)
    monkeypatch.setattr(PreparedProposalProvider, "generate", forbidden_provider)
    monkeypatch.setattr(ChatProposalProvider, "generate", forbidden_provider)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", forbidden_provider)
    config = {"pipeline": "automatic", "debug_capture": True, "provider": {"mode": "prepared"},
              "planning": {"map_table_fields": False}}
    root = tmp_path / "synthetic-runtime"
    executor = build_executor(root, config)
    run = executor.create_run({"manifest": manifest})
    paused = executor.execute(run, stop_after="N3")
    assert paused["status"] == "paused_debug", paused.get("error_code")
    prepared = executor.output(run, "N3")["prepared"]
    requirement, = prepared["requirements"]
    assert requirement["response_mode"] == "fixed_text" and requirement["category"] == "qualification"
    issue, = missing_body_issues(prepared, requirement["requirement_id"])
    frozen_n2 = next(node for node in paused["nodes"] if node["key"] == "N2")["output_ref"]
    original_n2_hash = hashlib.sha256((root / frozen_n2["path"]).read_bytes()).hexdigest()
    n2_before = copy.deepcopy(executor.output(run, "N2")["extracted"])

    finished = build_executor(root, config).resume(run, stop_after="N6")
    assert finished["status"] == "succeeded", finished.get("error_code")
    assert all(node["status"] == "succeeded" for node in finished["nodes"])
    assert next(node for node in finished["nodes"] if node["key"] == "N2")["output_ref"] == frozen_n2
    assert hashlib.sha256((root / frozen_n2["path"]).read_bytes()).hexdigest() == original_n2_hash
    assert executor.output(run, "N2")["extracted"] == n2_before
    n5 = executor.output(run, "N5")
    row, = n5["validation"]["requirement_coverage"]
    assert row["status"] == "pending_manual" and row.get("satisfaction_status") not in {"satisfied", "accepted"}
    assert n5["validation"]["writing_units"] == []
    assert not n5["package"]["review_actions"]
    blocks = [value for section in n5["package"]["sections"] for value in section["blocks"]]
    assert any(value["kind"] == "issue" and value["issue_id"] == issue["issue_id"] for value in blocks)
    assert not any(value.get("content_role") in {"fact", "proposal"} for value in blocks)
    assert not any(section["fact_refs"] for section in prepared["sections"])
    files = executor.output(run)["files"]
    bid_text = doc_text(Document(root / files["bid_docx"]["path"]))
    issue_text = doc_text(Document(root / files["issues_docx"]["path"]))
    assert "响应要求（招标原文）：\n" + SINGLE_DECLARATION in bid_text
    assert SINGLE_DECLARATION in issue_text
    assert issue["issue_id"] in bid_text and issue["issue_id"] in issue_text
    assert "【待补 " in bid_text and "正文" in issue_text
    assert all(value not in bid_text for value in ("我方承诺", "本单位已核验", "本公司没有事故"))
    assert gateway_calls == ["extract_requirements"]
    diagnostics = inspect_diagnostics(root, run)
    assert diagnostics["actual_call_attempts"] == 0 and diagnostics["gaps"] == []
