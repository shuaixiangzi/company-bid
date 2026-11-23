"""A source-bound empty annex page cannot waive requirements on any other page."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pdfplumber
import pypdfium2
import pytest

from company_bid import contract_heading_only_annex as annex
from company_bid import extraction_optional_fields_replay as replay_module
from company_bid.extraction import (
    PlanningError, _index, _validate_normalized_window_result, _windows,
    build_contract_clause_indexes, extraction_window_config, extraction_window_payload,
    run_extraction_window, validate_live_window,
)
from company_bid.observability import redact_payload
from company_bid.storage import digest, json_bytes
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.provider import ProviderFailure
from test_reading import pdf
from test_source_selected_citations import block, fixture


def scenario(tmp_path, monkeypatch, change=None):
    manifest, parsed = fixture(tmp_path)
    offset = 3
    pages = [
        [("contract", "第六部分 合同文本"), ("staff-title", "第一条 服务安排"),
         ("staff", "1.1 乙方应配备人员，名单见附件2。"), ("cost", "1.2 结算依附件3约定的单价。"),
         ("close-title", "第二条 合同生效"), ("listing", "2.1 本合同附件为本合同的组成部分。"),
         ("list-1", "附件1：服务内容"), ("list-2", "附件2：项目组名单"),
         ("list-3", "附件3：费用构成"), ("list-4", "附件4：诚信条款"), ("end", "（以下无正文）")],
        [("sign", "（本页无正文，为签章页）"), ("client", "甲方：某单位"), ("vendor", "乙方：【】")],
        [("a1", "附件1：服务内容"), ("services", "负责设备维护。")],
        [("a2", "附件2：项目组名单")],
        [("a3", "附件3：费用构成")],
        [("a4", "附件4：诚信条款"), ("integrity", "当事人应依法履约。")],
    ]
    if change == "extra_line":
        pages[3].append(("line", "供应商应按本页填写人员。"))
    elif change == "different_title":
        pages[4][0] = ("a3", "附件3：其他费用")
    elif change == "no_reference":
        pages[0][3] = ("cost", "1.2 结算依双方协商的单价。")
    elif change == "no_signature":
        pages[1][0] = ("sign", "附件签章")
    elif change == "no_neighbor_body":
        pages[-1].pop()
    elif change == "unrelated_number":
        pages[4][0] = ("a3", "附件5：费用构成")

    for page, lines in enumerate(pages, offset + 1):
        for ordinal, (identity, text) in enumerate([*lines, (f"f{page}", str(page + 4))]):
            footer = ordinal == len(lines)
            top = 793.0 if footer else 72.0 + ordinal * 20
            left, right = (292.0, 303.0) if footer else (90.0, 268.0)
            line = block(parsed, identity, text, page=page, origin="native", bbox=[left, top, right, top + 12])
            line["locator"] = f"pdf/page[{page}]/line[{ordinal}]"
        unit = parsed["documents"][0]["units"][-1]
        unit["coverage"].update(origin="native", unread_objects=[], ignored_objects=[], unmapped_glyphs=[],
                                transformations=[], error_code=None, warnings=[], table_count=0, image_count=0)
        if change == "unread" and page == offset + 4:
            unit["coverage"]["unread_objects"].append("unclassified_graphic")
    # Keep the synthetic single-source digest and PDF page count real while
    # independently exercising each graphics/parser guard below on an actual PDF.
    path = pdf(tmp_path / "source.pdf", [""] * (len(pages) + offset))
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["sources"][0].update(path=str(path), sha256=source_hash)
    parsed["documents"][0].update(format="pdf", sha256=source_hash)
    for page in range(offset, 0, -1):
        parsed["documents"][0]["units"].insert(0, {"unit_id": f"U{page}", "page": page, "source_id": "T",
            "locator": f"pdf/page[{page}]", "blocks": [], "coverage": {"status": "empty"}})

    monkeypatch.setattr(annex, "_native_glyph_counts", lambda page, lines: [len(line["text"]) for line in lines])
    monkeypatch.setattr(annex, "_raster_layout", lambda page, review, lines: {
        "physical_page": review["page"], "rendered_rgb_sha256": review["rendered_rgb_sha256"]})
    blocks, _, documents = _index(parsed, manifest)
    indexes = build_contract_clause_indexes(documents, blocks)
    bare, _ = _windows(documents, blocks, {}, manifest=manifest, contract_clause_indexes=indexes)
    target = next(window for window in bare if any(item["block_id"] == "a2" for item in window["blocks"]))
    config = extraction_window_config({annex.SOURCE_CONFIG_KEY: {"source_id": "T", "source_sha256": source_hash,
        "window_id": target["window_id"], "reviewed_pages": [
            {"page": page, "heading_block_id": identity, "rendered_rgb_sha256": "b" * 64}
            for page, identity in ((offset + 4, "a2"), (offset + 5, "a3"))]}})
    return manifest, blocks, documents, indexes, config, target


def _bind(data):
    manifest, blocks, documents, indexes, config, target = data
    windows, _ = _windows(documents, blocks, config, manifest=manifest, contract_clause_indexes=indexes)
    return next(window for window in windows if window["window_id"] == target["window_id"])


def _candidate(window):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": ["a2", "a3"], "requirements": [
                {"citation": {"block_ids": ["integrity"]}, "category": "commercial", "response_mode": "procedure",
                 "format_status": "not_prescribed"}], "format_chapters": [], "format_status": "none_verified"}


def test_heading_scope_replays_without_changing_wire_or_requirements(tmp_path, monkeypatch):
    data = scenario(tmp_path, monkeypatch)
    config, bare = data[-2:]
    window = _bind(data)
    assert extraction_window_payload(window, config) == extraction_window_payload(bare, extraction_window_config({}))
    raw = _candidate(window)
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, bare)
    result = run_extraction_window(None, window, config, lambda *_: copy.deepcopy(raw))
    assert result["raw_model_candidate"] == raw
    assert result["live_confirmation"]["unresolved_block_ids"] == ["a2", "a3"]
    assert result["processing"]["unresolved_block_ids"] == ["a2", "a3"]
    assert [row["citation"]["block_id"] for row in result["validated"]["requirements"]] == ["integrity"]
    assert not result["validated"]["format_chapters"]
    resolution = result[annex.RESOLUTION_KEY]
    assert resolution["raw_candidate_sha256"] == digest(raw)
    assert resolution["resolved_block_ids"] == ["a2", "a3"]
    assert resolution["semantic_review"] == "requires_source_review"
    assert [[ref["number"] for ref in page["contract_references"]] for page in resolution["proof"]["pages"]] == [["1.1"], ["1.2"]]
    # Durable units redact credentials before checkpoint storage. The original
    # signing-page marker is source evidence and must survive this path intact.
    checkpoint = json.loads(json_bytes(redact_payload(result)))
    assert checkpoint == result
    assert checkpoint[annex.RESOLUTION_KEY]["proof"]["signing_page_boundary"]["marker"]["block_id"] == "sign"
    assert _validate_normalized_window_result(checkpoint, window) == result["validated"]
    copy_result = copy.deepcopy(result)
    del copy_result[annex.RESOLUTION_KEY]
    with pytest.raises(PlanningError, match="EXTRACTION_CONTRACT_HEADING_ONLY_ANNEX_RESOLUTION_MISMATCH"):
        _validate_normalized_window_result(copy_result, window)


@pytest.mark.parametrize("change", ["extra_line", "different_title", "no_reference", "no_signature",
                                    "no_neighbor_body", "unrelated_number", "unread"])
def test_incomplete_or_conflicting_source_blocks_resolution(tmp_path, monkeypatch, change):
    data = scenario(tmp_path, monkeypatch, change)
    with pytest.raises(PlanningError, match=annex.SOURCE_ERROR):
        _bind(data)


@pytest.mark.parametrize("change", ["unrelated_critical", "selected_heading", "missing_heading", "changed_heading"])
def test_only_reviewed_unselected_heading_targets_are_resolved(tmp_path, monkeypatch, change):
    window = _bind(scenario(tmp_path, monkeypatch))
    raw = _candidate(window)
    if change == "unrelated_critical":
        raw["unresolved_block_ids"].append("integrity")
    elif change == "selected_heading":
        raw["requirements"][0]["citation"]["block_ids"] = ["a2"]
    elif change == "missing_heading":
        raw["unresolved_block_ids"].pop()
    else:
        next(line for line in window["blocks"] if line["block_id"] == "a2")["text"] += "，投标时填写"
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("extra", ["", " 10 110 25 5 re S", " BT /F1 10 Tf 10 100 Td (Extra) Tj ET"])
def test_render_and_native_checks_require_only_two_proven_lines(tmp_path, extra):
    path = pdf(tmp_path / "layout.pdf", ["BT /F1 10 Tf 30 190 Td (Annex) Tj ET "
          "BT /F1 10 Tf 147 15 Td (2) Tj ET" + extra])
    with pdfplumber.open(path) as parsed:
        native = parsed.pages[0]
        lines = [{"text": text, "bbox": [min(c["x0"] for c in glyphs), min(c["top"] for c in glyphs),
                                        max(c["x1"] for c in glyphs), max(c["bottom"] for c in glyphs)]}
                 for text, glyphs in (("Annex", native.chars[:5]), ("2", native.chars[5:6]))]
        with pypdfium2.PdfDocument(path) as document:
            review = {"page": 1, "rendered_rgb_sha256": annex.rendered_page_rgb_sha256(path, 1)}
            page = document[0]
            if extra:
                with pytest.raises(ValueError, match=annex.SOURCE_ERROR):
                    annex._native_glyph_counts(native, lines)
                with pytest.raises(ValueError, match=annex.SOURCE_ERROR):
                    annex._raster_layout(page, review, lines)
            else:
                assert annex._native_glyph_counts(native, lines) == [5, 1]
                assert annex._raster_layout(page, review, lines)["native_text_objects"] == [1, 1]
                with pytest.raises(ValueError, match=annex.SOURCE_ERROR):
                    annex._raster_layout(page, {**review, "rendered_rgb_sha256": "0" * 64}, lines)


@pytest.mark.parametrize("keys", [
    (annex.SOURCE_CONFIG_KEY,),
    (replay_module.FailedContractHeadingOnlyAnnexReplay.replay_key,),
    (annex.SOURCE_CONFIG_KEY, replay_module.FailedContractHeadingOnlyAnnexReplay.replay_key),
])
def test_heading_replay_requires_prior_authenticated_recovery(tmp_path, keys):
    with pytest.raises(PipelineFailure, match="FAILED_CONTRACT_HEADING_ONLY_ANNEX_CONFIG_INVALID"):
        build_executor(tmp_path, {"pipeline": "automatic", "extraction": dict.fromkeys(keys, {})})


def test_each_earlier_recovered_checkpoint_receives_its_verified_predecessor(monkeypatch):
    gate = replay_module.FailedContractHeadingOnlyAnnexReplay.__new__(replay_module.FailedContractHeadingOnlyAnnexReplay)
    gate.unit_id = "stop"
    gate.prefix = ("first", "older-recovery", "recent-recovery", gate.unit_id)
    source_windows = {unit: {"window_id": unit, "blocks": [unit]} for unit in gate.prefix}
    enriched = {unit: {**source_windows[unit], "evidence": unit} for unit in gate.prefix[1:3]}
    gate.predecessor = SimpleNamespace(unit_id="recent-recovery", prefix=gate.prefix[:-1],
                                       enriched_windows=lambda: enriched)
    checkpoints = {unit: {"status": "complete", "window_id": unit} for unit in gate.prefix}
    gate.run = SimpleNamespace(unit=lambda _, unit: (None, {"output_hash": digest(checkpoints[unit])},
                                                    None, checkpoints[unit], None))
    gate.prior_checkpoint_hashes = {}
    reads, checks = [], []
    bindings = {f"extract_requirements:{unit}": {"unit_id": unit} for unit in gate.prefix}
    replay = SimpleNamespace(windows=source_windows, window_config={}, config={"bindings": bindings},
                             _load_entry=lambda binding, task, window: reads.append(binding["unit_id"]))

    def verified(_, _replay, unit, _entry, window, _invalid, *, predecessor_result):
        checks.append((unit, window.get("evidence"), predecessor_result and predecessor_result["window_id"]))
        return digest(checkpoints[unit])

    monkeypatch.setattr("company_bid.extraction.extraction_window_payload", lambda window, _config: window)
    monkeypatch.setattr(replay_module, "_verify_replayed_checkpoint", verified)
    assert gate.actual_units(replay) == ("stop",)
    for unit in gate.predecessor.prefix:
        gate.verify_success(replay, {"unit_id": unit}, {}, source_windows[unit])
    assert reads == ["first", "older-recovery"]
    assert checks == [("first", None, None), ("older-recovery", "older-recovery", "first"),
                      ("recent-recovery", "recent-recovery", "older-recovery")]

    checkpoints["older-recovery"]["tampered"] = True
    with pytest.raises(ProviderFailure, match="FAILED_CONTRACT_HEADING_ONLY_ANNEX_SOURCE_INVALID"):
        gate.verify_success(replay, {"unit_id": "recent-recovery"}, {}, source_windows["recent-recovery"])
    replay.windows = {**source_windows, "stop": source_windows["stop"] | {"blocks": []}}
    with pytest.raises(ProviderFailure, match="FAILED_CONTRACT_HEADING_ONLY_ANNEX_CONFIG_INVALID"):
        gate.actual_units(replay)
