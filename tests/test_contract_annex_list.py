"""Cover numbered annex omissions without moving a succeeding owner's scope."""

import copy
import json

import pytest

from company_bid import contract_annex_list as annex
from company_bid import extraction as ext
from company_bid.observability import redact_payload
from company_bid.storage import digest, json_bytes
from test_source_selected_citations import block, fixture, live_candidate


def scenario(tmp_path, change=None):
    pages = [["附件9：安全履约条款", "5.对乙方的总体要求", "5.3 项目实施中乙方应遵守管理规定。", "5.3.2 工程管理禁令",
              "5.3.2.3 环境管理", "1）严禁向水体倾倒废物；", "2）严禁使用失效设备；", "71"],
             ["3）严禁擅自改换环保设施；", "4）严禁掩盖污染事故。", "5.3.3 现场规则", "1）动火作业必须经审批；",
              "2）作业交接应书面登记。", "5.3.4 应急管理", "保持疏散路线通畅。", "72"]]
    if change == "wrong_number":
        pages[1][0] = "5）严禁擅自改换环保设施；"
    elif change == "submission_instead_of_performance":
        pages[1][0] = "3）严禁投标时提供虚假资料；"
    elif change == "unclosed_obligation":
        pages[1][1] = "4）严禁掩盖污染事故"
    elif change == "hidden_second_item":
        pages[1][0] += " 4）严禁私自更换通风系统；"
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for page, entries in enumerate(pages, 1):
        for position, text in enumerate(entries):
            member = block(parsed, f"p{page}-{position}", text, page=page, origin="native",
                           bbox=[24.0, 20.0 + 18 * position, 250.0, 34.0 + 18 * position])
            member["locator"] = f"pdf/page[{page}]/line[{position}]"
    blocks, _, documents = ext._index(parsed, manifest)
    bare = ext.extraction_window_config({"window_max_blocks": 12})
    windows, _ = ext._windows(documents, blocks, bare, manifest=manifest)
    first, second = windows
    assert first["blocks"][-1]["block_id"] == "p2-3" and second["blocks"][0]["block_id"] == "p2-4"
    raw = live_candidate({"window": first}, {"block_ids": ["p1-5"]})
    raw["requirements"].append({**raw["requirements"][0], "citation": {"block_ids": ["p1-6"]}})
    raw["unresolved_block_ids"] = ["p2-0", "p2-1", "p2-4"]
    scope = {"source_id": "T", "source_sha256": documents[0]["sha256"], "window_id": first["window_id"],
             "raw_candidate_sha256": digest(raw), "list_heading_block_id": "p1-4",
             "reviewed_pages": [{"physical_page": number, "rendered_rgb_sha256": "a" * 64} for number in (1, 2)]}
    proof_pages = [{"footer": {"block_id": f"p{page}-7"}} for page in (1, 2)]
    return bare, windows, raw, scope, blocks, proof_pages


def bind(data):
    bare, original, raw, scope, blocks, pages = data
    config = ext.extraction_window_config({**bare, annex.SOURCE_CONFIG_KEY: scope})
    first, second = copy.deepcopy(original)
    proof = annex._list_proof(None, list(blocks.values()), pages, scope, blocks, [first, second], first)
    proof.update(rule=annex.RULE, source_id=scope["source_id"], source_sha256=scope["source_sha256"], scope=scope,
                 window_id=first["window_id"], following_owner_window_id=second["window_id"], reviewed_pages=pages)
    first[annex.WINDOW_CONTEXT_KEY] = {**proof, "proof_sha256": digest(proof)}
    return config, first, second


def test_numbered_tail_rebuilds_requirements_without_altering_earlier_or_following_owner(tmp_path):
    data = scenario(tmp_path)
    bare, windows, raw, _, blocks, _ = data
    config, first, second = bind(data)
    assert ext.extraction_window_payload(first, config) == ext.extraction_window_payload(windows[0], bare)
    assert windows[1] == second
    with pytest.raises(ext.PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        ext.validate_live_window(raw, windows[0])
    checkpoint = ext.run_extraction_window(None, first, config, lambda *_: raw)
    saved = json.loads(json_bytes(redact_payload(checkpoint)))
    assert saved == checkpoint
    assert checkpoint["raw_model_candidate"] == raw and len(checkpoint["validated"]["requirements"]) == 2
    assert checkpoint["live_confirmation"]["unresolved_block_ids"] == raw["unresolved_block_ids"]
    assert ext._validate_normalized_window_result(saved, first) == saved["validated"]
    resolution = saved[annex.RESOLUTION_KEY]
    assert [item["number"] for item in resolution["items"]] == [3, 4]
    assert resolution["deferred_following_items"] == [{"number": 2, "owner_window_id": second["window_id"],
        "block_ids": ["p2-4"], "semantic_status": "await_owner_window_review"}]
    requirements, _ = ext._merge_results([saved])
    assert [row["text"] for row in requirements] == [blocks[key]["text"] for key in ("p1-5", "p1-6", "p2-0", "p2-1")]
    assert all(req["mandatory"] and not req["attachment_keywords"] for req in requirements)
    saved[annex.RESOLUTION_KEY]["items"][0]["requirement"]["citation"]["quote"] = "changed"
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_ANNEX_LIST_MISMATCH"):
        ext._validate_normalized_window_result(saved, first)


@pytest.mark.parametrize("change", ["wrong_number", "submission_instead_of_performance", "unclosed_obligation", "hidden_second_item"])
def test_ambiguous_or_submission_source_does_not_create_performance_requirements(tmp_path, change):
    with pytest.raises(ValueError, match=annex.SOURCE_ERROR):
        bind(scenario(tmp_path, change))


@pytest.mark.parametrize("change", ["new_missing_target", "lost_sibling", "unknown_adjacent", "different_raw_digest"])
def test_existing_candidate_coverage_and_unresolved_digest_are_required(tmp_path, change):
    data = scenario(tmp_path)
    raw, scope = data[2:4]
    if change == "new_missing_target":
        raw["unresolved_block_ids"].insert(0, "p1-4")
    elif change == "lost_sibling":
        raw["requirements"].pop()
    elif change == "unknown_adjacent":
        raw["unresolved_block_ids"].append("p2-5")
    else:
        raw["requirements"][0]["confidence"] = "low"
    if change != "different_raw_digest":
        scope["raw_candidate_sha256"] = digest(raw)
    _, first, _ = bind(data)
    with pytest.raises(ext.PlanningError, match=annex.SOURCE_ERROR):
        ext.validate_live_window(raw, first)
