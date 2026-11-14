"""N3-owned constraint artifacts and N4 checks of their immutable provenance."""
from __future__ import annotations

import copy
import hashlib

from .project_constraints import (ProjectConstraintError, bind_project_constraints_to_task,
    extract_project_constraints, project_constraint_trace_attributes,
    validate_project_constraint_sources, validate_project_constraint_task_sources,
    validate_task_project_constraints)
from .storage import digest


def _derived_requirements(report, prepared, parsed):
    """Keep administrative source derivations consistent with actual N3 output."""
    from .preparation_rules import preparation_rule
    known = {item["requirement_id"]: item for item in prepared["requirements"]}
    sources = {item["source_id"]: item for item in prepared["sources"]}
    blocks = {}
    for document in parsed["documents"]:
        for unit in document["units"]:
            for block in unit["blocks"]:
                if block["block_id"] in blocks:
                    raise ProjectConstraintError("PROJECT_CONSTRAINT_DERIVED_SOURCE_CHANGED")
                blocks[block["block_id"]] = (block, unit, document)
    derived, seen = [], set()
    for rule in report["preparation_source_rules"]:
        item = rule.get("derived_requirement")
        if not item:
            continue
        rid = item["requirement_id"]
        actual = known.get(rid)
        proof = rule["source_role_proof"]
        if (rid in seen or not actual or rule["requirement_id"] != rid or
                preparation_rule(item["text"]) != rule["source_rule"] or
                digest(proof) != rule["source_role_proof_sha256"] or
                any(actual[key] != item.get(key, True if key == "mandatory" else None)
                    for key in ("text", "category", "mandatory", "response_mode")) or
                item["response_mode"] != "procedure" or not item.get("citations")):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_DERIVED_SOURCE_CHANGED")
        for citation in item["citations"]:
            ids = citation.get("block_ids") or [citation["block_id"]]
            members = [blocks[key] for key in ids]
            text = "\n".join(block["text"] for block, _, _ in members)
            quote_hash = hashlib.sha256(text.encode()).hexdigest()
            first, _, document = members[0]
            if (len(set(ids)) != len(ids) or ids[0] != citation["block_id"] or
                    text != item["text"] or citation["quote"] != text or
                    citation["quote_sha256"] != quote_hash or citation["source_id"] != document["source_id"] or
                    citation["locator"] != first["locator"] or citation["start"] != 0 or citation["end"] != len(text) or
                    any(block["kind"] != "paragraph" or source["source_id"] != document["source_id"] or
                        unit["coverage"]["status"] != "read" for block, unit, source in members) or
                    not any(sources[ref]["sha256"] == document["sha256"] and
                            sources[ref]["locator"].startswith(first["locator"] + ";") and
                            ("quote_sha256=" + quote_hash) in sources[ref]["locator"] for ref in actual["source_refs"])):
                raise ProjectConstraintError("PROJECT_CONSTRAINT_DERIVED_SOURCE_CHANGED")
        derived.append(copy.deepcopy(item))
        seen.add(rid)
    return derived


def _contract_pending_requirements(report, prepared, n1, requirements):
    """Rebuild source derivations; a report is never its own expectation."""
    from .contract_pending_plan import contract_pending_plan
    from .contract_review_slots import _CONTRACT
    from .extraction import _id

    recorded = report.get("contract_pending_plan")
    contract_present = any(_CONTRACT.fullmatch(block.get("text", ""))
        for document in n1["parsed"]["documents"] if document["role"] == "tender" and document.get("format") == "pdf"
        for unit in document["units"] for block in unit["blocks"])
    if not contract_present:
        if recorded and (recorded.get("bindings") or recorded.get("derived_requirements") or recorded.get("errors")):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
        return []
    rebuilt = contract_pending_plan(requirements, n1["parsed"], n1["manifest"])
    if rebuilt["errors"] or not isinstance(recorded, dict) or digest(recorded) != digest(rebuilt):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
    known = {item["requirement_id"]: item for item in prepared["requirements"]}
    sources = {item["source_id"]: item for item in prepared["sources"]}
    original = {item["requirement_id"]: item for item in requirements}
    derived = {item["requirement_id"]: item for item in rebuilt["derived_requirements"]}
    for rid in rebuilt["bindings"]:
        actual, expected = known.get(rid), derived.get(rid, original.get(rid))
        if (not actual or not expected or any(actual[key] != expected[key] for key in
                ("text", "category", "mandatory", "response_mode")) or
                any(rid in task["requirement_refs"] for section in prepared["sections"] for task in section["writing_tasks"])):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
        if rid in derived:
            expected_refs = []
            for citation in expected["citations"]:
                ref = _id("SCOPE-", citation)
                scoped = sources.get(ref)
                parent = next((item for item in n1["manifest"]["sources"] if item["source_id"] == citation["source_id"]), None)
                locator = (f"{citation['locator']}; basis={citation['basis']}; chars[{citation['start']}:{citation['end']}]; "
                           f"quote_sha256={citation['quote_sha256']}; through={citation['end_locator']}; blocks=" + ",".join(citation["block_ids"]))
                if not scoped or not parent or scoped["sha256"] != parent["sha256"] or scoped["locator"] != locator:
                    raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
                expected_refs.append(ref)
            if actual["source_refs"] != expected_refs:
                raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
    issues = {item["issue_id"]: item for item in prepared["issues"]}
    expected_rows = {(rid, item["pending_id"]): item for rid, items in rebuilt["bindings"].items() for item in items}
    rows = report.get("contract_pending_handling", [])
    if (len(rows) != len(expected_rows) or len({(row["requirement_id"], row["catalog_pending_id"]) for row in rows}) != len(rows)):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
    for row in rows:
        key = row["requirement_id"], row["catalog_pending_id"]
        pending = expected_rows.get(key)
        if (not pending or row["source_proof_sha256"] != pending["source_proof_sha256"] or
                row["pending_fields"] != pending["pending_fields"] or row["scope"] != pending["scope"] or
                row["owner_party"] != pending["owner_party"] or row["response_status"] != "pending_manual" or
                row["reading_status"] != "read" or row["value_status"] != pending["value_status"] or
                row["physical_blank_status"] != "not_determined" or row["satisfaction_status"] != "not_determined" or
                row["values_inferred"] is not False or row["writing_unit_ids"] or row["adopted_fact_ids"] or
                len(row["issue_ids"]) != 1 + len(pending["blocking_issues"]) or
                any(issue_id not in issues or key[0] not in issues[issue_id]["requirement_refs"] or
                    issues[issue_id]["source_requirement"] != known[key[0]]["text"] for issue_id in row["issue_ids"])):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
        blocking = row.get("blocking_issues", [])
        if ([{key: value for key, value in item.items() if key != "issue_id"} for item in blocking] != pending["blocking_issues"] or
                [item["issue_id"] for item in blocking] != row["issue_ids"][1:]):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_PENDING_CHANGED")
    reviewed = _contract_source_review_requirements(report, prepared, n1, requirements, rebuilt)
    return copy.deepcopy([*derived.values(), *reviewed])


def _contract_source_review_requirements(report, prepared, n1, requirements, rebuilt):
    """Check the actual frozen disposition, not only its declared report."""
    from .extraction import _id

    review = rebuilt.get("source_review", {})
    bindings = review.get("bindings", {})
    derived = {item["requirement_id"]: item for item in review.get("derived_requirements", [])}
    original = {item["requirement_id"]: item for item in requirements}
    known = {item["requirement_id"]: item for item in prepared["requirements"]}
    sources = {item["source_id"]: item for item in prepared["sources"]}
    sections = {section["section_id"]: section for section in prepared["sections"]}
    issues = {item["issue_id"]: item for item in prepared["issues"]}

    def changed():
        raise ProjectConstraintError("PROJECT_CONSTRAINT_CONTRACT_SOURCE_REVIEW_CHANGED")

    expected_rows = {(rid, item["review_id"]): item for rid, items in bindings.items() for item in items}
    rows = report.get("contract_source_review_handling", [])
    if (review.get("errors") or len(rows) != len(expected_rows) or
            len({(row.get("requirement_id"), row.get("review_id")) for row in rows}) != len(rows)):
        changed()
    for rid, items in bindings.items():
        actual, expected = known.get(rid), derived.get(rid, original.get(rid))
        if (not actual or not expected or any(actual[key] != expected[key] for key in
                ("text", "category", "mandatory", "response_mode")) or
                any(rid in task["requirement_refs"] for section in sections.values() for task in section["writing_tasks"])):
            changed()
        if rid in derived and actual["source_refs"] != [_id("SCOPE-", citation) for citation in expected["citations"]]:
            changed()
    for row in rows:
        rid = row["requirement_id"]
        item = expected_rows.get((rid, row["review_id"]))
        section = sections.get(row.get("section_id"))
        if (not item or not section or row.get("source_proof_sha256") != item["source_proof_sha256"] or
                row.get("response_status") != "pending_manual" or row.get("satisfaction_status") != "not_determined" or
                row.get("values_inferred") is not False or row.get("writing_unit_ids") != [] or row.get("adopted_fact_ids") != [] or
                type(row.get("literal_zero_reference_count")) is not int or
                row.get("literal_zero_reference_count") != item["literal_zero_reference_count"] or
                rid not in section["requirement_refs"] or rid not in section["nonwriting_requirement_refs"] or
                section["render_body"] is not True or section["fact_refs"]):
            changed()
        citation = item["citation"]
        ref = _id("SCOPE-", citation)
        source = sources.get(ref)
        parent = next((source for source in n1["manifest"]["sources"] if source["source_id"] == citation["source_id"]), None)
        locator = (f"{citation['locator']}; basis={citation['basis']}; chars[{citation['start']}:{citation['end']}]; "
                   f"quote_sha256={citation['quote_sha256']}; through={citation['end_locator']}; blocks=" + ",".join(citation["block_ids"]))
        if (not source or not parent or source["sha256"] != parent["sha256"] or source["locator"] != locator or
                not any(block.get("kind") == "paragraph" and
                        block.get("text") == "合同条款原文（履约及适用性待核对）：\n" + item["text"] and
                        block.get("source_refs") == [ref] for block in section["fixed_content"])):
            changed()
        issue_ids = row.get("issue_ids", [])
        title = "合同原文第0条回指待澄清" if item["literal_zero_reference_count"] else "合同条款履约及适用性待核对"
        if (not issue_ids or any(key not in issues or rid not in issues[key]["requirement_refs"] or
                issues[key]["source_requirement"] != item["text"] or issues[key]["title"] != title or
                issues[key]["section_id"] != section["section_id"] or issues[key]["severity"] not in {"missing", "warning"}
                for key in issue_ids)):
            changed()
    return copy.deepcopy(list(derived.values()))


def plan_project_constraints(parsed, requirements, manifest, sections):
    """Attach only proved prohibitions; retain the finite detector's unknowns."""
    sets = []
    for source in manifest["sources"]:
        if manifest["source_roles"][source["source_id"]] != "tender":
            continue
        sets.append(extract_project_constraints(parsed, requirements,
            tender_source_id=source["source_id"], tender_source_sha256=source["sha256"]).model_dump(mode="json"))
    tasks = {}
    for section in sections:
        for task in section["writing_tasks"]:
            if task["unit_id"] in tasks:
                raise ProjectConstraintError("PROJECT_CONSTRAINT_TASK_DUPLICATED")
            bindings = [bind_project_constraints_to_task(value, task["unit_id"], task["requirement_refs"],
                                                        requirements).model_dump(mode="json") for value in sets]
            for binding in bindings:
                for constraint in binding["constraints"]:
                    if constraint["forbidden_field"] not in task["forbidden_fields"]:
                        task["forbidden_fields"].append(constraint["forbidden_field"])
            tasks[task["unit_id"]] = bindings
    return {"schema_version": "project-constraint-plan-v1", "parsed_sha256": digest(parsed),
            "requirements_sha256": digest(requirements), "sources": sets, "tasks": tasks,
            "review_status": "not_checked", "unknowns_are_not_permissions": True}


def load_project_constraints(ctx, prepared=None):
    """Read the completed N3 unit, not an expectation copied from an N4 request.

    The local executor already validates its prefix before N4. Repeat checks on
    the exact auxiliary artifacts consumed here and include their references in
    the new task input. This follows the existing cross-unit artifact contract.
    """
    if ctx.execution_config.get("pipeline", "prepared") != "automatic":
        return None
    try:
        nodes = {node["key"]: node for node in ctx._state["nodes"]}
        if any(nodes[key]["status"] != "succeeded" for key in ("N1", "N2", "N3")):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_PREFIX_INCOMPLETE")
        unit = nodes["N3"]["units"]["plan-assemble"]
        if unit["status"] != "succeeded":
            raise ProjectConstraintError("PROJECT_CONSTRAINT_PLAN_INCOMPLETE")
        plan_input = ctx.executor.store.read_json(unit["input_ref"])
        if digest(plan_input) != unit["input_hash"]:
            raise ProjectConstraintError("PROJECT_CONSTRAINT_PLAN_INPUT_CHANGED")
        plan_output = ctx.executor._checkpoint_data(unit)
        if prepared is None:
            prepared = plan_output["prepared"]
        n1 = ctx.executor._checkpoint_data(nodes["N1"])
        n2 = ctx.executor._checkpoint_data(nodes["N2"])
        report = plan_output["report"]
        if (plan_output["prepared"] != prepared or
                plan_input["extracted"] != n2["extracted"] or
                plan_input["manifest"] != n1["manifest"] or
                plan_input["parsed_hash"] != digest(n1["parsed"]) or
                report["parsed_hash"] != digest(n1["parsed"])):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_PLAN_INPUT_CHANGED")
        requirements = copy.deepcopy(n2["extracted"]["requirements"])
        requirements.extend(_derived_requirements(report, prepared, n1["parsed"]))
        requirements.extend(_contract_pending_requirements(report, prepared, n1, requirements))
        # Rebuild independently from the frozen source and pre-serialization
        # task identities. Never use the actual N4 context as an expectation.
        sections = copy.deepcopy(prepared["sections"])
        rebuilt = plan_project_constraints(n1["parsed"], requirements, n1["manifest"], sections)
        expected = report["project_constraints"]
        if rebuilt != expected or sections != prepared["sections"]:
            raise ProjectConstraintError("PROJECT_CONSTRAINT_PLAN_BINDING_CHANGED")
        for source in expected["sources"]:
            validate_project_constraint_sources(source, n1["parsed"], requirements,
                tender_source_id=source["source_id"], tender_source_sha256=source["source_sha256"])
        sets = {source["source_id"]: source for source in expected["sources"]}
        for bindings in expected["tasks"].values():
            for binding in bindings:
                validate_project_constraint_task_sources(binding, sets[binding["source_id"]], requirements)
        references = {"N1": nodes["N1"]["output_ref"], "N2": nodes["N2"]["output_ref"],
                      "N3_plan_input": unit["input_ref"], "N3_plan_output": unit["output_ref"]}
        capture = ctx.capture(ctx.node_key.lower() + "-project-constraint-preflight", {"status": "passed",
            "artifact_refs": references, "plan_sha256": digest(expected),
            "task_count": len(expected["tasks"]), "review_status": "not_checked"})
        capture_status = capture.get("status") if isinstance(capture, dict) else None
        if (capture_status != "captured" and not
                (capture_status == "disabled_by_policy" and ctx.execution_config.get("debug_capture") is False)):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_CAPTURE_FAILED")
        ctx.call_event("project_constraints.checked", {"operation": "source_and_task_binding",
            "input_hash": digest(expected), "unit_count": len(expected["tasks"]),
            "status": "passed", "capture_status": capture_status})
        return {"plan": expected, "artifact_refs": references, "plan_sha256": digest(expected)}
    except ProjectConstraintError:
        raise
    except Exception as error:
        raise ProjectConstraintError("PROJECT_CONSTRAINT_PLAN_INVALID") from error


def validate_project_constraint_context(task, context, provenance):
    if provenance is None:
        return []
    expected = provenance["plan"]["tasks"].get(task["unit_id"])
    actual = context.get("project_constraints")
    if (not isinstance(expected, list) or actual != expected or
            context.get("writing_task") != task or context.get("section_id") != task["unit_id"]):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_TASK_BINDING_MISSING_OR_CHANGED")
    return [project_constraint_trace_attributes(validate_task_project_constraints(task, binding,
                supplied_binding=serialized)) for binding, serialized in zip(expected, actual)]


def project_constraint_trace_bindings(provenance):
    if provenance is None:
        return None
    return {unit_id: [project_constraint_trace_attributes(binding) for binding in bindings]
            for unit_id, bindings in provenance["plan"]["tasks"].items()}
