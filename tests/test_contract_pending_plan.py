"""Pure N3 planning tests from synthetic native sources; no services or Word."""
import copy

import pytest

from company_bid.contract_pending_plan import contract_pending_plan
from company_bid.contract_review_slots import contract_review_slot_catalog
from company_bid.extraction import _citation, _index, _pdf_footer_skips
from company_bid.storage import digest
from test_contract_review_slots import (
    acceptance, attachment_tail, contract_tail, fees, ip_source, rights, source,
)


def simple():
    return source([["第六章 合同条款", "第一条 合成事项", "1.1其他：", "1.2合成固定正文。"]])


def requirement(parsed, manifest, ids, *, rid="R-EXISTING", category="commercial", mode="procedure", quote=None):
    blocks, _, _ = _index(parsed, manifest)
    wire = {key: blocks[ids[0]][key] for key in ("source_id", "block_id", "locator")}
    wire["block_ids"] = ids
    if quote is not None:
        wire["quote"] = quote
    citation = _citation(wire, blocks, _pdf_footer_skips(blocks))
    return {"requirement_id": rid, "text": citation["quote"], "category": category, "response_mode": mode,
            "mandatory": True, "confidence": "high", "format_status": "not_prescribed", "format_title": None,
            "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": [],
            "citations": [citation], "window_ids": ["synthetic-window"]}


def catalog(parsed, manifest):
    return contract_review_slot_catalog(parsed, manifest)["items"]


def test_complete_existing_requirement_only_gets_binding_and_remains_unchanged():
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    req = requirement(parsed, manifest, item["block_ids"])
    original = copy.deepcopy((req, parsed, manifest))
    result = contract_pending_plan([req], parsed, manifest)
    assert not result["errors"] and not result["derived_requirements"]
    assert result["bindings"] == {"R-EXISTING": [item]}
    proof, = result["binding_proofs"]["R-EXISTING"]
    assert proof["kind"] == "existing_complete_source_citations" and proof["original_requirement_unchanged"]
    assert (req, parsed, manifest) == original
    assert req["response_mode"] == "procedure"


def test_omitted_requirement_is_derived_with_real_full_citation_and_source_provenance():
    parsed, manifest = rights()
    before = copy.deepcopy((parsed, manifest))
    result = contract_pending_plan([], parsed, manifest)
    assert not result["errors"] and len(result["derived_requirements"]) == 6
    derived = result["derived_requirements"][0]
    assert derived["requirement_id"].startswith("R-CONTRACT-")
    assert derived["text"].startswith("(1) 甲方") and derived["text"].endswith("(d) ；")
    assert derived["category"] == "commercial" and derived["response_mode"] == "factual_response"
    assert derived["confidence"] == "low" and derived["required_fact_labels"] == []
    assert derived["attachment_keywords"] == derived["reference_keywords"] == []
    assert derived["window_ids"] == []
    citation, = derived["citations"]
    assert citation["quote"] == derived["text"] and len(citation["block_ids"]) == 6
    derivation = derived["derivation"]
    assert derivation["kind"] == "derived_from_frozen_source" and derivation["not_model_output"] is True
    assert derivation["original_context"]["parent_context"][0]["text"] == "3.1甲方权利与义务"
    item, = result["bindings"][derived["requirement_id"]]
    assert item["owner_party"] == "甲方" and item["value_status"] == "value_not_supplied_in_read_text"
    assert item["satisfaction"] == "not_determined" and item["values_inferred"] is False
    assert "company_material" not in str(result)
    assert (parsed, manifest) == before


@pytest.mark.parametrize("single_block_substring", [False, True])
def test_partial_citation_derives_complete_source_without_replacing_partial_n2(single_block_substring):
    parsed, manifest = simple() if single_block_substring else rights()
    item = catalog(parsed, manifest)[0]
    quote = "其他：" if single_block_substring else None
    req = requirement(parsed, manifest, item["block_ids"][:1], quote=quote)
    before = copy.deepcopy(req)
    result = contract_pending_plan([req], parsed, manifest)
    assert not result["errors"] and "R-EXISTING" not in result["bindings"]
    derived = next(raw for raw in result["derived_requirements"] if raw["text"] == item["text"])
    assert derived["citations"][0]["block_ids"] == item["block_ids"]
    assert derived["derivation"]["overlapping_original_requirement_ids"] == ["R-EXISTING"]
    assert req == before


def test_complete_superset_citation_binds_multiple_pending_subclauses_with_all_other_obligations_intact():
    parsed, manifest = rights()
    item = catalog(parsed, manifest)[0]
    ids = [b["block_id"] for b in item["source_proof"]["complete_clause_members"]]
    req = requirement(parsed, manifest, ids)
    before = copy.deepcopy(req)
    result = contract_pending_plan([req], parsed, manifest)
    assert not result["errors"]
    assert len(result["bindings"]["R-EXISTING"]) == 4
    assert "乙方应提供书面说明" in req["text"] and req == before
    assert len(result["derived_requirements"]) == 2


def test_multiple_existing_complete_sources_are_bound_without_arbitrary_owner_selection():
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    first = requirement(parsed, manifest, item["block_ids"], rid="R-A")
    second = requirement(parsed, manifest, item["block_ids"], rid="R-B", mode="factual_response")
    result = contract_pending_plan([first, second], parsed, manifest)
    assert not result["errors"] and not result["derived_requirements"]
    assert set(result["bindings"]) == {"R-A", "R-B"}


@pytest.mark.parametrize("category, mode", [("technical", "writing"), ("technical", "procedure"), ("commercial", "writing")])
def test_existing_technical_or_writing_overlap_is_an_error_and_never_downgraded(category, mode):
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    req = requirement(parsed, manifest, item["block_ids"], category=category, mode=mode)
    before = copy.deepcopy(req)
    result = contract_pending_plan([req], parsed, manifest)
    assert result["errors"] == [{"code": "requires_source_review", "reason": "contract_pending_source_overlaps_technical_or_writing",
                                  "requirement_id": "R-EXISTING", "category": category, "response_mode": mode}]
    assert not result["derived_requirements"] and not result["bindings"] and req == before


def test_partial_technical_pending_overlap_and_unbounded_contract_writing_require_source_review():
    parsed, manifest = rights()
    item = catalog(parsed, manifest)[0]
    req = requirement(parsed, manifest, item["block_ids"][:1], category="technical", mode="writing")
    result = contract_pending_plan([req], parsed, manifest)
    assert any(error["code"] == "requires_source_review" for error in result["errors"])
    assert not any(item["pending_id"] == bound["pending_id"] for values in result["bindings"].values() for bound in values)
    parsed, manifest = simple()
    req = requirement(parsed, manifest, ["B1-3"], category="technical", mode="writing")
    result = contract_pending_plan([req], parsed, manifest)
    assert result["errors"] == [{"code": "requires_source_review", "reason": "contract_review_source_or_boundary_not_proven",
                                 "source_id": "S", "clause_number": "1.2"}]
    assert len(result["derived_requirements"]) == 1


@pytest.mark.parametrize("change", ["missing_source", "source_hash", "unread", "ocr_needed", "glyph", "image", "expected_units", "reading_incomplete"])
def test_missing_or_unread_contract_source_never_becomes_a_pending_value(change):
    parsed, manifest = simple()
    doc = parsed["documents"][0]
    if change == "missing_source": manifest["sources"] = []
    elif change == "source_hash": doc["sha256"] = "b" * 64
    elif change == "unread": doc["units"][0]["coverage"]["status"] = "failed"
    elif change == "ocr_needed": doc["units"][0]["coverage"]["status"] = "needs_ocr"
    elif change == "glyph": doc["units"][0]["coverage"]["unmapped_glyphs"] = ["?"]
    elif change == "image": doc["units"][0]["coverage"]["image_count"] = 1
    elif change == "expected_units": doc["expected_units"] = 2
    elif change == "reading_incomplete": parsed["coverage"]["required_complete"] = False
    result = contract_pending_plan([], parsed, manifest)
    assert not result["derived_requirements"] and not result["bindings"]
    # Image-only source spans are explicitly uncovered by the finite catalogue.
    if change == "image":
        assert result["catalog_summary"]["uncovered_clauses"]
    else:
        assert result["errors"] and all(error["code"] == "requires_source_review" for error in result["errors"])


def test_existing_full_requirement_cannot_override_now_unread_frozen_source():
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    req = requirement(parsed, manifest, item["block_ids"], mode="factual_response")
    before = copy.deepcopy(req)
    parsed["documents"][0]["units"][0]["coverage"]["status"] = "failed"
    result = contract_pending_plan([req], parsed, manifest)
    assert result["errors"] and not result["bindings"] and not result["derived_requirements"]
    assert req == before


@pytest.mark.parametrize("change", ["quote", "quote_hash", "locator", "source", "missing_block", "unit", "missing_citations"])
def test_existing_source_claims_are_rechecked_and_cannot_suppress_derivation_with_fake_coverage(change):
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    req = requirement(parsed, manifest, item["block_ids"])
    citation = req["citations"][0]
    if change == "quote": citation["quote"] = req["text"] = "猜造的合成合同文字。"
    elif change == "quote_hash": citation["quote_sha256"] = "0" * 64
    elif change == "locator": citation["locator"] = "pdf/page[9]/line[0]"
    elif change == "source": citation["source_id"] = "OTHER"
    elif change == "missing_block": citation["block_ids"] = ["MISSING"]
    elif change == "unit": citation["unit_id"] = "OTHER"
    elif change == "missing_citations": req["citations"] = []
    result = contract_pending_plan([req], parsed, manifest)
    assert result["errors"] and not result["bindings"] and not result["derived_requirements"]


@pytest.mark.parametrize("factory", [rights, fees, acceptance, ip_source, contract_tail, attachment_tail])
def test_all_current_finite_catalog_scopes_have_complete_derivation_or_binding(factory):
    parsed, manifest = factory()
    items = catalog(parsed, manifest)
    result = contract_pending_plan([], parsed, manifest)
    assert not result["errors"] and len(result["derived_requirements"]) == len(items)
    requirements = result["derived_requirements"]
    replay = contract_pending_plan(requirements, parsed, manifest)
    assert not replay["errors"] and not replay["derived_requirements"]
    assert set(replay["bindings"]) == {item["requirement_id"] for item in requirements}
    assert result["catalog_summary"]["exhaustive"] is False
    assert result["catalog_summary"]["bound_item_count"] == len(items)


def test_ip_reference_clarification_stays_separate_from_unselected_contract_values():
    parsed, manifest = ip_source()
    result = contract_pending_plan([], parsed, manifest)
    derived = next(raw for raw in result["derived_requirements"] if "采用以下第" in raw["text"])
    item, = result["bindings"][derived["requirement_id"]]
    assert item["blocking_issues"][0]["value_status"] == "reference_requires_clarification"
    assert item["blocking_issues"][0]["resolved_reference"] is None
    assert len(item["pending_fields"]) == 2 and item["owner_party"] is None
    assert len(derived["citations"][0]["block_ids"]) == 6 and "50%" in derived["text"]


def test_main_contract_and_annex_context_and_explicit_eof_boundary_are_preserved():
    parsed, manifest = attachment_tail()
    result = contract_pending_plan([], parsed, manifest)
    derived, = result["derived_requirements"]
    item, = result["bindings"][derived["requirement_id"]]
    assert derived["derivation"]["scope"]["attachment_number"] == "五"
    assert item["source_proof"]["document_end_boundary"]["expected_units"] == 3
    assert derived["derivation"]["original_context"]["scope_heading"]["text"].startswith("附件五")


def test_stable_source_identity_independent_of_existing_requirement_order_and_no_n2_mutation():
    parsed, manifest = rights()
    items = catalog(parsed, manifest)
    first = requirement(parsed, manifest, items[1]["block_ids"], rid="R-A")
    second = requirement(parsed, manifest, items[2]["block_ids"], rid="R-B")
    requirements = [first, second]
    before = copy.deepcopy(requirements)
    one = contract_pending_plan(requirements, parsed, manifest)
    two = contract_pending_plan(list(reversed(requirements)), parsed, manifest)
    assert [item["requirement_id"] for item in one["derived_requirements"]] == [item["requirement_id"] for item in two["derived_requirements"]]
    assert requirements == before and one["n2_requirements_sha256"] == digest(requirements)
    assert one["n2_candidate_mutation"] is False and one["values_inferred"] is False


def test_unsupported_and_unbounded_source_is_explicitly_uncovered_without_guessing():
    parsed, manifest = source([["第六章 合同条款", "第一条 合成事项", "1.1按未知形式在 日内完成。", "1.2其他："]])
    result = contract_pending_plan([], parsed, manifest)
    assert not result["derived_requirements"] and not result["bindings"]
    assert result["catalog_summary"]["exhaustive"] is False and result["limitations"]
    assert result["catalog_summary"]["uncovered_clauses"][-1]["reason"] == "source_or_boundary_not_proven"


def test_duplicate_existing_ids_and_deterministic_derived_id_collision_are_errors():
    parsed, manifest = simple()
    item, = catalog(parsed, manifest)
    req = requirement(parsed, manifest, item["block_ids"])
    result = contract_pending_plan([req, copy.deepcopy(req)], parsed, manifest)
    assert result["errors"] and not result["bindings"]
    derived_id = contract_pending_plan([], parsed, manifest)["derived_requirements"][0]["requirement_id"]
    unrelated = requirement(parsed, manifest, ["B1-3"], rid=derived_id)
    result = contract_pending_plan([unrelated], parsed, manifest)
    assert result["errors"][0]["reason"] == "derived_requirement_id_collision" and not result["derived_requirements"]
