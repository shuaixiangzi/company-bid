"""Selected project choices need native source and concrete task bindings."""
import copy
import json

import pytest
from pydantic import ValidationError

from company_bid.project_constraints import (
    ProjectConstraintError, ProjectConstraintSet, TaskProjectConstraints,
    bind_project_constraints_to_task, extract_project_constraints,
    project_constraint_trace_attributes, validate_project_constraint_sources,
    validate_project_constraint_task_sources, validate_task_project_constraints,
)
from company_bid.table_continuations import build_table_row_catalog


SOURCE_HASH = "a" * 64


def fixture():
    table = {"block_id": "TABLE", "source_id": "TENDER", "kind": "table", "origin": "native",
             "locator": "pdf/page[8]/table[0]", "text": "",
             "rows": [["条款号", "条款名称", "编列内容"], ["1.9", "分包", "☑不允许"],
                      ["1.10.2", "对非关键条款的偏差", "☑不允许偏差"], ["2.1", "其他要求", "保留其它行"]],
             "column_widths": [50.0, 120.0, 280.0], "merges": [], "header_rows": []}
    unit = {"unit_id": "PAGE8", "source_id": "TENDER", "locator": "pdf/page[8]", "page": 8,
            "blocks": [table], "coverage": {"status": "read", "unread_objects": [], "unmapped_glyphs": []}}
    document = {"source_id": "TENDER", "sha256": SOURCE_HASH, "format": "pdf", "role": "tender", "units": [unit]}
    parsed = {"documents": [document]}
    return parsed, requirements_for(parsed)


def requirements_for(parsed):
    document = parsed["documents"][0]
    blocks = []
    for unit in document["units"]:
        for original in unit["blocks"]:
            block = copy.deepcopy(original)
            block.update(unit_id=unit["unit_id"], page=unit["page"], ordinal=len(blocks))
            block["text_basis"] = "table_rows_tsv_v1"
            block["text"] = "\n".join("\t".join(row) for row in block["rows"])
            blocks.append(block)
    requirements = []
    for index, row in enumerate(build_table_row_catalog(document, blocks, [])):
        citation = {"source_id": row["source_id"], "block_id": row["block_id"], "block_ids": row["block_ids"],
            "locator": row["locator"], "quote": row["quote"], "quote_sha256": row["quote_sha256"],
            "basis": "frozen_table_rows_tsv_v1", "unit_id": "PAGE8", "page": 8,
            "table_row_id": row["table_row_id"], "table_row_proof_sha256": row["proof_sha256"],
            "table_row_parts": row["parts"], "table_row_parents": row["tables"], "cell_refs": row["cell_refs"],
            "start": 0, "end": len(row["quote"])}
        requirements.append({"requirement_id": f"R-{index}", "text": row["quote"], "citations": [citation],
            "category": "commercial", "response_mode": "procedure", "format_status": "not_prescribed"})
    # A distant technical method and a conditional clause remain independent.
    requirements.extend([
        {"requirement_id": "R-METHOD", "text": "应编制项目巡检方法。", "category": "technical", "response_mode": "writing",
         "citations": [{"source_id": "TENDER", "block_id": "METHOD", "locator": "pdf/page[40]/line[1]", "quote": "应编制项目巡检方法。"}]},
        {"requirement_id": "R-CONDITIONAL", "text": "拟分包的，应按前附表规定作出说明。", "category": "commercial",
         "response_mode": "factual_response", "citations": [{"source_id": "TENDER", "block_id": "CLAUSE"}]},
    ])
    return requirements


def extract(parsed, requirements):
    return extract_project_constraints(parsed, requirements, tender_source_id="TENDER", tender_source_sha256=SOURCE_HASH)


def task_fixture():
    parsed, requirements = fixture()
    constraints = extract(parsed, requirements)
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    task = {"unit_id": "method-1", "requirement_refs": ["R-METHOD"],
            "forbidden_fields": ["禁止编造公司事实", *[item.forbidden_field for item in binding.constraints]]}
    return parsed, requirements, constraints, binding, task


def test_complete_native_rows_cells_grid_and_distinct_target_links_are_frozen():
    parsed, requirements = fixture()
    before = copy.deepcopy((parsed, requirements))
    result = extract(parsed, requirements)
    assert not result.unresolved
    assert [item.operator for item in result.constraints] == ["prohibit_subcontracting", "prohibit_noncritical_deviation"]
    first, second = result.constraints
    assert first.source.row == 1 and second.source.row == 2
    assert first.source.quote == "1.9\t分包\t☑不允许"
    assert [cell.text for cell in first.source.cells] == ["1.9", "分包", "☑不允许"]
    assert first.source.parent_grid["rows"] == parsed["documents"][0]["units"][0]["blocks"][0]["rows"]
    assert first.source.parent_grid["rows"][-1][-1] == "保留其它行"
    assert first.source.source_sha256 == SOURCE_HASH
    assert result.schema_version == "project-constraints-v2"
    assert len(result.source_reading_sha256) == len(result.source_set_sha256) == 64
    assert first.source_requirements[0].requirement_id != second.source_requirements[0].requirement_id
    assert all(item.review_status == "not_checked" for item in result.constraints)
    assert "公司已符合" in first.instruction and "不能" in first.instruction
    assert (parsed, requirements) == before
    assert validate_project_constraint_sources(result.model_dump(mode="json"), parsed, requirements,
        tender_source_id="TENDER", tender_source_sha256=SOURCE_HASH) == result


@pytest.mark.parametrize("operator,row,choice,reason", [
    ("prohibit_subcontracting", 1, "□不允许\n□允许", "not_selected"),
    ("prohibit_subcontracting", 1, "□不允许\n☑允许", "allowance_not_supported"),
    ("prohibit_subcontracting", 1, "☑不允许\n☑允许", "conflicting_options"),
    ("prohibit_subcontracting", 1, "☑不允许，但经审批可以", "ambiguous_options"),
    ("prohibit_subcontracting", 1, "不允许", "ambiguous_options"),
    ("prohibit_subcontracting", 1, "☑不允许\n□不允许", "ambiguous_options"),
    ("prohibit_noncritical_deviation", 2, "□不允许偏差\n☑允许偏差", "allowance_not_supported"),
    ("prohibit_noncritical_deviation", 2, "☑不允许偏差\n☑允许偏差", "conflicting_options"),
    ("prohibit_noncritical_deviation", 2, "☑不允许偏差\n如需偏差另议", "ambiguous_options"),
    ("prohibit_noncritical_deviation", 2, "", "not_selected"),
])
def test_unselected_conflicting_or_ambiguous_options_never_become_permissions_or_prohibitions(operator, row, choice, reason):
    parsed, _ = fixture()
    parsed["documents"][0]["units"][0]["blocks"][0]["rows"][row][2] = choice
    result = extract(parsed, requirements_for(parsed))
    assert operator not in [item.operator for item in result.constraints]
    assert next(item for item in result.unresolved if item.operator == operator).reason == reason
    binding = bind_project_constraints_to_task(result, "method-1", ["R-METHOD"], requirements_for(parsed))
    assert len(binding.constraints) == 1
    assert binding.unresolved == result.unresolved
    assert operator not in [item.operator for item in binding.constraints]
    assert binding.source_set_sha256 == result.source_set_sha256


def test_unselected_allowance_is_retained_without_activation():
    parsed, _ = fixture()
    parsed["documents"][0]["units"][0]["blocks"][0]["rows"][1][2] = "☑不允许\n□允许"
    result = extract(parsed, requirements_for(parsed))
    assert len(result.constraints) == 2
    assert result.constraints[0].source.cells[2].text == "☑不允许\n□允许"
    assert "□允许" in result.constraints[0].source.quote


@pytest.mark.parametrize("damage,reason", [
    ("duplicate", "source_row_ambiguous"), ("wrong_clause", "source_row_missing"),
    ("wrong_name", "source_row_missing"), ("ocr", "native_row_unproved"),
    ("unread", "source_unread"), ("unmapped", "source_unread"), ("ignored", "source_unread"),
    ("tail", "native_row_unproved"), ("merged", "native_row_unproved"),
])
def test_unrelated_unread_or_incomplete_sources_cannot_prove_a_project_choice(damage, reason):
    parsed, requirements = fixture()
    unit = parsed["documents"][0]["units"][0]
    table = unit["blocks"][0]
    if damage == "duplicate": table["rows"].append(copy.deepcopy(table["rows"][1]))
    elif damage == "wrong_clause": table["rows"][1][0] = "1.90"
    elif damage == "wrong_name": table["rows"][1][1] = "分包说明示例"
    elif damage == "ocr": table["origin"] = "ocr"
    elif damage == "unread": unit["coverage"]["status"] = "partial"
    elif damage == "unmapped": unit["coverage"]["unmapped_glyphs"] = [{"text": "未知"}]
    elif damage == "ignored": unit["coverage"]["ignored_objects"] = [{"locator": table["locator"], "reason": "ignored"}]
    elif damage == "tail": table["rows"].insert(2, ["", "", "但另有条件尚未证明。"])
    elif damage == "merged": table["merges"] = [{"row": 1, "column": 0, "row_span": 1, "col_span": 2}]; table["rows"][1][1] = ""
    result = extract(parsed, requirements)
    unresolved = next(item for item in result.unresolved if item.operator == "prohibit_subcontracting")
    assert unresolved.reason == ("source_row_missing" if damage == "merged" else reason)


@pytest.mark.parametrize("damage", ["source_hash", "role"])
def test_only_the_explicit_frozen_tender_source_is_eligible(damage):
    parsed, requirements = fixture()
    parsed["documents"][0][{"source_hash": "sha256", "role": "role"}[damage]] = "wrong"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_TENDER_SOURCE_INVALID"):
        extract(parsed, requirements)


@pytest.mark.parametrize("damage", ["quote", "row_proof", "grid_proof", "cell_offset", "source_id", "missing"])
def test_n2_source_link_must_match_the_entire_rebuilt_row_proof(damage):
    parsed, requirements = fixture()
    item = requirements[0]
    cite = item["citations"][0]
    if damage == "quote": cite["quote"] = "不允许"
    elif damage == "row_proof": cite["table_row_proof_sha256"] = "b" * 64
    elif damage == "grid_proof": cite["table_row_parents"][0]["grid_sha256"] = "b" * 64
    elif damage == "cell_offset": cite["cell_refs"][0]["start"] += 1
    elif damage == "source_id": cite["source_id"] = "REFERENCE-BID"
    elif damage == "missing": item["citations"] = []
    result = extract(parsed, requirements)
    assert result.unresolved[0].reason == "source_requirement_invalid"


def test_selected_row_cannot_be_borrowed_from_a_distant_generic_clause():
    parsed, requirements = fixture()
    requirements = [item for item in requirements if item["requirement_id"] != "R-0"]
    result = extract(parsed, requirements)
    assert result.unresolved[0].reason == "source_requirement_missing"
    assert not any("R-CONDITIONAL" == link.requirement_id for item in result.constraints for link in item.source_requirements)


@pytest.mark.parametrize("damage", ["unrelated_cell", "width", "requirement_semantics", "row_selected"])
def test_full_frozen_source_revalidation_detects_changes(damage):
    parsed, requirements = fixture()
    result = extract(parsed, requirements)
    table = parsed["documents"][0]["units"][0]["blocks"][0]
    if damage == "unrelated_cell": table["rows"][-1][-1] += "变动"
    elif damage == "width": table["column_widths"][0] += 1
    elif damage == "requirement_semantics": requirements[0]["response_mode"] = "factual_response"
    elif damage == "row_selected": table["rows"][1][2] = "□不允许"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_SOURCE_BINDING_CHANGED"):
        validate_project_constraint_sources(result, parsed, requirements, tender_source_id="TENDER", tender_source_sha256=SOURCE_HASH)


def test_task_binding_is_explicit_and_does_not_mutate_n2_or_claim_compliance():
    parsed, requirements, constraints, binding, task = task_fixture()
    before = copy.deepcopy((parsed, requirements))
    assert [link.requirement_id for link in binding.target_requirements] == ["R-METHOD"]
    assert binding.applicability_status == "not_checked"
    assert binding.rule == "selected_project_prohibitions_to_explicit_task_v2"
    assert binding.source_set_sha256 == constraints.source_set_sha256
    assert binding.source_reading_sha256 == constraints.source_reading_sha256
    assert "project_constraint_binding" not in task
    supplied = json.loads(json.dumps(binding.model_dump(mode="json")))
    assert validate_task_project_constraints(task, binding, supplied_binding=supplied) == binding
    assert validate_project_constraint_task_sources(binding, constraints, requirements) == binding
    assert (parsed, requirements) == before
    assert next(item for item in requirements if item["requirement_id"] == "R-CONDITIONAL")["response_mode"] == "factual_response"


@pytest.mark.parametrize("damage", ["missing_binding", "strip_constraint", "strip_grid", "strip_forbidden", "task_id", "target", "numeric_bool"])
def test_serialization_or_recovery_cannot_strip_constraints_before_provider_invocation(damage):
    _, _, _, binding, task = task_fixture()
    supplied = binding.model_dump(mode="json")
    if damage == "missing_binding": supplied = None
    elif damage == "strip_constraint": supplied["constraints"].pop()
    elif damage == "strip_grid": supplied["constraints"][0]["source"].pop("parent_grid")
    elif damage == "strip_forbidden": task["forbidden_fields"].pop()
    elif damage == "task_id": task["unit_id"] = "another-task"
    elif damage == "target": task["requirement_refs"] = ["R-CONDITIONAL"]
    elif damage == "numeric_bool": supplied["constraints"][0]["source"]["cells"][0]["column"] = False
    provider_calls = []
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_TASK_BINDING_MISSING_OR_CHANGED"):
        validate_task_project_constraints(task, binding, supplied_binding=supplied)
        provider_calls.append("must never be reached")
    assert provider_calls == []


def test_revalidation_binds_the_full_actual_target_requirement_not_only_its_id():
    _, requirements, constraints, binding, _ = task_fixture()
    next(item for item in requirements if item["requirement_id"] == "R-METHOD")["text"] = "已改变的技术目标。"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_TARGET_BINDING_CHANGED"):
        validate_project_constraint_task_sources(binding, constraints, requirements)


@pytest.mark.parametrize("refs", [[], ["unknown"], ["R-METHOD", "R-METHOD"]])
def test_task_targets_must_be_explicit_known_and_unique(refs):
    _, requirements, constraints, _, _ = task_fixture()
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_TARGET_INVALID"):
        bind_project_constraints_to_task(constraints, "task-1", refs, requirements)


def test_typed_proof_rejects_tampering_even_when_caller_passes_a_model_instance():
    _, _, constraints, binding, task = task_fixture()
    bad = constraints.model_dump(mode="json")
    bad["constraints"][0]["source"]["cells"][2]["text"] += "伪造"
    with pytest.raises(ValidationError): ProjectConstraintSet.model_validate(bad)
    binding.constraints[0].source.parent_grid["rows"][-1][-1] += "伪造"
    with pytest.raises(ValidationError):
        validate_task_project_constraints(task, binding, supplied_binding=binding.model_dump(mode="json"))


def test_source_requirement_role_change_cannot_be_hidden_by_rebinding_a_task():
    _, requirements, constraints, _, _ = task_fixture()
    requirements[0]["response_mode"] = "factual_response"
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_SOURCE_REQUIREMENT_CHANGED"):
        bind_project_constraints_to_task(constraints, "task-1", ["R-METHOD"], requirements)


def test_native_row_proof_cannot_be_replaced_with_an_adjacent_parent_child_context():
    parsed, requirements = fixture()
    citation = requirements[0]["citations"][0]
    citation["basis"] = "consecutive_blocks_lf_v1"
    citation.pop("table_row_proof_sha256")
    result = extract(parsed, requirements)
    assert result.unresolved[0].reason == "source_requirement_invalid"


def test_trace_attributes_contain_only_ids_hashes_and_actual_target_refs():
    _, _, _, binding, _ = task_fixture()
    trace = project_constraint_trace_attributes(binding)
    assert trace["project_constraint_target_requirement_ids"] == ["R-METHOD"]
    assert len(trace["project_constraint_ids"]) == 2
    assert all(len(item) == 64 for item in trace["project_constraint_proof_sha256s"])
    encoded = json.dumps(trace, ensure_ascii=False)
    assert "分包" not in encoded and "偏差" not in encoded and "parent_grid" not in encoded


def test_binding_schema_cannot_claim_verified_or_omit_one_operator():
    _, _, _, binding, _ = task_fixture()
    data = binding.model_dump(mode="json")
    data["applicability_status"] = "satisfied"
    with pytest.raises(ValidationError): TaskProjectConstraints.model_validate(data)
    data = binding.model_dump(mode="json")
    data["constraints"].pop()
    with pytest.raises(ValidationError): TaskProjectConstraints.model_validate(data)


def test_one_prohibition_survives_an_unsupported_allowance_for_the_other_operator():
    parsed, _ = fixture()
    table = parsed["documents"][0]["units"][0]["blocks"][0]
    table["rows"][2][2] = "□不允许偏差\n☑允许偏差"
    requirements = requirements_for(parsed)
    constraints = extract(parsed, requirements)
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    assert [item.operator for item in binding.constraints] == ["prohibit_subcontracting"]
    assert binding.unresolved[0].operator == "prohibit_noncritical_deviation"
    assert binding.unresolved[0].detection_status == "unsupported"
    assert binding.unresolved[0].reason == "allowance_not_supported"
    assert binding.applicability_status == "not_checked"
    assert len([item.forbidden_field for item in binding.constraints]) == 1
    validate_project_constraint_task_sources(binding, constraints, requirements)
    task = {"unit_id": "method-1", "requirement_refs": ["R-METHOD"],
            "forbidden_fields": [item.forbidden_field for item in binding.constraints]}
    validate_task_project_constraints(task, binding, supplied_binding=binding.model_dump(mode="json"))


def test_zero_prohibitions_and_not_detected_rows_are_an_honest_finite_binding():
    parsed, _ = fixture()
    table = parsed["documents"][0]["units"][0]["blocks"][0]
    table["rows"][1][0], table["rows"][2][0] = "9.1", "9.2"
    requirements = requirements_for(parsed)
    constraints = extract(parsed, requirements)
    assert constraints.constraints == []
    assert [item.detection_status for item in constraints.unresolved] == ["not_detected", "not_detected"]
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    assert binding.constraints == [] and binding.unresolved == constraints.unresolved
    assert binding.applicability_status == "not_checked"
    task = {"unit_id": "method-1", "requirement_refs": ["R-METHOD"], "forbidden_fields": ["禁止编造事实"]}
    validate_project_constraint_task_sources(binding, constraints, requirements)
    validate_task_project_constraints(task, binding, supplied_binding=binding.model_dump(mode="json"))


def test_unsupported_source_format_is_reported_without_inventing_permissions_or_blocking_other_tasks():
    parsed, requirements = fixture()
    parsed["documents"][0]["format"] = "docx"
    constraints = extract(parsed, requirements)
    assert constraints.constraints == []
    assert [item.detection_status for item in constraints.unresolved] == ["unsupported", "unsupported"]
    assert all(item.reason == "source_format_unsupported" for item in constraints.unresolved)
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    assert binding.constraints == [] and len(binding.unresolved) == 2
    assert binding.applicability_status == "not_checked"


def test_conflict_for_one_operator_keeps_the_other_independently_proved_prohibition():
    parsed, _ = fixture()
    parsed["documents"][0]["units"][0]["blocks"][0]["rows"][1][2] = "☑不允许\n☑允许"
    requirements = requirements_for(parsed)
    constraints = extract(parsed, requirements)
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    assert [item.operator for item in binding.constraints] == ["prohibit_noncritical_deviation"]
    assert binding.unresolved[0].detection_status == "unresolved"
    assert binding.unresolved[0].reason == "conflicting_options"
    task = {"unit_id": "method-1", "requirement_refs": ["R-METHOD"],
            "forbidden_fields": [item.forbidden_field for item in binding.constraints]}
    validate_task_project_constraints(task, binding, supplied_binding=binding.model_dump(mode="json"))


@pytest.mark.parametrize("damage", ["unresolved", "unresolved_status", "unresolved_reason", "source_set_hash", "source_reading_hash", "missing_reading_hash"])
def test_serialization_cannot_remove_unknown_siblings_or_source_set_identity(damage):
    parsed, _ = fixture()
    parsed["documents"][0]["units"][0]["blocks"][0]["rows"][2][2] = "□不允许偏差"
    requirements = requirements_for(parsed)
    constraints = extract(parsed, requirements)
    binding = bind_project_constraints_to_task(constraints, "method-1", ["R-METHOD"], requirements)
    task = {"unit_id": "method-1", "requirement_refs": ["R-METHOD"],
            "forbidden_fields": [item.forbidden_field for item in binding.constraints]}
    supplied = binding.model_dump(mode="json")
    if damage == "unresolved": supplied["unresolved"] = []
    elif damage == "unresolved_status": supplied["unresolved"][0]["detection_status"] = "not_detected"
    elif damage == "unresolved_reason": supplied["unresolved"][0]["reason"] = "allowance_not_supported"
    elif damage == "source_set_hash": supplied["source_set_sha256"] = "b" * 64
    elif damage == "source_reading_hash": supplied["source_reading_sha256"] = "b" * 64
    elif damage == "missing_reading_hash": supplied.pop("source_reading_sha256")
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_TASK_BINDING_MISSING_OR_CHANGED"):
        validate_task_project_constraints(task, binding, supplied_binding=supplied)


def test_unchanged_unresolved_reason_cannot_hide_changed_n1_reading_bytes():
    parsed, _ = fixture()
    table = parsed["documents"][0]["units"][0]["blocks"][0]
    table["rows"][1][2] = "☑不允许，后附未知条件甲"
    requirements = requirements_for(parsed)
    constraints = extract(parsed, requirements)
    assert constraints.unresolved[0].reason == "ambiguous_options"
    table["rows"][1][2] = "☑不允许，后附未知条件乙"
    changed = extract(parsed, requirements_for(parsed))
    assert changed.unresolved == constraints.unresolved
    assert changed.source_reading_sha256 != constraints.source_reading_sha256
    assert changed.source_set_sha256 != constraints.source_set_sha256
    with pytest.raises(ProjectConstraintError, match="PROJECT_CONSTRAINT_SOURCE_BINDING_CHANGED"):
        validate_project_constraint_sources(constraints, parsed, requirements_for(parsed),
            tender_source_id="TENDER", tender_source_sha256=SOURCE_HASH)


def test_source_set_schema_rejects_missing_partition_members_or_downgraded_contract():
    parsed, requirements = fixture()
    data = extract(parsed, requirements).model_dump(mode="json")
    data["schema_version"] = "project-constraints-v1"
    with pytest.raises(ValidationError): ProjectConstraintSet.model_validate(data)
    data = extract(parsed, requirements).model_dump(mode="json")
    data.pop("source_reading_sha256")
    with pytest.raises(ValidationError): ProjectConstraintSet.model_validate(data)
