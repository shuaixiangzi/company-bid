"""Attachment-label evidence is confined to an original, selected PDF cell."""
import copy
import hashlib

import pytest

from company_bid.extraction import (
    PlanningError, _validate_normalized_window_result, _validate_window, _wire_candidate,
    extract_requirements, validate_live_window,
)
from company_bid.source_clauses import SourceClauseError
from company_bid.storage import digest
from test_source_selected_citations import Context, block, fixture, live_candidate, selected, window_for
from test_table_row_citations import selector


LABEL = "防汛物资调配记录"
SOURCE_TEXT = "防\n汛物资调配记录"


def table_source(tmp_path, rows=None, *, origin="native", source_format="pdf", widths=(80, 160, 240)):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = source_format
    table = block(parsed, "TABLE", "", kind="table", origin=origin,
                  rows=rows if rows is not None else [["人员", "身份", f"提供{SOURCE_TEXT}。"]],
                  merges=[], column_widths=list(widths), header_rows=[])
    table["locator"] = "pdf/page[1]/table[0]"
    return manifest, parsed, table


def raw_with_label(window, citation, field="attachment_keywords", label=LABEL):
    raw = live_candidate({"window": window}, citation)
    raw["requirements"][0][field] = [label]
    return raw


def checked_proof(result, label=LABEL):
    item, = result["requirements"]
    citation = item["citation"]
    grounding, = citation["label_grounding"]
    match, = grounding["matches"]
    proof = match["cell_proof"]
    assert grounding["basis"] == proof["rule"] == "native_pdf_cell_lf_join_v1"
    assert item["attachment_keywords"] == [label] and grounding["label"] == label
    assert grounding["field"] == "attachment_keywords" and grounding["label_index"] == 0
    assert "".join(citation["quote"][start:end] for start, end in match["quote_spans"]) == label
    assert citation["quote"][proof["quote_start"]:proof["quote_end"]] == proof["source_text"]
    assert proof["removed_lf_offsets"] == [i for i in range(proof["quote_start"], proof["quote_end"])
                                           if citation["quote"][i] == "\n"]
    assert digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"]
    return citation, proof


@pytest.mark.parametrize("original", [SOURCE_TEXT, "防\n汛物资调配\n记录"])
def test_attachment_join_preserves_source_candidate_and_quote(tmp_path, original):
    cell = f"提供{original}（保留）"
    manifest, parsed, table = table_source(tmp_path, [["人员", "身份", cell]])
    window = window_for(parsed, manifest)
    raw = raw_with_label(window, {"block_ids": [table["block_id"]]})
    initial = copy.deepcopy((raw, window))
    result = validate_live_window(raw, window)
    citation, proof = checked_proof(result)
    assert (proof["source_id"], proof["source_sha256"], proof["unit_id"]) == ("T", "a" * 64, "U1")
    assert (proof["page"], proof["block_id"], proof["locator"], proof["row"], proof["column"]) == (1, "TABLE", table["locator"], 0, 2)
    assert citation["quote"] == "人员\t身份\t" + cell
    assert proof["cell_sha256"] == hashlib.sha256(cell.encode()).hexdigest()
    assert proof["source_text"] == cell[proof["cell_start"]:proof["cell_end"]] == original
    wire = _wire_candidate(result)
    assert "label_grounding" not in wire["requirements"][0]["citation"]
    assert _validate_window(wire, window) == result
    assert (raw, window) == initial


def test_cell_join_keeps_punctuation_and_spaces_in_the_original_label(tmp_path):
    original = "防\n汛物资调配记录（含物资清单、A 版）"
    label = "防汛物资调配记录（含物资清单、A 版）"
    manifest, parsed, table = table_source(tmp_path, [["人员", "身份", f"提供{original}。"]])
    window = window_for(parsed, manifest)
    result = validate_live_window(raw_with_label(window, selected(table), label=label), window)
    assert checked_proof(result, label)[1]["source_text"] == original


@pytest.mark.parametrize("rows", [
    [["人员", "身份", "提供防"], ["汛物资调配记录", "", ""]],
    [["人员", "提供防", "汛物资调配记录"]],
    [["人员", "身份", "提供防\n\n汛物资调配记录"]],
    [["人员", "身份", "提供防\r\n汛物资调配记录"]],
    [["人员", "身份", "提供防 \n汛物资调配记录"]],
    [["人员", "身份", "提供防\n 汛物资调配记录"]],
    [["人员", "身份", "提供防。\n汛物资调配记录"]],
    [["人员", "身份", f"提供{SOURCE_TEXT}，或写成{SOURCE_TEXT}"]],
    [["人员", f"提供{SOURCE_TEXT}", "防汛物资调\n配记录"]],
])
def test_cell_join_refuses_non_lf_edits_and_ambiguous_or_structural_seams(tmp_path, rows):
    manifest, parsed, table = table_source(tmp_path, rows)
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw_with_label(window, selected(table)), window)


@pytest.mark.parametrize("change", ["docx", "ocr", "unknown_origin", "unread", "without_grid_widths", "missing_hash", "wrong_source", "image_region"])
def test_cell_join_requires_frozen_native_pdf_source(tmp_path, change):
    manifest, parsed, table = table_source(tmp_path, origin={"ocr": "ocr", "unknown_origin": None}.get(change, "native"),
                                          source_format="docx" if change == "docx" else "pdf",
                                          widths=[] if change == "without_grid_widths" else (80, 160, 240))
    if change == "image_region":
        table["locator"] = "pdf/page[1]/image[0]/ocr/block[0]"
    window = window_for(parsed, manifest)
    if change == "unread":
        window["blocks"][0]["read_disposition"] = {"status": "unread"}
    if change == "missing_hash":
        window.pop("source_sha256")
    if change == "wrong_source":
        window["source_id"] = "FOREIGN"
    kind, code = {
        "missing_hash": (SourceClauseError, "SOURCE_CLAUSE_WINDOW_IDENTITY_INVALID"),
        "wrong_source": (SourceClauseError, "SOURCE_CLAUSE_BLOCK_IDENTITY_INVALID"),
    }.get(change, (PlanningError, "EXTRACTION_LABEL_NOT_GROUNDED"))
    with pytest.raises(kind, match=code):
        validate_live_window(raw_with_label(window, selected(table)), window)


@pytest.mark.parametrize("field", ["required_fact_labels", "reference_keywords"])
def test_cell_join_is_attachment_only(tmp_path, field):
    manifest, parsed, table = table_source(tmp_path)
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED") as error:
        validate_live_window(raw_with_label(window, selected(table), field), window)
    assert error.value.diagnostic_context["field"] == field


def test_cell_proof_stays_inside_selected_row_and_maps_its_quote_offsets(tmp_path):
    rows = [["条款号", "条款名称", "编列内容"],
            ["4.1", "身份证明", f"须提供{SOURCE_TEXT}。"],
            ["4.2", "人员名单", f"保留重复的{SOURCE_TEXT}。"],
            ["4.3", "资格承诺", "按本条提交声明。"]]
    manifest, parsed, _ = table_source(tmp_path, rows)
    window = window_for(parsed, manifest)
    result = validate_live_window(raw_with_label(window, selector(window, "4.1")), window)
    citation, proof = checked_proof(result)
    assert citation["quote"] == "\t".join(rows[1])
    assert (proof["row"], proof["column"], proof["cell_start"]) == (1, 2, 3)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw_with_label(window, selector(window, "4.3")), window)


def test_legacy_partial_quote_requires_full_cell_match_inside_its_actual_range(tmp_path):
    manifest, parsed, table = table_source(tmp_path)
    window = window_for(parsed, manifest)
    candidate = {"reviewed_block_ids": ["TABLE"], "format_chapters": [], "format_status": "none_verified",
                 "requirements": [{"citation": selected(table, quote=SOURCE_TEXT), "category": "commercial",
                                   "response_mode": "factual_response", "format_status": "not_prescribed",
                                   "attachment_keywords": [LABEL]}]}
    citation, proof = checked_proof(_validate_window(candidate, window))
    assert citation["quote"] == SOURCE_TEXT and proof["quote_start"] == 0 and proof["cell_start"] == 2
    candidate["requirements"][0]["citation"]["quote"] = SOURCE_TEXT[1:]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        _validate_window(candidate, window)


def test_exact_source_label_still_uses_original_proof_path(tmp_path):
    manifest, parsed, table = table_source(tmp_path)
    window = window_for(parsed, manifest)
    result = validate_live_window(raw_with_label(window, selected(table), label=SOURCE_TEXT), window)
    proof, = result["requirements"][0]["citation"]["label_grounding"]
    assert proof["basis"] == "exact_quote" and proof["label"] == SOURCE_TEXT


@pytest.mark.parametrize("boundary", ["column", "row", "block"])
def test_literal_structural_separator_does_not_ground_a_table_attachment_label(tmp_path, boundary):
    manifest, parsed, table = table_source(tmp_path, [["人员", "身份", "须提供防"]])
    span = [table]
    label = SOURCE_TEXT
    if boundary == "row":
        table["rows"].append([LABEL[1:], "", ""])
    elif boundary == "column":
        table["rows"][0] = ["人员", "须提供防", LABEL[1:]]
        label = "防\t" + LABEL[1:]
    else:
        span.append(block(parsed, "NEXT", LABEL[1:], origin="native"))
    window = window_for(parsed, manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw_with_label(window, selected(*span), label=label), window)


def test_paragraph_exact_match_in_mixed_quote_is_unaffected(tmp_path):
    manifest, parsed, table = table_source(tmp_path, [["人员", "身份", "另附资料"]])
    paragraph = block(parsed, "NEXT", f"以下提供{SOURCE_TEXT}。", origin="native")
    window = window_for(parsed, manifest)
    result = validate_live_window(raw_with_label(window, selected(table, paragraph), label=SOURCE_TEXT), window)
    proof, = result["requirements"][0]["citation"]["label_grounding"]
    assert proof["basis"] == "exact_quote" and proof["label"] == SOURCE_TEXT


def test_cell_evidence_is_rebuilt_from_the_raw_model_response(tmp_path):
    manifest, parsed, table = table_source(tmp_path)
    window = window_for(parsed, manifest)

    def call(ctx, task_type, unit_id, payload, validate):
        return validate(raw_with_label(payload["window"], selected(table)))

    extracted = extract_requirements(Context(), parsed, manifest, call, {})
    result, = extracted["window_results"]
    assert checked_proof(_validate_normalized_window_result(result, window))[1]["source_text"] == SOURCE_TEXT
    evidence = result["validated"]["requirements"][0]["citation"]["label_grounding"][0]["matches"][0]["cell_proof"]
    evidence["column"] -= 1
    with pytest.raises(PlanningError, match="EXTRACTION_VALIDATION_MISMATCH"):
        _validate_normalized_window_result(result, window)
