"""Company eligibility is a factual response, not an action already satisfied."""
import copy

import pytest

from company_bid.extraction import PlanningError, source_selected_output_contract, validate_live_window
from test_source_selected_citations import block, fixture, live_candidate, selected, window_for


@pytest.mark.parametrize("category", ["qualification", "technical", "fixed_table"])
def test_nonbody_mode_cannot_hide_required_response(tmp_path, category):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "有不良信用记录者不得参加；截止日前已解除的除外。")
    window = window_for(parsed, manifest)
    raw = live_candidate({"window": window}, selected(source))
    raw["requirements"][0].update(category=category, response_mode="procedure")
    original = copy.deepcopy(raw)
    with pytest.raises(PlanningError, match="PROCEDURE_CLASSIFICATION_INVALID") as caught:
        validate_live_window(raw, window)
    assert caught.value.diagnostic_context["candidate_index"] == 0
    assert caught.value.diagnostic_context["field"] == "response_mode"
    assert raw == original  # No silent relabeling or deletion to pass the guard.


@pytest.mark.parametrize("category", ["qualification", "commercial"])
def test_factual_mode_keeps_full_condition_without_inventing_proof(tmp_path, category):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "有不良信用记录者不得参加；截止日前已解除的除外。")
    window = window_for(parsed, manifest)
    raw = live_candidate({"window": window}, selected(source))
    raw["requirements"][0].update(category=category, response_mode="factual_response")
    result = validate_live_window(raw, window)["requirements"][0]
    assert result["citation"]["quote"] == source["text"]
    assert result["response_mode"] == "factual_response"
    assert result["attachment_keywords"] == []
    assert result["required_fact_labels"] == []
    assert result["format_status"] == "not_prescribed"


@pytest.mark.parametrize("category", ["technical", "fixed_table", "procedure"])
def test_factual_mode_cannot_replace_technical_or_procedural_contract(tmp_path, category):
    manifest, parsed = fixture(tmp_path)
    source = block(parsed, "B1", "编制巡检方法并保留记录。")
    window = window_for(parsed, manifest)
    raw = live_candidate({"window": window}, selected(source))
    raw["requirements"][0].update(category=category, response_mode="factual_response")
    with pytest.raises(PlanningError, match="FACTUAL_RESPONSE_CLASSIFICATION_INVALID"):
        validate_live_window(raw, window)


def test_live_schema_expresses_cross_field_constraints_without_repairing_values():
    requirement = source_selected_output_contract()["$defs"]["_Requirement"]
    assert "factual_response" in requirement["properties"]["response_mode"]["enum"]
    constraints = {rule["if"]["properties"]["response_mode"]["const"]: rule for rule in requirement["allOf"]
                   if "const" in rule["if"]["properties"]["response_mode"]}
    assert set(constraints) == {"procedure", "factual_response"}
    assert constraints["procedure"]["then"]["properties"]["category"]["enum"] == ["procedure", "commercial"]
    assert constraints["factual_response"]["then"]["properties"]["category"]["enum"] == ["qualification", "commercial"]
    assert all(rule["if"]["required"] == ["response_mode"] and rule["then"]["required"] == ["category"]
               for rule in constraints.values())
