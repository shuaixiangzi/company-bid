"""An authenticated attachment label may move one wrap within its original PDF cell."""
import copy

import pytest

import company_bid.extraction as extraction
from company_bid.extraction import (
    RESPONSE_CELL_REFLOW_CONFIG_KEY, RESPONSE_CELL_REFLOW_RULE, PlanningError, _hash, _index,
    _response_cell_reflow_proof, _validate_normalized_window_result, _verified_response_cell_reflow_resolution,
    _windows, extraction_window_config, extraction_window_payload, run_extraction_window, validate_live_window,
)
from test_source_selected_citations import Context, block, fixture as source_fixture


_LABEL = "条件甲\n乙，须提供证明；退休时附合同"
_SOURCE = "前文：条件\n甲乙，须提供证明；退休时附合同。"


def _window(tmp_path, rows=None, *, origin="native"):
    manifest, parsed = source_fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    heading = block(parsed, "H", "固定表格", origin="native")
    source = block(parsed, "TBL", "", kind="table", rows=rows or [["备注", _SOURCE]],
                   column_widths=[100, 400], merges=[], origin=origin)
    source["locator"] = "pdf/page[1]/table[0]"
    parsed["documents"][0]["units"][0]["coverage"]["origin"] = "native"
    blocks, _, docs = _index(parsed, manifest)
    window = _windows(docs, blocks, {})[0][0]
    raw = {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
           "unresolved_block_ids": [], "format_status": "specified", "requirements": [
               {"citation": {"block_ids": [source["block_id"]]}, "category": "qualification", "response_mode": "attachment",
                "format_status": "not_prescribed", "attachment_keywords": [_LABEL], "mandatory": True}],
           "format_chapters": [{"title": heading["text"], "level": 1, "citation": {"block_ids": [heading["block_id"], source["block_id"]]},
                                "content_block_ids": [heading["block_id"], source["block_id"]], "requirement_indices": [0]}]}
    scope = {"source_id": window["source_id"], "source_sha256": window["source_sha256"], "window_id": window["window_id"],
             "block_id": source["block_id"], "locator": source["locator"], "page": 1, "row": 0, "column": 1,
             "candidate_index": 0, "label_index": 0, "raw_candidate_sha256": _hash(raw)}
    return docs, blocks, window, raw, scope


def test_replayed_wrap_keeps_original_candidate_and_all_characters(tmp_path, monkeypatch):
    docs, blocks, window, raw, scope = _window(tmp_path)
    monkeypatch.setattr(extraction, "reviewed_authority_tail", lambda *_: None)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED") as failed:
        validate_live_window(raw, window)
    frozen = copy.deepcopy(raw)
    proof = _response_cell_reflow_proof(scope, raw, window)
    scope["source_proof_sha256"] = proof["proof_sha256"]
    config = extraction_window_config({RESPONSE_CELL_REFLOW_CONFIG_KEY: scope})
    bound = _windows(docs, blocks, config)[0][0]
    assert extraction_window_payload(bound, config) == extraction_window_payload(window, extraction_window_config({}))
    receipt = _verified_response_cell_reflow_resolution(raw, bound)
    assert receipt["original_error"] == {"error_code": failed.value.code, "diagnostic_context": failed.value.diagnostic_context}
    assert receipt["proof"] == proof
    ctx = Context()
    result = run_extraction_window(ctx, bound, config, lambda _c, _t, _id, _p, verify: verify(raw))
    assert result["raw_model_candidate"] == raw == frozen
    assert result[RESPONSE_CELL_REFLOW_CONFIG_KEY]["proof"] == proof
    assert ctx.captures["n2-response-cell-reflow-source-resolution"] == receipt
    checked = _validate_normalized_window_result(result, bound)
    requirement = checked["requirements"][0]
    assert requirement["attachment_keywords"] == [_LABEL]
    citation = requirement["citation"]
    assert citation["quote"] == f"备注\t{_SOURCE}"
    grounding, = citation["label_grounding"]
    match, = grounding["matches"]
    assert grounding["basis"] == RESPONSE_CELL_REFLOW_RULE
    assert "".join(citation["quote"][start:end] for start, end in match["quote_spans"]) == _LABEL.replace("\n", "")
    assert match["cell_proof"]["source_text"] == _SOURCE[3:-1]
    result[RESPONSE_CELL_REFLOW_CONFIG_KEY]["proof"]["row"] += 1
    with pytest.raises(PlanningError, match="EXTRACTION_RESPONSE_CELL_REFLOW_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(result, bound)


@pytest.mark.parametrize("case,code", [
    ("duplicate_wrapped", "SOURCE_PROOF_MISMATCH"), ("duplicate_contiguous", "SOURCE_PROOF_MISMATCH"),
    ("cross_cell", "SOURCE_PROOF_MISMATCH"), ("cross_row", "SOURCE_PROOF_MISMATCH"),
    ("unknown_ownership", "SOURCE_PROOF_MISMATCH"), ("punctuation_changed", "SOURCE_PROOF_MISMATCH"),
    ("qualification_dropped", "SOURCE_PROOF_MISMATCH"), ("blank_line", "SOURCE_PROOF_MISMATCH"),
    ("changed_cell", "SOURCE_PROOF_MISMATCH"), ("ocr", "SOURCE_SCOPE_MISMATCH"),
    ("changed_candidate", "RAW_RESPONSE_MISMATCH"), ("changed_source", "WINDOW_SCOPE_MISMATCH"),
])
def test_wrap_proof_rejects_ambiguous_or_mismatched_sources(tmp_path, case, code):
    options = {
        "duplicate_wrapped": [[_SOURCE, _SOURCE]],
        "duplicate_contiguous": [[_SOURCE.replace("\n", ""), _SOURCE]],
        "cross_cell": [["条件", "甲乙，须提供证明；退休时附合同"]],
        "cross_row": [["备注", "条件"], ["", "甲乙，须提供证明；退休时附合同"]],
        "unknown_ownership": [["备注", _SOURCE.replace("\n", "")]],
        "punctuation_changed": [["备注", _SOURCE.replace("；", "、")]],
        "qualification_dropped": [["备注", _SOURCE.replace("退休时附合同", "")]],
        "blank_line": [["备注", _SOURCE.replace("\n", "\n\n")]],
    }
    _, _, window, raw, scope = _window(tmp_path, options.get(case), origin="ocr" if case == "ocr" else "native")
    if case == "changed_cell":
        scope["column"] = 0
    if case == "changed_candidate":
        raw["requirements"][0]["attachment_keywords"][0] += "。"
    if case == "changed_source":
        scope["source_sha256"] = "b" * 64
    with pytest.raises(PlanningError, match=f"RESPONSE_CELL_REFLOW_{code}"):
        _response_cell_reflow_proof(scope, raw, window)
