"""Pure source pointers are pending rules, never substitutes for remote rules.

These offline candidates exercise the unchanged selection/closure validators.
They do not claim that validators can infer semantic completeness from an empty
but structurally valid owner response, or classify mixed substantive clauses.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from company_bid.extraction import (
    PlanningError, _close_deferred_context, _index, _windows,
    extract_requirements, validate_live_window,
)
from company_bid.planning import plan_bid
from test_reference_context import chapter_sources, named_sources
from test_source_selected_citations import Context, live_candidate, no_call


RULES = {
    "qualification": {"text": "须提供有效检测资质证书。", "category": "qualification",
                      "response_mode": "attachment", "attachment_keywords": ["检测资质证书"]},
    "technical": {"text": "应编写测点复核方案，说明异常复测方法。", "category": "technical",
                  "response_mode": "writing"},
    "commercial": {"text": "不含税响应报价不得超过50万元。", "category": "commercial",
                   "response_mode": "procedure"},
}


def source_case(tmp_path, *, named=False, kind="qualification"):
    manifest, parsed = named_sources(tmp_path) if named else chapter_sources(tmp_path)
    values = parsed["documents"][0]["units"][0]["blocks"]
    by_id = {value["block_id"]: value for value in values}
    table_id = "REFERENCED_TABLE" if named else "TABLE"
    pointer = "评审标准：见第一章。" if named else "评审标准：见本章后表。"
    by_id["TRIGGER"]["text"] = pointer
    by_id[table_id]["rows"] = [["评审项目", "原始标准"], [kind, RULES[kind]["text"]]]
    source = Path(manifest["sources"][0]["path"])
    source.write_text(json.dumps(values, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["sources"][0]["sha256"] = parsed["documents"][0]["sha256"] = digest
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, {"window_max_blocks": 1})
    pointer_window = next(window for window in windows if window["blocks"][0]["block_id"] == "TRIGGER")
    owner_window = next(window for window in windows if window["blocks"][0]["block_id"] == table_id)
    local_ids = {value["block_id"] for value in pointer_window["blocks"] + pointer_window["adjacent_context"]}
    assert table_id not in local_ids  # This table is strictly reading-context-only here.
    assert any(table_id in packet["member_block_ids"] for packet in pointer_window["reference_context"]["packets"])
    return manifest, parsed, blocks, windows, pointer_window, owner_window, table_id


def response(window, table_id, *, kind="qualification"):
    first = window["blocks"][0]
    raw = live_candidate({"window": window}, {"block_ids": [first["block_id"]]})
    raw["requirements"] = []
    if first["block_id"] == "TRIGGER":
        raw["requirements"] = [{"citation": {"block_ids": ["TRIGGER"]},
            "category": "procedure", "response_mode": "procedure", "format_status": "not_prescribed"}]
    elif first["block_id"] == table_id:
        rule = {key: copy.deepcopy(value) for key, value in RULES[kind].items() if key != "text"}
        raw["requirements"] = [{"citation": {"block_ids": [table_id]},
                                "format_status": "not_prescribed", **rule}]
    return raw


def run_case(tmp_path, *, named=False, kind="qualification", owner_state=None, unresolved=None):
    case = source_case(tmp_path, named=named, kind=kind)
    manifest, parsed, _, _, _, _, table_id = case
    context, requests = Context(), []

    def call(ctx, task, unit, payload, validate):
        window = payload["window"]
        requests.append(copy.deepcopy(window))
        first = window["blocks"][0]["block_id"]
        if first == table_id and owner_state == "unfinished":
            raise PlanningError("SYNTHETIC_POINTER_OWNER_NOT_FINISHED")
        if first == table_id and owner_state == "empty_response":
            return {}  # No validated receipt: run_extraction_window must reject it.
        raw = response(window, table_id, kind=kind)
        if first == unresolved:
            raw["unresolved_block_ids"] = [first]
        return validate(raw)

    output = extract_requirements(context, parsed, manifest, call, {"window_max_blocks": 1})
    return case, output, context, requests


@pytest.mark.parametrize("named", [False, True], ids=["same-chapter-table", "named-remote-chapter"])
@pytest.mark.parametrize("kind", list(RULES))
def test_pointer_and_independent_remote_rule_both_survive_full_n2_to_n3(tmp_path, named, kind):
    case, extracted, ctx, requests = run_case(tmp_path, named=named, kind=kind)
    manifest, parsed, blocks, _, pointer_window, owner_window, table_id = case
    frozen = copy.deepcopy(extracted)
    pointer, = [item for item in extracted["requirements"] if item["text"] == blocks["TRIGGER"]["text"]]
    remote, = [item for item in extracted["requirements"] if item["text"] == blocks[table_id]["text"]]
    assert len(extracted["requirements"]) == 2
    assert pointer["requirement_id"] != remote["requirement_id"]
    assert pointer["category"] == pointer["response_mode"] == "procedure"
    assert pointer["citations"][0]["block_ids"] == ["TRIGGER"]
    assert pointer["window_ids"] == [pointer_window["window_id"]]
    assert remote["citations"][0]["block_ids"] == [table_id]
    assert remote["window_ids"] == [owner_window["window_id"]]
    assert remote["response_mode"] == RULES[kind]["response_mode"]
    assert RULES[kind]["text"] in remote["text"]
    assert extracted["format_chapters"] == []
    assert extracted["deferred_context_closure"]["semantic_review"] == "requires_source_review"
    wire = next(value for value in requests if value["window_id"] == pointer_window["window_id"])
    assert any(RULES[kind]["text"] in packet["source_text"] for packet in wire["reference_context"]["packets"])
    assert ctx.captures["n2-coverage-report"]["coverage"]["completed_window_count"] == len(requests)

    planned = plan_bid(Context(), extracted, parsed, manifest, no_call, {"map_table_fields": False})
    assert extracted == frozen
    assert {item["requirement_id"] for item in planned["requirements"]} == {
        pointer["requirement_id"], remote["requirement_id"]}
    pointer_section, = [item for item in planned["sections"] if pointer["requirement_id"] in item["requirement_refs"]]
    assert pointer_section["render_body"] is False
    assert not any(pointer_section[key] for key in ("fixed_content", "writing_tasks", "table_refs", "attachment_refs", "fact_refs"))
    action, = [item for item in planned["review_actions"] if pointer["requirement_id"] in item["requirement_refs"]]
    assert action["status"] == "pending_manual"
    assert action["source_requirement"] == blocks["TRIGGER"]["text"]
    assert action["requirement_refs"] == [pointer["requirement_id"]]
    assert planned["tables"] == []  # A source evaluation table is not a bidder response form.
    assert planned["attachments"] == []
    remote_requirement = next(item for item in planned["requirements"] if item["requirement_id"] == remote["requirement_id"])
    assert remote_requirement["source_refs"] != next(item for item in planned["requirements"] if
        item["requirement_id"] == pointer["requirement_id"])["source_refs"]
    if kind == "qualification":
        assert any(remote["requirement_id"] in item["requirement_refs"] and item["severity"] == "missing"
                   for item in planned["issues"])
    elif kind == "technical":
        task, = [task for section in planned["sections"] for task in section["writing_tasks"]]
        assert task["requirement_refs"] == [remote["requirement_id"]]
        assert task["must_cover"] == [blocks[table_id]["text"]]
    else:
        owner_action, = [item for item in planned["review_actions"] if remote["requirement_id"] in item["requirement_refs"]]
        assert owner_action["status"] == "not_checked"
        assert owner_action["source_requirement"] == blocks[table_id]["text"]


@pytest.mark.parametrize("which", ["pointer", "table-owner"])
def test_reading_context_does_not_waive_an_unresolved_actual_target(tmp_path, which):
    unresolved = "TRIGGER" if which == "pointer" else "TABLE"
    with pytest.raises(PlanningError, match="LIVE_REQUIRED_BLOCK_UNRESOLVED"):
        run_case(tmp_path, unresolved=unresolved)


@pytest.mark.parametrize("abuse", ["citation-only", "citation-with-target", "unresolved", "format-anchor", "format-content"])
def test_reading_only_remote_id_cannot_be_borrowed_into_current_window(tmp_path, abuse):
    _, _, _, _, window, _, table_id = source_case(tmp_path)
    raw = response(window, table_id)
    if abuse.startswith("citation"):
        raw["requirements"][0]["citation"] = {"block_ids": [table_id] if abuse == "citation-only" else ["TRIGGER", table_id]}
        expected = "CITATION_SPAN_ID_INVALID"
    elif abuse == "unresolved":
        raw["unresolved_block_ids"] = [table_id]
        expected = "LIVE_UNRESOLVED_BLOCK_IDS_INVALID"
    else:
        anchor = table_id if abuse == "format-anchor" else "TRIGGER"
        raw["format_status"] = "specified"
        raw["format_chapters"] = [{"title": window["blocks"][0]["text"], "level": 1,
            "citation": {"block_ids": [anchor]}, "content_block_ids": [table_id], "requirement_indices": []}]
        expected = "CITATION_SPAN_ID_INVALID" if abuse == "format-anchor" else "FORMAT_CONTENT_REFERENCE_INVALID"
    with pytest.raises(PlanningError, match=expected):
        validate_live_window(raw, window)


@pytest.mark.parametrize("damage", ["missing-owner", "empty-owner", "running-owner", "all-results-missing"])
def test_saved_full_n2_closure_cannot_hide_missing_or_empty_owner_receipts(tmp_path, damage):
    case, output, _, _ = run_case(tmp_path)
    manifest, parsed, _, windows, _, owner_window, _ = case
    broken = copy.deepcopy(output)
    owner = next(item for item in broken["window_results"] if item["window_id"] == owner_window["window_id"])
    if damage == "missing-owner":
        broken["window_results"].remove(owner)
    elif damage == "empty-owner":
        owner.clear()
        owner["window_id"] = owner_window["window_id"]
    elif damage == "running-owner":
        owner["status"] = "running"
    else:
        broken["window_results"] = []
    with pytest.raises(PlanningError, match="DEFERRED_CONTEXT_OWNER_INCOMPLETE"):
        _close_deferred_context(windows, broken["window_results"])
    with pytest.raises(PlanningError, match="EXTRACTION_(DENOMINATOR_MISMATCH|COVERAGE_INCOMPLETE)"):
        plan_bid(Context(), broken, parsed, manifest, no_call, {"map_table_fields": False})


@pytest.mark.parametrize("owner_state,code", [("unfinished", "SYNTHETIC_POINTER_OWNER_NOT_FINISHED"),
                                            ("empty_response", "EXTRACTION_SCHEMA_INVALID")])
def test_full_n2_cannot_finish_while_remote_owner_has_no_valid_response(tmp_path, owner_state, code):
    with pytest.raises(PlanningError, match=code):
        run_case(tmp_path, owner_state=owner_state)
