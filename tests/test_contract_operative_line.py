"""Strict heading guard with one independently proved numbered imperative."""

import copy
import json

import pytest

from company_bid import contract_operative_line as operative
from company_bid import extraction as ext
from company_bid.observability import redact_payload
from company_bid.storage import digest, json_bytes
from test_source_selected_citations import block, fixture, live_candidate


def scenario(tmp_path, change=None):
    original = ["附件3：安全履约条款", "4.1 乙方应落实安全防护，并", "保留作业记录。",
                "4.2 切实遵守现场安全规程", "4.3 乙方应每日评估作业风险，", "落实风险防控措施。",
                "4.4 工作准备事项", "4.5 乙方必须核验作业许可。", "71"]
    if change == "noun_only":
        original[3] = "4.2 现场监督管理要求"
    elif change == "number_gap":
        original[4] = "4.6 乙方应每日评估作业风险，"
    elif change == "preceding_open":
        original[2] = "保留作业记录，"
    elif change == "following_open":
        original[5] = "落实风险防控措施，"
    elif change == "unselected_continuation":
        original.insert(4, "同时落实危险作业预控措施。")

    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for number, text in enumerate(original):
        value = block(parsed, f"b{number}", text, origin="native",
                      bbox=[90.0, 20 + 20 * number, 250.0, 34 + 20 * number])
        value["locator"] = f"pdf/page[1]/line[{number}]"
    blocks, _, documents = ext._index(parsed, manifest)
    plain = ext.extraction_window_config({})
    bare, = ext._windows(documents, blocks, plain, manifest=manifest)[0]
    raw = live_candidate({"window": bare}, {"block_ids": ["b3"]})
    raw["requirements"][0]["reference_keywords"] = ["现场安全规程"]
    scope = {"source_id": "T", "source_sha256": documents[0]["sha256"], "window_id": bare["window_id"],
             "raw_candidate_sha256": digest(raw), "candidate_index": 0, "operative_block_id": "b3",
             "reviewed_pages": [{"physical_page": 1, "rendered_rgb_sha256": "a" * 64}]}
    pages = [{"footer": {"block_id": f"b{len(original) - 1}"}}]
    return plain, bare, raw, scope, blocks, pages


def bind(data):
    plain, bare, _, scope, blocks, pages = data
    config = ext.extraction_window_config({**plain, operative.SOURCE_CONFIG_KEY: scope})
    window = copy.deepcopy(bare)
    proof = operative._operative_position(list(blocks.values()), pages, scope, blocks, window)
    proof.update(rule=operative.RULE, source_id=scope["source_id"], source_sha256=scope["source_sha256"], scope=scope,
                 window_id=window["window_id"], reviewed_pages=pages)
    window[operative.WINDOW_CONTEXT_KEY] = {**proof, "proof_sha256": digest(proof)}
    return config, window


def test_complete_single_line_keeps_its_citation_and_the_next_numbered_sibling(tmp_path):
    data = scenario(tmp_path)
    plain, bare, raw, _, _, _ = data
    config, window = bind(data)
    assert ext.extraction_window_payload(bare, plain) == ext.extraction_window_payload(window, config)
    with pytest.raises(ext.PlanningError, match="HEADING_ONLY_REQUIREMENT_MISSES_CONSTRAINT"):
        ext.validate_live_window(raw, bare)
    result = ext.run_extraction_window(None, window, config, lambda *_: raw)
    saved = json.loads(json_bytes(redact_payload(result)))
    assert saved == result and saved["raw_model_candidate"] == raw
    assert ext._validate_normalized_window_result(saved, window) == saved["validated"]
    proof = saved[operative.RESOLUTION_KEY]["proof"]
    assert (proof["preceding_number"], proof["operative_number"], proof["following_number"]) == ("4.1", "4.2", "4.3")
    requirement, = saved["validated"]["requirements"]
    assert requirement["citation"]["quote"] == "4.2 切实遵守现场安全规程"
    assert requirement["reference_keywords"] == ["现场安全规程"]
    del saved[operative.RESOLUTION_KEY]
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_OPERATIVE_LINE_MISMATCH"):
        ext._validate_normalized_window_result(saved, window)


@pytest.mark.parametrize("change", ["noun_only", "number_gap", "preceding_open", "following_open", "unselected_continuation"])
def test_ambiguous_number_or_incomplete_clause_does_not_bypass_guard(tmp_path, change):
    with pytest.raises(ValueError, match=operative.SOURCE_ERROR):
        bind(scenario(tmp_path, change))


def test_another_numbered_heading_keeps_its_guard_in_same_window(tmp_path):
    data = scenario(tmp_path)
    raw, scope = data[2:4]
    raw["requirements"].append({**raw["requirements"][0], "citation": {"block_ids": ["b6"]}, "reference_keywords": []})
    scope["raw_candidate_sha256"] = digest(raw)
    _, window = bind(data)
    with pytest.raises(ext.PlanningError, match="HEADING_ONLY_REQUIREMENT_MISSES_CONSTRAINT") as error:
        ext.validate_live_window(raw, window)
    assert error.value.diagnostic_context["candidate_index"] == 1


def test_changed_saved_response_cannot_borrow_authenticated_imperative(tmp_path):
    data = scenario(tmp_path)
    _, window = bind(data)
    raw = data[2]
    raw["requirements"][0]["confidence"] = "low"
    with pytest.raises(ext.PlanningError, match=operative.SOURCE_ERROR):
        ext.validate_live_window(raw, window)
