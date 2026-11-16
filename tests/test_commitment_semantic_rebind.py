"""N3 semantic recall options cannot relax authenticated writing reuse."""
from __future__ import annotations

import copy

import pytest

from company_bid import commitment_drafting as commitments
from company_bid.provider import ProviderFailure
from test_commitment_drafting import (
    hybrid, packet, reviewed as reviewed_fixture, _reassembled_reviewed, run_reviewed,
)


@pytest.fixture
def scoped_reviewed(hybrid, monkeypatch, request):
    operation = getattr(request, "param", "add")
    hybrid["settings"]["planning"] = {"source_formats": True}
    if operation in {"delete", "change"}:
        hybrid["settings"]["planning"]["semantic_material_recall"] = {"enabled": False}
    result = reviewed_fixture.__wrapped__(hybrid, monkeypatch)
    result["option_operation"] = operation
    return result


def with_semantic_change(reviewed, mutation=None, *, rebind=True):
    changed = _reassembled_reviewed(reviewed, mutation, rebind=rebind)
    options = changed["review_config"]["planning"]
    if reviewed["option_operation"] == "delete":
        del options["semantic_material_recall"]
    else:
        options["semantic_material_recall"] = {"enabled": True}
    return changed


@pytest.mark.parametrize("scoped_reviewed", ["add", "delete", "change"], indirect=True)
def test_one_n3_option_rebind_reuses_both_letters_without_calls(scoped_reviewed):
    changed = with_semantic_change(scoped_reviewed)
    before = copy.deepcopy(changed["review_config"])
    executor, rid, state = run_reviewed(changed)
    assert state["status"] == "succeeded", state.get("error_code")
    links = executor.output(rid, "N5")["validation"]["commitment_partition"]["reviewed_new_unit_links"]
    assert len(links) == 2
    for link in links:
        proof = link["source_rebind"]
        assert proof["unchanged_request_sha256"] == link["request_sha256"]
        config_proof = proof["configuration_rebind"]
        assert config_proof["allowed_config_path"] == "planning.semantic_material_recall"
        assert config_proof["origin_option_present"] == (scoped_reviewed["option_operation"] != "add")
        assert config_proof["current_option_present"] == (scoped_reviewed["option_operation"] != "delete")
        assert link["company_confirmation"] == "pending" and link["satisfaction_status"] == "not_determined"
    assert changed["review_config"] == before
    assert len(scoped_reviewed["control"]["sent"]) == 2
    assert not state.get("call_budget", {}).get("reservations")
    assert executor.store.read_state(scoped_reviewed["origin_id"]) == scoped_reviewed["origin_state"]


@pytest.mark.parametrize("damage", ["no_rebind", "other_planning", "provider", "letter_provider", "identity", "prompt"])
def test_n3_option_does_not_hide_other_config_or_writing_changes(scoped_reviewed, monkeypatch, damage):
    def mutate(prepared):
        if damage == "identity":
            prepared["company"]["name"] = "Different confirmed company"
    changed = with_semantic_change(scoped_reviewed, mutate, rebind=damage != "no_rebind")
    config = changed["review_config"]
    if damage == "other_planning":
        config["planning"]["source_formats"] = False
    elif damage == "provider":
        config["provider"]["model"] += "-changed"
    elif damage == "letter_provider":
        config["commitment_drafting"]["direct_provider"]["max_output_tokens"] += 1
    elif damage == "prompt":
        monkeypatch.setattr(commitments, "SYSTEM_PROMPT", commitments.SYSTEM_PROMPT + "Changed instructions.")
    _, _, state = run_reviewed(changed)
    assert state["status"] == "failed"
    if damage in {"no_rebind", "other_planning", "letter_provider"}:
        assert state["error_code"] == "COMMITMENT_REVIEW_ORIGIN_CONFIG_CHANGED"
    assert len(scoped_reviewed["control"]["sent"]) == 2
    assert not state.get("call_budget", {}).get("reservations")
    assert scoped_reviewed["executor"].store.read_state(scoped_reviewed["origin_id"]) == scoped_reviewed["origin_state"]


def test_option_comparison_cannot_hide_container_or_unrelated_key_changes():
    binding = {"source_rebind": {"version": "exact-writing-inputs-v1"}}
    old = {"provider": {"model": "unchanged"}}
    new = {**copy.deepcopy(old), "planning": {"semantic_material_recall": {"enabled": True}}}
    assert commitments._review_config_rebind(old, new, binding)["origin_option_present"] is False
    assert commitments._review_config_rebind(new, old, binding)["current_option_present"] is False
    for invalid in (
        {**old, "planning": {}},
        {**old, "planning": None},
        {**new, "unrelated": True},
        {**old, "planning": {"semantic_material_recall": {}, "other": True}},
    ):
        with pytest.raises(ProviderFailure, match="REVIEW_ORIGIN_CONFIG_CHANGED"):
            commitments._review_config_rebind(old, invalid, binding)
