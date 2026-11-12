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


def named_sources(tmp_path, *, chapter_token="第一章", reference_token="第一章", reference_title=None):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    block(parsed, "REFERENCED_CHAPTER", chapter_token + " 投标人须知", origin="native")
    block(parsed, "REFERENCED_PROSE", "本章规定现场设备服务的原始条件。", origin="native")
    block(parsed, "REFERENCED_TABLE", "", kind="table", origin="native",
          rows=[["服务项目", "原始条件"], ["月度例行设备点检", "必须保存点检记录。"]],
          merges=[], column_widths=[2, 4], header_rows=[0])
    for index in range(8):
        block(parsed, f"OLD_FILL{index}", f"前章原始说明第{index}段。", origin="native")
    block(parsed, "CURRENT_CHAPTER", "第二章 服务要求", origin="native")
    title = "“" + reference_title + "”" if reference_title is not None else ""
    block(parsed, "TRIGGER", f"应说明巡检方法，具体服务细则见{reference_token}{title}。", origin="native")
    block(parsed, "CURRENT_BODY", "当前章节的其他一般说明。", origin="native")
    return manifest, parsed


def indexed_named_window(manifest, parsed):
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": 1})
    window = next(value for value in windows if value["blocks"][0]["block_id"] == "TRIGGER")
    return blocks, window


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
    assert packet["source_text"] == "\n".join(blocks[key]["text"] for key in expected_ids)
    assert packet["trigger_routes"] == [{"block_id": "TRIGGER", "basis": "same_chapter_table_reference"}]
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
    assert packet["reading_dispositions"] == [{"block_id": "IGNORED", "locator": ignored["locator"],
                                               "read_disposition": ignored["read_disposition"]}]
    assert [value["block_id"] for current in windows for value in current["blocks"]] == list(blocks)
    assert [key for unit in ledger for key in unit["block_ids"]] == list(blocks)
    assert len(windows) == len(blocks)
    targets, available, *_ = _window_source_catalogs(window)
    assert set(targets) == {"TRIGGER"} and set(available) == local_ids
    assert (manifest, parsed, blocks, documents) == original


def test_model_view_is_a_smaller_independent_copy_without_member_proof_blocks(tmp_path):
    _, _, _, _, _, _, window = prepared(tmp_path)
    original = copy.deepcopy(window)
    view = extraction._model_window(window)
    assert view["reference_context"]["representation"] == "reading_text_only_v1"
    packet, = view["reference_context"]["packets"]
    full_packet, = window["reference_context"]["packets"]
    assert set(packet) == {"source_id", "source_sha256", "trigger_block_ids", "trigger_routes", "chapter_start",
                           "chapter_end", "member_projection_sha256", "proof_sha256", "source_text",
                           "reading_dispositions", "member_count"}
    assert packet["member_count"] == len(full_packet["member_block_ids"])
    assert packet["source_text"] == full_packet["source_text"]
    assert packet["reading_dispositions"] == full_packet["reading_dispositions"]
    assert len(json.dumps(packet, ensure_ascii=False)) < len(json.dumps(full_packet, ensure_ascii=False))
    assert {key: value for key, value in view.items() if key != "reference_context"} == {
        key: value for key, value in window.items() if key != "reference_context"}
    packet["source_text"] += "模型视图独立改动。"
    packet["chapter_start"]["text"] += "边界独立改动。"
    packet["reading_dispositions"][0]["read_disposition"]["reason"] = "modified view only"
    view["blocks"][0]["text"] += "目标视图独立改动。"
    assert window == original
    _window_source_catalogs(window)


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


@pytest.mark.parametrize("chapter_token,reference_title,matches", [
    ("第一章", None, None), ("第一章", "投标人须知", True),
    ("第一章", "投标文件格式", False), ("第8章", None, None),
])
def test_explicit_chapter_number_reads_original_backward_chapter_and_retains_title_difference(
        tmp_path, chapter_token, reference_title, matches):
    manifest, parsed = named_sources(tmp_path, chapter_token=chapter_token, reference_token=chapter_token,
                                     reference_title=reference_title)
    blocks, window = indexed_named_window(manifest, parsed)
    packet, = window["reference_context"]["packets"]
    assert window["reference_context"]["unavailable"] == []
    assert packet["chapter_start"]["block_id"] == "REFERENCED_CHAPTER"
    assert packet["chapter_start"]["text"] == chapter_token + " 投标人须知"
    assert packet["chapter_end"]["block_id"] == "CURRENT_CHAPTER"
    assert "TRIGGER" not in packet["member_block_ids"]
    assert "CURRENT_BODY" not in packet["member_block_ids"]
    assert packet["trigger_routes"] == [{"block_id": "TRIGGER", "basis": "explicit_source_chapter_number",
                                          "chapter_token": chapter_token, "reference_title": reference_title,
                                          "reference_title_matches": matches}]
    assert packet["source_text"] == "\n".join(blocks[key]["text"] for key in packet["member_block_ids"])
    if reference_title == "投标文件格式":
        assert reference_title not in packet["source_text"]
    targets, available, *_ = _window_source_catalogs(window)
    assert set(targets) == {"TRIGGER"} and "REFERENCED_TABLE" not in available
    unresolved = raw_for(window)
    unresolved["unresolved_block_ids"] = ["TRIGGER"]
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        validate_live_window(unresolved, window)


@pytest.mark.parametrize("case", ["missing", "duplicate", "different_literal_number", "ocr_heading", "toc_heading"])
def test_named_chapter_reference_never_guesses_missing_ambiguous_or_alias_number(tmp_path, case):
    manifest, parsed = named_sources(tmp_path)
    values = parsed["documents"][0]["units"][0]["blocks"]
    heading = next(value for value in values if value["block_id"] == "REFERENCED_CHAPTER")
    if case == "missing":
        values.remove(heading)
    elif case == "duplicate":
        duplicate = block(parsed, "DUPLICATE", "第一章 同编号另一章原文", origin="native")
        values.remove(duplicate)
        values.insert(2, duplicate)
    elif case == "different_literal_number":
        heading["text"] = "第1章 投标人须知"
    elif case == "ocr_heading":
        heading["origin"] = "ocr"
    else:
        heading["text"] += "……4"
    _, window = indexed_named_window(manifest, parsed)
    assert window["reference_context"]["packets"] == []
    unavailable, = window["reference_context"]["unavailable"]
    assert unavailable == {"trigger_block_ids": ["TRIGGER"], "chapter_token": "第一章",
                           "reason": "referenced_chapter_not_unique"}


def test_named_title_difference_is_reading_context_without_creating_response_formats(tmp_path):
    manifest, parsed = named_sources(tmp_path, reference_title="投标文件格式")
    _, full_window = indexed_named_window(manifest, parsed)
    requests = []
    ctx = Context()

    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        raw = raw_for(window)
        if window["blocks"][0]["block_id"] != "TRIGGER":
            raw["requirements"] = []
        return validate(raw)

    extracted = extract_requirements(ctx, parsed, manifest, call, {"window_max_blocks": 1})
    wire = next(value for value in requests if value["window_id"] == full_window["window_id"])
    assert wire == extraction._model_window(full_window)
    assert wire["reference_context"]["packets"][0]["trigger_routes"][0]["reference_title_matches"] is False
    assert len(extracted["requirements"]) == 1
    assert extracted["requirements"][0]["text"] == full_window["blocks"][0]["text"]
    assert extracted["format_chapters"] == []
    planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {})
    assert planned["tables"] == []
    assert all(section["fixed_content"] == [] for section in planned["sections"])
    assert ctx.captures["n2-chapter-reading-context"]["packets"] == full_window["reference_context"]["packets"]
    changed = copy.deepcopy(parsed)
    changed["documents"][0]["units"][0]["blocks"][0]["text"] = "第一章 投标须知原题已改变"
    rebound = copy.deepcopy(extracted)
    rebound["parsed_hash"] = digest(changed)
    with pytest.raises(PlanningError, match="EXTRACTION_PROCESSING_LEDGER_MISMATCH"):
        plan_bid(Context(), rebound, changed, manifest, no_call, {})


def test_named_reference_cannot_claim_a_different_title_matches_the_source(tmp_path):
    manifest, parsed = named_sources(tmp_path, reference_title="投标文件格式")
    _, window = indexed_named_window(manifest, parsed)
    packet, = window["reference_context"]["packets"]
    packet["trigger_routes"][0]["reference_title_matches"] = True
    packet["proof_sha256"] = digest({key: value for key, value in packet.items() if key != "proof_sha256"})
    window["context_identity"] = extraction._window_context_identity(window)
    with pytest.raises(PlanningError, match="WINDOW_REFERENCE_CONTEXT_MISMATCH"):
        _window_source_catalogs(window)


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


def test_capacity_limit_counts_model_view_instead_of_private_membership_proof(tmp_path, monkeypatch):
    _, _, blocks, documents, _, _, window = prepared(tmp_path)
    packet, = window["reference_context"]["packets"]
    view_packet = extraction._model_window(window)["reference_context"]["packets"][0]
    model_size = len(json.dumps(view_packet, ensure_ascii=False))
    assert len(json.dumps(packet, ensure_ascii=False)) > model_size
    monkeypatch.setattr(extraction, "_REFERENCE_CONTEXT_MAX_CHARS", model_size)
    context = _reference_context_packets(documents[0], list(blocks.values()), window)
    assert context["packets"] == [packet] and context["unavailable"] == []
    monkeypatch.setattr(extraction, "_REFERENCE_CONTEXT_MAX_CHARS", model_size - 1)
    context = _reference_context_packets(documents[0], list(blocks.values()), window)
    assert context["packets"] == []
    unavailable, = context["unavailable"]
    assert unavailable["reason"] == "context_size_limit" and unavailable["required_chars"] == model_size
    assert "source_text" not in unavailable and "blocks" not in unavailable


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


@pytest.mark.parametrize("case", ["proof", "source_hash", "remote_text", "source_text", "dispositions",
                                  "duplicate_extra", "missing_member", "context_trigger", "route_basis",
                                  "chapter_start", "chapter_end", "local_text", "identity"])
def test_packet_tampering_is_rejected_even_after_refreshing_outer_context_hash(tmp_path, case):
    _, _, _, _, _, _, window = prepared(tmp_path)
    packet = window["reference_context"]["packets"][0]
    if case == "proof":
        packet["proof_sha256"] = "f" * 64
    elif case == "source_hash":
        packet["source_sha256"] = "f" * 64
    elif case == "remote_text":
        next(value for value in packet["blocks"] if value["block_id"] == "TABLE")["text"] += "伪造"
    elif case == "source_text":
        packet["source_text"] += "伪造拼接原文。"
    elif case == "dispositions":
        packet["reading_dispositions"] = []
    elif case == "duplicate_extra":
        packet["blocks"].append(copy.deepcopy(packet["blocks"][0]))
    elif case == "missing_member":
        packet["member_block_ids"].remove("TABLE")
    elif case == "context_trigger":
        packet["trigger_block_ids"] = ["TABLE"]
    elif case == "route_basis":
        packet["trigger_routes"][0]["basis"] = "automatically_resolved_reference"
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
    ctx = Context()

    def call(ctx, task_type, unit_id, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        raw = raw_for(window)
        if window["blocks"][0]["block_id"] != "TRIGGER":
            raw["requirements"] = []
        return validate(raw)

    output = extract_requirements(ctx, parsed, manifest, call, {"window_max_blocks": 1})
    assert len(requests) == len({value["block_id"] for unit in parsed["documents"][0]["units"] for value in unit["blocks"]})
    wire_window = next(value for value in requests if value["window_id"] == original_window["window_id"])
    assert wire_window == extraction._model_window(original_window)
    assert ctx.units[original_window["window_id"]][0]["window"] == wire_window
    captured = ctx.captures["n2-chapter-reading-context"]
    assert captured["packets"] == original_window["reference_context"]["packets"]
    assert captured["context_identity"] == original_window["context_identity"]
    assert "representation" not in captured
    assert "member_block_ids" in captured["packets"][0] and "blocks" in captured["packets"][0]
    assert "member_block_ids" not in wire_window["reference_context"]["packets"][0]
    assert "blocks" not in wire_window["reference_context"]["packets"][0]
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
