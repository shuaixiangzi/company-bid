"""Requirement source fields retain TSV empty cells through strict models."""
import hashlib

import pytest
from pydantic import ValidationError

from company_bid.models import Issue, ParagraphBlock, PrescribedBlock, ReferenceExcerpt, Requirement, ReviewAction, WritingTask


def contracts(source):
    return [
        (Requirement, dict(requirement_id=" R ", text=source, category="commercial", source_refs=[" S "])),
        (ReviewAction, dict(action_id=" A ", kind="commercial_constraint", status="not_checked",
            requirement_refs=[" R "], source_refs=[" S "], source_requirement=source, instruction=" 核对原文 ")),
        (Issue, dict(issue_id="M-001", title=" 待补 ", section_id=" SEC ", requirement_refs=[" R "],
            reason=" 缺材料 ", action=" 线下补齐 ", source_requirement=source)),
        (WritingTask, dict(unit_id="draft", title=" 拟实施方法 ", objective=" 说明拟开展的检查方法 ",
            must_cover=[source], requirement_refs=[" R "], forbidden_fields=[" 不编造资质 "],
            completion_criteria=[" 对照要求 "], operation="bounded_new_draft", no_reference_reason=" 无历史方法 ")),
    ]


@pytest.mark.parametrize("source", ["\t\t证明材料之一：合同或验收文件\t", " \n以合同签订或验收文件签署时间为准。\t\n "])
def test_source_fields_round_trip_exactly_while_labels_and_ids_still_trim(source):
    for model, values in contracts(source):
        value = model.model_validate(values)
        restored = model.model_validate_json(value.model_dump_json())
        field = "text" if model is Requirement else "must_cover" if model is WritingTask else "source_requirement"
        assert getattr(value, field) == ([source] if field == "must_cover" else source)
        assert restored == value
        dumped = value.model_dump()
        for key in ("requirement_id", "action_id", "section_id", "title", "instruction", "reason", "action"):
            if key in dumped:
                assert dumped[key] == dumped[key].strip()
        for key in ("requirement_refs", "source_refs"):
            if key in dumped:
                assert all(item == item.strip() for item in dumped[key])


@pytest.mark.parametrize("source", ["", " ", "\t\n\r", "\u3000\u00a0"])
def test_blank_only_source_remains_invalid(source):
    for model, values in contracts(source):
        with pytest.raises(ValidationError):
            model.model_validate(values)


def test_n3_writing_task_retains_original_table_delimiters(tmp_path):
    from company_bid.extraction import extract_requirements
    from company_bid.planning import plan_bid
    from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected
    manifest, parsed = fixture(tmp_path)
    table = block(parsed, "TABLE", "", kind="table", rows=[["方法", "说明", ""], ["巡检", "应保留原始记录。", ""]],
                  merges=[], column_widths=[1, 2, 1], header_rows=[])
    def caller(ctx, task_type, unit_id, payload, validate):
        return validate(live_candidate(payload, selected(table)))
    extracted = extract_requirements(Context(), parsed, manifest, caller, {})
    expected = extracted["requirements"][0]["citations"][0]["quote"]
    assert expected.endswith("\t")
    prepared = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert prepared["requirements"][0]["text"] == expected
    assert prepared["sections"][0]["writing_tasks"][0]["must_cover"] == [expected]


def test_prescribed_source_paragraph_and_hashed_reference_retain_whitespace():
    source = "\t\t固定说明：应保留原始记录。\t\n"
    fixed = PrescribedBlock(kind="paragraph", text=source, source_refs=["S"])
    rendered = ParagraphBlock(text=fixed.text, source_refs=fixed.source_refs, content_role="neutral")
    assert rendered.text == source
    assert ParagraphBlock.model_validate_json(rendered.model_dump_json()).text == source
    reference = dict(reference_id="REF", source_id="S", locator="paragraph[1]", text=source,
                     text_sha256=hashlib.sha256(source.encode()).hexdigest(), applicability="方法参考",
                     retained_points=["保留方法"], excluded_points=[])
    assert ReferenceExcerpt.model_validate(reference).text == source
    with pytest.raises(ValidationError, match="hash mismatch"):
        ReferenceExcerpt.model_validate({**reference, "text": source.strip()})


@pytest.mark.parametrize("value", ["", " \t\n "])
def test_paragraph_still_rejects_blank_only_text(value):
    with pytest.raises(ValidationError):
        ParagraphBlock(text=value)
