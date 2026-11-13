"""Two selected tender prohibitions, with native row and explicit task proofs.

This deliberately bounded consumer does not decide bidder compliance, activate
allowances, rewrite N2 requirements, or fill deviation tables. Full proofs belong
in private execution artifacts; ``project_constraint_trace_attributes`` exposes
only identities and hashes suitable for a trace.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .table_continuations import build_table_row_catalog


Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Operator = Literal["prohibit_subcontracting", "prohibit_noncritical_deviation"]
_RULES = {
    "prohibit_subcontracting": {
        "clause": "1.9", "name": "分包", "denied": "不允许", "allowed": "允许",
        "instruction": "本项目招标前附表已勾选不允许分包；拟实施技术方法不得安排分包。此为来源约束，不能据此声称公司已符合或已落实。",
        "forbidden": "不得在拟实施技术方法中安排分包或把禁止分包写成已核验的公司事实。",
    },
    "prohibit_noncritical_deviation": {
        "clause": "1.10.2", "name": "对非关键条款的偏差", "denied": "不允许偏差", "allowed": "允许偏差",
        "instruction": "本项目招标前附表已勾选不允许非关键条款偏差；拟实施技术方法不得设计偏差。保留商务和技术偏离表及其待填状态，不据此填写无偏差或声称已符合。",
        "forbidden": "不得设计非关键条款偏差、删去商务或技术偏离表，或根据禁令自动填写无偏差、符合等响应值。",
    },
}
_DETECTION_STATUS = {
    "source_row_missing": "not_detected",
    "source_format_unsupported": "unsupported",
    "native_row_unproved": "unsupported",
    "not_selected": "unsupported",
    "allowance_not_supported": "unsupported",
    "source_row_ambiguous": "unresolved",
    "source_unread": "unresolved",
    "ambiguous_options": "unresolved",
    "conflicting_options": "unresolved",
    "source_requirement_missing": "unresolved",
    "source_requirement_invalid": "unresolved",
}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ProjectConstraintError(ValueError):
    """Bounded diagnostic code; no source content in log messages."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


class ConstraintSourceCell(_Contract):
    column: int = Field(ge=0, le=2)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str
    text_sha256: Sha256


class ConstraintSourceRow(_Contract):
    source_id: Text
    source_sha256: Sha256
    unit_id: Text
    page: int = Field(ge=1)
    block_id: Text
    locator: Text
    ordinal: int = Field(ge=0)
    row: int = Field(ge=1)
    cells: list[ConstraintSourceCell] = Field(min_length=3, max_length=3)
    quote: Text
    quote_sha256: Sha256
    parent_grid: dict
    parent_grid_sha256: Sha256
    parent_text_sha256: Sha256
    table_row_id: Text
    table_row_proof_sha256: Sha256

    @model_validator(mode="after")
    def native_grid_integrity(self):
        grid = self.parent_grid
        if set(grid) != {"rows", "merges", "column_widths", "header_rows"}:
            raise ValueError("constraint parent grid fields differ")
        rows = grid["rows"]
        if (not isinstance(rows, list) or len(rows) <= self.row or
                rows[0] != ["条款号", "条款名称", "编列内容"] or
                any(not isinstance(row, list) or len(row) != 3 or
                    any(not isinstance(cell, str) for cell in row) for row in rows)):
            raise ValueError("constraint requires the complete native three-column grid")
        quote = "\t".join(rows[self.row])
        cursor = sum(len("\t".join(row)) + 1 for row in rows[:self.row])
        for column, cell in enumerate(self.cells):
            text = rows[self.row][column]
            if (cell.column != column or cell.text != text or cell.start != cursor or
                    cell.end != cursor + len(text) or cell.text_sha256 != _text_hash(text)):
                raise ValueError("constraint cell identity mismatch")
            cursor += len(text) + 1
        if (self.quote != quote or self.quote_sha256 != _text_hash(quote) or
                self.parent_grid_sha256 != _hash(grid) or
                self.parent_text_sha256 != _text_hash("\n".join("\t".join(row) for row in rows)) or
                self.table_row_id != "TROW-" + self.table_row_proof_sha256[:24] or
                not re.fullmatch(rf"pdf/page\[{self.page}\]/table\[\d+\]", self.locator)):
            raise ValueError("constraint native row identity mismatch")
        return self


class ConstraintRequirementLink(_Contract):
    requirement_id: Text
    requirement_sha256: Sha256


class ProjectConstraint(_Contract):
    constraint_id: Text
    rule: Literal["native_pdf_selected_project_row_v1"] = "native_pdf_selected_project_row_v1"
    operator: Operator
    source: ConstraintSourceRow
    source_requirements: list[ConstraintRequirementLink] = Field(min_length=1)
    instruction: Text
    forbidden_field: Text
    review_status: Literal["not_checked"] = "not_checked"
    proof_sha256: Sha256

    @model_validator(mode="after")
    def proof_integrity(self):
        rule = _RULES[self.operator]
        cells = [cell.text for cell in self.source.cells]
        payload = self.model_dump(mode="json", exclude={"constraint_id", "proof_sha256"})
        if (cells[:2] != [rule["clause"], rule["name"]] or
                _option_status(cells[2], rule) != "selected_prohibition" or
                self.instruction != rule["instruction"] or self.forbidden_field != rule["forbidden"] or
                len({item.requirement_id for item in self.source_requirements}) != len(self.source_requirements) or
                self.proof_sha256 != _hash(payload) or self.constraint_id != "PJC-" + self.proof_sha256[:24]):
            raise ValueError("project constraint proof identity mismatch")
        return self


class UnresolvedProjectConstraint(_Contract):
    """Finite extractor state; never a project permission or exemption."""

    operator: Operator
    detection_status: Literal["not_detected", "unsupported", "unresolved"]
    reason: Literal["source_row_missing", "source_format_unsupported", "source_row_ambiguous", "native_row_unproved",
                    "source_unread", "ambiguous_options", "conflicting_options", "not_selected",
                    "allowance_not_supported", "source_requirement_missing", "source_requirement_invalid"]
    source_id: Text
    block_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def finite_detection_status(self):
        if self.detection_status != _DETECTION_STATUS[self.reason]:
            raise ValueError("project constraint finite detection status differs")
        return self


class ProjectConstraintSet(_Contract):
    schema_version: Literal["project-constraints-v2"] = "project-constraints-v2"
    source_id: Text
    source_sha256: Sha256
    source_reading_sha256: Sha256
    constraints: list[ProjectConstraint] = Field(default_factory=list, max_length=2)
    unresolved: list[UnresolvedProjectConstraint] = Field(default_factory=list, max_length=2)
    source_set_sha256: Sha256

    @model_validator(mode="after")
    def finite_operator_partition(self):
        operators = [item.operator for item in [*self.constraints, *self.unresolved]]
        if (len(operators) != 2 or set(operators) != set(_RULES) or
                any(item.source.source_id != self.source_id or item.source.source_sha256 != self.source_sha256
                    for item in self.constraints) or
                any(item.source_id != self.source_id for item in self.unresolved)):
            raise ValueError("project constraint operator partition mismatch")
        if self.source_set_sha256 != _hash(self.model_dump(mode="json", exclude={"source_set_sha256"})):
            raise ValueError("project constraint source set identity mismatch")
        return self


class TaskProjectConstraints(_Contract):
    binding_id: Text
    rule: Literal["selected_project_prohibitions_to_explicit_task_v2"] = "selected_project_prohibitions_to_explicit_task_v2"
    task_id: Text
    source_id: Text
    source_sha256: Sha256
    source_reading_sha256: Sha256
    source_set_sha256: Sha256
    target_requirements: list[ConstraintRequirementLink] = Field(min_length=1)
    constraints: list[ProjectConstraint] = Field(default_factory=list, max_length=2)
    unresolved: list[UnresolvedProjectConstraint] = Field(default_factory=list, max_length=2)
    applicability_status: Literal["not_checked"] = "not_checked"
    binding_sha256: Sha256

    @model_validator(mode="after")
    def binding_integrity(self):
        # Reconstruct the complete finite source partition. An unresolved
        # sibling never suppresses a separately proved prohibition.
        ProjectConstraintSet.model_validate({
            "source_id": self.source_id, "source_sha256": self.source_sha256,
            "source_reading_sha256": self.source_reading_sha256, "source_set_sha256": self.source_set_sha256,
            "constraints": [item.model_dump(mode="json") for item in self.constraints],
            "unresolved": [item.model_dump(mode="json") for item in self.unresolved],
        })
        payload = self.model_dump(mode="json", exclude={"binding_id", "binding_sha256"})
        if (len({item.requirement_id for item in self.target_requirements}) != len(self.target_requirements) or
                self.binding_sha256 != _hash(payload) or self.binding_id != "PJCT-" + self.binding_sha256[:24]):
            raise ValueError("project constraint task binding mismatch")
        return self


def _option_status(text: str, rule: dict) -> str:
    options = []
    for line in text.splitlines():
        match = re.fullmatch(r"([☑□])\s*(" + re.escape(rule["denied"]) + "|" + re.escape(rule["allowed"]) + r")", line.strip())
        if not match:
            return "ambiguous_options"
        options.append((match[1], match[2]))
    selected = [choice for marker, choice in options if marker == "☑"]
    if len(selected) > 1:
        return "conflicting_options"
    if not selected:
        return "not_selected"
    if len({choice for _, choice in options}) != len(options):
        return "ambiguous_options"
    return "selected_prohibition" if selected == [rule["denied"]] else "allowance_not_supported"


def _requirement_index(requirements: list[dict]) -> dict:
    if (not isinstance(requirements, list) or any(not isinstance(item, dict) or
            not isinstance(item.get("requirement_id"), str) or not item["requirement_id"] for item in requirements)):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_REQUIREMENTS_INVALID")
    result = {item["requirement_id"]: item for item in requirements}
    if len(result) != len(requirements):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_REQUIREMENTS_DUPLICATE")
    return result


def _native_view(parsed: dict, source_id: str, source_sha256: str):
    documents = parsed.get("documents", [])
    matches = [doc for doc in documents if doc.get("source_id") == source_id]
    if (len(matches) != 1 or matches[0].get("role") != "tender" or
            matches[0].get("sha256") != source_sha256 or not re.fullmatch(r"[a-f0-9]{64}", source_sha256)):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_TENDER_SOURCE_INVALID")
    document, blocks, units, ordinal, seen = matches[0], [], {}, 0, set()
    for doc in documents:
        for unit in doc.get("units", []):
            for original in unit.get("blocks", []):
                if original.get("block_id") in seen:
                    raise ProjectConstraintError("PROJECT_CONSTRAINT_SOURCE_ID_DUPLICATE")
                seen.add(original.get("block_id"))
                if doc is document:
                    if (unit.get("source_id") != source_id or original.get("source_id") != source_id or
                            unit.get("unit_id") in units and units[unit["unit_id"]] is not unit):
                        raise ProjectConstraintError("PROJECT_CONSTRAINT_SOURCE_ID_INVALID")
                    units[unit["unit_id"]] = unit
                    block = copy.deepcopy(original)
                    block.update(unit_id=unit["unit_id"], page=unit.get("page"), ordinal=ordinal)
                    if block.get("kind") == "table" and isinstance(block.get("rows"), list):
                        rows = block["rows"]
                        if all(isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in rows):
                            block["text"] = "\n".join("\t".join(row) for row in rows)
                            block["text_basis"] = "table_rows_tsv_v1"
                    blocks.append(block)
                ordinal += 1
    return document, blocks, units


def _citation_matches(citation: dict, row: dict, unit_id: str) -> bool:
    expected = {
        "source_id": row["source_id"], "block_id": row["block_id"], "block_ids": row["block_ids"],
        "locator": row["locator"], "quote": row["quote"], "quote_sha256": row["quote_sha256"],
        "basis": "frozen_table_rows_tsv_v1", "unit_id": unit_id, "page": row["tables"][0]["page"],
        "table_row_id": row["table_row_id"], "table_row_proof_sha256": row["proof_sha256"],
        "table_row_parts": row["parts"], "table_row_parents": row["tables"], "cell_refs": row["cell_refs"],
        "start": 0, "end": len(row["quote"]),
    }
    return isinstance(citation, dict) and all(citation.get(key) == value for key, value in expected.items())


def extract_project_constraints(parsed: dict, requirements: list[dict], *, tender_source_id: str,
                                tender_source_sha256: str) -> ProjectConstraintSet:
    """Read only two exact three-column native source rows and their N2 links.

    Missing, unselected, allowed, unread, conflicting or unknown structures stay
    in a separate finite detection partition. They do not suppress an independently
    proved prohibition or imply project permission. This API requires the complete frozen N1 source and normalized,
    source-validated N2 requirements; it does not claim whole-N2 completeness.
    """
    _requirement_index(requirements)
    document, blocks, units = _native_view(parsed, tender_source_id, tender_source_sha256)
    # No implicit joins: this finite consumer supports one complete source row.
    catalog = build_table_row_catalog(document, blocks, [])
    constraints, unresolved = [], []
    for operator, rule in _RULES.items():
        hits = [(block, index, row) for block in blocks if block.get("kind") == "table"
                for index, row in enumerate(block.get("rows", []))
                if isinstance(row, list) and len(row) == 3 and row[:2] == [rule["clause"], rule["name"]]]
        reason = ("source_format_unsupported" if document.get("format") != "pdf" else
                  "source_row_missing" if not hits else "source_row_ambiguous" if len(hits) != 1 else None)
        if reason is None:
            block, index, cells = hits[0]
            unit = units[block["unit_id"]]
            if (unit.get("coverage", {}).get("status") != "read" or
                    unit.get("coverage", {}).get("unread_objects") or
                    unit.get("coverage", {}).get("unmapped_glyphs") or
                    any(item.get("locator") == block["locator"] for item in unit.get("coverage", {}).get("ignored_objects", []))):
                reason = "source_unread"
            candidates = [row for row in catalog if row["block_id"] == block["block_id"] and
                          len(row["parts"]) == 1 and row["parts"][0]["row"] == index]
            if reason is None and len(candidates) != 1:
                reason = "native_row_unproved"
            if reason is None:
                row = candidates[0]
                status = _option_status(cells[2], rule)
                reason = None if status == "selected_prohibition" else status
            if reason is None:
                matching = [item for item in requirements if item.get("text") == row["quote"]]
                if not matching:
                    reason = "source_requirement_missing"
                elif any(not item.get("citations") or not all(
                        _citation_matches(cite, row, block["unit_id"]) for cite in item["citations"]) for item in matching):
                    reason = "source_requirement_invalid"
        if reason:
            unresolved.append({"operator": operator, "detection_status": _DETECTION_STATUS[reason],
                               "reason": reason, "source_id": tender_source_id,
                               "block_ids": list(dict.fromkeys(hit[0]["block_id"] for hit in hits))})
            continue
        parent = row["tables"][0]
        source = {"source_id": tender_source_id, "source_sha256": tender_source_sha256,
                  "unit_id": block["unit_id"], "page": parent["page"], "block_id": block["block_id"],
                  "locator": block["locator"], "ordinal": block["ordinal"], "row": index,
                  "cells": [{"column": ref["column"], "start": ref["start"], "end": ref["end"],
                             "text": cells[ref["column"]], "text_sha256": _text_hash(cells[ref["column"]])}
                            for ref in row["cell_refs"]],
                  "quote": row["quote"], "quote_sha256": row["quote_sha256"],
                  "parent_grid": {key: copy.deepcopy(block.get(key, [])) for key in ("rows", "merges", "column_widths", "header_rows")},
                  "parent_grid_sha256": parent["grid_sha256"], "parent_text_sha256": parent["text_sha256"],
                  "table_row_id": row["table_row_id"], "table_row_proof_sha256": row["proof_sha256"]}
        proof = {"rule": "native_pdf_selected_project_row_v1", "operator": operator, "source": source,
                 "source_requirements": [{"requirement_id": item["requirement_id"], "requirement_sha256": _hash(item)} for item in matching],
                 "instruction": rule["instruction"], "forbidden_field": rule["forbidden"], "review_status": "not_checked"}
        digest = _hash(proof)
        constraints.append({"constraint_id": "PJC-" + digest[:24], **proof, "proof_sha256": digest})
    source_set = {"schema_version": "project-constraints-v2", "source_id": tender_source_id,
                  "source_sha256": tender_source_sha256, "source_reading_sha256": _hash(document),
                  "constraints": constraints, "unresolved": unresolved}
    return ProjectConstraintSet.model_validate({**source_set, "source_set_sha256": _hash(source_set)})


def validate_project_constraint_sources(constraints: ProjectConstraintSet | dict, parsed: dict,
                                        requirements: list[dict], *, tender_source_id: str,
                                        tender_source_sha256: str) -> ProjectConstraintSet:
    frozen = ProjectConstraintSet.model_validate(constraints)
    rebuilt = extract_project_constraints(parsed, requirements, tender_source_id=tender_source_id,
                                          tender_source_sha256=tender_source_sha256)
    if frozen != rebuilt:
        raise ProjectConstraintError("PROJECT_CONSTRAINT_SOURCE_BINDING_CHANGED")
    return frozen


def bind_project_constraints_to_task(constraints: ProjectConstraintSet | dict, task_id: str,
                                    requirement_refs: list[str], requirements: list[dict]) -> TaskProjectConstraints:
    """Explicitly bind project choices to a caller-selected concrete task.

    Selection of affected technical tasks remains N3's responsibility. These
    distant project choices are never presented as contiguous parent context.
    All finite detection states are retained; only proved prohibitions become
    instructions. Unknown/unsupported sibling states do not block this binding.
    """
    frozen = ProjectConstraintSet.model_validate(constraints)
    index = _requirement_index(requirements)
    if not requirement_refs or len(set(requirement_refs)) != len(requirement_refs) or any(ref not in index for ref in requirement_refs):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_TARGET_INVALID")
    for constraint in frozen.constraints:
        if any(link.requirement_id not in index or _hash(index[link.requirement_id]) != link.requirement_sha256
               for link in constraint.source_requirements):
            raise ProjectConstraintError("PROJECT_CONSTRAINT_SOURCE_REQUIREMENT_CHANGED")
    payload = {"rule": "selected_project_prohibitions_to_explicit_task_v2", "task_id": task_id,
               "source_id": frozen.source_id, "source_sha256": frozen.source_sha256,
               "source_reading_sha256": frozen.source_reading_sha256, "source_set_sha256": frozen.source_set_sha256,
               "target_requirements": [{"requirement_id": ref, "requirement_sha256": _hash(index[ref])} for ref in requirement_refs],
               "constraints": [item.model_dump(mode="json") for item in frozen.constraints],
               "unresolved": [item.model_dump(mode="json") for item in frozen.unresolved], "applicability_status": "not_checked"}
    digest = _hash(payload)
    return TaskProjectConstraints.model_validate({"binding_id": "PJCT-" + digest[:24], **payload, "binding_sha256": digest})


def validate_project_constraint_task_sources(binding: TaskProjectConstraints | dict,
                                             constraints: ProjectConstraintSet | dict,
                                             requirements: list[dict]) -> TaskProjectConstraints:
    """Recheck frozen target identities against the full normalized N2 set.

    ``constraints`` must first be source-revalidated against frozen N1 by
    ``validate_project_constraint_sources``; neither API reads artifact paths.
    """
    frozen = TaskProjectConstraints.model_validate(binding)
    rebuilt = bind_project_constraints_to_task(constraints, frozen.task_id,
        [item.requirement_id for item in frozen.target_requirements], requirements)
    if frozen != rebuilt:
        raise ProjectConstraintError("PROJECT_CONSTRAINT_TARGET_BINDING_CHANGED")
    return frozen


def validate_task_project_constraints(task: dict, expected_binding: TaskProjectConstraints | dict, *,
                                      supplied_binding: dict | None = None) -> TaskProjectConstraints:
    """Gate serialized/recovered task content before any provider invocation.

    The expected binding must come from the frozen N3 plan, independently of
    the serialized request being checked. ``supplied_binding`` is the actual
    serialized context.project_constraints (or equivalent execution-unit field),
    not a new WritingTask field. Recomputing the expectation from that request
    would hide a removed constraint.
    """
    expected = TaskProjectConstraints.model_validate(expected_binding)
    if (task.get("unit_id") != expected.task_id or
            task.get("requirement_refs") != [item.requirement_id for item in expected.target_requirements] or
            not isinstance(supplied_binding, dict) or
            _hash(supplied_binding) != _hash(expected.model_dump(mode="json")) or
            not isinstance(task.get("forbidden_fields"), list) or
            any(item.forbidden_field not in task["forbidden_fields"] for item in expected.constraints)):
        raise ProjectConstraintError("PROJECT_CONSTRAINT_TASK_BINDING_MISSING_OR_CHANGED")
    return expected


def project_constraint_trace_attributes(binding: TaskProjectConstraints | dict) -> dict:
    """Identity-only trace attributes; native text and grids remain private."""
    value = TaskProjectConstraints.model_validate(binding)
    return {"project_constraint_binding_id": value.binding_id,
            "project_constraint_binding_sha256": value.binding_sha256,
            "project_constraint_source_set_sha256": value.source_set_sha256,
            "project_constraint_source_reading_sha256": value.source_reading_sha256,
            "project_constraint_ids": [item.constraint_id for item in value.constraints],
            "project_constraint_proof_sha256s": [item.proof_sha256 for item in value.constraints],
            "project_constraint_target_requirement_ids": [item.requirement_id for item in value.target_requirements]}
