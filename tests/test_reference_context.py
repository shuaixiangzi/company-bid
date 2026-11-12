"""Bounded chapter reading supplements preserve citation and processing fences."""
import copy
import hashlib
import json

import pytest

import company_bid.extraction as extraction
from company_bid.extraction import (
    PlanningError, _index, _reference_context_packets, _window_source_catalogs,
    _windows, extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, block, fixture, live_candidate, no_call, selected


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def chapter_sources(tmp_path):
    manifest, parsed = fixture(tmp_path)
    document = parsed["documents"][0]
    document["format"] = "pdf"
    block(parsed, "H0", "第一章 项目概况", origin="native")
    block(parsed, "BEFORE", "前章专属内容，不属于服务要求。", origin="native")
    block(parsed, "H1", "第二章 服务要求", origin="native")
    block(parsed, "TRIGGER", "应说明巡检方法，相关服务细则见后表。", origin="native")
    for index in range(8):
        block(parsed, f"FILL{index}", f"本章背景说明第{index}段。", origin="native")
    block(parsed, "TABLE", "", kind="table", origin="native", bbox=[40, 80, 510, 160],
          rows=[["服务项目", "工作内容"], ["月度例行设备点检", "保留  两个空格\n与单元格换行。"]],
          merges=[], column_widths=[2, 4], header_rows=[0])
    block(parsed, "REMOTE", "本章远端补充说明，保留原始标点及空格。", origin="native")
    ignored = block(parsed, "IGNORED", "本章冻结读取规则忽略的装饰文字。", origin="native")
    document["units"][0]["coverage"]["ignored_objects"] = [
        {"locator": ignored["locator"], "reason": "Synthetic decorative text, not requirement content."}]
    block(parsed, "H2", "第三章 商务条件", origin="native")
    block(parsed, "AFTER", "后章专属内容，不属于服务要求。", origin="native")
    return manifest, parsed


def prepared(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    blocks, _, documents = _index(parsed, manifest)
    windows, ledger = _windows(documents, blocks, {"window_max_blocks": 1})
    window = next(value for value in windows if value["blocks"][0]["block_id"] == "TRIGGER")
    return manifest, parsed, blocks, documents, windows, ledger, window


def raw_for(window):
    return live_candidate({"window": window}, selected(window["blocks"][0]))


def test_complete_same_chapter_is_compact_verbatim_and_not_duplicate_local_input(tmp_path):
    manifest, parsed, blocks, documents, windows, ledger, window = prepared(tmp_path)
    original = copy.deepcopy((manifest, parsed, blocks, documents))
    context = window["reference_context"]
    assert context["schema_version"] == "chapter-reading-context-v1"
    assert context["scope"] == "reading_only_not_citation_or_processing_coverage"
    assert context["unavailable"] == []
    packet, = context["packets"]
    assert packet["source_id"] == "T" and packet["source_sha256"] == manifest["sources"][0]["sha256"]
    assert packet["trigger_block_ids"] == ["TRIGGER"]
    expected_ids = ["H1", "TRIGGER", *[f"FILL{i}" for i in range(8)], "TABLE", "REMOTE", "IGNORED"]
    assert packet["member_block_ids"] == expected_ids
    assert packet["chapter_start"]["block_id"] == "H1"
    assert packet["chapter_end"]["block_id"] == "H2"
    local_ids = {value["block_id"] for value in window["blocks"] + window["adjacent_context"]}
    assert "TABLE" not in local_ids and "REMOTE" not in local_ids
    assert [value["block_id"] for value in packet["blocks"]] == [key for key in expected_ids if key not in local_ids]
    table = next(value for value in packet["blocks"] if value["block_id"] == "TABLE")
    assert table["text"] == "服务项目\t工作内容\n月度例行设备点检\t保留  两个空格\n与单元格换行。"
    assert table["text_basis"] == "table_rows_tsv_v1"
    for value in packet["blocks"]:
        assert {"block_id", "locator", "kind", "text", "ordinal"} <= set(value)
        assert not {"rows", "merges", "column_widths", "header_rows", "bbox", "image_ref"} & set(value)
    ignored = next(value for value in packet["blocks"] if value["block_id"] == "IGNORED")
    assert ignored["read_disposition"] == blocks["IGNORED"]["read_disposition"]
    assert [value["block_id"] for current in windows for value in current["blocks"]] == list(blocks)
    assert [key for unit in ledger for key in unit["block_ids"]] == list(blocks)
    assert len(windows) == len(blocks)
    targets, available, *_ = _window_source_catalogs(window)
    assert set(targets) == {"TRIGGER"} and set(available) == local_ids
    assert (manifest, parsed, blocks, documents) == original


def test_other_source_chapters_and_tables_are_never_added(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    other = copy.deepcopy(parsed["documents"][0])
    other.update(source_id="OTHER", sha256="b" * 64)
    for unit in other["units"]:
        unit.update(source_id="OTHER", unit_id="OTHER-" + unit["unit_id"])
        unit["coverage"].pop("ignored_objects", None)
        for value in unit["blocks"]:
            value.update(source_id="OTHER", block_id="OTHER-" + value["block_id"])
    parsed["documents"].append(other)
    manifest["sources"].append({**manifest["sources"][0], "source_id": "OTHER", "sha256": "b" * 64})
    manifest["source_roles"]["OTHER"] = "tender"
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": 1})
    window = next(value for value in windows if value["blocks"][0]["block_id"] == "TRIGGER")
    packet, = window["reference_context"]["packets"]
    assert packet["source_id"] == "T"
    assert all(not key.startswith("OTHER-") for key in packet["member_block_ids"])


def test_last_chapter_uses_end_of_document_without_guessing_an_end_heading(tmp_path):
    manifest, parsed = chapter_sources(tmp_path)
    parsed["documents"][0]["units"][0]["blocks"] = [
        value for value in parsed["documents"][0]["units"][0]["blocks"] if value["block_id"] not in {"H2", "AFTER"}]
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": 1})[0][3]
    packet, = window["reference_context"]["packets"]
    assert packet["chapter_end"] is None
    assert packet["member_block_ids"][-1] == "IGNORED"


@pytest.mark.parametrize("case", ["no_chapter", "dot_leader", "ellipsis_leader", "ocr_heading", "sentence_heading"])
def test_missing_reliable_chapter_boundary_is_explicit_not_guessed(tmp_path, case):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    if case != "no_chapter":
        title = {"dot_leader": "第一章 服务要求......4", "ellipsis_leader": "第一章 服务要求……4",
                 "ocr_heading": "第一章 服务要求", "sentence_heading": "第一章 服务要求。"}[case]
        block(parsed, "HEADING", title, origin="ocr" if case == "ocr_heading" else "native")
    block(parsed, "TRIGGER", "应说明巡检方法，相关服务细则见后表。", origin="native")
    blocks, _, documents = _index(parsed, manifest)
    window = next(value for value in _windows(documents, blocks, {"window_max_blocks": 1})[0]
                  if value["blocks"][0]["block_id"] == "TRIGGER")
    assert window["reference_context"]["packets"] == []
    unavailable, = window["reference_context"]["unavailable"]
    assert unavailable["trigger_block_ids"] == ["TRIGGER"]
    assert unavailable["reason"] == "chapter_boundary_unavailable"


@pytest.mark.parametrize("case", ["not_explicit", "attachment_only", "ocr", "table", "adjacent_only", "ignored"])
def test_only_native_explicit_target_paragraph_reference_triggers_supplement(tmp_path, case):
    _, _, blocks, documents, _, _, window = prepared(tmp_path)
    target = blocks["TRIGGER"]
    if case == "not_explicit":
        target["text"] = "应说明巡检方法并按要求填写表格。"
    elif case == "attachment_only":
        target["text"] = "应说明巡检方法，详见附件。"
    elif case == "ocr":
        target["origin"] = "ocr"
    elif case == "table":
        target["kind"] = "table"
    elif case == "ignored":
        target["read_disposition"] = {"status": "ignored_by_frozen_reader_rule"}
    else:
        window["blocks"] = [blocks["FILL0"]]
        window["adjacent_context"] = [target]
    if case != "adjacent_only":
        window["blocks"] = [target]
    context = _reference_context_packets(documents[0], list(blocks.values()), window)
    assert context["packets"] == context["unavailable"] == []


def test_capacity_limit_is_reported_without_sending_a_partial_chapter(tmp_path, monkeypatch):
    _, _, blocks, documents, _, _, window = prepared(tmp_path)
    monkeypatch.setattr(extraction, "_REFERENCE_CONTEXT_MAX_CHARS", 400)
    context = _reference_context_packets(documents[0], list(blocks.values()), window)
    assert context["packets"] == []
    unavailable, = context["unavailable"]
    assert unavailable["reason"] == "context_size_limit"
    assert unavailable["required_chars"] > 400
    assert unavailable["member_count"] == 13
    assert "blocks" not in unavailable
    assert "月度例行设备点检" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.parametrize("selection", ["context_only", "target_plus_context"])
def test_reading_context_is_never_a_citation_source(tmp_path, selection):
    _, _, blocks, _, _, _, window = prepared(tmp_path)
    raw = raw_for(window)
    raw["requirements"][0]["citation"] = selected(blocks["TABLE"]) if selection == "context_only" else selected(
        blocks["TRIGGER"], blocks["TABLE"])
    with pytest.raises(PlanningError):
        validate_live_window(raw, window)


def test_reading_context_does_not_expand_unresolved_ids_or_waive_target_unresolved(tmp_path):
    _, _, _, _, _, _, window = prepared(tmp_path)
    raw = raw_for(window)
    raw["unresolved_block_ids"] = ["TABLE"]
    with pytest.raises(PlanningError, match="LIVE_UNRESOLVED_BLOCK_IDS_INVALID"):
        validate_live_window(raw, window)
    raw["unresolved_block_ids"] = ["TRIGGER"]
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(raw, window)


def test_reference_alias_does_not_auto_ground_labels_from_the_remote_table(tmp_path):
    _, _, _, _, _, _, window = prepared(tmp_path)
    raw = raw_for(window)
    raw["requirements"][0]["reference_keywords"] = ["月度例行设备点检"]
    with pytest.raises(PlanningError, match="EXTRACTION_LABEL_NOT_GROUNDED"):
        validate_live_window(raw, window)


@pytest.mark.parametrize("case", ["proof", "source_hash", "remote_text", "duplicate_extra", "missing_member",
                                  "context_trigger", "chapter_start", "chapter_end", "local_text", "identity"])
def test_packet_tampering_is_rejected_even_after_refreshing_outer_context_hash(tmp_path, case):
    _, _, _, _, _, _, window = prepared(tmp_path)
    packet = window["reference_context"]["packets"][0]
    if case == "proof":
        packet["proof_sha256"] = "f" * 64
    elif case == "source_hash":
        packet["source_sha256"] = "f" * 64
    elif case == "remote_text":
        next(value for value in packet["blocks"] if value["block_id"] == "TABLE")["text"] += "伪造"
    elif case == "duplicate_extra":
        packet["blocks"].append(copy.deepcopy(packet["blocks"][0]))
    elif case == "missing_member":
        packet["member_block_ids"].remove("TABLE")
    elif case == "context_trigger":
        packet["trigger_block_ids"] = ["TABLE"]
    elif case == "chapter_start":
        packet["chapter_start"]["text"] = "第二章 伪造边界"
    elif case == "chapter_end":
        packet["chapter_end"]["ordinal"] += 1
    elif case == "local_text":
        window["blocks"][0]["text"] += "不得悄悄改写本段。"
    else:
        window["context_identity"]["sha256"] = "f" * 64
    if case not in {"proof", "identity"}:
        packet["proof_sha256"] = digest({key: value for key, value in packet.items() if key != "proof_sha256"})
    if case != "identity":
        window["context_identity"] = extraction._window_context_identity(window)
    with pytest.raises(PlanningError, match="WINDOW_REFERENCE_CONTEXT_MISMATCH"):
        _window_source_catalogs(window)


def test_remote_original_text_changes_context_identity_and_n3_rebuilds_it(tmp_path):
    manifest, parsed, _, _, _, _, original_window = prepared(tmp_path)
    requests = []

    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        raw = raw_for(window)
        if window["blocks"][0]["block_id"] != "TRIGGER":
            raw["requirements"] = []
        return validate(raw)

    output = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 1})
    assert len(requests) == len({value["block_id"] for unit in parsed["documents"][0]["units"] for value in unit["blocks"]})
    assert plan_bid(Context(), output, parsed, manifest, no_call, {})["requirements"]
    changed = copy.deepcopy(parsed)
    remote = next(value for value in changed["documents"][0]["units"][0]["blocks"] if value["block_id"] == "REMOTE")
    remote["text"] += "远端原文发生变化。"
    blocks, _, documents = _index(changed, manifest)
    rebuilt = next(value for value in _windows(documents, blocks, {"window_max_blocks": 1})[0]
                   if value["window_id"] == original_window["window_id"])
    assert rebuilt["blocks"] == original_window["blocks"]
    assert rebuilt["adjacent_context"] == original_window["adjacent_context"]
    assert rebuilt["context_identity"] != original_window["context_identity"]
    with pytest.raises(PlanningError, match="EXTRACTION_INPUT_MISMATCH"):
        plan_bid(Context(), output, changed, manifest, no_call, {})
    rebound = copy.deepcopy(output)
    rebound["parsed_hash"] = digest(changed)
    with pytest.raises(PlanningError, match="EXTRACTION_PROCESSING_LEDGER_MISMATCH"):
        plan_bid(Context(), rebound, changed, manifest, no_call, {})
