"""Source-bound performance record routing keeps the original N2 candidate."""

import copy
import json

import pytest

from company_bid import contract_performance_record as record
from company_bid import extraction as ext
from company_bid.observability import redact_payload
from company_bid.storage import digest, json_bytes
from test_source_selected_citations import block, fixture, live_candidate


def scenario(tmp_path, change=None):
    pages = [["附件4：安全协议"],
             ["7.2 甲方的义务", "7.2.1 甲方应进行设施验收。", "8.乙方的权利和义务", "8.1 乙方的权利",
              "8.1.1 乙方有权要求提供资料。", "70"],
             ["8.2 乙方的义务", "8.2.1 设备要求", "（5）乙方应制定应急预案。",
              "（6）乙方受甲方委托提供的监测设备必须符合标准，", "并有检测质量记录。乙方为完成合同而自用设备应符合标准，",
              "相关费用由乙方承担。", "（7）乙方应配置合格作业人员。", "71"]]
    if change == "bid_submission":
        pages[2][5] = "检测质量记录应作为响应文件递交，相关费用由乙方承担。"
    elif change == "different_actor":
        pages[1][0] = "7.2 乙方的义务"
    elif change == "unread_item_tail":
        pages[2][5] = "相关费用由乙方承担，"
    elif change == "partial_role_scope":
        pages[2][0] = "8.2 缔约安排"
    elif change == "missing_cost":
        pages[2][5] = "费用另行商议。"

    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for page, texts in enumerate(pages, 1):
        for line, text in enumerate(texts):
            value = block(parsed, f"p{page}-{line}", text, page=page, origin="native",
                          bbox=[80.0, 60.0 + 18 * line, 500.0, 72.0 + 18 * line])
            value["locator"] = f"pdf/page[{page}]/line[{line}]"
    blocks, _, documents = ext._index(parsed, manifest)
    plain = ext.extraction_window_config({})
    original, = ext._windows(documents, blocks, plain, manifest=manifest)[0]
    raw = live_candidate({"window": original}, {"block_ids": ["p3-3", "p3-4", "p3-5"]})
    raw["requirements"][0].update(response_mode="attachment", attachment_keywords=["检测质量记录"])
    scope = {"source_id": "T", "source_sha256": "a" * 64, "window_id": original["window_id"], "raw_candidate_sha256": digest(raw),
             "candidate_index": 0, "clause_start_block_id": "p3-3", "expected_record_label": "检测质量记录",
             "owner_duties_heading_block_id": "p2-0", "contractor_rights_heading_block_id": "p2-3",
             "reviewed_pages": [{"physical_page": page, "rendered_rgb_sha256": "a" * 64} for page in (2, 3)]}
    reviewed_pages = [{"footer": {"block_id": footer}} for footer in ("p2-5", "p3-7")]
    return plain, original, blocks, raw, scope, reviewed_pages


def bind(data):
    plain, original, blocks, _, scope, pages = data
    config = ext.extraction_window_config({**plain, record.SOURCE_CONFIG_KEY: scope})
    window = copy.deepcopy(original)
    proof = record._reviewed_positions([value for value in blocks.values() if value["page"] in {2, 3}], pages, scope, blocks, window)
    proof.update(rule=record.RULE, source_id=window["source_id"], source_sha256=window["source_sha256"],
                 window_id=window["window_id"], scope=scope, reviewed_pages=pages)
    window[record.WINDOW_CONTEXT_KEY] = {**proof, "proof_sha256": digest(proof)}
    return config, window


def test_performance_record_stays_in_exact_quote_but_not_as_tender_attachment(tmp_path):
    data = scenario(tmp_path)
    plain, original, _, raw, _, _ = data
    config, window = bind(data)
    assert ext.extraction_window_payload(window, config) == ext.extraction_window_payload(original, plain)
    result = ext.run_extraction_window(None, window, config, lambda *_: raw)
    saved = json.loads(json_bytes(redact_payload(result)))
    assert saved == result and saved["raw_model_candidate"] == raw
    assert ext._validate_normalized_window_result(saved, window) == saved["validated"]
    assert result["validated"] == ext.validate_live_window(raw, original)
    original_requirement, = saved["validated"]["requirements"]
    assert original_requirement["attachment_keywords"] == ["检测质量记录"]
    requirements, _ = ext._merge_results([saved])
    merged, = requirements
    assert merged["response_mode"] == "procedure" and not merged["attachment_keywords"]
    assert merged["citations"] == [original_requirement["citation"]]
    assert "检测质量记录" in merged["text"] and "相关费用由乙方承担" in merged["text"]
    assert merged["contract_evidence_scope"]["capability_status"] == "not_checked"
    proof = saved[record.RESOLUTION_KEY]["proof"]
    duties, rights = proof["source_contexts"]
    assert [(duties["role"], duties["owned_items"][0]["number"]), (rights["role"], rights["owned_items"][0]["number"])] == [
        ("employer_contract_duty", "7.2.1"), ("contractor_contract_right", "8.1.1")]
    saved[record.RESOLUTION_KEY]["requirement"]["attachment_keywords"] = ["changed"]
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_PERFORMANCE_RECORD_MISMATCH"):
        ext._validate_normalized_window_result(saved, window)


@pytest.mark.parametrize("change", ["bid_submission", "different_actor", "unread_item_tail", "partial_role_scope", "missing_cost"])
def test_requires_exact_performance_conditions_and_distinct_source_actors(tmp_path, change):
    with pytest.raises(ValueError, match=record.SOURCE_ERROR):
        bind(scenario(tmp_path, change))


def test_changed_response_cannot_borrow_performance_route(tmp_path):
    data = scenario(tmp_path)
    _, window = bind(data)
    raw = data[3]
    raw["requirements"][0]["confidence"] = "low"
    with pytest.raises(ext.PlanningError, match=record.SOURCE_ERROR):
        ext.validate_live_window(raw, window)
