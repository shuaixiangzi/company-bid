"""Synthetic three-column scoring recovery with a pinned historical candidate."""
import copy

import pytest

from company_bid.extraction import (
    SCORING_ROW_SOURCE_CONFIG_KEY as KEY,
    PlanningError,
    _hash,
    _index,
    _pdf_footer_skips,
    _validate_normalized_window_result,
    _windows,
    extract_requirements,
    extraction_window_config,
    extraction_window_payload,
    validate_live_window,
)
from company_bid.planning import plan_bid
from company_bid.scoring_row_sources import build_scoring_row_source_proof
from test_source_selected_citations import Context, block, fixture


def scoring_fixture(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"

    def add(key, locator, text, page, bbox, **extra):
        item = block(parsed, key, text, page=page, bbox=bbox, origin="native", **extra)
        item["locator"] = f"pdf/page[{page}]/{locator}"
        return item

    def footer(page):
        return add(f"F{page}", "line[8]", str(page), page, [293, 793, 300, 804])

    def table(key, locator, page, bbox, rows, *, total=False):
        merges = [{"row": len(rows) - 1, "column": 0, "row_span": 1, "col_span": 2}] if total else []
        return add(key, locator, "", page, bbox, rows=rows, merges=merges,
                   header_rows=[], column_widths=[65, 330, 30], kind="table")

    header = ["评价项目", "评价内容", "分值"]
    add("A", "line[0]", "虚构项目前言。", 1, [94, 78, 210, 89])
    footer(1)
    add("CH", "line[0]", "商务评价条款", 2, [250, 105, 345, 119])
    table("C", "table[1]", 2, [85, 124, 510, 366], [header, ["组织经验（4分）", "提交案例凭据。", "4"],
        ["协调经验（2分）", "提交案例凭据及签名记录。", "2"], ["合计", "", "6"]], total=True)
    add("TH", "line[1]", "技术评价条款", 2, [250, 373, 345, 384])
    table("L", "table[2]", 2, [85, 390, 510, 760], [header, ["方法（1分）", "编写设备配置。", "1"],
        ["管理（1分）", "编写分工计划。", "1"], ["进度（1分）", "编写工序进度。", "1"],
        ["QHSE管理", "安排部署，", "2"]])
    footer(2)
    table("R", "table[0]", 3, [85, 72, 510, 472], [["方案\n（2分）", "符合要求并列出质量办法。", ""],
        ["应急（1分）", "编写调度预案。", "1"], ["团队（3分）", "提供职\n称凭据。", "3"],
        ["合计", "", "9"]], total=True)
    footer(3)
    add("NEXT", "line[0]", "11. 其他需要说明", 4, [90, 83, 238, 94])
    add("N", "line[1]", "须按时提交。", 4, [94, 106, 208, 117])

    old_config = extraction_window_config({"window_max_blocks": 9})
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, old_config, manifest=manifest)
    owner = next(window for window in windows if "R" in {item["block_id"] for item in window["blocks"]})
    requirements = []
    for index, labels in enumerate([
        ["案例凭据"], ["案例凭据", "签名记录"], ["设备配置"], ["分工计划"], ["工序进度"],
        ["质量办法"], ["调度预案"], ["职称凭据"],
    ]):
        writing = 2 <= index < 7
        requirements.append({"citation": {"block_ids": ["C"] if index < 2 else ["L", "R"]},
            "category": "commercial" if index < 2 else "technical" if writing else "qualification",
            "response_mode": "writing" if writing else "attachment", "mandatory": False,
            "format_status": "not_prescribed", "reference_keywords" if writing else "attachment_keywords": labels})
    raw = {"window_id": owner["window_id"], "requirements": requirements, "format_chapters": [],
           "format_status": "none_verified", "processing_declaration": "processed_not_semantically_accepted",
           "unresolved_block_ids": ["N"]}
    scope = {"source_id": "T", "source_sha256": "a" * 64, "window_id": owner["window_id"],
             "table_block_ids": ["C", "L", "R"], "raw_candidate_sha256": _hash(raw)}
    native = [blocks[key] for key in ("CH", "C", "TH", "L", "F2", "R")]
    proof = build_scoring_row_source_proof(scope, native, _pdf_footer_skips(blocks)["F2"])
    assert proof
    scope["source_proof_sha256"] = proof["proof_sha256"]
    return manifest, parsed, windows, old_config, raw, scope


def test_n2_exact_scoring_rows_replayed_and_rebuilt_from_n1_in_n3(tmp_path):
    manifest, parsed, old_windows, old_config, raw, scope = scoring_fixture(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    config = extraction_window_config({**old_config, KEY: scope})
    windows, _ = _windows(documents, blocks, config, manifest=manifest)
    assert [window["window_id"] for window in windows if KEY in window] == [scope["window_id"]]
    assert [extraction_window_payload(window, config) for window in windows] == [
        extraction_window_payload(window, old_config) for window in old_windows]
    with pytest.raises(PlanningError, match="TABLE_CONTINUATION_NOT_PROVEN"):
        validate_live_window(raw, old_windows[0])

    def recorded(ctx, kind, unit, payload, validate):
        if unit == raw["window_id"]:
            return validate(raw)
        return validate({**raw, "window_id": unit, "requirements": [], "unresolved_block_ids": []})

    ctx, original = Context(), copy.deepcopy(raw)
    extracted = extract_requirements(ctx, parsed, manifest, recorded, config)
    result = extracted["window_results"][0]
    assert raw == original == result["raw_model_candidate"]
    assert result["processing"]["unresolved_block_ids"] == ["N"]
    assert [item["block_id"] for item in result["processing"]["deferred_context"]] == ["N"]
    handoff, = extracted["deferred_context_closure"]["items"]
    assert handoff["block_id"] == "N" and handoff["owner_window_id"] == windows[1]["window_id"]
    assert not result[KEY]["model_intent_inferred"] and result[KEY] == ctx.captures["n2-scoring-row-source-resolution"]
    assert _validate_normalized_window_result(result, windows[0]) == result["validated"]
    orig_requirements = result["raw_model_candidate"]["requirements"]
    narrowed = result["validated"]["requirements"]
    assert len(extracted["requirements"]) == len(narrowed) == len(orig_requirements) == 8
    assert orig_requirements[7]["category"] == "qualification"
    assert narrowed[7]["category"] == "technical" and not narrowed[7]["mandatory"]
    assert narrowed[7]["citation"]["label_grounding"][0]["basis"] == "native_pdf_cell_lf_join_v1"
    assert [len(req["citation"]["block_ids"]) for req in narrowed] == [1, 1, 1, 1, 1, 2, 1, 1]
    assert [(p["block_id"], p["row"]) for p in narrowed[5]["citation"]["table_row_parts"]] == [("L", 4), ("R", 0)]
    assert narrowed[5]["citation"]["skipped_block_ids"] == ["F2"]
    assert "应急" not in narrowed[5]["citation"]["quote"] and "合计" not in narrowed[7]["citation"]["quote"]
    planned = plan_bid(Context(), extracted, parsed, manifest, None, {"map_table_fields": False})
    managed = next(req for req in planned["requirements"] if req["text"] == narrowed[7]["citation"]["quote"])
    assert managed["category"] == "technical" and not managed["mandatory"]

    forged = copy.deepcopy(result)
    forged[KEY]["proof"]["rows"][5]["parts"][1]["row"] = 1
    with pytest.raises(PlanningError, match="EXTRACTION_SCORING_ROW_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(forged, windows[0])


@pytest.mark.parametrize("damage", ["columns", "tail", "total", "footer", "reviewed_native_digest"])
def test_scoring_source_proof_rejects_changed_native_structure_before_call(tmp_path, damage):
    manifest, parsed, _, old_config, _, scope = scoring_fixture(tmp_path)
    pages = parsed["documents"][0]["units"]
    left, footer = pages[1]["blocks"][-2:]
    right = pages[2]["blocks"][0]
    if damage == "columns":
        right["column_widths"] = [69, 326, 30]
    elif damage == "tail":
        right["rows"][0][0] = "另一项目（2分）"
    elif damage == "total":
        right["rows"][-1][-1] = "10"
    elif damage == "footer":
        footer["text"] = "另有条件。"
    else:
        left["rows"][1][1] += "未审补写。"
    with pytest.raises(PlanningError, match="SCORING_ROW_SOURCE_CONTINUATION_UNPROVEN"):
        extract_requirements(Context(), parsed, manifest, lambda *args: pytest.fail("unexpected call"), {**old_config, KEY: scope})


def test_scoring_rows_preserve_unresolved_claims_and_reject_mutated_raw_or_cross_row_label(tmp_path):
    manifest, parsed, _, old_config, raw, scope = scoring_fixture(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {**old_config, KEY: scope}, manifest=manifest)
    with pytest.raises(PlanningError, match="SCORING_ROW_SOURCE_RAW_RESPONSE_MISMATCH"):
        validate_live_window({**raw, "unresolved_block_ids": []}, windows[0])
    wrong = copy.deepcopy(raw)
    wrong["requirements"][5]["reference_keywords"] = ["调度预案"]
    reviewed_scope = {**scope, "raw_candidate_sha256": _hash(wrong)}
    reviewed, _ = _windows(documents, blocks, {**old_config, KEY: reviewed_scope}, manifest=manifest)
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(wrong, reviewed[0])
