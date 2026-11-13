"""A shared requirement must survive on each assembled table, not just once."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.sample import create_synthetic_sample


@pytest.fixture
def assembled(tmp_path):
    data = create_synthetic_sample(tmp_path / "source").model_dump(mode="json")
    data["tables"][1]["requirement_refs"].append("R-001")
    data["sections"][2]["requirement_refs"].append("R-001")
    runner = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}, "debug_capture": True})
    run = runner.create_run({"prepared": data, "proposals": {"S2": {
        "paragraphs": ["拟核对作业许可并编号检查点，记录异常值和复测结果后交付。"], "steps": []}}})
    assert runner.execute(run, stop_after="N4")["status"] == "paused_debug"
    value = runner.output(run, "N4")
    handler = next(node.handler for node in runner.nodes if node.key == "N5")
    captures = []
    ctx = SimpleNamespace(execution_config=runner.execution_config,
                          capture=lambda name, payload: captures.append((name, payload)))
    return runner, run, value, handler, ctx, captures


def tables(value):
    return [block["table"] for section in value["package"]["sections"]
            for block in section["blocks"] if block["kind"] == "table"]


def test_shared_requirement_survives_both_tables_and_is_captured(assembled):
    runner, run, value, handler, ctx, captures = assembled
    before = deepcopy(value)
    result = handler(ctx, value)
    links = result["validation"]["table_requirement_links"]
    assert len(links) == 2
    assert all("R-001" in link["requirement_refs"] for link in links)
    assert all(link["satisfaction_status"] == "not_determined" for link in links)
    assert captures == [("validation", result["validation"])]
    assert value == before == runner.output(run, "N4")
    assert runner.resume(run, stop_after="N6")["status"] == "succeeded"
    assert runner.output(run)["validation"]["table_requirement_links"] == links


@pytest.mark.parametrize("mutation", ["drop_shared", "replace", "reorder", "metadata_section", "host_section",
                                         "consistent_move", "host_requirement", "missing_expectation", "empty_expectations", "malformed_expectations"])
def test_n5_rejects_lost_or_changed_association_with_same_physical_table_ids(assembled, mutation):
    runner, run, original, handler, ctx, captures = assembled
    value = deepcopy(original)
    second = tables(value)[1]
    if mutation == "drop_shared":
        second["requirement_refs"].remove("R-001")
    elif mutation == "replace":
        second["requirement_refs"] = ["R-004", "R-002"]
    elif mutation == "reorder":
        second["requirement_refs"].reverse()
    elif mutation == "metadata_section":
        second["section_id"] = "S1"
    elif mutation == "host_section":
        sections = value["package"]["sections"]
        block = next(block for block in sections[2]["blocks"] if block["kind"] == "table")
        sections[2]["blocks"].remove(block)
        sections[0]["blocks"].append(block)
    elif mutation == "consistent_move":
        sections = value["package"]["sections"]
        block = next(block for block in sections[2]["blocks"] if block["kind"] == "table")
        sections[2]["blocks"].remove(block)
        sections[0]["blocks"].append(block)
        second["section_id"] = sections[0]["section_id"]
        sections[0]["requirement_refs"] = list(dict.fromkeys([*sections[0]["requirement_refs"], *second["requirement_refs"]]))
    elif mutation == "host_requirement":
        value["package"]["sections"][2]["requirement_refs"].remove("R-001")
    elif mutation == "missing_expectation":
        del value["expected_table_links"]["T-PRICE"]
    elif mutation == "empty_expectations":
        value["expected_table_links"] = {}
    else:
        value["expected_table_links"] = []
    assert {table["table_id"] for table in tables(value)} == set(value["expected_table_ids"])
    error = ValidationError if mutation in {"metadata_section", "host_section"} else PipelineFailure
    message = "table must occur in its declared section" if error is ValidationError else "TABLE_REQUIREMENT_LINK_MISMATCH"
    with pytest.raises(error, match=message):
        handler(ctx, value)
    assert not captures
    assert runner.output(run, "N4") == original


def test_automatic_input_requires_expectations(assembled):
    _, _, value, handler, ctx, _ = assembled
    value["input_kind"] = "automatic"
    value.pop("expected_table_links")
    with pytest.raises(PipelineFailure, match="TABLE_REQUIREMENT_LINKS_MISSING"):
        handler(ctx, value)


def test_legacy_prepared_input_exposes_missing_association_check(assembled):
    _, _, value, handler, ctx, _ = assembled
    value.pop("expected_table_links")
    assert handler(ctx, value)["validation"]["table_requirement_links_status"] == "not_recorded"


def test_outer_block_may_reference_fewer_requirements_than_inner_source_table(assembled):
    _, _, value, handler, ctx, _ = assembled
    for section in value["package"]["sections"]:
        for block in section["blocks"]:
            if block["kind"] == "table" and block["table"]["table_id"] == "T-PRICE":
                block["requirement_refs"] = ["R-004"]
    assert handler(ctx, value)["validation"]["table_requirement_links_status"] == "checked"
