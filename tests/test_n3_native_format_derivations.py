"""Bound N3 format derivations from frozen native source, without HTTP calls."""
import copy

import pytest

from company_bid.extraction import PlanningError, _citation, _index, extract_requirements
from company_bid.models import file_sha256
from company_bid.planning import (
    _format_plan, _format_region_instruction, _pdf_footer_skips,
    _source_format_additions, plan_bid,
)
from test_joint_deviation_formats import association_items, source_case
from test_n3_source_roles import line
from test_source_selected_citations import Context, fixture, no_call


SELF_TITLE = "8 技术服务方案"
SELF_NOTE = "（响应人可自行编写）"
MATERIAL_NOTE = "注：所有材料按照“第二章 响应人须知”中要求提供"
REGION_RULE = "响应文件应按第六章“响应文件格式”进行编写，如有必要，可以增加附页，作为响应文件的组成部分。"
QUOTATION_RULE = "（2）询比文件要求的各类报价表格（格式见响应文件部分格式3、4）"
QUOTATION_ROWS = [["项目名称", ""], ["响应人名称", ""], ["报价", ""]]
BREAKDOWN_ROWS = [["序号", "内容", "不含税报价（万元）", "备注"], ["1", "合成服务", "", ""]]


def native_case(tmp_path, *, family=False, procedure=False, change=None):
    """Produce real N2 output from synthetic lines, omitting only the outer form."""
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    requirement_ids = []
    if procedure:
        line(parsed, "RULE", REGION_RULE)
        requirement_ids.append(("RULE",))
    line(parsed, "REGION", "第六章 响应文件格式", page=2)
    if family:
        line(parsed, "OUTER", "7 资格审查材料", page=2)
        line(parsed, "NOTE", MATERIAL_NOTE, page=2)
        line(parsed, "CHILD1", "7.1 资质材料：", page=2)
        line(parsed, "CHILD2", "7.2 财务材料：", page=2)
        requirement_ids.extend([("OUTER", "NOTE"), ("CHILD1",), ("CHILD2",)])
        chapter_ids = ["CHILD1", "CHILD2"]
    else:
        if not procedure:
            line(parsed, "OUTER", SELF_TITLE, page=2)
            line(parsed, "NOTE", SELF_NOTE, page=2)
            requirement_ids.append(("OUTER", "NOTE"))
        line(parsed, "FORM", "9 合成固定说明", page=3)
        line(parsed, "BODY", "此段为独立固定原文，不得被自拟许可覆盖。", page=3)
        chapter_ids = ["FORM"]
    line(parsed, "END", "第七章 其他事项", page=4)
    for unit in document["units"]:
        unit["coverage"]["image_count"] = 0
    if change:
        change(manifest, parsed)
    path = tmp_path / "native-format-source.txt"
    path.write_text("\n".join(value["text"] for unit in document["units"] for value in unit["blocks"]), encoding="utf-8")
    document["sha256"] = file_sha256(path)
    manifest["sources"][0].update(path=str(path), sha256=document["sha256"])

    def candidate(ctx, task, unit, payload, validate):
        values = {value["block_id"]: value for value in payload["window"]["blocks"]}
        requirements = [{"citation": {"block_ids": list(ids)},
            "category": "procedure" if procedure else "qualification" if family else "technical",
            "response_mode": "procedure" if procedure else "factual_response" if family else "writing",
            "format_status": "prescribed" if procedure else "not_prescribed", "format_title": None}
            for ids in requirement_ids if ids[0] in values]
        chapters = [{"title": values[identity]["text"], "level": 1,
            "citation": {"block_ids": [identity]}, "content_block_ids": ["BODY"] if identity == "FORM" else [],
            "requirement_indices": [], "status": "prescribed"}
            for identity in chapter_ids if identity in values]
        return validate({"window_id": payload["window"]["window_id"],
            "processing_declaration": "processed_not_semantically_accepted", "unresolved_block_ids": [],
            "requirements": requirements, "format_chapters": chapters,
            "format_status": "specified" if chapters else "none_verified"})

    extracted = extract_requirements(Context(), parsed, manifest, candidate, {})
    return manifest, parsed, extracted


def additions_for(manifest, parsed, extracted):
    blocks, units, documents = _index(parsed, manifest)
    additions = _source_format_additions(extracted, blocks, documents, extracted["format_chapters"])
    return additions, blocks, units, documents


def test_native_self_drafting_outer_section_is_derived_without_rewriting_n2_or_fixed_body(tmp_path):
    manifest, parsed, extracted = native_case(tmp_path)
    original = copy.deepcopy((manifest, parsed, extracted))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert [item["title"] for item in extracted["format_chapters"]] == ["9 合成固定说明"]
    outer = next(section for section in prepared["sections"] if section["title"] == SELF_TITLE)
    task, = outer["writing_tasks"]
    assert task["must_cover"] == [SELF_TITLE + "\n" + SELF_NOTE]
    fixed = [item["text"] for section in prepared["sections"] for item in section["fixed_content"]]
    assert "此段为独立固定原文，不得被自拟许可覆盖。" in fixed
    assert SELF_NOTE not in fixed
    derivation, = ctx.captures["n3-source-format-derivations"]["items"]
    assert derivation["source_proof"]["kind"] == "native_self_drafting_section"
    assert [member["block_id"] for member in derivation["source_proof"]["members"]] == ["OUTER", "NOTE"]
    assert (manifest, parsed, extracted) == original


def test_material_prefix_has_its_own_source_proof_and_keeps_reference_unresolved(tmp_path):
    manifest, parsed, extracted = native_case(tmp_path, family=True)
    original = copy.deepcopy((parsed, extracted))
    additions, blocks, _, documents = additions_for(manifest, parsed, extracted)
    prefix, = additions
    assert prefix["title"] == "7 资格审查材料"
    assert prefix["content_block_ids"] == ["OUTER", "NOTE"]
    proof = prefix["source_derivation"]
    assert proof["kind"] == "native_material_family_prefix"
    assert proof["reference_text"] == "第二章 响应人须知"
    assert proof["reference_resolution"] == "not_determined"
    assert proof["sibling_block_ids"] == ["CHILD1", "CHILD2"]
    formats, _, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    assert {chapter["citation"]["block_id"] for chapter in formats} == {"OUTER", "CHILD1", "CHILD2"}
    assert (parsed, extracted) == original


@pytest.mark.parametrize("instruction, eligible", [
    ("（格式自拟，要求法定代表人或授权代表签字并加盖单位公章）", True),
    ("（格式自拟，如经采购人同意，要求法定代表人签字并加盖单位公章）", False),
])
def test_signable_drafting_instruction_requires_a_whole_unconditional_native_sentence(tmp_path, instruction, eligible):
    def signing_source(manifest, parsed):
        values = {value["block_id"]: value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]}
        values["OUTER"]["text"] = "8 其他承诺"
        values["NOTE"]["text"] = instruction
    manifest, parsed, extracted = native_case(tmp_path, change=signing_source)
    original = copy.deepcopy(extracted)
    additions, _, _, _ = additions_for(manifest, parsed, extracted)
    assert bool(additions) is eligible
    if eligible:
        outer, = additions
        assert outer["title"] == "8 其他承诺"
        assert outer["content_block_ids"] == ["OUTER", "NOTE"]
        assert outer["source_derivation"]["kind"] == "native_self_drafting_section"
    assert extracted == original


@pytest.mark.parametrize("family", [False, True], ids=["self-drafting", "material-prefix"])
@pytest.mark.parametrize("invalid", ["no_n2", "title_only", "partial_last_line", "ocr", "unread", "unread_object", "cross_region"])
def test_native_outer_format_is_not_inferred_from_incomplete_or_unproved_source(tmp_path, family, invalid):
    manifest, parsed, extracted = native_case(tmp_path, family=family)
    _, blocks, _, documents = additions_for(manifest, parsed, extracted)
    outer_requirement = extracted["requirements"][0]
    if invalid == "no_n2":
        extracted["requirements"] = extracted["requirements"][1:]
    elif invalid in {"title_only", "partial_last_line"}:
        # A title alone can be an exact citation, but cannot prove the body.
        # A shortened final block in a multiblock citation is rejected by
        # the shared source validator before N3 can derive any format.
        quote = blocks["OUTER"]["text"]
        ids = ["OUTER"]
        if invalid == "partial_last_line":
            quote += "\n" + blocks["NOTE"]["text"][:-2]
            ids.append("NOTE")
        wire = {"source_id": "T", "block_id": "OUTER", "locator": blocks["OUTER"]["locator"],
                "block_ids": ids, "quote": quote}
        if invalid == "partial_last_line":
            with pytest.raises(PlanningError, match="CITATION_SPAN_MUST_QUOTE_ALL_BLOCKS"):
                _citation(wire, blocks, _pdf_footer_skips(blocks))
            return
        outer_requirement["text"] = quote
        outer_requirement["citations"] = [_citation(wire, blocks, _pdf_footer_skips(blocks))]
    elif invalid == "ocr":
        blocks["NOTE"]["origin"] = "ocr"
    elif invalid in {"unread", "unread_object"}:
        coverage = documents[0]["units"][0]["coverage"]
        coverage.update({"status": "partial"} if invalid == "unread" else {"unread_objects": ["unread-object"]})
    else:
        blocks["REGION"]["text"] = "第六章 合同附件"
    original = copy.deepcopy(extracted)
    assert _source_format_additions(extracted, blocks, documents, extracted["format_chapters"]) == []
    assert extracted == original


def test_source_contents_entries_do_not_become_derived_response_formats(tmp_path):
    def source_contents(manifest, parsed):
        unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == 2)
        unit["blocks"][1]["text"] = "目 录"
        unit["blocks"][2]["text"] = "1. 技术服务方案"
        line(parsed, "ENTRY2", "2. 合成说明", page=2)
    manifest, parsed, extracted = native_case(tmp_path, change=source_contents)
    additions, _, _, _ = additions_for(manifest, parsed, extracted)
    assert additions == []


def quotation_case(tmp_path, *, invalid=None):
    def quotation_forms(manifest, parsed):
        values = {value["block_id"]: value for unit in parsed["documents"][0]["units"] for value in unit["blocks"]}
        values["BUSINESS"]["text"] = "3 报价表"
        values["TECHNICAL"]["text"] = "4 分项报价明细表"
        values["BUSINESS_GRID"].update(rows=copy.deepcopy(QUOTATION_ROWS), merges=[], column_widths=[3, 7])
        values["TECHNICAL_GRID"].update(rows=copy.deepcopy(BREAKDOWN_ROWS), merges=[], column_widths=[1, 3, 2, 2])
        if invalid == "wrong_number":
            values["TECHNICAL"]["text"] = "8 分项报价明细表"
        elif invalid == "wrong_columns":
            values["TECHNICAL_GRID"]["rows"][0][2] = "合成未知列"
        elif invalid == "ocr":
            values["TECHNICAL_GRID"]["origin"] = "ocr"
        elif invalid == "separate_region":
            values["TECHNICAL"]["text"] = "第七章 分项报价明细表"
    declaration = QUOTATION_RULE.replace("3、4", "3、3") if invalid == "same_number" else QUOTATION_RULE
    return source_case(tmp_path, declaration=declaration, change=quotation_forms)


def test_explicit_numbered_quotation_pair_keeps_both_native_grids_and_one_n2_obligation(tmp_path):
    manifest, parsed, extracted = quotation_case(tmp_path)
    original = copy.deepcopy((parsed, extracted))
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    requirement, = extracted["requirements"]
    proof, = association_items(ctx)
    assert proof["rule"] == "explicit_numbered_quotation_pair_v1"
    assert {target["role"] for target in proof["targets"]} == {"quotation_summary", "quotation_breakdown"}
    assert proof["satisfaction_status"] == "not_determined"
    assert proof["n2_candidate_mutation"] is False
    assert len(prepared["sections"]) == len(prepared["tables"]) == 2
    assert all(requirement["requirement_id"] in item["requirement_refs"] for item in prepared["tables"])
    for target, rows in zip(proof["targets"], [QUOTATION_ROWS, BREAKDOWN_ROWS]):
        assert target["tables"][0]["rows"] == rows
    summary, breakdown = prepared["tables"]
    assert summary["rows"][0][1]["text"] == manifest["project"]["name"]
    assert summary["rows"][1][1]["text"] == manifest["company"]["name"]
    for cell in (summary["rows"][2][1], breakdown["rows"][1][2]):
        assert cell["text"] == "" and not cell["fact_refs"] and cell["issue_refs"]
    assert [[cell["text"] for cell in row] for row in breakdown["rows"]] == BREAKDOWN_ROWS
    assert (parsed, extracted) == original


@pytest.mark.parametrize("invalid", ["wrong_number", "same_number", "wrong_columns", "ocr", "separate_region"])
def test_quotation_pair_does_not_guess_number_role_or_native_grid(tmp_path, invalid):
    manifest, parsed, extracted = quotation_case(tmp_path, invalid=invalid)
    original = copy.deepcopy(extracted)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == original


def test_complete_format_region_instruction_remains_pending_manual_and_does_not_create_a_form(tmp_path):
    manifest, parsed, extracted = native_case(tmp_path, procedure=True)
    original = copy.deepcopy(extracted)
    ctx = Context()
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    action, = prepared["review_actions"]
    assert action["status"] == "pending_manual"
    assert action["source_requirement"] == REGION_RULE
    assert [section["title"] for section in prepared["sections"] if section["render_body"]] == ["9 合成固定说明"]
    assert not any(section["writing_tasks"] or section["table_refs"] for section in prepared["sections"])
    proof, = ctx.captures["n3-format-coverage"]["format_region_resolutions"]
    assert proof["status"] == "source_proven_pending_manual"
    assert proof["satisfaction_status"] == "not_determined"
    assert proof["n2_candidate_mutation"] is False
    assert extracted == original


@pytest.mark.parametrize("invalid", ["wrong_chapter", "wrong_actor", "partial_quote", "ocr"])
def test_format_region_instruction_requires_exact_chapter_and_complete_native_reference(tmp_path, invalid):
    manifest, parsed, extracted = native_case(tmp_path, procedure=True)
    blocks, _, documents = _index(parsed, manifest)
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    requirement = copy.deepcopy(extracted["requirements"][0])
    if invalid == "wrong_chapter":
        # The existing sixth chapter must not discharge an explicit fifth-
        # chapter reference merely because both say response-file format.
        blocks["RULE"]["text"] = REGION_RULE.replace("第六章", "第五章")
    elif invalid == "wrong_actor":
        blocks["RULE"]["text"] = REGION_RULE.replace("响应文件应按", "响应文件不得按")
    elif invalid == "ocr":
        blocks["RULE"]["origin"] = "ocr"
    quote = blocks["RULE"]["text"]
    if invalid == "partial_quote":
        blocks["RULE"]["text"] += "但本项目须另行取得书面同意。"
    requirement["text"] = quote
    requirement["citations"] = [_citation({"source_id": "T", "block_id": "RULE",
        "locator": blocks["RULE"]["locator"], "quote": quote}, blocks, _pdf_footer_skips(blocks))]
    assert _format_region_instruction(requirement, ledger, blocks) is None
