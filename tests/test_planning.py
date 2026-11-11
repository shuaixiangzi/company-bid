"""Synthetic contract tests: no network, document generation or company data."""

import copy

import pytest

from company_bid.models import PreparedBid
from company_bid.planning import PlanningError, extract_requirements, plan_bid


class Context:
    def __init__(self):
        self.units = {}
        self.attempts = []
        self.captures = {}

    def run_unit(self, unit_id, input_data, fn):
        if unit_id in self.units:
            assert self.units[unit_id][0] == input_data
            return copy.deepcopy(self.units[unit_id][1])
        self.attempts.append(unit_id)
        result = fn()
        self.units[unit_id] = (copy.deepcopy(input_data), copy.deepcopy(result))
        return result

    def capture(self, name, payload, *, partial=False):
        self.captures[name] = copy.deepcopy(payload)


def fixture(tmp_path):
    source = {"source_id": "tender", "title": "虚构招标资料", "path": str(tmp_path / "tender.txt"),
              "sha256": "a" * 64, "locator": "document", "kind": "tender", "provenance": "synthetic", "synthetic": True}
    company_source = {**source, "source_id": "company", "title": "虚构公司明确输入", "sha256": "b" * 64,
                      "kind": "manual_input", "path": str(tmp_path / "company.txt")}
    manifest = {"project": {"project_id": "P", "name": "虚构设备巡检项目", "source_refs": ["tender"]},
                "company": {"company_id": "C", "name": "虚构测试公司", "source_refs": ["company"]},
                "sources": [source, company_source], "source_roles": {"tender": "tender", "company": "company_material"},
                "facts": [{"fact_id": "F-ADDRESS", "label": "注册地址", "value": "测试路1号（虚构）", "source_refs": ["company"]}],
                "attachments": [], "synthetic": True, "preparation_note": "仅供合成测试，非真实投标资料。"}
    parsed = {"documents": [{"source_id": "tender", "role": "tender", "sha256": source["sha256"], "units": []}],
              "coverage": {"status": "complete", "required_complete": True}}
    return manifest, parsed


def add_unit(parsed, unit_id, text, *, kind="paragraph", status="read", **extra):
    document = parsed["documents"][0]
    block = {"block_id": "B-" + unit_id, "source_id": document["source_id"], "kind": kind,
             "text": text, "locator": "paragraph:" + unit_id, **extra}
    unit = {"unit_id": unit_id, "source_id": document["source_id"], "locator": "unit:" + unit_id,
            "page": len(document["units"]) + 1, "coverage": {"status": status}, "blocks": [] if status == "empty" else [block]}
    document["units"].append(unit)
    return block


def cite(block, quote=None):
    return {key: block[key] for key in ("source_id", "block_id", "locator")} | {"quote": quote or block["text"]}


def requirement(block, *, category="technical", mode="writing", **extra):
    return {"citation": cite(block), "category": category, "response_mode": mode,
            "format_status": "not_prescribed", **extra}


def response(payload, requirements=None, formats=None, **extra):
    return {"reviewed_block_ids": [b["block_id"] for b in payload["window"]["blocks"]],
            "requirements": requirements or [], "format_chapters": formats or [],
            "format_status": "specified" if formats else "none_verified", **extra}


def extract(ctx, parsed, manifest, make, config=None):
    def caller(ctx, task_type, unit_id, payload, validate):
        assert task_type == "extract_requirements"
        assert payload["output_contract"]["type"] == "object"
        return validate(make(payload))
    return extract_requirements(ctx, parsed, manifest, caller, config or {})


def no_call(*args):
    pytest.fail("Unexpected model call")


def test_one_proof_bundle_is_matched_with_all_pages_in_order(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应提供营业执照复印件。")
    manifest["attachments"] = [{"attachment_id": f"A-{page}", "section_id": "unassigned", "title": "营业执照复印件",
        "description": "合成证明", "path": str(tmp_path / f"proof-{page}.png"), "sha256": "c"*64,
        "source_refs": ["company"], "requirement_refs": ["unassigned"], "synthetic": True,
        "bundle_id": "LICENSE", "page_index": page, "page_count": 2} for page in [2, 1]]
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block, category="qualification",
                          mode="attachment", attachment_keywords=["营业执照"])]))
    output = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert [a["page_index"] for a in output["attachments"]] == [1, 2]
    assert len(output["sections"][0]["attachment_refs"]) == 2
    assert not output["issues"]
    manifest["attachments"] = manifest["attachments"][:1]
    with pytest.raises(ValueError, match="all ordered pages"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})


def test_all_units_in_denominator_empty_unit_no_model_and_bounded_draft(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应编写巡检方法，说明测点编号和异常复测流程。")
    add_unit(parsed, "U2", "", status="empty")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block)]))
    assert extracted["coverage"]["tender_unit_count"] == 2
    assert extracted["coverage"]["window_count"] == 2
    assert all(row["window_ids"] for row in extracted["coverage"]["units"])
    prepared = PreparedBid.model_validate(plan_bid(ctx, extracted, parsed, manifest, no_call, {}))
    task = prepared.sections[0].writing_tasks[0]
    assert task.operation == "bounded_new_draft" and task.no_reference_reason
    assert prepared.input_kind == "automatic"
    assert prepared.requirements[0].text == block["text"]
    assert prepared.sources[-1].sha256 == "a" * 64
    assert "quote_sha256=" in prepared.sources[-1].locator


@pytest.mark.parametrize("status", ["needs_ocr", "failed", "unsupported"])
def test_unread_tender_cannot_be_success(tmp_path, status):
    manifest, parsed = fixture(tmp_path)
    add_unit(parsed, "U1", "未读内容", status=status)
    with pytest.raises(PlanningError, match="TENDER_READING_INCOMPLETE"):
        extract_requirements(Context(), parsed, manifest, no_call, {})


@pytest.mark.parametrize("mutation,code", [
    (lambda value: value["requirements"][0]["citation"].update(quote="并不存在的原文"), "CITATION_QUOTE"),
    (lambda value: value["requirements"][0]["citation"].update(locator="elsewhere"), "CITATION_LOCATION"),
    (lambda value: value.update(reviewed_block_ids=[]), "WINDOW_BLOCK_DENOMINATOR"),
    (lambda value: value["requirements"][0].update(required_fact_labels=["未出现字段"]), "LABEL_NOT_GROUNDED"),
])
def test_source_grounding_and_denominator_reject_candidates(tmp_path, mutation, code):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应编写明确的巡检方案。")
    def make(payload):
        value = response(payload, [requirement(block)])
        mutation(value)
        return value
    with pytest.raises(PlanningError, match=code):
        extract(Context(), parsed, manifest, make)


def test_repeated_quote_requires_unambiguous_larger_excerpt(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应记录。应记录。")
    candidate = requirement(block)
    candidate["citation"]["quote"] = "应记录。"
    with pytest.raises(PlanningError, match="CITATION_QUOTE_NOT_UNIQUE_EXACT"):
        extract(Context(), parsed, manifest, lambda p: response(p, [candidate]))


def test_failed_window_resume_reuses_completed_and_preserves_money_and_negation(tmp_path):
    manifest, parsed = fixture(tmp_path)
    b1 = add_unit(parsed, "U1", "投标保证金为100元，不得现金支付。")
    b2 = add_unit(parsed, "U2", "投标保证金为200元，可以现金支付。")
    ctx, calls = Context(), []
    config = {"window_max_blocks": 1}
    failed_once = False
    def caller(ctx, task_type, unit_id, payload, validate):
        nonlocal failed_once
        calls.append(unit_id)
        block = payload["window"]["blocks"][0]
        if block["block_id"] == b2["block_id"] and not failed_once:
            failed_once = True
            raise TimeoutError("synthetic transient failure")
        return validate(response(payload, [requirement(block, category="procedure", mode="procedure")]))
    with pytest.raises(TimeoutError):
        extract_requirements(ctx, parsed, manifest, caller, config)
    result = extract_requirements(ctx, parsed, manifest, caller, config)
    assert len(calls) == 3 and calls[0] != calls[1] == calls[2]
    assert len(result["requirements"]) == 2
    assert {r["text"] for r in result["requirements"]} == {b1["text"], b2["text"]}
    prepared = plan_bid(ctx, result, parsed, manifest, no_call, config)
    assert all(not s["render_body"] for s in prepared["sections"])


def formatted_fixture(tmp_path, *, two_targets=False):
    manifest, parsed = fixture(tmp_path)
    heading = add_unit(parsed, "H", "一、企业基本情况表")
    before = add_unit(parsed, "P1", "请保持本表原格式。")
    rows = [["企业基本情况", "", ""], ["投标人名称", "", ""] if two_targets else ["投标人名称", "", "原印刷备注"],
            ["注册地址", "", "原印刷单位：项"], ["报价（万元）", "", "需明确输入"]]
    table = add_unit(parsed, "T", "企业基本情况\n投标人名称\n注册地址\n报价（万元）", kind="table", rows=rows,
                     merges=[{"row": 0, "column": 0, "row_span": 1, "col_span": 3}], column_widths=[2, 4, 3], header_rows=[0])
    after = add_unit(parsed, "P2", "附：上述内容按原表填写。")
    def make(payload):
        return response(payload, formats=[{"title": heading["text"], "level": 1, "citation": cite(heading),
                                          "content_block_ids": [before["block_id"], table["block_id"], after["block_id"]]}])
    return manifest, parsed, table, make


def test_original_format_order_grid_labels_merges_and_unknown_value_issues(tmp_path):
    manifest, parsed, source_table, make = formatted_fixture(tmp_path)
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, make)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    section, table = prepared["sections"][0], prepared["tables"][0]
    assert [b["kind"] for b in section["fixed_content"]] == ["paragraph", "table_ref", "paragraph"]
    assert table["header_from_columns"] is False and table["repeat_header_rows"] == 1
    assert table["merges"] == source_table["merges"] and table["column_widths"] == [2, 4, 3]
    assert table["rows"][0][0]["text"] == "企业基本情况"
    assert table["rows"][0][1] == {"text": "", "fact_refs": [], "issue_refs": []}
    assert table["rows"][1][1]["text"] == "虚构测试公司"
    assert table["rows"][2][1]["fact_refs"] == ["F-ADDRESS"]
    assert table["rows"][3][0]["text"] == "报价（万元）"
    assert table["rows"][3][1]["issue_refs"] == [prepared["issues"][0]["issue_id"]]
    assert not section["writing_tasks"]
    assert len(ctx.captures["n3-planning-report"]["tables"]) == 1


def test_narrow_mapping_gets_no_values_and_cannot_overwrite_or_use_wrong_label(tmp_path):
    manifest, parsed, table, make = formatted_fixture(tmp_path, two_targets=True)
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, make)
    def invalid(ctx, task_type, unit_id, payload, validate):
        assert task_type == "map_form_fields"
        assert all("value" not in fact for fact in payload["allowed_facts"])
        return {"grid_sha256": payload["grid_sha256"], "fills": [{"row": 1, "column": 0,
                "fact_id": payload["allowed_facts"][0]["fact_id"], "label": "投标人名称", "label_row": 1, "label_column": 0}]}
    with pytest.raises(PlanningError, match="TABLE_MAPPING_TARGET_INVALID"):
        plan_bid(ctx, extracted, parsed, manifest, invalid, {})
    assert "plan-assemble" not in ctx.units


def test_unread_format_and_prescribed_but_unlocated_format_never_free_write(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应按指定格式编写技术方案。")
    for status in ("unread", "prescribed"):
        ctx = Context()
        candidate = requirement(block, format_status=status)
        extracted = extract(ctx, parsed, manifest, lambda p: response(p, [candidate], format_status="unread" if status == "unread" else "none_verified"))
        with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
            plan_bid(ctx, extracted, parsed, manifest, no_call, {})
        assert ctx.captures["n3-format-coverage"]["status"] == "failed"


def test_reference_bid_method_is_scoped_and_historical_facts_not_imported(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应说明设备测点编号与异常复测流程。")
    source = {**manifest["sources"][0], "source_id": "reference", "kind": "company_material", "provenance": "derived_from_bid"}
    manifest["sources"].append(source)
    manifest["source_roles"]["reference"] = "reference_bid"
    parsed["documents"].append({"source_id": "reference", "role": "reference_bid", "sha256": source["sha256"], "units": [
        {"unit_id": "REFU", "source_id": "reference", "locator": "paragraph:1", "page": None, "coverage": {"status": "read"}, "blocks": [
            {"block_id": "REFB", "source_id": "reference", "locator": "paragraph:1", "kind": "paragraph",
             "text": "设备测点编号采用现场标签，异常复测记录应保留初测值和复测方法。"}]}]})
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block, reference_keywords=["设备测点编号", "异常复测"])]))
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert prepared["sections"][0]["writing_tasks"][0]["operation"] == "reference_adaptation"
    assert prepared["reference_excerpts"][0]["source_id"] == "reference"
    assert all(f["source_refs"] != ["reference"] for f in prepared["facts"])


def test_attachment_exact_scope_selected_once_and_expiry_not_assumed(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应附营业执照，须在有效期内。")
    manifest["attachments"] = [{"attachment_id": "A1", "section_id": "old-section", "title": "营业执照测试页", "path": str(tmp_path / "proof.png"),
        "sha256": "c" * 64, "source_refs": ["company"], "requirement_refs": ["old-requirement"], "synthetic": True,
        "description": "测试用途的虚构营业执照图片"}]
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block, category="qualification", mode="attachment", attachment_keywords=["营业执照"])]))
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert len(prepared["attachments"]) == 1
    assert prepared["attachments"][0]["requirement_refs"] == [prepared["requirements"][0]["requirement_id"]]
    assert any("适用条件" in issue["title"] for issue in prepared["issues"])


def test_aggregate_cannot_be_mutated_after_source_validation(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "必须按原格式提交。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block, category="procedure", mode="procedure")]))
    extracted["requirements"][0]["response_mode"] = "writing"
    with pytest.raises(PlanningError, match="EXTRACTION_MERGE_MISMATCH"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})


def test_oversized_block_is_not_silently_truncated(tmp_path):
    manifest, parsed = fixture(tmp_path)
    add_unit(parsed, "U1", "必须" * 500)
    with pytest.raises(PlanningError, match="SOURCE_BLOCK_EXCEEDS_WINDOW_LIMIT"):
        extract_requirements(Context(), parsed, manifest, no_call, {"window_max_chars": 256})


def test_valid_narrow_mapping_checkpoint_reused_after_later_capture_failure(tmp_path):
    manifest, parsed, table, make = formatted_fixture(tmp_path, two_targets=True)
    class FlakyContext(Context):
        fail_once = True
        def capture(self, name, payload, *, partial=False):
            if name == "n3-planning-report" and self.fail_once:
                self.fail_once = False
                raise OSError("synthetic capture interruption")
            super().capture(name, payload, partial=partial)
    ctx, calls = FlakyContext(), []
    extracted = extract(ctx, parsed, manifest, make)
    def caller(ctx, task_type, unit_id, payload, validate):
        calls.append(unit_id)
        fact = next(f for f in payload["allowed_facts"] if f["label"] == "投标人名称")
        return validate({"grid_sha256": payload["grid_sha256"], "fills": [{"row": 1, "column": 1,
            "fact_id": fact["fact_id"], "label": "投标人名称", "label_row": 1, "label_column": 0}]})
    with pytest.raises(OSError):
        plan_bid(ctx, extracted, parsed, manifest, caller, {})
    result = plan_bid(ctx, extracted, parsed, manifest, caller, {})
    assert len(calls) == 1
    assert result["tables"][0]["rows"][1][1]["text"] == "虚构测试公司"
    assert result["tables"][0]["rows"][1][2]["issue_refs"]


@pytest.mark.parametrize("role,provenance,expected", [
    ("company_material", "manual_input", False),
    ("project_supplement", "derived_from_bid", False),
    ("project_supplement", "manual_input", True),
])
def test_prices_require_this_project_explicit_input(tmp_path, role, provenance, expected):
    manifest, parsed, source_table, make = formatted_fixture(tmp_path)
    manifest["sources"][1]["provenance"] = provenance
    manifest["source_roles"]["company"] = role
    manifest["facts"].append({"fact_id": "F-PRICE", "label": "报价（万元）", "value": "12.00", "source_refs": ["company"]})
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, make)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    cell = prepared["tables"][0]["rows"][3][1]
    assert bool(cell["fact_refs"]) is expected
    assert (cell["text"] == "12.00") is expected


def test_missing_tender_document_is_not_hidden_by_complete_page_ledger(tmp_path):
    manifest, parsed = fixture(tmp_path)
    add_unit(parsed, "U1", "必须提交方案。")
    source = {**manifest["sources"][0], "source_id": "tender-omitted"}
    manifest["sources"].append(source)
    manifest["source_roles"][source["source_id"]] = "tender"
    with pytest.raises(PlanningError, match="TENDER_DOCUMENT_DENOMINATOR_MISMATCH"):
        extract_requirements(Context(), parsed, manifest, no_call, {})


def test_real_docx_reader_dto_flows_to_extraction_and_plan_with_frozen_windows(tmp_path):
    from docx import Document
    from docx.oxml import OxmlElement
    from company_bid.models import file_sha256
    from company_bid.reading import read_documents

    manifest, _ = fixture(tmp_path)
    document = Document()
    document.add_paragraph("一、响应人基本情况表")
    document.add_paragraph("")
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "原表标题"
    table.cell(1, 0).text = "投标人名称"
    table.cell(2, 0).text = "注册地址"
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    document.add_paragraph("拟实施方法应说明设备测点编号和异常复测。")
    document.sections[0].footer.paragraphs[0].text = "递交事项：本文件必须密封。"
    path = tmp_path / "synthetic-source.docx"
    document.save(path)
    manifest["sources"][0].update(path=str(path), sha256=file_sha256(path))
    ctx = Context()
    parsed = read_documents(ctx, [{**manifest["sources"][0], "role": "tender"}], {})
    assert parsed["coverage"]["required_complete"]
    assert any(u["coverage"]["status"] == "empty" for u in parsed["documents"][0]["units"])
    def make(payload):
        blocks = payload["window"]["blocks"]
        requirements, formats = [], []
        for block in blocks:
            if "拟实施方法" in block["text"]:
                requirements.append(requirement(block))
            elif "递交事项" in block["text"]:
                requirements.append(requirement(block, category="procedure", mode="procedure"))
        headings = [b for b in blocks if b["text"] == "一、响应人基本情况表"]
        grids = [b for b in blocks if b["kind"] == "table"]
        if headings:
            assert len(grids) == 1
            formats.append({"title": headings[0]["text"], "level": 1, "citation": cite(headings[0]),
                            "content_block_ids": [grids[0]["block_id"]]})
        return response(payload, requirements, formats)
    extracted = extract(ctx, parsed, manifest, make, {"window_max_chars": 9000, "window_max_blocks": 15})
    # N3's own planning options must not rebuild different N2 windows.
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted["window_config"]["window_max_chars"] == 9000
    assert all(unit["page"] is None for unit in extracted["coverage"]["units"])
    assert len(prepared["tables"]) == 1 and prepared["tables"][0]["merges"][0]["row"] == 0
    assert any(section["writing_tasks"] for section in prepared["sections"])
    assert any(not section["render_body"] for section in prepared["sections"])


def test_format_continuation_across_windows_is_original_order_and_accounted(tmp_path):
    manifest, parsed = fixture(tmp_path)
    major = add_unit(parsed, "U0", "第六章 投标文件格式")
    heading = add_unit(parsed, "U1", "一、企业基本情况表")
    paragraph = add_unit(parsed, "U2", "本表全部内容按原格式填写。")
    table = add_unit(parsed, "U3", "公司名称\n注册地址", kind="table", rows=[["公司名称", ""], ["注册地址", ""]],
                     merges=[], column_widths=[1, 2], header_rows=[])
    ctx = Context()
    def make(payload):
        block = payload["window"]["blocks"][0]
        formats = [{"title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": []}] if block["block_id"] == heading["block_id"] else []
        return response(payload, formats=formats)
    extracted = extract(ctx, parsed, manifest, make, {"window_max_blocks": 1})
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert [block["kind"] for block in prepared["sections"][0]["fixed_content"]] == ["paragraph", "table_ref"]
    ledger = ctx.captures["n3-format-coverage"]["blocks"]
    assert {row["block_id"] for row in ledger} == {major["block_id"], heading["block_id"], paragraph["block_id"], table["block_id"]}
    assert sum(row["status"] == "structural_continuation" for row in ledger) == 2
    assert ctx.captures["n3-format-coverage"]["status"] == "complete"


def test_unassigned_format_heading_cannot_disappear_across_windows(tmp_path):
    manifest, parsed = fixture(tmp_path)
    add_unit(parsed, "U0", "第六章 投标文件格式")
    heading = add_unit(parsed, "U1", "一、基本情况表")
    add_unit(parsed, "U2", "填写说明。")
    missed = add_unit(parsed, "U3", "二、授权委托书")
    ctx = Context()
    def make(payload):
        formats = [{"title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": []}] if any(b["block_id"] == heading["block_id"] for b in payload["window"]["blocks"]) else []
        return response(payload, formats=formats)
    extracted = extract(ctx, parsed, manifest, make, {"window_max_blocks": 1})
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert any(error.get("block_id") == missed["block_id"] for error in ctx.captures["n3-format-coverage"]["errors"])


def test_mixed_prescribed_format_and_technical_writing_keep_distinct_coverage(tmp_path):
    manifest, parsed = fixture(tmp_path)
    heading = add_unit(parsed, "U1", "一、技术服务方案")
    fixed = add_unit(parsed, "U2", "本部分须保留以下声明文字。")
    permission = add_unit(parsed, "U2A", "（响应人自行编制）")
    tech = add_unit(parsed, "U3", "应说明设备隔离许可与异常复测流程。")
    ctx = Context()
    def make(payload):
        candidate = requirement(tech, format_status="prescribed", format_title=heading["text"])
        chapter = {"title": heading["text"], "level": 1, "citation": cite(heading),
                   "content_block_ids": [fixed["block_id"], permission["block_id"]], "requirement_indices": [0]}
        return response(payload, [candidate], [chapter])
    extracted = extract(ctx, parsed, manifest, make)
    prepared = PreparedBid.model_validate(plan_bid(ctx, extracted, parsed, manifest, no_call, {}))
    section = prepared.sections[0]
    assert len(section.writing_tasks) == 1
    assert section.writing_tasks[0].requirement_refs == [extracted["requirements"][0]["requirement_id"]]
    assert section.nonwriting_requirement_refs and section.nonwriting_requirement_refs[0].startswith("R-FORM-")
    assert not set(section.writing_tasks[0].requirement_refs) & set(section.nonwriting_requirement_refs)
    assert len(section.writing_tasks[0].must_cover) == 1
    assert section.fixed_content[0].text == fixed["text"]
    assert section.writing_tasks[0].format_status == "not_prescribed"
    assert not any(block.text == permission["text"] for block in section.fixed_content)


@pytest.mark.parametrize("state", ["unread", "conflict"])
def test_format_window_conflict_is_not_a_business_missing_value(tmp_path, state):
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "U1", "应按该表逐项填写，不得遗漏。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, [requirement(block, category="fixed_table", mode="table", format_status=state)], format_status=state))
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert ctx.captures["n3-format-coverage"]["errors"]


@pytest.mark.parametrize("label", ["投标总价（元）", "单价（元）", "税额（元）"])
@pytest.mark.parametrize("role,provenance,expected", [("company_material", "derived_from_bid", False), ("project_supplement", "manual_input", True)])
def test_project_price_and_tax_fields_never_inherit_historical_values(tmp_path, label, role, provenance, expected):
    manifest, parsed = fixture(tmp_path)
    manifest["sources"][1]["provenance"] = provenance
    manifest["source_roles"]["company"] = role
    manifest["facts"] = [{"fact_id": "F-AMOUNT", "label": label, "value": "123.45", "source_refs": ["company"]}]
    heading = add_unit(parsed, "H", "1 报价表")
    table = add_unit(parsed, "T", "", kind="table", rows=[[label, ""]], merges=[], column_widths=[1, 2])
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda p: response(p, formats=[{"title": heading["text"], "level": 1,
        "citation": cite(heading), "content_block_ids": [table["block_id"]]}]))
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert bool(prepared["tables"][0]["rows"][0][1]["fact_refs"]) is expected


def test_conflicting_facts_cannot_be_selected_by_mapping_model(tmp_path):
    manifest, parsed, table, make = formatted_fixture(tmp_path, two_targets=True)
    table["rows"][1][0] = "注册地址"
    manifest["facts"].append({"fact_id": "F-ADDRESS-CONFLICT", "label": "注册地址", "value": "另一个冲突地址", "source_refs": ["company"]})
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, make)
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert all(not prepared["tables"][0]["rows"][1][column]["fact_refs"] for column in (1, 2))
    assert all(prepared["tables"][0]["rows"][1][column]["issue_refs"] for column in (1, 2))


def pdf_line(parsed, page, index, text, box):
    document = parsed["documents"][0]
    unit = next((u for u in document["units"] if u["page"] == page), None)
    if unit is None:
        unit = {"unit_id": f"U{page}", "source_id": "tender", "locator": f"pdf/page[{page}]", "page": page,
                "coverage": {"status": "read"}, "blocks": []}
        document["units"].append(unit)
    block = {"block_id": f"B{page}-{index}", "source_id": "tender", "locator": f"pdf/page[{page}]/line[{index}]",
             "kind": "paragraph", "text": text, "bbox": box}
    unit["blocks"].append(block)
    return block


def three_pdf_pages(tmp_path):
    manifest, parsed = fixture(tmp_path)
    condition = pdf_line(parsed, 1, 0, "未获得隔离许可时，", [60, 100, 300, 112])
    footer1 = pdf_line(parsed, 1, 1, "- 1 -", [290, 773, 305, 782])
    negative = pdf_line(parsed, 2, 0, "不得开展带电检测。", [60, 100, 300, 112])
    footer2 = pdf_line(parsed, 2, 1, "- 2 -", [290, 773, 305, 782])
    pdf_line(parsed, 3, 0, "递交文件须密封。", [60, 100, 300, 112])
    footer3 = pdf_line(parsed, 3, 1, "- 3 -", [290, 773, 305, 782])
    return manifest, parsed, condition, negative, [footer1, footer2, footer3]


def test_cross_page_span_preserves_condition_negation_and_only_skips_verified_footer(tmp_path):
    manifest, parsed, condition, negative, footers = three_pdf_pages(tmp_path)
    ctx = Context()
    candidate = requirement(condition)
    candidate["citation"].update(block_ids=[condition["block_id"], negative["block_id"]], quote=condition["text"] + "\n" + negative["text"])
    def make(payload):
        assert payload["window"]["context_identity"]["sha256"]
        blocks = payload["window"]["blocks"]
        return response(payload, [candidate] if any(b["block_id"] == condition["block_id"] for b in blocks) else [])
    extracted = extract(ctx, parsed, manifest, make, {"window_max_blocks": 1})
    citation = extracted["requirements"][0]["citations"][0]
    assert citation["basis"] == "consecutive_blocks_lf_v1"
    assert citation["skipped_block_ids"] == [footers[0]["block_id"]]
    assert citation["pages"] == [1, 2]
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert prepared["requirements"][0]["text"] == condition["text"] + "\n" + negative["text"]
    assert "through=pdf/page[2]/line[0]" in prepared["sources"][-1]["locator"]
    skipped = [row for row in ctx.captures["n3-format-coverage"]["blocks"] if row["status"] == "skipped_page_footer"]
    assert len(skipped) == 3 and all(row["bbox"] and len(row["evidence_pages"]) == 3 for row in skipped)


@pytest.mark.parametrize("failure", ["ordinary_gap", "cropped_quote", "outside_target"])
def test_span_cannot_drop_ordinary_text_condition_or_preempt_another_window(tmp_path, failure):
    manifest, parsed, condition, negative, footers = three_pdf_pages(tmp_path)
    if failure == "ordinary_gap":
        ordinary = {**condition, "block_id": "ORDINARY", "locator": "pdf/page[1]/line[extra]", "text": "作业前还须取得业主书面授权。", "bbox": [60, 120, 300, 132]}
        parsed["documents"][0]["units"][0]["blocks"].insert(1, ordinary)
    candidate = requirement(condition)
    candidate["citation"].update(block_ids=[condition["block_id"], negative["block_id"]],
                                  quote=negative["text"] if failure == "cropped_quote" else condition["text"] + "\n" + negative["text"])
    def make(payload):
        target = payload["window"]["blocks"][0]
        emit = target["block_id"] == (footers[1]["block_id"] if failure == "outside_target" else condition["block_id"])
        return response(payload, [candidate] if emit else [])
    code = {"ordinary_gap": "CITATION_SPAN_NOT_CONTIGUOUS", "cropped_quote": "CITATION_SPAN_MUST_QUOTE_ALL_BLOCKS", "outside_target": "CITATION_OUTSIDE_REVIEW_WINDOW"}[failure]
    with pytest.raises(PlanningError, match=code):
        extract(Context(), parsed, manifest, make, {"window_max_blocks": 1})


def test_table_citation_view_is_explicitly_tsv_and_source_cells_are_recoverable(tmp_path):
    manifest, parsed = fixture(tmp_path)
    table = add_unit(parsed, "T", "", kind="table", rows=[["递交要求", "不得迟于截止时间"]], merges=[], column_widths=[1, 2])
    ctx = Context()
    def make(payload):
        canonical = payload["window"]["blocks"][0]
        assert canonical["text"] == "递交要求\t不得迟于截止时间"
        return response(payload, [requirement(canonical, category="procedure", mode="procedure")])
    extracted = extract(ctx, parsed, manifest, make)
    citation = extracted["requirements"][0]["citations"][0]
    assert citation["basis"] == "table_rows_tsv_v1" and citation["cell_refs"] == [{"row": 0, "column": 0}, {"row": 0, "column": 1}]
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert "basis=table_rows_tsv_v1" in prepared["sources"][-1]["locator"]
    assert table["text"] == ""  # Reader's original DTO was not mutated.


def test_plain_number_format_heading_is_boundary_but_numbered_prose_is_not(tmp_path):
    from company_bid.planning import _heading_level
    assert _heading_level("1 法人代表授权书") == 1
    assert _heading_level("2 公司基本情况") == 1
    assert _heading_level("1 应提供公司基本情况") is None
    assert _heading_level("1 现场按编号记录设备检查结果") is None
    manifest, parsed = fixture(tmp_path)
    add_unit(parsed, "M", "第五章 响应文件要求及格式")
    heading = add_unit(parsed, "H", "1 公司基本情况表")
    add_unit(parsed, "P", "按表填写。")
    missed = add_unit(parsed, "H2", "2 质量保证承诺书")
    ctx = Context()
    def make(payload):
        return response(payload, formats=[{"title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": []}])
    extracted = extract(ctx, parsed, manifest, make)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert any(error.get("block_id") == missed["block_id"] for error in ctx.captures["n3-format-coverage"]["errors"])


def test_cross_window_format_title_uses_frozen_catalog_and_grounded_writing_space(tmp_path):
    from company_bid.provider import validate_writing_context
    manifest, parsed = fixture(tmp_path)
    tech = add_unit(parsed, "T", "技术服务方案应说明异常复测方法。")
    heading = add_unit(parsed, "H", "8 技术服务方案")
    permission = add_unit(parsed, "P", "（响应人自行编制）")
    ctx = Context()
    def make(payload):
        block = payload["window"]["blocks"][0]
        assert any(row["title"] == heading["text"] for row in payload["window"]["source_heading_catalog"])
        return response(payload, [requirement(tech, format_status="prescribed", format_title=heading["text"])] if block["block_id"] == tech["block_id"] else [],
                        [{"title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": []}] if block["block_id"] == heading["block_id"] else [])
    extracted = extract(ctx, parsed, manifest, make, {"window_max_blocks": 1})
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    task = prepared["sections"][0]["writing_tasks"][0]
    validate_writing_context({"section_id": task["unit_id"], "writing_task": task, "reference_excerpts": []})
    decision = ctx.captures["n3-planning-report"]["writing_space_decisions"][0]
    assert decision["decision"] == "grounded_open_body_in_prescribed_section" and decision["citations"][0]["block_id"] == permission["block_id"]


@pytest.mark.parametrize("heading_text,permission_text", [("8 技术服务方案", "响应人不得自行编制。"), ("9 质量保证承诺书", "（响应人自行编制）")])
def test_title_or_fixed_commitment_cannot_authorize_technical_free_writing(tmp_path, heading_text, permission_text):
    manifest, parsed = fixture(tmp_path)
    heading = add_unit(parsed, "H", heading_text)
    permission = add_unit(parsed, "P", permission_text)
    tech = add_unit(parsed, "T", "应说明异常复测方法。")
    ctx = Context()
    def make(payload):
        return response(payload, [requirement(tech, format_status="prescribed", format_title=heading["text"])],
                        [{"title": heading["text"], "level": 1, "citation": cite(heading), "content_block_ids": [permission["block_id"]], "requirement_indices": [0]}])
    extracted = extract(ctx, parsed, manifest, make)
    with pytest.raises(PlanningError, match="REQUIRED_FORMAT_UNRESOLVED"):
        plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert any(error["reason"] == "technical_writing_space_not_grounded" for error in ctx.captures["n3-format-coverage"]["errors"])


def test_table_whole_placeholders_fill_but_composite_price_marker_stays_protected(tmp_path):
    manifest, parsed = fixture(tmp_path)
    heading = add_unit(parsed, "H", "1 基本情况表")
    table = add_unit(parsed, "T", "", kind="table", rows=[["投标人名称", "________"], ["注册地址", "（）"], ["投标总价", "___万元"]],
                     merges=[], column_widths=[1, 2])
    paragraph = add_unit(parsed, "P", "项目名称：（填写项目名称），授权人：（响应人名称）。")
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda payload: response(payload, formats=[{"title": heading["text"], "level": 1,
        "citation": cite(heading), "content_block_ids": [table["block_id"], paragraph["block_id"]]}]))
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    rows = prepared["tables"][0]["rows"]
    assert rows[0][1]["text"] == "虚构测试公司" and rows[0][1]["fact_refs"]
    assert rows[1][1]["text"] == "测试路1号（虚构）" and rows[1][1]["fact_refs"]
    assert rows[2][1]["text"] == "___万元" and rows[2][1]["issue_refs"]
    assert any("指定文字格式" in issue["title"] for issue in prepared["issues"])


def test_independent_n2_entry_and_planning_reexport_are_the_same_contract(tmp_path):
    from company_bid.extraction import extract_requirements as independent_extract
    manifest, parsed = fixture(tmp_path)
    block = add_unit(parsed, "N2", "必须提供可追溯巡检记录。")
    assert independent_extract is extract_requirements
    assert independent_extract.__module__ == "company_bid.extraction"
    result = independent_extract(Context(), parsed, manifest,
        lambda ctx, task_type, unit_id, payload, validate: validate(response(payload, [requirement(block)])), {})
    assert result["requirements"][0]["text"] == block["text"]


def test_fixed_table_and_cross_page_continuation_keep_footer_and_contract_out_of_body(tmp_path):
    manifest, parsed = fixture(tmp_path)
    major = pdf_line(parsed, 1, 0, "第五章 响应文件格式", [60, 60, 500, 78])
    heading = pdf_line(parsed, 1, 1, "1 公司基本情况表", [60, 90, 500, 104])
    table = {"block_id": "TABLE", "source_id": "tender", "locator": "pdf/page[1]/table[0]", "kind": "table", "text": "",
             "rows": [["投标人名称", ""]], "merges": [], "column_widths": [1, 2], "bbox": [60, 120, 500, 180]}
    parsed["documents"][0]["units"][0]["blocks"].append(table)
    pdf_line(parsed, 1, 2, "- 1 -", [290, 773, 305, 782])
    continuation = pdf_line(parsed, 2, 0, "本表原文后续说明。", [60, 100, 500, 112])
    pdf_line(parsed, 2, 1, "- 2 -", [290, 773, 305, 782])
    pdf_line(parsed, 3, 0, "第六章 合同条款", [60, 60, 500, 78])
    pdf_line(parsed, 3, 1, "合同约定内容不得混入上一格式。", [60, 100, 500, 112])
    pdf_line(parsed, 3, 2, "- 3 -", [290, 773, 305, 782])
    ctx = Context()
    extracted = extract(ctx, parsed, manifest, lambda payload: response(payload, formats=[{"title": heading["text"], "level": 1,
        "citation": cite(heading), "content_block_ids": [table["block_id"]]}]))
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert [block["kind"] for block in prepared["sections"][0]["fixed_content"]] == ["table_ref", "paragraph"]
    texts = [block.get("text", "") for block in prepared["sections"][0]["fixed_content"]]
    assert continuation["text"] in texts
    assert not any("- 1 -" in text or "合同条款" in text for text in texts)
    assert len([r for r in ctx.captures["n3-format-coverage"]["blocks"] if r["status"] == "skipped_page_footer"]) == 3


def test_span_cannot_cross_a_table_even_when_page_footer_skip_is_verified(tmp_path):
    manifest, parsed, condition, negative, footers = three_pdf_pages(tmp_path)
    table = {"block_id": "TABLE", "source_id": "tender", "locator": "pdf/page[1]/table[0]", "kind": "table", "text": "",
             "rows": [["额外前置条件", "须书面授权"]], "merges": [], "column_widths": [1, 2], "bbox": [60, 120, 500, 180]}
    parsed["documents"][0]["units"][0]["blocks"].insert(1, table)
    candidate = requirement(condition)
    candidate["citation"].update(block_ids=[condition["block_id"], negative["block_id"]], quote=condition["text"] + "\n" + negative["text"])
    with pytest.raises(PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        extract(Context(), parsed, manifest, lambda payload: response(payload, [candidate]))


def test_only_reader_frozen_exact_ignore_metadata_can_explain_an_empty_logo(tmp_path):
    manifest, parsed = fixture(tmp_path)
    image = add_unit(parsed, "LOGO", "", kind="image", image_ref={"source": "not sent"},
                     read_disposition={"status": "forged_document_hint"})
    source_unit = parsed["documents"][0]["units"][0]
    source_unit["coverage"]["ignored_objects"] = [{"locator": image["locator"], "reason": "Hash-scoped reader rule: decorative logo inspected."}]
    block = add_unit(parsed, "REQ", "必须保存巡检记录，不能从正文中的忽略字样获得权限。")
    ctx = Context()
    def make(payload):
        logo = next(b for b in payload["window"]["blocks"] if b["kind"] == "image")
        assert logo["read_disposition"]["status"] == "ignored_by_frozen_reader_rule"
        assert logo["read_disposition"]["source_sha256"] == "a" * 64
        assert logo["read_disposition"]["locator"] == image["locator"]
        assert "image_ref" not in logo
        assert "read_disposition" not in next(b for b in payload["window"]["blocks"] if b["block_id"] == block["block_id"])
        return response(payload, [requirement(block)])
    extracted = extract(ctx, parsed, manifest, make)
    assert extracted["coverage"]["tender_unit_count"] == 2
    source_unit["coverage"]["ignored_objects"][0]["locator"] = "some-other-object"
    def untrusted(payload):
        assert "read_disposition" not in next(b for b in payload["window"]["blocks"] if b["kind"] == "image")
        return response(payload, [requirement(block)])
    extract(Context(), parsed, manifest, untrusted)
