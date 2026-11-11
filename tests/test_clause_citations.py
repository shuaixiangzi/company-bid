"""A proved source clause reaches writing without neighbouring obligations."""
from copy import deepcopy

import pytest

from company_bid.extraction import (
    PlanningError, _citation, _index, _windows, _wire_candidate,
    extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from company_bid.source_clauses import build_clause_catalog
from test_source_selected_citations import Context, block, fixture, no_call


def source(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    lines = ["一、工作范围", "1、巡检内容：",
             "1）开展外观检查，保留现场记录，不得自行维修。2）检测绝缘",
             "性能并提交记录。3）完成专项培训。",
             "2、服务期限：", "期限为30日。"]
    for i, text in enumerate(lines):
        block(parsed, f"B{i}", text, origin="native", locator=f"pdf/page[1]/line[{i}]",
              bbox=[70., 100. + i * 16, 510., 112. + i * 16])
    indexed, _, docs = _index(parsed, manifest)
    window = _windows(docs, indexed, {})[0][0]
    clauses = build_clause_catalog(window)
    assert len(clauses) == 3
    return manifest, parsed, indexed, window, clauses


def wire(window, clauses):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "format_chapters": [], "format_status": "none_verified",
            "requirements": [{"citation": {k: c[k] for k in ("clause_id", "source_id", "block_id", "locator", "block_ids")},
                              "category": "technical", "response_mode": "writing", "format_status": "not_prescribed"}
                             for c in clauses]}


def test_exact_clauses_reach_n3_titles_and_writing_objectives(tmp_path):
    manifest, parsed, indexed, window, clauses = source(tmp_path)
    ctx = Context()
    def call(ctx, task_type, unit_id, payload, validate):
        return validate(wire(payload["window"], build_clause_catalog(payload["window"])))
    result = extract_requirements(ctx, parsed, manifest, call, {})
    assert len(result["requirements"]) == 3
    assert [r["text"] for r in result["requirements"]] == [c["quote"] for c in clauses]
    assert "不得自行维修" in result["requirements"][0]["text"]
    assert "2）" not in result["requirements"][0]["text"]
    assert "1）" not in result["requirements"][1]["text"] and "3）" not in result["requirements"][1]["text"]
    assert result["requirements"][2]["text"].startswith("3）")
    prepared = plan_bid(Context(), result, parsed, manifest, no_call, {})
    tasks = [task for section in prepared["sections"] for task in section["writing_tasks"]]
    assert len(tasks) == 3 and [task["must_cover"] for task in tasks] == [[c["quote"]] for c in clauses]
    assert all(task["title"].startswith(f"{i}）") for i, task in enumerate(tasks, 1))
    assert result["coverage"]["units"][0]["block_ids"] == list(indexed)  # No N1 denominator editing.
    for requirement, clause in zip(result["requirements"], clauses):
        citation = requirement["citations"][0]
        assert citation["basis"] == "frozen_clause_span_v1"
        assert citation["quote_sha256"] == clause["quote_sha256"]
        assert citation["clause_proof_sha256"] == clause["proof_sha256"]
    assert "n2-source-clause-boundaries" in ctx.captures


@pytest.mark.parametrize("mutation", ["unknown_id", "wrong_blocks", "free_offset", "quote", "wrong_mode", "wrong_category"])
def test_model_cannot_use_catalog_to_invent_or_arbitrarily_shorten_text(tmp_path, mutation):
    _, _, _, window, clauses = source(tmp_path)
    raw = wire(window, clauses)
    r = raw["requirements"][0]
    if mutation == "unknown_id": r["citation"]["clause_id"] = "INVENTED"
    elif mutation == "wrong_blocks": r["citation"]["block_ids"].append("B3")
    elif mutation == "free_offset": r["citation"]["start"] = 8
    elif mutation == "quote": r["citation"]["quote"] = "可以自行维修。"
    elif mutation == "wrong_mode": r["response_mode"] = "attachment"
    else: r["category"] = "qualification"
    with pytest.raises(PlanningError): validate_live_window(raw, window)


def test_forged_catalog_is_rebuilt_from_frozen_source(tmp_path):
    _, _, _, window, clauses = source(tmp_path)
    raw = wire(window, clauses)
    changed = deepcopy(window)
    changed["source_clause_catalog"][0]["quote"] = "未经授权也可以操作。"
    with pytest.raises(PlanningError, match="WINDOW_CLAUSE_CATALOG_MISMATCH"):
        validate_live_window(raw, changed)


@pytest.mark.parametrize("parent_blocks", [["B2"], ["B3"], ["B2", "B3"]])
def test_mixed_physical_line_must_select_a_proved_clause(tmp_path, parent_blocks):
    _, _, indexed, window, clauses = source(tmp_path)
    raw = wire(window, clauses[:1])
    raw["requirements"][0]["citation"] = {
        **{key: indexed[parent_blocks[0]][key] for key in ("source_id", "block_id", "locator")},
        "block_ids": parent_blocks,
    }
    with pytest.raises(PlanningError, match="CLAUSE_SELECTION_REQUIRED") as caught:
        validate_live_window(raw, window)
    assert caught.value.diagnostic_context["candidate_index"] == 0
    assert caught.value.diagnostic_context["field"] == "citation.clause_id"
    assert len(caught.value.diagnostic_context["clause_ids"]) >= 2


def test_clause_round_trip_rechecks_exact_quote_and_rejects_unproved_caller(tmp_path):
    _, _, indexed, window, clauses = source(tmp_path)
    normalized = validate_live_window(wire(window, clauses), window)
    internal = _wire_candidate(normalized)
    citation = internal["requirements"][0]["citation"]
    with pytest.raises(PlanningError, match="CLAUSE_ID_NOT_IN_FROZEN_CATALOG"):
        _citation(citation, indexed)
    citation["quote"] = "1）开展外观检查。"
    with pytest.raises(PlanningError, match="CLAUSE_QUOTE_MISMATCH"):
        _citation(citation, indexed, clause_catalog=clauses)


def test_labels_may_join_only_proved_lines_inside_selected_clause(tmp_path):
    _, _, _, window, clauses = source(tmp_path)
    raw = wire(window, clauses)
    raw["requirements"][1]["reference_keywords"] = ["绝缘性能"]
    normalized = validate_live_window(raw, window)
    grounding = normalized["requirements"][1]["citation"]["label_grounding"]
    assert grounding[0]["basis"] == "native_pdf_line_join_v1"
    assert grounding[0]["matches"][0]["edge_sha256s"]
    raw["requirements"][1]["reference_keywords"] = ["不得自行维修"]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw, window)


def test_n3_rejects_changed_clause_even_if_merged_output_is_updated(tmp_path):
    manifest, parsed, _, _, _ = source(tmp_path)
    def call(ctx, task_type, unit_id, payload, validate):
        return validate(wire(payload["window"], build_clause_catalog(payload["window"])))
    result = extract_requirements(Context(), parsed, manifest, call, {})
    result["requirements"][0]["text"] = "1）仅进行外观检查。"
    result["requirements"][0]["citations"][0]["quote"] = result["requirements"][0]["text"]
    with pytest.raises(PlanningError):
        plan_bid(Context(), result, parsed, manifest, no_call, {})
