"""The slash-marked inventory proof must leave every unrelated source intact."""
import copy

import pytest

from company_bid.contract_optional_fields import RESOLUTION_KEY, WINDOW_CONTEXT_KEY
from company_bid.extraction import (
    PlanningError, _index, _validate_normalized_window_result, _windows,
    build_contract_clause_indexes, extraction_window_payload, run_extraction_window, validate_live_window,
)
from company_bid.storage import digest
from test_source_selected_citations import block, fixture


def scenario(tmp_path, change=None, *, first_article=18):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"

    def line(identity, text, *, page=1, left=110, top=None, origin="native"):
        unit = next((unit for unit in document["units"] if unit["page"] == page), None)
        ordinal = len(unit["blocks"]) if unit else 0
        top = 40 + 16 * ordinal if top is None else top
        result = block(parsed, identity, text, page=page, origin=origin, bbox=[left, top, left + 95, top + 12])
        result["locator"] = f"pdf/page[{page}]/line[{ordinal}]"
        document["units"][-1]["coverage"]["origin"] = "native"
        return result

    line("contract", "第四部分 合同文本")
    for n in range(1, first_article):
        line(f"H{n}", f"第{n}条 合同事项")
        line(f"N{n}", f"{n}.1 履约事项由双方按合同处理。")
    line("H18", f"第{first_article}条 术语", page=2)
    line("P18a", "【双方确认：本合同与相关附件中涉及的有关词语及技术术语，", page=2)
    line("P18b", "其含义及解释如下：／" if change != "missing_slash" else "其含义及解释如下：", page=2)
    for n in range(1, 7):
        text = f"{n}．【　】；" if n < 5 else "5．【　】。】" if n == 5 else "6.其他：【】"
        line(f"A{n}", text, page=2)
    line("H19", f"第{first_article + 1}条 技术文件", page=2)
    line("P19", "【和履行本合同相关的以下技术文件，属于本合同组成部分：/", page=2)
    for n in range(1, 7):
        text = f"{n}．【 】；" if n < 5 else "5．【 】。】" if n == 5 else "6.其他：【】"
        if n == 3 and change == "substantive_entry":
            text = "3．【必须随报价附技术协议】；"
        line(f"B{n}", text, page=2, left=130 if n == 3 and change == "different_column" else 110,
             origin="ocr" if n == 3 and change == "ocr" else "native")
    if change == "extra_line":
        line("ADD", "供应商另须随报价提交技术协议。", page=2)
    line("H20", f"第{first_article + 2}条 合同生效及其他事项", page=2)
    line("Q1", f"{first_article + 2}.1 依法签署并加盖双方印章后，本合同生效。", page=2)
    line("END", "（以下无正文）", page=2)
    line("SIGN", "（本页无正文，为签章页）", page=3)
    line("COMPANY", "乙方：【 】", page=3)
    if change == "unread_region":
        document["units"][1]["coverage"]["unread_objects"] = ["unknown_object"]
    return manifest, parsed


def windows(tmp_path, change=None, *, first_article=18):
    manifest, parsed = scenario(tmp_path, change, first_article=first_article)
    blocks, _, documents = _index(parsed, manifest)
    config = {"window_max_blocks": 86}
    plain, _ = _windows(documents, blocks, config, manifest=manifest)
    indexed, _ = _windows(documents, blocks, config, manifest=manifest,
                          contract_clause_indexes=build_contract_clause_indexes(documents, blocks))
    old, = plain
    current, = indexed
    return config, old, current


def raw_for(window):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": ["P18b", *(f"A{n}" for n in range(1, 7)),
                                     "P19", *(f"B{n}" for n in range(1, 7))],
            "requirements": [{"citation": {"block_ids": ["Q1"]}, "category": "commercial",
                              "response_mode": "procedure", "format_status": "not_prescribed"}],
            "format_chapters": [], "format_status": "none_verified"}


@pytest.mark.parametrize("first_article", [18, 12])
def test_window_and_n3_preserve_original_inventory_and_rebuild_disposition(tmp_path, first_article):
    config, legacy, window = windows(tmp_path, first_article=first_article)
    assert WINDOW_CONTEXT_KEY in window
    assert extraction_window_payload(window, config) == extraction_window_payload(legacy, config)
    raw = raw_for(window)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, legacy)
    result = run_extraction_window(None, window, config, lambda *_: copy.deepcopy(raw))
    assert result["raw_model_candidate"] == raw
    assert result["processing"]["unresolved_block_ids"] == raw["unresolved_block_ids"]
    resolution = result[RESOLUTION_KEY]
    assert resolution["raw_candidate_sha256"] == digest(raw)
    assert resolution["resolved_block_ids"] == raw["unresolved_block_ids"]
    assert [group["article_number"] for group in resolution["proof"]["groups"]] == [first_article, first_article + 1]
    assert [item["citation"]["block_id"] for item in result["validated"]["requirements"]] == ["Q1"]
    assert not result["validated"]["format_chapters"]
    assert _validate_normalized_window_result(result, window) == result["validated"]
    for field in ("raw_candidate_sha256", "proof"):
        altered = copy.deepcopy(result)
        altered[RESOLUTION_KEY][field] = None
        with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_OPTIONAL_FIELDS_RESOLUTION_MISMATCH"):
            _validate_normalized_window_result(altered, window)


@pytest.mark.parametrize("change", ["missing_slash", "substantive_entry", "extra_line", "different_column", "ocr", "unread_region"])
def test_plain_blanks_with_unverified_sources_do_not_close(tmp_path, change):
    _, _, window = windows(tmp_path, change)
    assert WINDOW_CONTEXT_KEY not in window
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw_for(window), window)


@pytest.mark.parametrize("case", ["missing_slot", "selected_prompt", "signature", "article20", "tampered_target"])
def test_optional_proof_cannot_explain_unrelated_or_overlapping_critical_targets(tmp_path, case):
    _, _, window = windows(tmp_path)
    raw = raw_for(window)
    if case == "missing_slot":
        raw["unresolved_block_ids"].remove("B3")
    elif case == "selected_prompt":
        raw["requirements"][0]["citation"] = {"block_ids": ["P18a", "P18b"]}
    elif case == "signature":
        raw["unresolved_block_ids"].append("COMPANY")
    elif case == "article20":
        raw["unresolved_block_ids"].append("Q1")
    elif case == "tampered_target":
        next(block for block in window["blocks"] if block["block_id"] == "B6")["text"] += "另行提供证明。"
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, window)


def test_private_optional_field_proof_preserves_original_selector_digest(tmp_path):
    from company_bid.extraction import _normalize_source_selectors

    _, plain, indexed = windows(tmp_path)
    assert _normalize_source_selectors({}, plain)[1] == _normalize_source_selectors({}, indexed)[1]
