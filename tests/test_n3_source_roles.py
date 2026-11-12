"""Source-role accounting is separate from response content and fulfillment."""
import copy

import pytest

from company_bid.extraction import PlanningError, _index, extract_requirements
from company_bid.planning import _format_plan, _instruction_placements, plan_bid
from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected


def line(parsed, identity, text, *, page=1, **extra):
    value = block(parsed, identity, text, page=page, origin="native", **extra)
    unit = next(unit for unit in parsed["documents"][0]["units"] if unit["page"] == page)
    value["locator"] = f"pdf/page[{page}]/line[{len(unit['blocks']) - 1}]"
    return value


def sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    line(parsed, "REGION", "第六章 投标文件格式")
    line(parsed, "COMPOSITION", "1.投标文件组成，包括以下合成材料：")
    line(parsed, "PLAN", "（1）应编写合成测点的巡检方案。")
    line(parsed, "PROOF", "（2）应附合成检测证明。")
    line(parsed, "PERMISSION", "2.投标文件格式（未提供格式的投标文件可由投标人自定义格式）")
    line(parsed, "INDEX", "目 录", page=2)
    line(parsed, "ENTRY1", "1. 合成说明书", page=2)
    line(parsed, "ENTRY2", "2. 合成材料清单", page=2)
    line(parsed, "FORM", "一、合成说明书", page=3)
    line(parsed, "FIXED", "此处保留合成模板的固定文字。", page=3)
    line(parsed, "CONTINUED", "模板后续原文也应完整保留。", page=4)
    line(parsed, "END", "第七章 合同事项", page=4)
    return manifest, parsed


def extract(manifest, parsed, *, change=None):
    def call(ctx, task, unit, payload, validate):
        values = {value["block_id"]: value for value in payload["window"]["blocks"]}
        raw = live_candidate(payload, selected(next(iter(values.values()))))
        raw["requirements"] = []
        if "PLAN" in values:
            raw["requirements"] = [
            {"citation": selected(values["PLAN"]), "category": "technical",
             "response_mode": "writing", "format_status": "not_prescribed"},
            {"citation": selected(values["PROOF"]), "category": "qualification",
             "response_mode": "attachment", "format_status": "not_prescribed"},
            {"citation": selected(values["PERMISSION"]), "category": "procedure",
             "response_mode": "procedure", "format_status": "not_prescribed"},
            ]
        raw["format_chapters"] = [{"title": values["FORM"]["text"], "level": 1,
            "citation": selected(values["FORM"]), "content_block_ids": ["FIXED"], "requirement_indices": []}] if "FORM" in values else []
        raw["format_status"] = "specified" if raw["format_chapters"] else "none_verified"
        if change:
            change(raw)
        return validate(raw)
    return extract_requirements(Context(), parsed, manifest, call, {})


def prepared(tmp_path, *, change=None):
    manifest, parsed = sources(tmp_path)
    extracted = extract(manifest, parsed, change=change)
    blocks, _, documents = _index(parsed, manifest)
    return manifest, parsed, extracted, blocks, documents


def test_instructions_and_source_contents_have_proof_without_a_format_owner(tmp_path):
    manifest, parsed, extracted, blocks, documents = prepared(tmp_path)
    original = copy.deepcopy((parsed, extracted, blocks, documents))
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    rows = {row["block_id"]: row for row in ledger}
    assert set(rows) == set(blocks) - {"END"}
    assert rows["REGION"]["source_role"] == "format_region_boundary"
    assert rows["COMPOSITION"]["source_role"] == "preparation_scope"
    for identity in ("PLAN", "PROOF", "PERMISSION"):
        assert rows[identity]["source_role"] == "preparation_instruction"
        assert rows[identity]["format_id"] is None
        assert rows[identity]["instruction_requirements"]
        assert rows[identity]["role_proof_sha256"]
    for identity in ("INDEX", "ENTRY1", "ENTRY2"):
        assert rows[identity]["source_role"] == "source_contents"
        assert rows[identity]["format_id"] is None
    assert formats[0]["content_block_ids"] == ["FIXED", "CONTINUED"]
    assert rows["CONTINUED"]["status"] == "structural_continuation"
    assert (parsed, extracted, blocks, documents) == original


def test_n3_maps_real_handlers_and_never_prints_instruction_or_index_as_response(tmp_path):
    manifest, parsed, extracted, blocks, _ = prepared(tmp_path)
    ctx = Context()
    planned = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    fixed = [value["text"] for section in planned["sections"] for value in section["fixed_content"]
             if value["kind"] == "paragraph"]
    assert fixed == [blocks["FIXED"]["text"], blocks["CONTINUED"]["text"]]
    assert any(section["writing_tasks"] for section in planned["sections"])
    proof_id = next(item["requirement_id"] for item in extracted["requirements"]
                    if item["response_mode"] == "attachment")
    assert any(proof_id in issue["requirement_refs"] for issue in planned["issues"])
    assert planned["review_actions"][0]["status"] == "pending_manual"
    handling = ctx.captures["n3-instruction-handling"]
    assert handling["errors"] == []
    assert {item["status"] for item in handling["items"]} == {"mapped_pending", "mapped_with_issue"}


def test_whole_region_claimed_as_prescribed_response_is_a_role_conflict(tmp_path):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    extracted["format_chapters"].append({"format_id": "BAD-REGION", "title": blocks["REGION"]["text"],
        "citation": selected(blocks["REGION"]), "level": 1, "status": "prescribed", "requirement_refs": [],
        "content_block_ids": ["COMPOSITION", "PLAN", "PROOF", "PERMISSION"]})
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "source_role_conflict" and item["block_id"] == "REGION" for item in errors)
    assert any(item["reason"] == "source_role_conflict" and item["block_id"] == "PLAN" for item in errors)


def test_a_source_index_cannot_be_a_response_format(tmp_path):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    extracted["format_chapters"].append({"format_id": "BAD-INDEX", "title": blocks["INDEX"]["text"],
        "citation": selected(blocks["INDEX"]), "level": 1, "status": "prescribed", "requirement_refs": [],
        "content_block_ids": ["ENTRY1", "ENTRY2"]})
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "source_role_conflict" and item["block_id"] == "INDEX" for item in errors)


def test_a_missing_real_n2_instruction_cannot_be_cleared_by_source_classification(tmp_path):
    manifest, parsed, extracted, blocks, documents = prepared(tmp_path)
    missing = next(item["requirement_id"] for item in extracted["requirements"]
                   if item["response_mode"] == "writing")
    extracted["requirements"] = [item for item in extracted["requirements"] if item["requirement_id"] != missing]
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "unmapped_preparation_instruction" and item["block_id"] == "PLAN" for item in errors)
    assert next(row for row in ledger if row["block_id"] == "PLAN")["source_role"] == "preparation_instruction"


def test_all_procedure_does_not_discharge_substantive_components(tmp_path):
    def change(raw):
        for item in raw["requirements"]:
            item.update(category="procedure", response_mode="procedure")
    manifest, parsed, extracted, blocks, documents = prepared(tmp_path, change=change)
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert {item["block_id"] for item in errors if item["reason"] == "preparation_instruction_response_role_invalid"} == {"PLAN", "PROOF"}
    ctx = Context()
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert ctx.captures["n3-format-coverage"]["status"] == "failed"


def test_a_proof_placeholder_does_not_replace_the_required_plan_handler(tmp_path):
    def change(raw):
        if raw["requirements"]:
            raw["requirements"][0].update(response_mode="attachment")
    _, _, extracted, blocks, documents = prepared(tmp_path, change=change)
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "preparation_instruction_response_role_invalid" and
               item["block_id"] == "PLAN" for item in errors)


def test_attachment_does_not_discharge_a_separately_named_list_component(tmp_path):
    manifest, parsed = sources(tmp_path)
    parsed["documents"][0]["units"][0]["blocks"][3]["text"] = "（2）应提交合成项目清单及对应的检测证明。"
    extracted = extract(manifest, parsed)
    blocks, _, documents = _index(parsed, manifest)
    _, _, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "preparation_instruction_component_unmapped" and item["block_id"] == "PROOF" for item in errors)


def test_evidence_compilation_keeps_an_actual_handler_without_inventing_a_table(tmp_path):
    manifest, parsed = sources(tmp_path)
    parsed["documents"][0]["units"][0]["blocks"][3]["text"] = "（2）应提交合成检测证明文件汇总。"
    extracted = extract(manifest, parsed)
    blocks, _, documents = _index(parsed, manifest)
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    item = next(row for row in ledger if row["block_id"] == "PROOF")["instruction_requirements"][0]
    assert item["proof_compilation"] is True
    _, failures = _instruction_placements(ledger, [], [], [], [])
    assert any(error["requirement_id"] == item["requirement_id"] for error in failures)
    assert [item["response_mode"] for item in extracted["requirements"]] == ["writing", "attachment", "procedure"]


@pytest.mark.parametrize("change", ["body", "table", "missing_heading", "bad_sequence", "ocr", "line_gap", "modal"])
def test_mixed_or_unproved_source_contents_is_not_guessed(tmp_path, change):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    if change == "body":
        blocks["ENTRY2"]["text"] = "响应人必须在此签字并盖章。"
    elif change == "table":
        blocks["ENTRY2"].update(kind="table", rows=[["字段", "值"]])
    elif change == "missing_heading":
        blocks["INDEX"]["text"] = "本页其他说明"
    elif change == "bad_sequence":
        blocks["ENTRY2"]["text"] = "4. 合成材料清单"
    elif change == "ocr":
        blocks["ENTRY2"]["origin"] = "ocr"
    elif change == "line_gap":
        blocks["ENTRY2"]["locator"] = "pdf/page[2]/line[9]"
    else:
        blocks["ENTRY2"]["text"] = "2. 需按要求准备其他合成材料"
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert not any(row.get("source_role") == "source_contents" for row in ledger)
    assert errors


def test_unknown_mixed_instruction_source_stays_unresolved(tmp_path):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    blocks["PROOF"]["kind"] = "image"
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert not next(row for row in ledger if row["block_id"] == "PROOF").get("source_role")
    assert any(item["reason"] == "unread_editable_format_image" for item in errors)


@pytest.mark.parametrize("change", ["docx", "ocr", "line_gap", "missing_termination"])
def test_unproved_instruction_container_cannot_gain_auxiliary_ownership(tmp_path, change):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    if change == "docx":
        documents[0]["format"] = "docx"
    elif change == "ocr":
        blocks["PROOF"]["origin"] = "ocr"
    elif change == "line_gap":
        blocks["PROOF"]["locator"] = "pdf/page[1]/line[20]"
    else:
        blocks["PERMISSION"]["text"] = "没有可证明下一节编号的合成说明。"
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert not next(row for row in ledger if row["block_id"] == "PLAN").get("source_role")
    assert any(item["reason"] == "unassigned_format_block" and item["block_id"] == "PLAN" for item in errors)


def test_form_continuation_stops_before_a_proved_source_index(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    region = line(parsed, "REGION", "第六章 投标文件格式")
    heading = line(parsed, "FORM", "一、合成响应说明书")
    line(parsed, "FIXED", "合成固定文字保留。")
    line(parsed, "CONT", "另一页合成续文保留。", page=2)
    line(parsed, "INDEX", "目 录", page=3)
    line(parsed, "ENTRY1", "1. 合成响应说明书", page=3)
    line(parsed, "ENTRY2", "2. 合成补充资料", page=3)
    line(parsed, "END", "第七章 合同事项", page=4)
    blocks, _, documents = _index(parsed, manifest)
    extracted = {"requirements": [], "format_chapters": [{"format_id": "FORM-ID", "title": heading["text"],
        "citation": selected(heading), "level": 1, "status": "prescribed", "requirement_refs": [],
        "content_block_ids": ["FIXED"]}]}
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert errors == []
    assert formats[0]["content_block_ids"] == ["FIXED", "CONT"]
    rows = {row["block_id"]: row for row in ledger}
    assert rows["CONT"]["status"] == "structural_continuation"
    assert rows["INDEX"]["source_role"] == "source_contents"
    assert rows["FORM"]["boundary_basis"]["block_id"] == "INDEX"


def test_condition_scope_cannot_be_inferred_from_a_child_quote_alone(tmp_path):
    manifest, parsed = sources(tmp_path)
    values = parsed["documents"][0]["units"][0]["blocks"]
    outer = line(parsed, "OUTER", "B．技术材料不得包含商业报价，包括：")
    parent = line(parsed, "PARENT", "（1）测试服务适用")
    values.remove(outer)
    values.remove(parent)
    values[2:2] = [outer, parent]
    for i, value in enumerate(values):
        value["locator"] = f"pdf/page[1]/line[{i}]"
    extracted = extract(manifest, parsed)
    blocks, _, documents = _index(parsed, manifest)
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    assert any(item["reason"] == "preparation_instruction_scope_unproved" for item in errors)
    row = next(row for row in ledger if row["block_id"] == "PLAN")
    assert row["scope_block_ids"] == ["OUTER", "PARENT"]
    proof = row["role_proof_sha256"]
    assert row["role_proof"]["scope_members"] and row["role_proof"]["container_members"]
    blocks["PARENT"]["text"] = "（1）另一类合成服务适用"
    _, changed, _ = _format_plan(extracted, blocks, documents)
    assert next(row for row in changed if row["block_id"] == "PLAN")["role_proof_sha256"] != proof


@pytest.mark.parametrize("number,ambiguous", [(2, False), (4, True)])
def test_conditional_sibling_replaces_prior_condition_and_gaps_stay_ambiguous(tmp_path, number, ambiguous):
    manifest, parsed = sources(tmp_path)
    values = parsed["documents"][0]["units"][0]["blocks"]
    outer = line(parsed, "OUTER", "B．技术材料不得包含商业报价，包括：")
    first = line(parsed, "FIRST_CONDITION", "（1）合成服务适用")
    second = line(parsed, "SECOND_CONDITION", f"（{number}）合成货物适用")
    for value in (outer, first, second):
        values.remove(value)
    values[2:2] = [outer, first]
    values.insert(values.index(next(value for value in values if value["block_id"] == "PROOF")), second)
    for i, value in enumerate(values):
        value["locator"] = f"pdf/page[1]/line[{i}]"
    extracted = extract(manifest, parsed)
    blocks, _, documents = _index(parsed, manifest)
    _, ledger, errors = _format_plan(extracted, blocks, documents)
    row = next(row for row in ledger if row["block_id"] == "PROOF")
    assert row["scope_block_ids"] == ["OUTER", "SECOND_CONDITION"]
    assert row["scope_ambiguous"] is ambiguous
    assert (any(item["reason"] == "preparation_instruction_scope_ambiguous" and
                item["block_id"] == "PROOF" for item in errors)) is ambiguous


def test_original_cover_has_complete_source_contract_not_generic_cover_success(tmp_path):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    cover = copy.deepcopy(blocks["FORM"])
    values = []
    for i, text in enumerate(("投标文件封面", "招标编号：____", "供应商签字并盖章：____", "签署日期：____年____月____日")):
        value = {**cover, "block_id": f"COVER{i}", "text": text, "unit_id": "COVER-UNIT", "page": 5,
                 "locator": f"pdf/page[5]/line[{i}]", "ordinal": blocks["END"]["ordinal"] - 0.5 + i / 100}
        values.append(value)
        blocks[value["block_id"]] = value
    # Preserve source order while keeping this synthetic cover before the next chapter.
    for i, value in enumerate(sorted(blocks.values(), key=lambda value: value["ordinal"])):
        value["ordinal"] = i
    extracted["format_chapters"].append({"format_id": "SOURCE-COVER", "title": values[0]["text"],
        "citation": selected(values[0]), "level": 1, "status": "prescribed", "requirement_refs": [],
        "content_block_ids": [value["block_id"] for value in values]})
    formats, ledger, errors = _format_plan(extracted, blocks, documents)
    assert not any("cover" in item["reason"] for item in errors)
    recognized = next(item for item in formats if item["format_id"] == "SOURCE-COVER")
    assert recognized["source_role"] == "prescribed_cover"
    assert [item["block_id"] for item in recognized["cover_source_proof"]["members"]] == [value["block_id"] for value in values]
    assert all(next(row for row in ledger if row["block_id"] == value["block_id"])["source_role"] == "prescribed_cover" for value in values)


def test_source_proof_rebuilds_after_changes_instead_of_trusting_a_stored_role(tmp_path):
    _, _, extracted, blocks, documents = prepared(tmp_path)
    _, ledger, _ = _format_plan(extracted, blocks, documents)
    old = next(row for row in ledger if row["block_id"] == "ENTRY2")["role_proof_sha256"]
    blocks["ENTRY2"]["text"] = "2. 另一份合成资料"
    blocks["ENTRY2"]["source_role"] = "preparation_instruction"
    _, rebuilt, _ = _format_plan(extracted, blocks, documents)
    row = next(row for row in rebuilt if row["block_id"] == "ENTRY2")
    assert row["source_role"] == "source_contents" and row["role_proof_sha256"] != old


def test_matching_source_ids_without_an_actual_handler_is_not_completion():
    ledger = [{"instruction_requirements": [{"requirement_id": "ACTUAL", "response_mode": "writing"}]}]
    results, errors = _instruction_placements(ledger, [], [], [], [])
    assert errors == [{"reason": "preparation_instruction_handler_missing", "requirement_id": "ACTUAL"}]
    assert results[0]["status"] == "unresolved"
