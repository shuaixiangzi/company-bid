"""One historical candidate may be retained as reviewed screenshot context."""
from copy import deepcopy

import pytest

from company_bid.extraction import (
    PlanningError, _index, _merge_results, _pdf_footer_skips, _validate_normalized_window_result, _windows,
    extract_requirements, extraction_window_payload, run_extraction_window, validate_live_window,
)
from company_bid.extraction_illustrations import (
    ILLUSTRATION_SOURCE_CONFIG_KEY as KEY, build_illustration_context_proof, illustration_source_uses,
)
from company_bid.reading_source_review import KIND, VERSION, review_box_on_page
from company_bid.storage import digest
from test_source_selected_citations import Context, block, fixture


def _raw(window, requirements=()):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": list(requirements), "format_chapters": [],
            "format_status": "none_verified"}


@pytest.fixture
def case(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document, = parsed["documents"]
    document["format"] = "pdf"
    first = block(parsed, "native-a", "Submit a current screenshot for both designated lists.", origin="native")
    lead = block(parsed, "native-b", "The following image illustrates the query steps.", page=2, origin="native")
    first["locator"] = "pdf/page[1]/line[0]"
    lead["locator"] = "pdf/page[2]/line[0]"
    prior_unit, review_unit = document["units"]
    prior_unit["coverage"]["origin"] = "native"
    review_unit["coverage"].update(origin="mixed", transformations=[])

    def screenshot(index, entries):
        locator = f"pdf/page[2]/image[{index}]"
        left, top, right, bottom = extent = [30.0, 40.0 + index * 100, 130.0, 110.0 + index * 100]
        image_sha = str(index + 1) * 64
        parent = block(parsed, f"im{index}", "", kind="image", page=2, origin="native", bbox=extent)
        parent["locator"] = locator
        review = {"kind": KIND, "version": VERSION, "status": "confirmed", "source_id": "T",
                  "source_sha256": document["sha256"], "locator": locator, "page": 2,
                  "bbox": extent, "image_sha256": image_sha, "reviewed_image": {"sha256": image_sha},
                  "renderer": {"image_sha256": image_sha, "region": {"pixel_crop": [left, top, right, bottom],
                               "page_size": [200, 300]}, "full_page_pixels": [200, 300]}, "unknown": [], "entries": []}
        review_unit["coverage"]["transformations"].append(review)
        for i, (identity, role, text) in enumerate(entries):
            pixels = [10, i * 10 + 2, 55, i * 10 + 10]
            entry = {"text": text, "role": role, "locator": f"{locator}/source-review/entry[{i}]", "pixel_bbox": pixels}
            review["entries"].append(entry)
            child = block(parsed, identity, text, page=2, origin="source_review", bbox=review_box_on_page(review, pixels))
            child["locator"] = entry["locator"]

    screenshot(0, [("open", "instruction", "Open the website"), ("address", "navigation", "Example URL"),
                   ("search", "instruction", "Press Search"), ("enter", "instruction", "Enter organization name")])
    screenshot(1, [("result", "example_result", "Illustrative sample organization has no records")])

    config = {"window_max_blocks": 4}
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config, manifest=manifest)
    predecessor, target, after = windows
    native_req = {"citation": {"block_ids": ["native-a", "native-b"]}, "category": "qualification",
                  "response_mode": "attachment", "format_status": "not_prescribed", "attachment_keywords": ["screenshot"]}
    raw_predecessor = _raw(predecessor, [native_req])
    validated_req, = validate_live_window(raw_predecessor, predecessor)["requirements"]
    selector = {"citation": {"block_ids": ["address", "search", "enter"]}, "category": "procedure",
                "response_mode": "procedure", "format_status": "not_prescribed", "format_title": None,
                "mandatory": False, "confidence": "low", "required_fact_labels": [], "attachment_keywords": [],
                "reference_keywords": []}
    raw = _raw(target, [selector])
    scope = {"source_id": "T", "source_sha256": document["sha256"], "window_id": target["window_id"], "page": 2,
             "predecessor_window_id": predecessor["window_id"], "predecessor_requirement_sha256": digest(validated_req),
             "native_requirement_block_ids": native_req["citation"]["block_ids"],
             "candidate_block_ids": selector["citation"]["block_ids"], "instruction_block_ids": ["open", "enter", "search"],
             "raw_candidate_sha256": digest(raw)}
    uses = illustration_source_uses(documents, [], manifest)
    annotated = {key: b | {"illustration_source_use": uses[key]} if key in uses else b for key, b in blocks.items()}
    proof = build_illustration_context_proof(scope, target, predecessor, document, annotated, _pdf_footer_skips(annotated))
    scope["source_proof_sha256"] = proof["proof_sha256"]
    config[KEY] = scope
    return manifest, parsed, windows, config, [raw_predecessor, raw, _raw(after)], proof


def test_only_the_audited_window_can_close_and_keep_all_prose_in_its_owner(case):
    manifest, parsed, baseline, config, raws, proof = case
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config, manifest=manifest)
    assert [extraction_window_payload(w, config) for w in windows] == [
        extraction_window_payload(w, config) for w in baseline]
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_NOT_CITABLE"):
        validate_live_window(raws[1], baseline[1])
    context = Context()
    by_window = {row["window_id"]: row for row in raws}
    out = extract_requirements(context, parsed, manifest, lambda ctx, task, unit, request, validate: validate(by_window[unit]), config)
    result = out["window_results"][1]
    assert result["raw_model_candidate"] == raws[1]
    assert result["status"] == "complete" and result["candidate"]["requirements"] == []
    assert result[KEY]["proof"] == proof
    assert result[KEY]["disposition"]["candidate"] == raws[1]["requirements"][0]
    assert [row["text"] for row in proof["instructions"]] == ["Open the website", "Enter organization name", "Press Search"]
    assert context.captures["n2-illustration-source-resolution"] == result[KEY]
    assert out["requirements"][0]["citations"][0]["block_ids"] == ["native-a", "native-b"]
    assert len(out["requirements"]) == 1 and out["format_chapters"] == []
    assert out["coverage"]["semantic_review"] == "requires_source_review"
    broken = deepcopy(out["window_results"])
    broken[0]["validated"]["requirements"] = []
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_PREDECESSOR_MISMATCH"):
        _merge_results(broken)
    result[KEY]["disposition"]["candidate"]["mandatory"] = True
    with pytest.raises(PlanningError, match="EXTRACTION_ILLUSTRATION_CONTEXT_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(result, windows[1])


@pytest.mark.parametrize("update", [
    {"confidence": "high"}, {"mandatory": True}, {"required_fact_labels": ["Organization name"]},
    {"category": "qualification", "response_mode": "attachment"},
])
def test_configured_recovery_cannot_discard_other_candidate_meanings(case, update):
    manifest, parsed, _, config, raws, _ = case
    raw = deepcopy(raws[1])
    raw["requirements"][0].update(update)
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, config, manifest=manifest)
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_RAW_RESPONSE_MISMATCH"):
        run_extraction_window(Context(), windows[1], config, lambda *args: args[-1](raw))


@pytest.mark.parametrize("damage", ["new_prose", "unknown_text", "changed_image", "partial_instruction_list"])
def test_stale_or_incomplete_source_prevents_replay_before_any_call(case, damage):
    manifest, parsed, _, config, _, _ = case
    unit = parsed["documents"][0]["units"][1]
    review = unit["coverage"]["transformations"][0]
    if damage == "new_prose":
        extra = block(parsed, "new-native", "Submit an additional verification form.", page=2, origin="native")
        extra["locator"] = "pdf/page[2]/line[9]"
        unit["blocks"].insert(4, unit["blocks"].pop())
    elif damage == "unknown_text":
        review["unknown"] = [{"critical": True}]
    elif damage == "changed_image":
        review["image_sha256"] = review["reviewed_image"]["sha256"] = review["renderer"]["image_sha256"] = "0" * 64
    else:
        config[KEY]["instruction_block_ids"].remove("open")
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_SOURCE_PROOF_MISMATCH"):
        extract_requirements(Context(), parsed, manifest, lambda *args: pytest.fail("source proof must stop before any call"), config)
