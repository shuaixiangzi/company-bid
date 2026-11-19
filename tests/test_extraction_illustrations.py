"""Known guide screenshots cannot become tender obligations or company evidence."""
from copy import deepcopy
import json

import pytest
from pypdf import PdfWriter

from company_bid.extraction import (
    PlanningError, _index, _normalize_source_selectors, _windows, extract_requirements, extraction_window_config,
    extraction_window_payload, validate_live_window,
)
from company_bid.extraction_illustrations import CONFIG_KEY, ocr_region_block_digest
from company_bid.ocr import DEEPSEEK_OCR_VERSION, RasterOptions, _prepare_image, _Request
from company_bid.planning import plan_bid
from company_bid.provider import ChatConfig, build_json_request
from company_bid.structured import SYSTEM_PROMPT, _existing_inventory_is_grounded
from test_reading import source, text
from test_reading_source_review import ReviewContext, history, original, read  # noqa: F401
from test_source_selected_citations import Context, fixture as tender_fixture, no_call


@pytest.fixture
def screenshot_case(tmp_path, original, history):
    legacy_source, review, unit_id = original
    path = tmp_path / "with-native-obligation.pdf"
    writer = PdfWriter(clone_from=legacy_source["path"])
    content = (text("Submit a registration screenshot.", 25, 208) + "q 150 0 0 150 20 20 cm /Im1 Do Q\n"
               "q 65 0 0 65 220 55 cm /Im1 Do Q\n" + text("Use the current registration record.", 25, 10))
    writer.pages[0]["/Contents"].set_data(content.encode("ascii"))
    writer.write(path)
    document = {**legacy_source, **source(path), "locator": "document"}
    review["source_sha256"] = document["sha256"]
    review["unknown"] = []
    for i, (role, excerpt) in enumerate([
        ("example_input", "Illustrative Company A"),
        ("example_result", "No results for Illustrative Company A"),
        ("interface", "Filter option"),
        ("context", "Browser news article"),
        ("navigation", "Portal navigation"),
    ], start=4):
        review["entries"].append({"text": excerpt, "role": role, "pixel_bbox": [20, i * 20, 150, i * 20 + 15]})

    def recorded(request):
        return ({k: request[k] for k in ("source_id", "unit_id", "locator", "page")} |
                {"blocks": [{"kind": "paragraph", "text": "Illustrative Company B: No records, category 987."}]})

    parsed = read(ReviewContext(), (document, review, unit_id), recorded)
    unit, = parsed["documents"][0]["units"]
    review_record, = unit["coverage"]["transformations"]
    by_role = {entry["role"]: next(b for b in unit["blocks"] if b["locator"] == entry["locator"])
               for entry in review_record["entries"]}
    by_role["illustrative_ocr"], = [b for b in unit["blocks"] if b["origin"] == "ocr"]
    by_role["native"] = next(b for b in unit["blocks"] if b["text"] == "Submit a registration screenshot.")
    parent = next(b for b in unit["blocks"] if b["locator"] == "pdf/page[1]/image[1]")
    request = _Request(source_id=document["source_id"], sha256=document["sha256"], path=str(path),
                       unit_id=unit_id, locator=parent["locator"], page=1, bbox=parent["bbox"],
                       object_kind="pdf_region", ocr_version=DEEPSEEK_OCR_VERSION)
    _, raster = _prepare_image(request, path.read_bytes(), RasterOptions())
    region = {"source_id": document["source_id"], "source_sha256": document["sha256"], "locator": parent["locator"],
              "ocr_blocks_sha256": ocr_region_block_digest(unit["blocks"], parent["locator"]),
              "image_sha256": raster["image_sha256"], "render_dpi": raster["render_dpi"]}
    manifest, _ = tender_fixture(tmp_path)
    manifest["sources"] = [{key: value for key, value in document.items() if key != "role"}]
    manifest["sources"][0].update(provenance="synthetic", synthetic=True)
    manifest["source_roles"] = {document["source_id"]: "tender"}
    manifest["project"]["source_refs"] = [document["source_id"]]
    manifest["company"]["source_refs"] = [document["source_id"]]
    return manifest, parsed, {CONFIG_KEY: [region]}, by_role


def _window(case):
    manifest, parsed, config, _ = case
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, extraction_window_config(config), manifest=manifest)
    return windows[0]


def _requirement(block, category, mode, **other):
    return {"citation": {"block_ids": [block["block_id"]]}, "category": category, "response_mode": mode,
            "format_status": "not_prescribed", **other}


def _reply(window_id, *requirements):
    return {"window_id": window_id, "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": [], "requirements": list(requirements),
            "format_chapters": [], "format_status": "none_verified"}


def test_actual_wire_and_n3_preserve_native_proof_and_reviewed_operation(screenshot_case):
    manifest, parsed, config, sources = screenshot_case
    before = deepcopy(parsed)
    ctx = Context()

    def candidate(_, task, window_id, payload, validate):
        wire = build_json_request(ChatConfig(endpoint="https://example.invalid", model="test", max_input_chars=50000),
                                  {**payload, "task_type": task}, SYSTEM_PROMPT)
        serialized = wire["messages"][1]["content"]
        request = json.loads(serialized)
        assert CONFIG_KEY not in request["window_config"]
        for role in ("example_input", "example_result", "context", "illustrative_ocr"):
            assert sources[role]["text"] not in serialized
        for role in ("instruction", "native"):
            assert sources[role]["text"] in serialized
        assert "illustration_source_use" in request["task_instructions"]
        raw = _reply(window_id, _requirement(sources["instruction"], "procedure", "procedure"),
                     _requirement(sources["native"], "qualification", "attachment",
                                  attachment_keywords=["registration screenshot"]))
        return validate(raw)

    extracted = extract_requirements(ctx, parsed, manifest, candidate, config)
    assert extracted["window_config"][CONFIG_KEY] == config[CONFIG_KEY]
    prepared = plan_bid(ctx, extracted, parsed, manifest, no_call, {})
    assert [item["text"] for item in prepared["requirements"]] == [
        "Open the portal", "Submit a registration screenshot."]
    assert {item["value"] for item in prepared["facts"]} == {manifest["project"]["name"], manifest["company"]["name"]}
    assert "Illustrative Company" not in json.dumps(prepared)
    assert parsed == before


@pytest.mark.parametrize("role", [
    "example_input", "example_result", "context", "interface", "navigation", "illustrative_ocr",
])
def test_illustrations_cannot_be_cited_even_with_a_verbatim_label(screenshot_case, role):
    source_block = screenshot_case[3][role]
    window = _window(screenshot_case)
    raw = _reply(window["window_id"], _requirement(source_block, "qualification", "factual_response",
                                                   required_fact_labels=[source_block["text"]]))
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_NOT_CITABLE"):
        validate_live_window(raw, window)


def test_operation_is_not_a_fact_or_a_format_and_native_format_remains_editable(screenshot_case):
    manifest, parsed, config, sources = screenshot_case
    window = _window(screenshot_case)
    instruction, native = sources["instruction"], sources["native"]
    raw = _reply(window["window_id"], _requirement(instruction, "qualification", "attachment",
                                                  attachment_keywords=[instruction["text"]]))
    with pytest.raises(PlanningError, match="ILLUSTRATION_INSTRUCTION_PROCEDURE_ONLY"):
        validate_live_window(raw, window)
    raw = _reply(window["window_id"])
    raw.update(format_status="specified", format_chapters=[{
        "title": native["text"], "level": 1, "citation": {"block_ids": [native["block_id"]]},
        "content_block_ids": [instruction["block_id"]], "requirement_indices": [],
    }])
    with pytest.raises(PlanningError, match="ILLUSTRATION_FORMAT_NOT_ALLOWED"):
        validate_live_window(raw, window)

    raw["format_chapters"][0]["content_block_ids"] = []
    ctx = Context()
    output = extract_requirements(ctx, parsed, manifest, lambda *args: args[-1](raw), config)
    prepared = plan_bid(ctx, output, parsed, manifest, no_call, {})
    fixed = [item["text"] for section in prepared["sections"] for item in section["fixed_content"]]
    assert fixed == ["Use the current registration record."]


@pytest.mark.parametrize("field,expected", [
    ("image_sha256", "ILLUSTRATION_OCR_RASTER_MISMATCH"),
    ("ocr_blocks_sha256", "ILLUSTRATION_OCR_REGION_MISMATCH"),
    ("source_sha256", "ILLUSTRATION_OCR_REGION_MISMATCH"),
])
def test_changed_evidence_stops_before_any_candidate(screenshot_case, field, expected):
    manifest, parsed, config, _ = screenshot_case
    config[CONFIG_KEY][0][field] = "0" * 64
    with pytest.raises(PlanningError, match=expected):
        extract_requirements(Context(), parsed, manifest, no_call, config)


def test_unclassified_ocr_keeps_its_source_contract(screenshot_case):
    manifest, parsed, _, sources = screenshot_case
    window = _window((manifest, parsed, {}, sources))
    ocr = sources["illustrative_ocr"]
    actual = next(b for b in extraction_window_payload(window, {})["window"]["blocks"] if b["block_id"] == ocr["block_id"])
    assert actual["text"] == ocr["text"] and "illustration_source_use" not in actual
    raw = _reply(window["window_id"], _requirement(ocr, "qualification", "attachment",
                                                  attachment_keywords=[ocr["text"]]))
    assert validate_live_window(raw, window)["requirements"][0]["citation"]["quote"] == ocr["text"]


def test_context_only_neighbor_cannot_lend_words_to_an_operation(screenshot_case):
    manifest, parsed, config, sources = screenshot_case
    frozen = extraction_window_config({**config, "window_max_blocks": 1})
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, frozen, manifest=manifest)
    instruction = sources["instruction"]["block_id"]
    example = sources["example_input"]["block_id"]
    window = next(window for window in windows if [b["block_id"] for b in window["blocks"]] == [instruction])
    view = extraction_window_payload(window, frozen)["window"]
    adjacent = next(b for b in view["adjacent_context"] if b["block_id"] == example)
    assert adjacent["illustration_source_use"] == "example_input" and not adjacent["text"]
    requirement = _requirement(sources["instruction"], "procedure", "procedure")
    requirement["citation"]["block_ids"].append(example)
    with pytest.raises(PlanningError, match="ILLUSTRATION_CONTEXT_NOT_CITABLE"):
        validate_live_window(_reply(window["window_id"], requirement), window)


def test_chapter_reading_packet_redacts_illustrative_content_at_its_frozen_offsets(screenshot_case):
    manifest, parsed, config, sources = screenshot_case
    unit = parsed["documents"][0]["units"][0]
    unit["blocks"][:0] = [
        {"block_id": key, "source_id": unit["source_id"], "locator": f"pdf/page[1]/line[{index}]",
         "kind": "paragraph", "origin": "native", "text": value}
        for index, (key, value) in enumerate([("reference", "查询方法详见第九章。"), ("chapter", "第九章 查询方法")], 100)
    ]
    window = _window(screenshot_case)
    original_packet, = window["reference_context"]["packets"]
    model_packet, = extraction_window_payload(window, config)["window"]["reference_context"]["packets"]
    assert original_packet["proof_sha256"] == model_packet["proof_sha256"]
    assert "Illustrative Company" in original_packet["source_text"] and "Illustrative Company" not in model_packet["source_text"]
    assert "Browser news article" not in model_packet["source_text"] and "Open the portal" in model_packet["source_text"]
    assert {b["role"] for b in model_packet["illustration_source_uses"]} >= {"instruction", "example_result", "illustrative_ocr"}
    assert validate_live_window(_reply(window["window_id"]), window)["requirements"] == []
    raw = _reply(window["window_id"], _requirement(sources["example_result"], "qualification", "attachment"))
    with pytest.raises(PlanningError, match="CITATION_.*INVALID"):
        validate_live_window(raw, window)


def test_provider_inventory_checks_use_wire_roles_without_losing_a_valid_operation(screenshot_case):
    _, _, config, sources = screenshot_case
    window = _window(screenshot_case)
    model_view = extraction_window_payload(window, config)["window"]
    raw = _reply(window["window_id"], _requirement(sources["instruction"], "procedure", "procedure"))
    normalized, _ = _normalize_source_selectors(raw, window)
    assert _existing_inventory_is_grounded(normalized, model_view)

    raw["requirements"].append(_requirement(sources["example_result"], "qualification", "attachment"))
    normalized, _ = _normalize_source_selectors(raw, window)
    assert not _existing_inventory_is_grounded(normalized, model_view)
