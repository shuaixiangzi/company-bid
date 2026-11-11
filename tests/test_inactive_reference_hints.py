"""Unused method-search hints cannot block or alter non-writing requirements."""
import copy

import pytest

from company_bid.extraction import (
    PlanningError, _index, _windows, validate_live_window, extract_requirements,
    source_selected_output_contract,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, fixture, block, selected, live_candidate, no_call


def source(tmp_path):
    manifest, parsed = fixture(tmp_path)
    b = block(parsed, "RULE", "参与本项目产生的费用由响应人自行承担，取消采购时不予补偿。")
    blocks, _, docs = _index(parsed, manifest)
    return manifest, parsed, b, _windows(docs, blocks, {})[0][0]


def response(window, b, mode="procedure"):
    raw = live_candidate({"window": window}, selected(b))
    raw["requirements"][0].update(category="commercial", response_mode=mode,
                                   reference_keywords=["不在引文里的技术检索提示"])
    return raw


@pytest.mark.parametrize("mode", ["procedure", "attachment", "factual_response", "fixed_text", "table"])
def test_inactive_reference_only_is_removed_without_mutating_raw_or_quote(tmp_path, mode):
    manifest, parsed, b, window = source(tmp_path)
    raw = response(window, b, mode)
    before = copy.deepcopy(raw)
    result = validate_live_window(raw, window)
    assert raw == before
    requirement = result["requirements"][0]
    assert requirement["reference_keywords"] == []
    assert requirement["citation"]["quote"] == b["text"]
    assert requirement["response_mode"] == mode


@pytest.mark.parametrize("field", ["reference_keywords", "attachment_keywords", "required_fact_labels"])
def test_applicable_or_fact_and_evidence_hints_still_require_source_grounding(tmp_path, field):
    manifest, parsed, b, window = source(tmp_path)
    raw = response(window, b)
    requirement = raw["requirements"][0]
    if field == "reference_keywords":
        requirement.update(category="technical", response_mode="writing")
    else:
        requirement[field] = ["原文不存在的事实或证明"]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw, window)


def test_ignored_hints_cannot_hide_unresolved_required_content(tmp_path):
    manifest, parsed, b, window = source(tmp_path)
    raw = response(window, b)
    raw["unresolved_block_ids"] = [b["block_id"]]
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("invalid", [None, "not-a-list", ["x"] * 16])
def test_inactive_hints_do_not_bypass_raw_schema_validation(tmp_path, invalid):
    manifest, parsed, b, window = source(tmp_path)
    raw = response(window, b)
    raw["requirements"][0]["reference_keywords"] = invalid
    with pytest.raises(PlanningError, match="EXTRACTION_SCHEMA_INVALID"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("damage", [None, "ledger", "raw", "candidate"])
def test_private_normalization_ledger_and_n3_reconstruction(tmp_path, damage):
    manifest, parsed, b, window = source(tmp_path)
    context = Context()
    def call(ctx, task_type, unit_id, payload, validate):
        return validate(response(payload["window"], b))
    extracted = extract_requirements(context, parsed, manifest, call, {})
    result = extracted["window_results"][0]
    ignored = result["inactive_reference_hints"]["ignored_fields"]
    assert ignored[0]["raw_values"] == ["不在引文里的技术检索提示"]
    assert result["raw_model_candidate"]["requirements"][0]["reference_keywords"] == ignored[0]["raw_values"]
    assert result["candidate"]["requirements"][0]["reference_keywords"] == []
    assert "n2-inactive-reference-hints" in context.captures
    if damage:
        changed = copy.deepcopy(extracted)
        row = changed["window_results"][0]
        if damage == "ledger":
            row["inactive_reference_hints"]["ignored_fields"] = []
        elif damage == "raw":
            row["raw_model_candidate"]["requirements"][0]["reference_keywords"] = []
        else:
            row["validated"]["requirements"][0]["reference_keywords"] = ["费用"]
        with pytest.raises(PlanningError):
            plan_bid(Context(), changed, parsed, manifest, no_call, {})
    else:
        planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
        assert planned["review_actions"][0]["source_requirement"] == b["text"]
        def clean_call(ctx, task_type, unit_id, payload, validate):
            raw = response(payload["window"], b)
            raw["requirements"][0]["reference_keywords"] = []
            return validate(raw)
        clean = extract_requirements(Context(), parsed, manifest, clean_call, {})
        clean_plan = plan_bid(Context(), clean, parsed, manifest, no_call, {})
        assert {key: planned[key] for key in ("facts", "sections", "review_actions")} == {
            key: clean_plan[key] for key in ("facts", "sections", "review_actions")}


def test_live_contract_limits_method_hints_to_writing():
    rules = source_selected_output_contract()["$defs"]["_Requirement"]["allOf"]
    assert any(rule.get("then", {}).get("properties", {}).get("reference_keywords") == {"maxItems": 0}
               for rule in rules)
