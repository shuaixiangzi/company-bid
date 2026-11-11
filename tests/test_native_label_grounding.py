"""Narrow native line joins, substantive requirements and private diagnostics."""
import copy
import hashlib

import pytest

from company_bid.extraction import (
    PlanningError, _index, _validate_window, _windows, _wire_candidate,
    extract_requirements, validate_live_window,
)
from test_source_selected_citations import Context, fixture, block, selected, live_candidate as candidate, internal_candidate, window_for


def native_pair(tmp_path, *, left_text="公司应提供完整的项目负责", right_text="人姓名作为明确输入。", source_format="pdf", origin="native"):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = source_format
    a = block(parsed, "B1", left_text, bbox=[40, 20, 300, 30], origin=origin)
    b = block(parsed, "B2", right_text, bbox=[40, 38, 220, 48], origin=origin)
    a["locator"], b["locator"] = "pdf/page[1]/line[0]", "pdf/page[1]/line[1]"
    return manifest, parsed, a, b


def with_label(window, a, b, label="项目负责人姓名"):
    raw = candidate({"window": window}, selected(a, b))
    raw["requirements"][0]["required_fact_labels"] = [label]
    return raw


def test_verified_native_wrap_grounds_label_without_changing_quote_or_source_whitespace(tmp_path):
    manifest, parsed, a, b = native_pair(tmp_path)
    window = window_for(parsed, manifest)
    assert len(window["native_pdf_line_edges"]) == 1
    assert "bbox" not in window["blocks"][0]
    result = validate_live_window(with_label(window, a, b), window)
    citation = result["requirements"][0]["citation"]
    assert citation["quote"] == a["text"] + "\n" + b["text"]
    grounding = citation["label_grounding"][0]
    assert grounding["basis"] == "native_pdf_line_join_v1"
    match = grounding["matches"][0]
    assert "".join(citation["quote"][start:end] for start, end in match["quote_spans"]) == "项目负责人姓名"
    assert match["edge_sha256s"] == [window["native_pdf_line_edges"][0]["evidence_sha256"]]
    assert _validate_window(_wire_candidate(result), window) == result
    assert "label_grounding" not in _wire_candidate(result)["requirements"][0]["citation"]


@pytest.mark.parametrize("case", ["docx", "ocr", "column", "paragraph_gap", "short_line", "line_index_gap", "source_space", "punctuation"])
def test_unproven_or_changed_source_seams_do_not_get_general_whitespace_normalization(tmp_path, case):
    manifest, parsed, a, b = native_pair(tmp_path)
    if case == "docx":
        parsed["documents"][0]["format"] = "docx"
    elif case == "ocr":
        a["origin"] = b["origin"] = "ocr"
    elif case == "column":
        b["bbox"] = [400, 38, 580, 48]
    elif case == "paragraph_gap":
        b["bbox"] = [40, 110, 220, 120]
    elif case == "short_line":
        a["bbox"] = [40, 20, 100, 30]
    elif case == "line_index_gap":
        b["locator"] = "pdf/page[1]/line[2]"
    elif case == "source_space":
        a["text"] = "公司应提供完整的项目负 责"
    else:
        a["text"] += "，"
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(with_label(window, a, b), window)


def test_unproven_cross_line_label_can_instead_keep_its_original_lf(tmp_path):
    manifest, parsed, a, b = native_pair(tmp_path, source_format="docx")
    window = window_for(parsed, manifest)
    raw = with_label(window, a, b, "项目负责\n人姓名")
    result = validate_live_window(raw, window)
    assert result["requirements"][0]["citation"]["label_grounding"][0]["basis"] == "exact_quote"


def test_parenthesized_annual_period_continuation_is_not_a_numbered_heading(tmp_path):
    manifest, parsed, a, b = native_pair(tmp_path,
        left_text="2025 年度）经审计的财务报表，包括资产负债表、现金",
        right_text="流量表、利润表。")
    window = window_for(parsed, manifest)
    result = validate_live_window(with_label(window, a, b, "现金流量表"), window)
    citation = result["requirements"][0]["citation"]
    assert citation["quote"] == a["text"] + "\n" + b["text"]
    assert citation["label_grounding"][0]["basis"] == "native_pdf_line_join_v1"
    assert _validate_window(_wire_candidate(result), window) == result


def test_numbered_lookup_instruction_is_prose_but_lookup_title_is_not(tmp_path):
    manifest, parsed, a, b = native_pair(tmp_path,
        left_text="5.2 查询信用信息系统（https://example.invalid）中公开的经营异常",
        right_text="名录信息并记录查询时间。")
    window = window_for(parsed, manifest)
    result = validate_live_window(with_label(window, a, b, "经营异常名录信息"), window)
    citation = result["requirements"][0]["citation"]
    assert citation["quote"] == a["text"] + "\n" + b["text"]
    assert citation["label_grounding"][0]["basis"] == "native_pdf_line_join_v1"
    a["text"] = "5.2 查询流程"
    b["text"] = "记录作为查询依据。"
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(with_label(window_for(parsed, manifest), a, b, "流程记录"), window_for(parsed, manifest))


@pytest.mark.parametrize("change", ["year_title", "column", "gap", "ocr", "omitted_character"])
def test_annual_period_fix_does_not_join_titles_or_unproved_or_rewritten_text(tmp_path, change):
    manifest, parsed, a, b = native_pair(tmp_path,
        left_text="2025 年度）经审计的财务报表，包括资产负债表、现金",
        right_text="流量表、利润表。")
    label = "现金流量表"
    if change == "year_title":
        a["text"] = "2025 年度财务报表和现金"
    elif change == "column":
        b["bbox"] = [400, 38, 580, 48]
    elif change == "gap":
        b["bbox"] = [40, 110, 220, 120]
    elif change == "ocr":
        a["origin"] = b["origin"] = "ocr"
    else:
        label = "现金流表"
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(with_label(window_for(parsed, manifest), a, b, label), window_for(parsed, manifest))


def test_geometry_evidence_is_frozen_in_context_identity_and_cannot_be_self_reported(tmp_path):
    manifest, parsed, a, b = native_pair(tmp_path)
    first = window_for(parsed, manifest)
    b["bbox"] = [40, 39, 220, 49]
    second = window_for(parsed, manifest)
    assert first["window_id"] == second["window_id"]
    assert first["context_identity"]["sha256"] != second["context_identity"]["sha256"]
    damaged = copy.deepcopy(second)
    damaged["native_pdf_line_edges"][0]["boxes"][1][1] += .2
    with pytest.raises(PlanningError, match="NATIVE_LINE_JOIN_EVIDENCE_INVALID"):
        validate_live_window(with_label(damaged, a, b), damaged)
    raw = with_label(second, a, b)
    raw["native_pdf_line_edges"] = second["native_pdf_line_edges"]
    with pytest.raises(PlanningError, match="EXTRACTION_SCHEMA_INVALID"):
        validate_live_window(raw, second)


def test_native_pdf_reader_real_lines_can_ground_a_wrapped_label(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    from company_bid.reading import read_documents
    manifest, _ = fixture(tmp_path)
    writer = PdfWriter()
    page = writer.add_blank_page(width=360, height=240)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 11 Tf 40 200 Td (Applicants must provide the full projectna) Tj ET\nBT /F1 11 Tf 40 182 Td (me for this proposal.) Tj ET\n")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "native.pdf"
    writer.write(path)
    source = manifest["sources"][0]
    source.update(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    ctx = Context()
    parsed = read_documents(ctx, [{**source, "role": "tender"}], {})
    source_lines = parsed["documents"][0]["units"][0]["blocks"]
    assert len(source_lines) == 2 and all(value["origin"] == "native" for value in source_lines)
    window = window_for(parsed, manifest)
    result = validate_live_window(with_label(window, *source_lines, label="projectname"), window)
    assert result["requirements"][0]["citation"]["label_grounding"][0]["basis"] == "native_pdf_line_join_v1"


def test_heading_without_adjacent_limit_body_is_not_an_extracted_constraint(tmp_path):
    manifest, parsed = fixture(tmp_path)
    title = block(parsed, "TITLE", "2.5 服务价款")
    body = block(parsed, "BODY", "最高限价为100万元，超过限价的响应无效。")
    window = window_for(parsed, manifest)
    first = candidate({"window": window}, selected(title))
    first["requirements"][0].update(category="commercial", response_mode="procedure")
    with pytest.raises(PlanningError, match="HEADING_ONLY_REQUIREMENT_MISSES_CONSTRAINT") as error:
        validate_live_window(first, window)
    assert error.value.diagnostic_context["candidate_index"] == 0 and error.value.diagnostic_context["adjacent_block_id"] == body["block_id"]
    first["requirements"][0]["citation"] = selected(title, body)
    assert "100万元" in validate_live_window(first, window)["requirements"][0]["citation"]["quote"]


@pytest.mark.parametrize("mode,title", [("table", "3 报价表"), ("fixed_text", "1 投标函"), ("attachment", "7 资格证明")])
def test_legitimate_prescribed_format_or_attachment_titles_are_not_heading_false_positives(tmp_path, mode, title):
    manifest, parsed = fixture(tmp_path)
    heading = block(parsed, "TITLE", title)
    block(parsed, "BODY", "证明页位置如下。" if mode == "attachment" else "应按以下指定格式保留全部约束。")
    window = window_for(parsed, manifest)
    raw = candidate({"window": window}, selected(heading))
    raw["requirements"][0].update(category="fixed_table" if mode == "table" else "qualification", response_mode=mode, format_status="prescribed")
    assert validate_live_window(raw, window)["requirements"][0]["citation"]["quote"] == title


def test_attachment_title_cannot_omit_adjacent_grade_or_validity_constraints(tmp_path):
    manifest, parsed = fixture(tmp_path)
    heading = block(parsed, "TITLE", "3.2 资格证明")
    body = block(parsed, "BODY", "必须提供有效期内的一级资质证明。")
    window = window_for(parsed, manifest)
    raw = candidate({"window": window}, selected(heading))
    raw["requirements"][0].update(category="qualification", response_mode="attachment")
    with pytest.raises(PlanningError, match="HEADING_ONLY_REQUIREMENT_MISSES_CONSTRAINT"):
        validate_live_window(raw, window)
    raw["requirements"][0]["citation"] = selected(heading, body)
    assert "有效期" in validate_live_window(raw, window)["requirements"][0]["citation"]["quote"]


@pytest.mark.parametrize("category,allowed", [("commercial", True), ("procedure", True), ("technical", False), ("qualification", False)])
def test_commercial_constraints_can_be_procedure_but_technical_and_qualification_cannot_hide(category, allowed, tmp_path):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "服务周期应为12个月，不能超过既定周期。")
    window = window_for(parsed, manifest)
    raw = candidate({"window": window}, selected(source))
    raw["requirements"][0].update(category=category, response_mode="procedure")
    if allowed:
        assert validate_live_window(raw, window)["requirements"][0]["category"] == category
    else:
        with pytest.raises(PlanningError, match="PROCEDURE_CLASSIFICATION_INVALID"):
            validate_live_window(raw, window)


@pytest.mark.parametrize("failure", ["label", "heading", "title"])
def test_second_candidate_has_private_field_and_source_diagnostics_without_source_in_message(tmp_path, failure):
    manifest, parsed = fixture(tmp_path)
    a = block(parsed, "A", "应保留第一条完整要求。")
    b = block(parsed, "B", "2.5 服务价款" if failure == "heading" else "应保留第二条完整要求。")
    block(parsed, "C", "最高限价不得超过100万元。")
    window = window_for(parsed, manifest)
    raw = candidate({"window": window}, selected(a))
    raw["requirements"].append(copy.deepcopy(raw["requirements"][0]))
    raw["requirements"][1]["citation"] = selected(b)
    if failure == "label":
        raw["requirements"][1]["required_fact_labels"] = ["不存在的字段CANARY"]
    elif failure == "title":
        raw["requirements"][1]["format_title"] = "不存在的标题CANARY"
    else:
        raw["requirements"][1].update(category="commercial", response_mode="procedure")
    with pytest.raises(PlanningError) as failure_info:
        validate_live_window(raw, window)
    context = failure_info.value.diagnostic_context
    assert context["candidate_type"] == "requirement" and context["candidate_index"] == 1 and context["block_id"] == "B"
    assert context["field"] == {"label": "required_fact_labels", "heading": "citation", "title": "format_title"}[failure]
    assert str(failure_info.value) == failure_info.value.code and "CANARY" not in str(failure_info.value)


@pytest.mark.parametrize("quote", ["仅结论", "应保留全部条件，不得省略。", None])
def test_live_cannot_secretly_echo_quote_even_when_internal_recorded_contract_accepts_it(tmp_path, quote):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "应保留全部条件，不得省略。")
    window = window_for(parsed, manifest)
    raw = candidate({"window": window}, selected(source, quote=quote))
    with pytest.raises(PlanningError, match="LIVE_CITATION_QUOTE_FORBIDDEN"):
        validate_live_window(raw, window)
    if quote is None or quote == source["text"]:
        assert _validate_window(internal_candidate(raw, window), window)["requirements"][0]["citation"]["quote"] == source["text"]
    with pytest.raises(PlanningError, match="LIVE_CITATION_QUOTE_FORBIDDEN"):
        extract_requirements(Context(), parsed, manifest, lambda *args: raw, {})
