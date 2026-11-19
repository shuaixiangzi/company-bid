"""Native contract references close only against the independently frozen annex."""
from copy import deepcopy

import pytest

from company_bid.contract_annex_sources import (
    CONTRACT_ANNEX_SOURCE_CONFIG_KEY, build_contract_annex_proof, contract_annex_pages,
)
from company_bid.extraction import (
    PlanningError, _index, _pdf_footer_skips, _windows, extract_requirements, extraction_window_payload,
    validate_live_window,
)
from company_bid.planning import plan_bid
from company_bid.storage import digest
from test_source_selected_citations import Context, block, fixture, no_call


def _reply(window, *sources, unresolved=()):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": "none_verified", "format_chapters": [],
            "requirements": [{"citation": {"block_ids": ids}, "category": "technical", "response_mode": "writing",
                              "format_status": "not_prescribed", "mandatory": True} for ids in sources]}


@pytest.fixture
def annex_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    first = "对虚构区域的传感器（共18 个）、采样器（共3 台）进行运维"
    second = "并开展预防性维检修工作。"
    pages = [
        ["虚构采购简介", "2.2 采购范围：" + first.replace("18 个", "18个"), second, "采购范围说明结束。"],
        ["第一条 技术服务范围", "1.1 技术服务内容：详见附件1技术服务内容。", "1.2 技术服务要求：", "按时检查传感器。"],
        ["虚构合同正文。", "甲方应按期验收。", "乙方须提交报告。", "合同正文结束。"],
        ["20.5 本合同附件具有同等效力。", "附件1：技术服务内容", "附件2：项目组成员", "合同附件列表结束。"],
        ["（本页无正文，为签章页）", "甲方签章：", "乙方签章：", "签署日期："],
        ["附件1：技术服务内容", first, second],
        ["附件2：项目组成员"],
    ]
    document = parsed["documents"][0]
    document["format"] = "pdf"
    for page, lines in enumerate(pages, 1):
        for index, line in enumerate([*lines, str(page)]):
            y = 770 if index == len(lines) else 90 + index * 24
            record = block(parsed, f"P{page}-L{index}", line, page=page, bbox=[70, y, 370, y + 11], origin="native")
            record["locator"] = f"pdf/page[{page}]/line[{index}]"
        document["units"][-1]["coverage"] |= dict(
            origin="native", unread_objects=[], ignored_objects=[], unmapped_glyphs=[], transformations=[],
            error_code=None, table_count=0, image_count=0)
    base = {"window_max_blocks": 5, "window_max_chars": 14000}
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, base, manifest=manifest)
    owners = {record["block_id"]: window for window in windows for record in window["blocks"]}
    target, owner = owners["P2-L1"], owners["P6-L0"]
    raw = _reply(target, ["P2-L3"], unresolved=["P2-L1"])
    scope = {"source_id": "T", "source_sha256": document["sha256"], "reference_page": 2, "annex_page": 6,
             "reference_block_id": "P2-L1", "window_id": target["window_id"], "owner_window_id": owner["window_id"],
             "raw_candidate_sha256": digest(raw)}
    reviewed_pages = contract_annex_pages(scope, document, blocks)
    footer = _pdf_footer_skips(blocks)["P6-L3"]
    scope["source_proof_sha256"] = build_contract_annex_proof(scope, reviewed_pages, footer)["proof_sha256"]
    return manifest, parsed, base, scope, raw, target, owners


@pytest.mark.parametrize("owner_selection", [True, False])
@pytest.mark.parametrize("split_quantity", [False, True])
def test_annex_whole_window_and_prepared_bid_keep_primary_quote_and_secondary_source(
        annex_case, owner_selection, split_quantity):
    manifest, parsed, base, scope, raw, original_window, owners = annex_case
    if split_quantity:
        first = parsed["documents"][0]["units"][0]["blocks"][1]
        first["text"] = first["text"].replace("18个", "1 8个")
    configured = {**base, CONTRACT_ANNEX_SOURCE_CONFIG_KEY: scope}
    purchase_owner = owners["P1-L1"]["window_id"]
    calls = []

    def recorded(_ctx, _task, unit_id, payload, validate):
        calls.append(unit_id)
        assert CONTRACT_ANNEX_SOURCE_CONFIG_KEY not in payload["window"]
        if unit_id == purchase_owner:
            payload_raw = _reply(payload["window"], ["P1-L1", "P1-L2"])
        elif unit_id == scope["window_id"]:
            payload_raw = deepcopy(raw)
        elif unit_id == scope["owner_window_id"] and owner_selection:
            payload_raw = _reply(payload["window"], ["P6-L0", "P6-L1", "P6-L2"])
        else:
            payload_raw = _reply(payload["window"])
        return validate(payload_raw)

    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, original_window)
    ctx = Context()
    extracted = extract_requirements(ctx, parsed, manifest, recorded, configured)
    result = next(row for row in extracted["window_results"] if row["window_id"] == scope["window_id"])
    assert result["raw_model_candidate"] == raw
    assert result["processing"]["unresolved_block_ids"] == ["P2-L1"]
    assert result[CONTRACT_ANNEX_SOURCE_CONFIG_KEY]["proof"]["owner_window_id"] == scope["owner_window_id"]
    assert len(result["validated"]["requirements"]) == len(raw["requirements"])
    merged, = [item for item in extracted["requirements"] if item["text"].startswith("2.2 采购范围：")]
    assert [item["page"] for item in merged["citations"]] == [1]
    if split_quantity:
        application_requirement = result[CONTRACT_ANNEX_SOURCE_CONFIG_KEY]["requirement"]
        source_citation = application_requirement["citation"]
        assert "equivalent_annex_citations" not in merged
        annex, = [item for item in extracted["requirements"] if source_citation in item["citations"]]
        assert annex["text"] == source_citation["quote"]
        return
    assert all(item["page"] == 6 for item in merged["equivalent_annex_citations"])
    assert all(c["quote"] == merged["text"] for c in merged["citations"])
    assert (scope["owner_window_id"] in merged["window_ids"]) is owner_selection
    assert len(extracted["requirements"]) == 1 + len(raw["requirements"])
    assert calls.count(scope["owner_window_id"]) == 1
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    item, = [item for item in prepared["requirements"] if item["requirement_id"] == merged["requirement_id"]]
    assert item["text"] == merged["text"]
    refs = [r["locator"] for r in prepared["sources"] if r["source_id"] in item["source_refs"]]
    assert any(ref.startswith("pdf/page[1]/line[1];") for ref in refs)
    assert any(ref.startswith("pdf/page[6]/line[1];") for ref in refs)
    merged["equivalent_annex_citations"][0] = {**merged["equivalent_annex_citations"][0], "quote": "替换附件的数量和范围。"}
    with pytest.raises(PlanningError, match="EXTRACTION_MERGE_MISMATCH"):
        plan_bid(Context(), extracted, parsed, manifest, no_call, {})


@pytest.mark.parametrize("tamper", ["title_only", "changed_quantity", "foreign_source", "different_owner", "unread", "wrong_raw"])
def test_annex_evidence_cannot_close_a_modified_or_different_reference(annex_case, tamper):
    manifest, parsed, base, scope, raw, _, _ = annex_case
    if tamper == "wrong_raw":
        raw["unresolved_block_ids"] = []
    elif tamper == "different_owner":
        scope["owner_window_id"] = scope["window_id"]
    else:
        annex = parsed["documents"][0]["units"][5]
        if tamper == "title_only":
            del annex["blocks"][1:3]
        elif tamper == "changed_quantity":
            annex["blocks"][1]["text"] = annex["blocks"][1]["text"].replace("18", "81")
        elif tamper == "foreign_source":
            annex["blocks"][1]["origin"] = "source_review"
        else:
            annex["coverage"]["unread_objects"] = [{"locator": "unread"}]
    blocks, _, documents = _index(parsed, manifest)
    configured = {**base, CONTRACT_ANNEX_SOURCE_CONFIG_KEY: scope}
    with pytest.raises(PlanningError):
        windows, _ = _windows(documents, blocks, configured, manifest=manifest)
        window = next(window for window in windows if window["window_id"] == scope["window_id"])
        assert extraction_window_payload(window, configured)["window"]["window_id"] == scope["window_id"]
        validate_live_window(raw, window)
