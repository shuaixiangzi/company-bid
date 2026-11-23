import copy

import pytest

from company_bid import contract_annex_predecessor as seam
from company_bid import extraction as ext
from company_bid.storage import digest
from test_source_selected_citations import block, fixture


def response(window, selections, unresolved=()):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": "none_verified", "format_chapters": [],
            "requirements": [{"citation": {"block_ids": ids}, "category": "procedure", "response_mode": "procedure",
                              "mandatory": True, "format_status": "not_prescribed", "format_title": None, "confidence": "high",
                              "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []} for ids in selections]}


def execute(window, config, raw, previous=None):
    return ext.run_extraction_window(None, window, config,
        lambda _ctx, _task, _unit, _payload, validate: validate(copy.deepcopy(raw)), predecessor_result=previous)


@pytest.fixture
def scenario(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    source = [
        ("annex", 1, "附件2：施工安全条款", True),
        ("section", 2, "8.现场管理", True),
        ("before", 2, "8.2 乙方应制订安全计划。", True),
        ("start", 2, "8.3 乙方应检查每台设备，", True),
        ("tail-1", 2, "确认设备出厂记录，并将", False),
        ("tail-2", 2, "核查记录抄送甲方。", False),
        ("after", 2, "8.4 乙方应保护设备标识，", True),
        ("after-tail", 2, "避免造成无法识别的污损。", False),
        ("later", 2, "8.5 乙方应记录检查时间。", True),
    ]
    for index, (identity, page, line, indent) in enumerate(source):
        row = block(parsed, identity, line, page=page, origin="native",
                    bbox=[113.0 if indent else 89.0, 10 + 20 * index, 400.0, 28 + 20 * index])
        row["locator"] = f"pdf/page[{page}]/line[{index if page == 1 else index - 1}]"
    blocks, _, docs = ext._index(parsed, manifest)
    config = ext.extraction_window_config({"window_max_blocks": 4, "adjacent_context_blocks": 4})
    windows, _ = ext._windows(docs, blocks, config, manifest=manifest)
    previous, window, following = windows
    assert [[row["block_id"] for row in value["blocks"]] for value in windows] == [
        ["annex", "section", "before", "start"], ["tail-1", "tail-2", "after", "after-tail"], ["later"]]
    prior_raw = response(previous, [["start", "tail-1", "tail-2"]], unresolved=["tail-1"])
    past = execute(previous, config, prior_raw)
    raw = response(window, [["after", "after-tail"]], unresolved=["tail-1", "tail-2", "later"])
    scope = {"source_id": "T", "source_sha256": docs[0]["sha256"], "window_id": window["window_id"],
             "raw_candidate_sha256": digest(raw), "clause_start_block_id": "start", "section_heading_block_id": "section",
             "annex_heading_block_id": "annex", "annex_page": {"physical_page": 1, "rendered_rgb_sha256": "1" * 64},
             "reviewed_pages": [{"physical_page": 2, "rendered_rgb_sha256": "2" * 64}], "deferred_block_ids": ["later"],
             "predecessor": {"window_id": previous["window_id"], "raw_candidate_sha256": digest(prior_raw), "candidate_index": 0,
                             "requirement_sha256": digest(past["validated"]["requirements"][0])}}
    return config, windows, blocks, scope, raw, past


def bind(scenario):
    config, (previous, window, following), blocks, scope, _, _ = scenario
    clean = ext.extraction_window_config({**config, seam.SOURCE_CONFIG_KEY: scope})
    lines = list(blocks.values())
    pages = [{"footer": {"block_id": "not_a_real_footer"}}]
    proof = seam._source_proof(lines[1:], pages, lines[:1], scope["annex_page"], scope, blocks, previous, window, following)
    proof.update(rule=seam.RULE, source_id=scope["source_id"], source_sha256=scope["source_sha256"], window_id=window["window_id"],
                 scope=scope, reviewed_pages=pages)
    return clean, {**window, seam.WINDOW_CONTEXT_KEY: {**proof, "proof_sha256": digest(proof)}}


def test_existing_exact_clause_only_and_deferred_owner_closure(scenario):
    config, (previous, window, following), _, _, raw, past = scenario
    reviewed, target = bind(scenario)
    assert ext.extraction_window_payload(window, config) == ext.extraction_window_payload(target, reviewed)
    with pytest.raises(ext.PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        ext.validate_live_window(raw, window, predecessor_result=past)
    result = execute(target, reviewed, raw, past)
    assert result["raw_model_candidate"] == raw
    assert result["processing"]["unresolved_block_ids"] == ["tail-1", "tail-2", "later"]
    assert result[seam.RESOLUTION_KEY]["resolved_block_ids"] == ["tail-1", "tail-2"]
    assert result[seam.RESOLUTION_KEY]["deferred_to_following_window"][0]["member"]["block_id"] == "later"
    assert len(result["validated"]["requirements"]) == 1
    assert past["validated"]["requirements"][0]["citation"]["quote"].endswith("核查记录抄送甲方。")
    assert ext._validate_normalized_window_result(result, target, predecessor_result=lambda: past) == result["validated"]
    successor = execute(following, config, response(following, [["later"]]))
    with pytest.raises(ext.PlanningError, match="DEFERRED_CONTEXT_OWNER_INCOMPLETE"):
        ext._close_deferred_context([previous, target, following], [past, result])
    closure = ext._close_deferred_context([previous, target, following], [past, result, successor])
    assert [(row["block_id"], row["owner_window_id"], row["resolution"]) for row in closure["items"]] == [
        ("tail-1", target["window_id"], "owner_target_complete_annex_clause_already_cited_by_predecessor"),
        ("later", following["window_id"], "owner_target_structurally_processed")]


@pytest.mark.parametrize("change", ["absent", "wrong_window", "failed", "truncated_validated", "partial_raw", "duplicate"])
def test_changed_predecessor_cannot_close_tail(scenario, change):
    _, _, _, _, raw, past = scenario
    _, target = bind(scenario)
    tampered = copy.deepcopy(past)
    if change == "absent":
        tampered = None
    elif change == "wrong_window":
        tampered["window_id"] = "unrelated"
    elif change == "failed":
        tampered["status"] = "failed"
    elif change == "truncated_validated":
        tampered["validated"]["requirements"][0]["citation"]["quote"] = "删去抄送甲方。"
    elif change == "partial_raw":
        tampered["raw_model_candidate"]["requirements"][0]["citation"]["block_ids"].pop()
    else:
        tampered["validated"]["requirements"].append(tampered["validated"]["requirements"][0])
    with pytest.raises(ext.PlanningError, match=seam.PREDECESSOR_ERROR):
        ext.validate_live_window(raw, target, predecessor_result=tampered)


@pytest.mark.parametrize("change", ["different_section", "second_start", "interrupted_paragraph", "different_owner"])
def test_ambiguous_native_structure_does_not_bind(scenario, change):
    _, (_, _, owner), blocks, _, _, _ = scenario
    if change == "different_section":
        blocks["section"]["text"] = "6.现场管理"
    elif change == "second_start":
        blocks["later"]["text"] = "8.3 乙方应按时检查。"
    elif change == "interrupted_paragraph":
        blocks["tail-1"]["bbox"][1] = blocks["start"]["bbox"][1]
    else:
        owner["blocks"] = []
    with pytest.raises(ValueError, match=seam.SOURCE_ERROR):
        bind(scenario)


@pytest.mark.parametrize("change", ["distinct_target_unresolved", "deferred_omitted", "duplicate_clause"])
def test_other_targets_and_future_declarations_cannot_use_predecessor_proof(scenario, change):
    _, (previous, window, following), _, scope, raw, past = scenario
    if change == "distinct_target_unresolved":
        raw["unresolved_block_ids"].insert(2, "after")
    elif change == "deferred_omitted":
        raw["unresolved_block_ids"].pop()
    else:
        raw["requirements"].extend(response(window, [["start", "tail-1", "tail-2"]])["requirements"])
    scope["raw_candidate_sha256"] = digest(raw)
    _, target = bind(scenario)
    with pytest.raises(ext.PlanningError, match=seam.SOURCE_ERROR):
        ext.validate_live_window(raw, target, predecessor_result=past)
