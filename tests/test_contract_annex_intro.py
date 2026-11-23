"""Only a complete, source-reviewed HSE recital can settle opening-window fragments."""

import hashlib
import json

import pdfplumber
import pytest

from company_bid import contract_annex_intro as intro
from company_bid import extraction as ext
from company_bid.contract_heading_only_annex import rendered_page_rgb_sha256
from company_bid.observability import redact_payload
from company_bid.pdf_text_lines import pdf_page_text_lines
from company_bid.storage import digest, json_bytes
from test_contract_attachment_citation_split import _native_pdf
from test_source_selected_citations import block, fixture, live_candidate


def scenario(tmp_path, change=None):
    texts = ["附件4：健康、安全与环境（HSE）管理条款", "甲方（发包人）：某单位", "乙方（承包商）：",
             "鉴于甲、乙双方签署了《合成合同》（以下简称“主合同”），为保障健",
             "康，依照《某法》有关规定，遵循公平的原则，", "双方就本项目的HSE事项协商一致，订立本",
             "协议。", "1.定义及解释", "1.1 HSE：指健康、安全与环境。", "1.2 甲方应提供规定的资料。"]
    if change == "filled_contractor":
        texts[2] += "某单位"
    elif change == "operative_clause":
        texts[4] += "乙方应提交证明，"
    elif change == "truncated_recital":
        texts[6] = "协"
    preceding = ["乙方应按合同完成工作。", "合同应保留原件。", "条款按签约文件确认。", "工程地点为某地。", "双方另行协商。"]
    path = _native_pdf(tmp_path, texts, preceding=preceding)
    manifest, parsed = fixture(tmp_path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0].update(path=str(path), sha256=source_hash)
    parsed["documents"][0].update(format="pdf", sha256=source_hash)
    with pdfplumber.open(path) as document:
        for page, expected in enumerate([preceding, texts], 1):
            native = list(pdf_page_text_lines(document.pages[page - 1], ()))
            assert [text for _, text, _, _, _ in native] == expected
            prefix = "p" if page == 1 else "b"
            for i, text, bbox, _, _ in native:
                line = block(parsed, f"{prefix}{i}", text, origin="native", bbox=bbox, page=page)
                line["locator"] = f"pdf/page[{page}]/line[{i}]"
            parsed["documents"][0]["units"][page - 1]["coverage"].update(origin="native", unread_objects=[], ignored_objects=[],
                unmapped_glyphs=[], error_code=None, warnings=[], table_count=0, image_count=0, transformations=[])
    blocks, _, documents = ext._index(parsed, manifest)
    base = ext.extraction_window_config({"window_max_blocks": 9})
    windows, _ = ext._windows(documents, blocks, base, manifest=manifest)
    raw = live_candidate({"window": windows[0]}, {"block_ids": ["p0"]})
    raw["requirements"][0].update(category="commercial", response_mode="procedure")
    raw["unresolved_block_ids"] = ["b0", "b1", "b2", "b3"]
    config = {"window_id": windows[0]["window_id"], "source_id": "T", "source_sha256": source_hash,
              "raw_candidate_sha256": digest(raw), "physical_page": 2, "annex_heading_block_id": "b0",
              "rendered_rgb_sha256": rendered_page_rgb_sha256(path, 2)}
    return manifest, documents, blocks, base, windows, raw, config


def _bind(data):
    manifest, documents, blocks, base, _, _, scope = data
    frozen = ext.extraction_window_config({**base, intro.SOURCE_CONFIG_KEY: scope})
    return frozen, ext._windows(documents, blocks, frozen, manifest=manifest)[0]


def test_review_preserves_blank_recital_and_the_original_unresolved_declaration(tmp_path):
    data = scenario(tmp_path)
    _, _, _, base, bare, raw, _ = data
    frozen, windows = _bind(data)
    target, owner = windows[:2]
    assert windows[1:] == bare[1:]
    assert ext.extraction_window_payload(bare[0], base) == ext.extraction_window_payload(target, frozen)
    with pytest.raises(ext.PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        ext.validate_live_window(raw, bare[0])
    result = ext.run_extraction_window(None, target, frozen, lambda *args: raw)
    persisted = json.loads(json_bytes(redact_payload(result)))
    assert ext._validate_normalized_window_result(persisted, target) == result["validated"]
    assert persisted == result and persisted["raw_model_candidate"] == raw
    assert persisted["live_confirmation"]["unresolved_block_ids"] == ["b0", "b1", "b2", "b3"]
    resolution = persisted[intro.RESOLUTION_KEY]
    assert resolution["resolved_block_ids"] == ["b0", "b1", "b2", "b3"]
    requirements = persisted["validated"]["requirements"]
    assert len(requirements) == 1 and requirements[0]["citation"]["block_ids"] == ["p0"]
    proof = resolution["proof"]
    assert proof["contractor_value_status"] == "visibly_unfilled_in_contract_template"
    assert "保障健\n康" in proof["recital_source_quote"]
    assert proof["recital_source_quote"].endswith("订立本\n协议。")
    assert proof["next_heading"]["block_id"] == "b7"
    assert intro.following_owner_matches(proof, owner)
    wrong_owner = {**owner, "blocks": owner["blocks"][1:]}
    assert not intro.following_owner_matches(proof, wrong_owner)
    successor_raw = live_candidate({"window": owner}, {"block_ids": ["b9"]})
    successor = ext.run_extraction_window(None, owner, frozen, lambda *args: successor_raw)
    assert ext._close_deferred_context(windows, [persisted, successor])["status"] == "structurally_closed"
    del persisted[intro.RESOLUTION_KEY]
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_ANNEX_INTRO_MISMATCH"):
        ext._validate_normalized_window_result(persisted, target)


@pytest.mark.parametrize("change", ["filled_contractor", "operative_clause", "truncated_recital", "different_following_owner"])
def test_source_proof_rejects_filled_roles_substantive_text_and_improper_handoff(tmp_path, change):
    data = scenario(tmp_path, change)
    if change == "different_following_owner":
        data[3]["window_max_blocks"] += 1
    with pytest.raises(ext.PlanningError, match=intro.SOURCE_ERROR):
        _bind(data)


@pytest.mark.parametrize("change", ["unrelated_unresolved", "missing_unresolved", "already_selected", "unbound_digest"])
def test_handoff_never_suppresses_other_critical_blocks_or_changes_original_candidates(tmp_path, change):
    data = scenario(tmp_path)
    raw, scope = data[-2:]
    if change == "unrelated_unresolved":
        raw["unresolved_block_ids"][3] = "p1"
    elif change == "missing_unresolved":
        raw["unresolved_block_ids"].pop()
    elif change == "already_selected":
        response = live_candidate({"window": data[4][0]}, {"block_ids": ["b2"]})
        raw["requirements"] = response["requirements"]
    else:
        scope["raw_candidate_sha256"] = "0" * 64
    if change != "unbound_digest":
        scope["raw_candidate_sha256"] = digest(raw)
    _, windows = _bind(data)
    with pytest.raises(ext.PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        ext.validate_live_window(raw, windows[0])
