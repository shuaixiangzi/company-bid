import pytest

from company_bid.contract_clause_packing import POLICY_KEY, WHOLE_CLAUSES, block_cost
from company_bid.extraction import (
    PlanningError, _index, _normalize_source_selectors, _windows, build_contract_clause_indexes, extraction_window_config,
    extraction_window_payload,
)
from company_bid.pipeline import build_executor
from test_contract_clause_window_resolution import _scenario


def test_new_requests_keep_complete_clauses_within_cap_and_original_order(tmp_path):
    manifest, parsed, config = _scenario(tmp_path, 4)
    blocks, _, docs = _index(parsed, manifest)
    indexes = build_contract_clause_indexes(docs, blocks)

    def pack(settings):
        wc = extraction_window_config(settings)
        windows, _ = _windows(docs, blocks, wc, manifest=manifest, contract_clause_indexes=indexes)
        return [extraction_window_payload(window, wc)["window"]["blocks"] for window in windows]

    legacy = pack(config)
    new = pack({**config, POLICY_KEY: WHOLE_CLAUSES})
    ordered = lambda groups: [block["block_id"] for group in groups for block in group]
    assert ordered(new) == ordered(legacy)
    for clause in indexes[0]["entries"]:
        clause_ids = set(clause["block_ids"])
        if len(clause_ids) <= 4:
            assert any(clause_ids <= {block["block_id"] for block in group} for group in new)
    assert all(len(group) <= 4 for group in new)
    assert not any({"N1", "N2", "N3", "N4"} <= set(ordered([group])) for group in legacy)

    small = pack({**config, POLICY_KEY: WHOLE_CLAUSES, "window_max_blocks": 2})
    assert ordered(small) == ordered(new) and all(len(group) <= 2 for group in small)
    sized = pack({**config, POLICY_KEY: WHOLE_CLAUSES, "window_max_chars": 750})
    assert ordered(sized) == ordered(new)
    assert all(sum(block_cost(blocks[b["block_id"]]) for b in group) <= 750 for group in sized)


def test_document_proof_preserves_the_original_selector_window_digest(tmp_path):
    manifest, parsed, config = _scenario(tmp_path, 4)
    blocks, _, docs = _index(parsed, manifest)
    plain, _ = _windows(docs, blocks, config, manifest=manifest)
    indexed, _ = _windows(docs, blocks, config, manifest=manifest, contract_clause_indexes=build_contract_clause_indexes(docs, blocks))
    assert any(a != b for a, b in zip(plain, indexed, strict=True))
    for old, new in zip(plain, indexed, strict=True):
        assert _normalize_source_selectors({}, old) == _normalize_source_selectors({}, new)


def test_packing_policy_is_frozen_for_new_live_runs(tmp_path):
    base = {"pipeline": "automatic", "structured_provider": {"mode": "chat_completions"}}
    live = build_executor(tmp_path, base)
    assert live.execution_config["extraction"][POLICY_KEY] == WHOLE_CLAUSES
    assert "extraction" not in base
    recorded = build_executor(tmp_path, {**base, "structured_provider": {"mode": "recorded"}})
    assert POLICY_KEY not in recorded.execution_config.get("extraction", {})
    with pytest.raises(PlanningError, match="CONTRACT_WINDOW_PACKING_INVALID"):
        extraction_window_config({POLICY_KEY: "unknown"})
