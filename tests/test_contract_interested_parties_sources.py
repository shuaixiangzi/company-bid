"""A split contract clause is resolved only by its original boundary and prior citation."""
from copy import deepcopy

import pytest

from company_bid.contract_interested_parties_sources import (
    CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_KEY as KEY,
    build_contract_interested_parties_proof,
)
from company_bid.extraction import (
    PlanningError, _index, _merge_results, _validate_normalized_window_result, _windows,
    extract_requirements, extraction_window_payload, validate_live_window,
)
from company_bid.storage import digest
from test_source_selected_citations import Context, block, fixture


def reply(window, *selections, unresolved=()):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": "none_verified", "format_chapters": [],
            "requirements": [{"citation": {"block_ids": ids}, "category": "commercial", "response_mode": "procedure",
                              "format_status": "not_prescribed", "mandatory": True} for ids in selections]}


@pytest.fixture
def contract_case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    lines = [
        ("(2) 日志每周归档。", [70, 25, 225, 36]),
        ("(3) 交接期间，更换柜锁前承包方应取得场馆书面确认并向", [70, 50, 420, 64]),
        ("保管员登记钥匙编号。", [95, 68, 240, 80]),
        ("(4) 设备使用记录应归档。", [70, 95, 420, 109]),
    ]
    for i, (text, box) in enumerate(lines):
        row = block(parsed, f"L{i}", text, bbox=box, origin="native")
        row["locator"] = f"pdf/page[1]/line[{i}]"
    document["units"][0]["coverage"]["origin"] = "native"
    base = {"window_max_blocks": 2}
    named_blocks, _, docs = _index(parsed, manifest)
    (prior_window, window), _ = _windows(docs, named_blocks, base, manifest=manifest)
    prior_raw = reply(prior_window, ["L1", "L2"], unresolved=["L2"])
    raw = reply(window, ["L3"], unresolved=["L2"])
    prior, = validate_live_window(prior_raw, prior_window)["requirements"]
    scope = {"source_id": "T", "source_sha256": document["sha256"], "physical_page": 1,
             "window_id": window["window_id"], "predecessor_window_id": prior_window["window_id"],
             "block_ids": ["L1", "L2", "L3"], "raw_candidate_sha256": digest(raw),
             "predecessor_requirement_sha256": digest(prior)}
    edge, = [edge for edge in window["native_pdf_line_edges"] if edge["blocks"] == scope["block_ids"][:2]]
    scope["source_proof_sha256"] = build_contract_interested_parties_proof(
        scope, [named_blocks[key] for key in scope["block_ids"]], edge)["proof_sha256"]
    return manifest, parsed, base, scope, prior_raw, raw, window


def run_case(manifest, parsed, base, scope, prior_raw, raw):
    def local_candidate(_ctx, _task, identity, payload, validate):
        assert KEY not in payload["window"] and KEY not in payload["window_config"]
        return validate(prior_raw if identity == prior_raw["window_id"] else raw)

    return extract_requirements(Context(), parsed, manifest, local_candidate, {**base, KEY: scope})


def test_keep_raw_failure_and_existing_full_clause_with_a_separate_next_item(contract_case):
    manifest, parsed, base, scope, prior_raw, raw, original = contract_case
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, original)
    extracted = run_case(manifest, parsed, base, scope, prior_raw, raw)
    prior_result, result = extracted["window_results"]
    assert result["raw_model_candidate"] == raw
    assert result["live_confirmation"]["unresolved_block_ids"] == ["L2"]
    assert result[KEY]["original_error"]["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert result[KEY]["resolved_block_ids"] == ["L2"]
    merged_prior, _ = _merge_results([prior_result])
    assert extracted["requirements"][0] == merged_prior[0]
    assert [req["citations"][0]["block_ids"] for req in extracted["requirements"]] == [["L1", "L2"], ["L3"]]
    closure, = extracted["deferred_context_closure"]["items"]
    assert closure["resolution"] == "owner_target_source_proven_contract_continuation"
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {**base, KEY: scope}, manifest=manifest)[0][1]
    assert extraction_window_payload(window, {**base, KEY: scope}) == extraction_window_payload(original, base)
    result[KEY]["resolved_block_ids"] = ["L3"]
    with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_INTERESTED_PARTIES_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(result, window)


@pytest.mark.parametrize("alteration", ["raw", "wrong_page", "new_sibling", "non_native", "unread_gap", "unrelated_predecessor"])
def test_scope_cannot_accept_a_different_response_source_or_prior_requirement(contract_case, alteration):
    args = list(deepcopy(contract_case)[:-1])
    _, parsed, _, scope, prior_raw, raw = args
    if alteration == "raw":
        raw["unresolved_block_ids"] = []
    elif alteration == "wrong_page":
        scope["physical_page"] = 2
    elif alteration == "new_sibling":
        lines = parsed["documents"][0]["units"][0]["blocks"]
        lines[3]["text"] = "(5) 更换设备应通知。"
    elif alteration == "non_native":
        parsed["documents"][0]["units"][0]["blocks"][2]["origin"] = "ocr"
    elif alteration == "unread_gap":
        parsed["documents"][0]["units"][0]["coverage"]["unread_objects"] = [{"locator": "other"}]
    elif alteration == "unrelated_predecessor":
        prior_raw["requirements"][0]["citation"]["block_ids"] = ["L0"]
    with pytest.raises(PlanningError, match="CONTRACT_INTERESTED_PARTIES_"):
        run_case(*args)


def test_scope_does_not_hide_independent_requirement_validation_errors(contract_case):
    manifest, parsed, base, scope, prior, raw, original = contract_case
    raw["requirements"][0]["category"] = "technical"
    scope["raw_candidate_sha256"] = digest(raw)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, original)
    with pytest.raises(PlanningError, match="PROCEDURE_CLASSIFICATION_INVALID"):
        run_case(manifest, parsed, base, scope, prior, raw)


def test_scope_cannot_accept_a_duplicate_claim_on_the_resolved_tail(contract_case):
    manifest, parsed, base, scope, prior, raw, _ = contract_case
    raw["requirements"].extend(reply(raw, ["L2"])["requirements"])
    scope["raw_candidate_sha256"] = digest(raw)
    with pytest.raises(PlanningError, match="CONTRACT_INTERESTED_PARTIES_REQUIREMENT_BOUNDARY_MISMATCH"):
        run_case(manifest, parsed, base, scope, prior, raw)
