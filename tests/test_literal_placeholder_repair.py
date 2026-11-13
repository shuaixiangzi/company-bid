"""Read literal source notes remain source-bound and pending semantic review.

Synthetic HTTP exercises the production executor, accounting and trace path;
no real model, historical run, credential or source document is accessed.
"""
import copy
import hashlib
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import (
    _index, _model_window, _windows, extraction_window_payload,
    source_selected_output_contract, validate_live_window,
)
from company_bid.runtime import Executor
from company_bid.storage import digest
from company_bid.structured import _literal_placeholder_resolution_groups, _repair_coverage
from test_repair_semantic_inventory import run_repair
from test_source_selected_citations import fixture, live_candidate
from test_structured_validation_repair import artifacts, response, setup_run
from test_targeted_validation_repair import native_line, assert_two_distinct_settled_calls


def literal_case(tmp_path, *, block_limit=60):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for key, text in [
        ("FEE", "响应人自行承担服务费用，不得另行增加。"),
        ("PROOF", "须提供有效检测资质证书。"),
        ("TITLE", "5.2 开启程序"),
        ("LITERAL", "/"),
        ("NEXT", "5.3递交响应文件的响应人不足的情形"),
        ("AFTER", "响应人不足三家时，须按原规定处理，不得自行变更条件。"),
    ]:
        native_line(parsed, key, text)
    blocks, _, documents = _index(parsed, manifest)
    window = _windows(documents, blocks, {"window_max_blocks": block_limit})[0][0]
    first = live_candidate({"window": window}, {"block_ids": ["FEE"]})
    first["requirements"] = [
        {"citation": {"block_ids": ["FEE"]}, "category": "commercial", "response_mode": "procedure",
         "format_status": "not_prescribed"},
        {"citation": {"block_ids": ["PROOF"]}, "category": "qualification", "response_mode": "attachment",
         "format_status": "not_prescribed", "attachment_keywords": ["检测资质证书"]},
    ]
    first["unresolved_block_ids"] = ["LITERAL"]
    second = copy.deepcopy(first)
    second["unresolved_block_ids"] = []
    second["requirements"].append({"citation": {"block_ids": ["TITLE", "LITERAL"]},
        "category": "procedure", "response_mode": "procedure", "mandatory": False, "confidence": "low",
        "format_status": "not_prescribed", "format_title": None, "required_fact_labels": [],
        "attachment_keywords": [], "reference_keywords": []})
    return window, first, second


@pytest.mark.parametrize("block_limit", [4, 60], ids=["boundary-is-adjacent", "all-targets"])
def test_literal_note_requires_exact_source_append_and_keeps_real_obligations(tmp_path, monkeypatch, block_limit):
    window, first, second = literal_case(tmp_path, block_limit=block_limit)
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "succeeded" and validated == [first, second]
    assert relation["schema_version"] == "validation-repair-v7" and relation["status"] == "completed"
    assert relation["first_candidate_hash"] == digest(first)
    assert relation["second_candidate_hash"] == digest(second)
    assert relation["first_request_hash"] != relation["repair_request_hash"]
    assert_two_distinct_settled_calls(state, relation)
    group, = feedback["required_coverage"]["resolution_groups"]
    assert group["resolution_kind"] == "read_literal_source_note"
    assert group["block_ids"] == ["TITLE", "LITERAL"] and group["required_target_ids"] == ["LITERAL"]
    proof = group["source_proof"]
    assert proof["source_sha256"] == window["source_sha256"] and proof["window_id"] == window["window_id"]
    assert proof["semantic_review"] == "requires_source_review"
    assert group["source_proof_sha256"] == digest(proof)
    assert [block["block_id"] for block in proof["blocks"]] == ["TITLE", "LITERAL", "NEXT"]
    source = {block["block_id"]: block for block in window["blocks"] + window["adjacent_context"]}
    assert all(row["text_sha256"] == hashlib.sha256(source[row["block_id"]]["text"].encode()).hexdigest()
               for row in proof["blocks"])
    final = validate_live_window(second, window)
    assert final["requirements"][-1]["citation"]["quote"] == "5.2 开启程序\n/"
    assert final["requirements"][-1]["mandatory"] is False
    assert second["requirements"][:-1] == first["requirements"]
    assert "不证明无要求、不适用或免除义务" in feedback["instructions"]


@pytest.mark.parametrize("damage", [
    "just_clear", "only_literal", "only_title", "include_next_heading", "include_substantive_body", "reverse_source",
    "change_old_source", "drop_old", "insert_before_old", "swap_old", "change_old_category", "drop_old_labels",
    "change_old_default", "new_mandatory", "default_mandatory", "new_confidence", "new_category", "new_mode",
    "new_format", "new_title", "fact_label", "attachment_label", "reference_label", "repeat_note", "keep_unresolved",
])
def test_note_cannot_clear_flags_rewrite_old_business_or_invent_obligations(tmp_path, monkeypatch, damage):
    window, first, second = literal_case(tmp_path)
    note = second["requirements"][-1]
    if damage == "just_clear":
        second["requirements"].pop()
    elif damage in {"only_literal", "only_title", "include_next_heading", "include_substantive_body", "reverse_source"}:
        note["citation"]["block_ids"] = {
            "only_literal": ["LITERAL"], "only_title": ["TITLE"],
            "include_next_heading": ["TITLE", "LITERAL", "NEXT"],
            "include_substantive_body": ["TITLE", "LITERAL", "NEXT", "AFTER"],
            "reverse_source": ["LITERAL", "TITLE"],
        }[damage]
    elif damage == "change_old_source":
        second["requirements"][0]["citation"]["block_ids"] = ["PROOF"]
    elif damage == "drop_old":
        second["requirements"].pop(0)
    elif damage == "insert_before_old":
        second["requirements"].insert(0, second["requirements"].pop())
    elif damage == "swap_old":
        second["requirements"][:2] = reversed(second["requirements"][:2])
    elif damage == "change_old_category":
        second["requirements"][0]["category"] = "procedure"
    elif damage == "drop_old_labels":
        second["requirements"][1]["attachment_keywords"] = []
    elif damage == "change_old_default":
        second["requirements"][0]["mandatory"] = False
    elif damage == "new_mandatory":
        note["mandatory"] = True
    elif damage == "default_mandatory":
        note.pop("mandatory")
    elif damage == "new_confidence":
        note["confidence"] = "high"
    elif damage == "new_category":
        note["category"] = "commercial"
    elif damage == "new_mode":
        note["response_mode"] = "factual_response"
    elif damage == "new_format":
        note["format_status"] = "prescribed"
    elif damage == "new_title":
        note["format_title"] = "开启程序"
    elif damage in {"fact_label", "attachment_label", "reference_label"}:
        field = {"fact_label": "required_fact_labels", "attachment_label": "attachment_keywords",
                 "reference_label": "reference_keywords"}[damage]
        note[field] = ["开启程序"]
    elif damage == "repeat_note":
        second["requirements"].append(copy.deepcopy(note))
    else:
        second["unresolved_block_ids"] = ["LITERAL"]
    state, _, relation, _ = run_repair(tmp_path, monkeypatch, window, first, second)
    assert state["status"] == "failed" and state["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST"
    assert relation["status"] == "failed"
    assert_two_distinct_settled_calls(state, relation)


@pytest.mark.parametrize("damage", [
    "docx", "missing_format", "missing_hash", "zero_hash", "ocr", "missing_origin", "image", "table",
    "foreign_source", "other_page", "wrong_locator_page", "line_gap", "ordinal_gap", "unit_gap", "other_unit",
    "no_title", "no_boundary", "boundary_number_gap", "boundary_parent", "same_number", "substantive_title",
    "substantive_body", "ambiguous_body", "spaced_slash", "unread_literal", "unread_title", "unread_boundary",
    "safe_skip_literal", "duplicate_id", "extra_unresolved", "root_unread", "old_unread",
])
def test_literal_admission_rejects_missing_uncertain_or_substantive_source(tmp_path, damage):
    window, first, _ = literal_case(tmp_path)
    source = {block["block_id"]: block for block in window["blocks"] + window["adjacent_context"]}
    literal = source["LITERAL"]
    if damage == "docx":
        window["source_format"] = "docx"
    elif damage == "missing_format":
        window.pop("source_format")
    elif damage == "missing_hash":
        window.pop("source_sha256")
    elif damage == "zero_hash":
        window["source_sha256"] = "0" * 64
    elif damage in {"ocr", "missing_origin"}:
        literal["origin"] = "ocr" if damage == "ocr" else None
    elif damage in {"image", "table"}:
        literal["kind"] = damage
    elif damage == "foreign_source":
        literal["source_id"] = "FOREIGN"
    elif damage == "other_page":
        source["NEXT"].update(page=2, locator="pdf/page[2]/line[4]")
    elif damage == "wrong_locator_page":
        literal["locator"] = "pdf/page[2]/line[3]"
    elif damage == "line_gap":
        source["NEXT"]["locator"] = "pdf/page[1]/line[5]"
    elif damage == "ordinal_gap":
        source["NEXT"]["ordinal"] += 10
    elif damage == "unit_gap":
        source["NEXT"]["unit_ordinal"] += 10
    elif damage == "other_unit":
        source["NEXT"]["unit_id"] = "OTHER"
    elif damage in {"no_title", "no_boundary"}:
        key = "TITLE" if damage == "no_title" else "NEXT"
        window["blocks"] = [block for block in window["blocks"] if block["block_id"] != key]
    elif damage in {"boundary_number_gap", "boundary_parent", "same_number"}:
        source["NEXT"]["text"] = {"boundary_number_gap": "5.4 后续程序", "boundary_parent": "6.3 后续程序",
                                   "same_number": "5.2 后续程序"}[damage]
    elif damage == "substantive_title":
        source["TITLE"]["text"] = "5.2 必须提供资质证书"
    elif damage in {"substantive_body", "ambiguous_body", "spaced_slash"}:
        literal["text"] = {"substantive_body": "须提供/不得省略", "ambiguous_body": "待补", "spaced_slash": " / "}[damage]
    elif damage.startswith("unread_"):
        key = {"unread_literal": "LITERAL", "unread_title": "TITLE", "unread_boundary": "NEXT"}[damage]
        source[key]["read_disposition"] = {"status": "unread"}
    elif damage == "safe_skip_literal":
        window["safe_skip_blocks"].append({"block_id": "LITERAL"})
    elif damage == "duplicate_id":
        window["adjacent_context"].append(copy.deepcopy(literal))
    elif damage == "extra_unresolved":
        first["unresolved_block_ids"].append("AFTER")
    elif damage == "root_unread":
        first["format_status"] = "unread"
    else:
        first["requirements"][0]["format_status"] = "unread"
    assert _literal_placeholder_resolution_groups(first, window) is None


@pytest.mark.parametrize("damage", ["substantive_body", "unread", "bad_old_source"])
def test_disallowed_literal_cases_do_not_schedule_a_second_call(tmp_path, monkeypatch, damage):
    window, first, second = literal_case(tmp_path)
    if damage == "substantive_body":
        next(block for block in window["blocks"] if block["block_id"] == "LITERAL")["text"] = "须提交证书。"
    elif damage == "unread":
        next(block for block in window["blocks"] if block["block_id"] == "LITERAL")["read_disposition"] = {"status": "unread"}
    else:
        first["requirements"][0]["citation"]["block_ids"] = ["FEE", "TITLE"]
    state, validated, relation, feedback = run_repair(tmp_path, monkeypatch, window, first, second, expected_calls=1)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert validated == [first] and relation is feedback is None


def test_failed_literal_correction_preserves_trace_and_does_not_buy_a_third_response_after_restart(tmp_path, monkeypatch):
    window, first, second = literal_case(tmp_path)
    second["requirements"].pop()
    executor, rid, _, payload, _, sent, _, _ = setup_run(
        tmp_path, monkeypatch, max_input_chars=50000, validate=lambda value, _: validate_live_window(value, window),
        transport=lambda _, count: response(first if count == 1 else second))
    payload.update(window=_model_window(window), output_contract=source_selected_output_contract())
    initial = executor.execute(rid)
    assert initial["error_code"] == "VALIDATION_REPAIR_COVERAGE_LOST" and len(sent) == 2
    sealed = [(record["ref"], record["hash"]) for record, _ in artifacts(executor, initial)]
    budget = copy.deepcopy(initial["call_budget"]["reservations"])
    restarted = Executor(executor.store.root, executor.nodes, executor.execution_config)
    state = restarted.resume(rid)
    assert state["error_code"] == "VALIDATION_REPAIR_ALREADY_ATTEMPTED" and len(sent) == 2
    assert state["call_budget"]["reservations"] == budget
    assert all(hashlib.sha256((executor.store.root / ref["path"]).read_bytes()).hexdigest() == sha for ref, sha in sealed)
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and not report["gaps"]


def test_prompt_distinguishes_read_literal_from_missing_content_without_granting_an_exemption(tmp_path):
    window, first, _ = literal_case(tmp_path)
    request = extraction_window_payload(window, {})
    assert request["producer_version"] == "requirements-window-v21"
    assert "不是读取失败，也不证明无要求、不适用或免除义务" in request["task_instructions"]
    coverage = _repair_coverage(first, request, error_code="LIVE_REQUIRED_BLOCK_UNRESOLVED")
    assert coverage["original_unresolved_block_ids"] == ["LITERAL"]
    assert coverage["entries"]["requirements"][0]["protected_fields"]["mandatory"] is True
