"""The former window authenticates the title for exactly one frozen tail."""
import copy

import pytest

from company_bid.extraction import PlanningError, _hash, _merge_results, _verified_response_letter_resolution
from company_bid.response_letter_sources import RESPONSE_LETTER_CONFIG_KEY, build_response_letter_proof
from company_bid.response_review_table_sources import RESPONSE_REVIEW_TABLE_CONFIG_KEY, build_response_review_table_proof
from test_response_review_table_sources import _native_preliminary_form


def _letter_fixture():
    config, native, footers = _native_preliminary_form()
    owner_proof = build_response_review_table_proof(config, native, footers)
    owner_config = {**config, "window_id": "prior", "cover_owner_window_id": "first",
                    "letter_continuation_owner_window_id": "next", "raw_candidate_sha256": "b" * 64,
                    "source_proof_sha256": owner_proof["proof_sha256"]}
    scope = {**config, "physical_page": 12, "owner_window_id": "prior", "window_id": "next"}
    proof = build_response_letter_proof(scope, owner_proof)
    assert proof is not None
    selected = proof["selected_block_ids"]
    title = native[18]["text"]
    selector = {"block_ids": list(selected)}
    tail = {"format_title": title, "format_status": "prescribed", "category": "commercial",
            "response_mode": "fixed_text", "citation": copy.deepcopy(selector)}
    chapter = {"title": title, "level": 2, "status": "prescribed", "citation": copy.deepcopy(selector),
               "content_block_ids": list(selected), "requirement_indices": [0]}
    raw = {"window_id": "next", "unresolved_block_ids": [], "format_status": "specified",
           "requirements": [tail, {}, {}, {}, {}, {}, {}], "format_chapters": [chapter, {}, {}, {}]}
    scope.update(raw_candidate_sha256=_hash(raw), source_proof_sha256=proof["proof_sha256"])
    window = {"window_id": "next", "source_format": "pdf",
              "source_id": config["source_id"], "source_sha256": config["source_sha256"],
              "blocks": [{key: value for key, value in block.items() if key != "bbox"} for block in native[30:38]],
              "safe_skip_blocks": [footers[native[36]["block_id"]]],
              RESPONSE_LETTER_CONFIG_KEY: {"config": scope, "owner_context": {"config": owner_config,
                  "native_blocks": native, "footer_proofs": footers}}}
    return window, raw, owner_proof


def test_only_the_verified_tail_declares_a_cross_window_title():
    window, raw, _ = _letter_fixture()
    untouched = copy.deepcopy(raw)
    bound = _verified_response_letter_resolution(raw, window)
    assert bound["owner_window_id"] == "prior" and bound["format_index"] == 0
    assert bound["proof"]["selected_block_ids"] == raw["format_chapters"][0]["citation"]["block_ids"]
    assert bound["proof"]["letter_footer"]["block_id"] == window["blocks"][6]["block_id"]
    assert bound["proof"]["following_heading_block_id"] == window["blocks"][7]["block_id"]
    assert raw == untouched
    assert _verified_response_letter_resolution(raw, {"window_id": "next"}) is None


@pytest.mark.parametrize(("fault", "error"), [
    ("unrelated_candidate", "RESPONSE_LETTER_RAW_RESPONSE_MISMATCH"),
    ("tail_reordered", "RESPONSE_LETTER_RAW_SELECTION_MISMATCH"),
    ("other_title", "RESPONSE_LETTER_RAW_SELECTION_MISMATCH"),
    ("source_text", "RESPONSE_LETTER_OWNER_PROOF_MISMATCH"),
    ("window_tail", "RESPONSE_LETTER_WINDOW_SCOPE_MISMATCH"),
    ("removed_footer", "RESPONSE_LETTER_WINDOW_FOOTER_MISMATCH"),
])
def test_letter_source_binding_rejects_unreviewed_candidate_or_proof(fault, error):
    window, raw, _ = _letter_fixture()
    context = window[RESPONSE_LETTER_CONFIG_KEY]
    if fault == "unrelated_candidate":
        raw["requirements"][2]["extra_fact"] = True
    elif fault == "tail_reordered":
        raw["format_chapters"][0]["content_block_ids"].reverse()
        context["config"]["raw_candidate_sha256"] = _hash(raw)
    elif fault == "other_title":
        raw["format_chapters"][0]["title"] = "报价明细表"
        context["config"]["raw_candidate_sha256"] = _hash(raw)
    elif fault == "source_text":
        context["owner_context"]["native_blocks"][32]["text"] = "另一条承诺。"
    elif fault == "window_tail":
        window["blocks"].pop(3)
    else:
        window["safe_skip_blocks"].clear()
    with pytest.raises(PlanningError, match=error):
        _verified_response_letter_resolution(raw, window)


def test_merge_keeps_the_original_anchor_level_and_both_requirement_links():
    window, raw, owner_proof = _letter_fixture()
    resolution = _verified_response_letter_resolution(raw, window)
    proof = resolution["proof"]
    head, tail = proof["owner_block_ids"], proof["selected_block_ids"]
    earlier = {"window_id": "prior", "validated": {},
               RESPONSE_REVIEW_TABLE_CONFIG_KEY: {"proof": owner_proof, "cover": {"format_index": -1},
                   "letter": {"continuation_owner_window_id": "next", "target_block_ids": head}}}
    chapter = {"title": resolution["title"], "level": 1, "status": "prescribed", "requirement_indices": [0],
               "citation": {"block_id": head[0], "block_ids": head + tail}, "content_block_ids": head}
    earlier["response_review_table_merge_projection"] = {"validated": {
        "requirements": [{"citation": {"quote": "head"}}], "format_chapters": [chapter]}}
    later_chapter = {**raw["format_chapters"][0], "content_block_ids": tail}
    later = {"window_id": "next", RESPONSE_LETTER_CONFIG_KEY: resolution, "validated": {
        "requirements": [{"citation": {"quote": "tail"}}], "format_chapters": [later_chapter]}}
    previous = copy.deepcopy(earlier)
    requirements, formats = _merge_results([earlier, later])
    assert earlier == previous
    assert len(formats) == 1 and formats[0]["level"] == 1
    assert formats[0]["citation"] == chapter["citation"] and formats[0]["content_block_ids"] == head + tail
    assert formats[0]["requirement_refs"] == [r["requirement_id"] for r in requirements]
    assert formats[0]["source_continuation_derivation"] == proof
    with pytest.raises(PlanningError, match="RESPONSE_LETTER_OWNER_CHECKPOINT_MISMATCH"):
        _merge_results([later])
