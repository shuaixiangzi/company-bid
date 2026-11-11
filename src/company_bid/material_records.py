"""Native historical declarations from explicit, hash-bound table scopes.

This module never selects current-project staff, verifies qualifications, fills
prices, reads images or calls a model. Every value remains an attributed
declaration from the selected source. No legacy package dependency is used.
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import defaultdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .slots import has_fill_marker, whole_cell_slot
from .storage import digest


MATERIAL_RECORD_VERSION = "native-material-records-v2"
Text = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class RecordContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RecordScope(RecordContract):
    kind: Literal["personnel_declaration", "historical_performance"]
    source_id: Text
    source_sha256: Sha256
    locator: Text
    merge_group: Text | None = None


class CellCoordinate(RecordContract):
    row: int = Field(ge=0)
    column: int = Field(ge=0)


class CellCitation(RecordContract):
    source_id: Text
    source_sha256: Sha256
    unit_id: Text
    block_id: Text
    locator: Text
    page: int | None = Field(default=None, ge=1)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    logical_row: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    quote: str
    quote_sha256: Sha256
    header_quotes: list[Text] = Field(min_length=1)
    header_cells: list[CellCoordinate] = Field(min_length=1)
    table_sha256: Sha256
    basis: Literal["native_table_cell_v1"] = "native_table_cell_v1"

    @model_validator(mode="after")
    def exact_quote_and_merge_span(self):
        if hashlib.sha256(self.quote.encode("utf-8")).hexdigest() != self.quote_sha256:
            raise ValueError("cell quote hash mismatch")
        if not self.row <= self.logical_row < self.row + self.row_span:
            raise ValueError("logical row is outside native cell merge")
        if len(self.header_quotes) != len(self.header_cells):
            raise ValueError("header values require exact source coordinates")
        return self


class NativeValue(RecordContract):
    value: str
    unit: str | None = None
    status: Literal["declared", "missing"]
    citations: list[CellCitation] = Field(min_length=1)

    @model_validator(mode="after")
    def value_is_cited(self):
        if any(citation.quote != self.value for citation in self.citations):
            raise ValueError("native value must equal each exact cell quote")
        if self.status == "declared" and (whole_cell_slot(self.value) or has_fill_marker(self.value)):
            raise ValueError("blank or template marker cannot be a declaration")
        if self.unit is not None and any(not any(re.search(r"[（(]" + re.escape(self.unit) + r"[）)]", _norm(header))
                                               for header in citation.header_quotes) for citation in self.citations):
            raise ValueError("unit must be printed in the cited native header")
        return self


class MaterialRowCitation(RecordContract):
    source_id: Text
    source_sha256: Sha256
    unit_id: Text
    block_id: Text
    locator: Text
    page: int | None = Field(default=None, ge=1)
    row: int = Field(ge=0)
    row_values: list[str] = Field(min_length=1)
    row_sha256: Sha256
    table_sha256: Sha256
    basis: Literal["native_table_row_v1"] = "native_table_row_v1"

    @model_validator(mode="after")
    def exact_row(self):
        if digest(self.row_values) != self.row_sha256:
            raise ValueError("native row hash mismatch")
        return self


class CertificateDeclaration(RecordContract):
    declaration_id: Text
    status: Literal["usable_provisional", "unresolved", "conflict"]
    certificate_name: NativeValue
    level: NativeValue
    number: NativeValue
    specialty: NativeValue
    note: NativeValue
    citations: list[MaterialRowCitation] = Field(min_length=1)
    independently_verified: Literal[False] = False


class PersonnelFields(RecordContract):
    name: NativeValue
    roles: list[NativeValue] = Field(min_length=1)
    titles: list[NativeValue] = Field(min_length=1)
    certificate_declarations: list[CertificateDeclaration] = Field(min_length=1)


class HistoricalPerformanceFields(RecordContract):
    sequence: NativeValue
    project_name: NativeValue
    project_lead: NativeValue
    client: NativeValue
    service_location: NativeValue
    service_date: NativeValue
    contract_amount: NativeValue
    historical_bid_staff_participation: NativeValue


class HeaderColumn(RecordContract):
    column: int = Field(ge=0)
    field: Text
    quotes: list[Text] = Field(min_length=1)
    cells: list[CellCoordinate] = Field(min_length=1)
    unit: str | None = None


class NativeTableMerge(RecordContract):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)


class TableLocation(RecordContract):
    source_id: Text
    source_sha256: Sha256
    unit_id: Text
    block_id: Text
    locator: Text
    page: int | None = Field(default=None, ge=1)


class TableEvidence(RecordContract):
    """Internal source-view consistency, not independent proof of file contents.

    Preparation must still bind this grid to a verified frozen N1 original.
    A self-reported coherent grid/hash cannot establish that external trust.
    """
    kind: Literal["personnel_declaration", "historical_performance"]
    table_sha256: Sha256
    rows: list[list[str]] = Field(min_length=1)
    merges: list[NativeTableMerge] = Field(default_factory=list)
    header_rows: int = Field(ge=1)
    header_map: list[HeaderColumn] = Field(min_length=1)
    locations: list[TableLocation] = Field(min_length=1)

    @model_validator(mode="after")
    def reconstruct_native_grid_and_headers(self):
        table = {"rows": self.rows, "merges": [merge.model_dump() for merge in self.merges]}
        rows, owners, _ = _grid(table)
        if _table_digest(rows, table["merges"]) != self.table_sha256:
            raise ValueError("table evidence hash mismatch")
        layout = _headers(self.kind, rows, owners)
        if layout is None or layout != (self.header_rows, [column.model_dump(mode="json") for column in self.header_map]):
            raise ValueError("table header evidence does not match native grid")
        identities = [_location_key(location) for location in self.locations]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate table evidence location")
        return self


class _MaterialRecord(RecordContract):
    record_id: Text
    status: Literal["usable_provisional", "unresolved", "conflict"]
    provenance: Literal["derived_from_bid"] = "derived_from_bid"
    assurance: Literal["user_authorized_declaration_not_independent_proof"] = "user_authorized_declaration_not_independent_proof"
    usage_scope: Literal["historical_declaration_only"] = "historical_declaration_only"
    citations: list[MaterialRowCitation] = Field(min_length=1)
    table_evidence: list[TableEvidence] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    independently_verified: Literal[False] = False
    current_project_assignment: Literal[False] = False

    @model_validator(mode="after")
    def native_references_are_internally_consistent(self):
        _validate_record_evidence(self)
        return self


class PersonnelDeclarationRecord(_MaterialRecord):
    kind: Literal["personnel_declaration"] = "personnel_declaration"
    fields: PersonnelFields


class HistoricalPerformanceRecord(_MaterialRecord):
    kind: Literal["historical_performance"] = "historical_performance"
    fields: HistoricalPerformanceFields
    current_project_price: Literal[False] = False


MaterialRecord = Annotated[PersonnelDeclarationRecord | HistoricalPerformanceRecord, Field(discriminator="kind")]


class RowProcessing(RecordContract):
    row: int = Field(ge=0)
    status: Literal["prepared", "empty", "missing_name", "unresolved"]
    record_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class RecordProcessing(RecordContract):
    scope_id: Text
    kind: Literal["personnel_declaration", "historical_performance"]
    source_id: Text
    source_sha256: Sha256
    locator: Text
    status: Literal["prepared", "partial", "conflict", "source_unparsed", "scope_not_found", "unread", "unknown_headers", "unsupported_origin", "not_authorized"]
    header_rows: int = Field(default=0, ge=0)
    header_map: list[HeaderColumn] = Field(default_factory=list)
    data_row_count: int = Field(default=0, ge=0)
    record_ids: list[str] = Field(default_factory=list)
    row_processing: list[RowProcessing] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    table_sha256: Sha256 | None = None
    duplicate_table_of: str | None = None


class MaterialPreparationResult(RecordContract):
    schema_version: Literal["material.records.v2"] = "material.records.v2"
    producer_version: Literal["native-material-records-v2"] = MATERIAL_RECORD_VERSION
    status: Literal["prepared", "partial", "conflict"]
    material_records: list[MaterialRecord] = Field(default_factory=list)
    record_processing: list[RecordProcessing] = Field(default_factory=list)


class MaterialRecordError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _check(condition, code):
    if not condition:
        raise MaterialRecordError(code)


def _id(prefix, value):
    return prefix + digest(value)[:24]


def _norm(value):
    return re.sub(r"[\s/／]+", "", value)


_PERSON_FIELDS = {
    "layer": {"层级"}, "role": {"职务", "岗位"}, "name": {"姓名", "人员姓名"}, "title": {"职称", "技术职称"},
    "certificate_name": {"证书名称", "资格证书名称"}, "level": {"级别", "等级", "证书等级"},
    "number": {"证号", "证书编号", "证书号码"}, "specialty": {"专业", "专业类别"}, "note": {"备注"},
}
_HISTORY_FIELDS = {
    "sequence": {"序号"}, "project_name": {"项目名称", "历史项目名称"}, "project_lead": {"项目负责人"},
    "client": {"发包人", "建设单位", "业主名称", "客户名称"}, "service_location": {"服务地点", "项目地点"},
    "service_date": {"服务日期", "服务时间", "履约日期"}, "contract_amount": {"合同额", "合同金额"},
    "historical_bid_staff_participation": {"本项目拟投入人员是否参与", "拟投入人员是否参与"},
}


def _grid(table):
    rows = table.get("rows", [])
    _check(rows and isinstance(rows[0], list) and rows[0] and all(isinstance(row, list) and len(row) == len(rows[0]) and
           all(isinstance(value, str) for value in row) for row in rows), "MATERIAL_TABLE_GRID_INVALID")
    owners, spans = {}, {}
    for merge in table.get("merges", []):
        r, c, rs, cs = merge.get("row"), merge.get("column"), merge.get("row_span", 1), merge.get("col_span", 1)
        _check(all(type(v) is int for v in (r, c, rs, cs)) and min(r, c) >= 0 and min(rs, cs) >= 1 and rs * cs > 1 and
               r + rs <= len(rows) and c + cs <= len(rows[0]), "MATERIAL_TABLE_MERGE_INVALID")
        spans[(r, c)] = (rs, cs)
        for ri in range(r, r + rs):
            for ci in range(c, c + cs):
                _check((ri, ci) not in owners, "MATERIAL_TABLE_MERGE_INVALID")
                _check((ri, ci) == (r, c) or not rows[ri][ci], "MATERIAL_COVERED_CELL_NOT_EMPTY")
                owners[(ri, ci)] = (r, c)
    return rows, owners, spans


def _headers(kind, rows, owners):
    aliases = _PERSON_FIELDS if kind == "personnel_declaration" else _HISTORY_FIELDS
    required = set(aliases) - {"layer"}
    # Explicitly supported native geometries: a flat header or a two-row
    # parent/leaf personnel header. Unknown shapes do not become guesses.
    for height in ([2, 1] if kind == "personnel_declaration" else [1]):
        if len(rows) <= height:
            continue
        columns, used = [], set()
        for c in range(len(rows[0])):
            quotes, coords = [], []
            for r in range(height):
                ar, ac = owners.get((r, c), (r, c))
                quote = rows[ar][ac]
                if quote.strip() and {"row": ar, "column": ac} not in coords:
                    quotes.append(quote); coords.append({"row": ar, "column": ac})
            if not quotes:
                break
            key = _norm(quotes[-1])
            unit = None
            unit_match = re.fullmatch(r"(合同额|合同金额)[（(](万元|元)[）)]", key)
            if unit_match:
                key, unit = unit_match.groups()
            field = next((name for name, values in aliases.items() if key in values), None)
            if field is None or field in used:
                break
            # A two-row parent is accepted only for the known certificate group.
            if height == 2 and len(quotes) > 1 and (field not in {"certificate_name", "level", "number", "specialty"} or
                    _norm(quotes[0]) not in {"执业或职业资格证明", "执业资格证明", "职业资格证明"}):
                break
            used.add(field)
            columns.append({"column": c, "field": field, "quotes": quotes, "cells": coords, "unit": unit})
        if len(columns) == len(rows[0]) and required <= used:
            return height, columns
    return None


def _normalized_merges(merges):
    return [{"row": merge["row"], "column": merge["column"], "row_span": merge.get("row_span", 1), "col_span": merge.get("col_span", 1)}
            for merge in merges]


def _table_digest(rows, merges):
    return digest({"rows": rows, "merges": _normalized_merges(merges)})


def _location_key(value):
    get = (lambda key: getattr(value, key)) if isinstance(value, BaseModel) else value.get
    return tuple(get(key) for key in ("source_id", "source_sha256", "unit_id", "block_id", "locator", "page"))


def _row_key(value):
    return (*_location_key(value), value.row if isinstance(value, BaseModel) else value["row"])


def _validate_record_evidence(record):
    tables, evidence_keys = {}, set()
    for evidence in record.table_evidence:
        if evidence.kind != record.kind or (evidence.kind, evidence.table_sha256) in evidence_keys:
            raise ValueError("record table evidence must be unique and match its kind")
        evidence_keys.add((evidence.kind, evidence.table_sha256))
        rows, owners, spans = _grid({"rows": evidence.rows, "merges": [merge.model_dump() for merge in evidence.merges]})
        if any(owners.get((r, c), (r, c))[0] < evidence.header_rows for r in range(evidence.header_rows, len(rows)) for c in range(len(rows[0]))):
            raise ValueError("record data cannot inherit a header cell")
        for location in evidence.locations:
            key = _location_key(location)
            if key in tables:
                raise ValueError("one source location cannot claim different table evidence")
            tables[key] = (evidence, rows, owners, spans, {header.field: header for header in evidence.header_map})

    def validate_row(citation):
        context = tables.get(_location_key(citation))
        if context is None:
            raise ValueError("record row source has no matching table location")
        evidence, rows, _, _, _ = context
        if citation.table_sha256 != evidence.table_sha256 or not evidence.header_rows <= citation.row < len(rows):
            raise ValueError("record row table hash or row index differs from evidence")
        if citation.row_values != rows[citation.row]:
            raise ValueError("record row values differ from the original grid")
        return _row_key(citation)

    record_rows = {}
    for citation in record.citations:
        key = validate_row(citation)
        if key in record_rows:
            raise ValueError("duplicate record row citation")
        record_rows[key] = citation
    if {_location_key(row) for row in record.citations} != set(tables):
        raise ValueError("record includes an unused table evidence location")

    def validate_value(value, field, allowed_rows):
        touched = set()
        for citation in value.citations:
            context = tables.get(_location_key(citation))
            if context is None:
                raise ValueError("field citation source has no matching record table")
            evidence, rows, owners, spans, headers = context
            logical_key = (*_location_key(citation), citation.logical_row)
            if logical_key not in allowed_rows or citation.table_sha256 != evidence.table_sha256 or field not in headers:
                raise ValueError("field citation does not belong to the declared record row")
            header = headers[field]
            anchor = owners.get((citation.logical_row, header.column), (citation.logical_row, header.column))
            if (citation.row, citation.column) != anchor or (citation.row_span, citation.col_span) != spans.get(anchor, (1, 1)):
                raise ValueError("field anchor or merge differs from the native grid")
            if citation.quote != rows[anchor[0]][anchor[1]]:
                raise ValueError("field quote differs from its native anchor cell")
            if citation.header_quotes != header.quotes or [cell.model_dump() for cell in citation.header_cells] != [cell.model_dump() for cell in header.cells]:
                raise ValueError("field header citation differs from verified table header")
            if value.unit != header.unit:
                raise ValueError("field unit differs from original table header")
            status = "missing" if whole_cell_slot(value.value) or has_fill_marker(value.value) else "declared"
            if value.status != status:
                raise ValueError("field presence status differs from original cell")
            logical_owners = [owners.get((citation.logical_row, h.column), (citation.logical_row, h.column)) for h in headers.values()]
            if len(set(logical_owners)) != len(logical_owners):
                raise ValueError("a data merge crosses distinct semantic fields")
            touched.add(logical_key)
        return touched

    if record.kind == "historical_performance":
        for name in _HISTORY_FIELDS:
            if validate_value(getattr(record.fields, name), name, record_rows) != set(record_rows):
                raise ValueError("historical value must preserve every declared source row")
    else:
        if validate_value(record.fields.name, "name", record_rows) != set(record_rows):
            raise ValueError("person name does not cover every claimed native row")
        for field, values in (("role", record.fields.roles), ("title", record.fields.titles)):
            covered = set()
            for value in values:
                covered.update(validate_value(value, field, record_rows))
            if covered != set(record_rows):
                raise ValueError("person field list does not cover every claimed row")
        declared_rows = set()
        for declaration in record.fields.certificate_declarations:
            rows = {}
            for citation in declaration.citations:
                key = validate_row(citation)
                if key not in record_rows or citation != record_rows[key] or key in rows:
                    raise ValueError("certificate row is not one exact row of its person record")
                rows[key] = citation
            if declared_rows & set(rows):
                raise ValueError("one native row cannot silently become two certificate declarations")
            declared_rows.update(rows)
            for field in ("certificate_name", "level", "number", "specialty", "note"):
                if validate_value(getattr(declaration, field), field, rows) != set(rows):
                    raise ValueError("certificate field does not preserve its exact source rows")
        if declared_rows != set(record_rows):
            raise ValueError("certificate declarations do not cover all native person rows")


def _make_table_evidence(scope, unit, table, rows, height, headers, table_hash):
    return TableEvidence(kind=scope.kind, table_sha256=table_hash, rows=copy.deepcopy(rows),
        merges=_normalized_merges(table.get("merges", [])), header_rows=height, header_map=headers,
        locations=[{"source_id": scope.source_id, "source_sha256": scope.source_sha256, "unit_id": unit["unit_id"],
                    "block_id": table["block_id"], "locator": table["locator"], "page": unit.get("page")}]).model_dump(mode="json")


def _merge_table_evidence(target, additions):
    for addition in additions:
        existing = next((item for item in target if item["kind"] == addition["kind"] and item["table_sha256"] == addition["table_sha256"]), None)
        if existing is None:
            target.append(copy.deepcopy(addition))
        else:
            if any(existing[key] != addition[key] for key in ("rows", "merges", "header_rows", "header_map")):
                raise MaterialRecordError("MATERIAL_TABLE_EVIDENCE_MERGE_MISMATCH")
            _unique_extend(existing["locations"], addition["locations"])


def _row_citation(scope, unit, table, rows, r, table_hash):
    return {"source_id": scope.source_id, "source_sha256": scope.source_sha256, "unit_id": unit["unit_id"],
            "block_id": table["block_id"], "locator": table["locator"], "page": unit.get("page"), "row": r,
            "row_values": copy.deepcopy(rows[r]), "row_sha256": digest(rows[r]), "table_sha256": table_hash}


def _value(scope, unit, table, rows, owners, spans, header, r, table_hash):
    c = header["column"]
    ar, ac = owners.get((r, c), (r, c))
    value = rows[ar][ac]
    rs, cs = spans.get((ar, ac), (1, 1))
    citation = {"source_id": scope.source_id, "source_sha256": scope.source_sha256, "unit_id": unit["unit_id"],
        "block_id": table["block_id"], "locator": table["locator"], "page": unit.get("page"), "row": ar, "column": ac,
        "logical_row": r, "row_span": rs, "col_span": cs, "quote": value, "quote_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "header_quotes": header["quotes"], "header_cells": header["cells"], "table_sha256": table_hash}
    return NativeValue(value=value, unit=header["unit"], status="missing" if whole_cell_slot(value) or has_fill_marker(value) else "declared",
                       citations=[CellCitation.model_validate(citation)]).model_dump(mode="json")


def _scope_rows(scope, source, document, adoptable_sources):
    processing = {"scope_id": _id("MS-", scope.model_dump()), "kind": scope.kind, "source_id": scope.source_id,
                  "source_sha256": scope.source_sha256, "locator": scope.locator, "status": "source_unparsed"}
    if document is None:
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    _check(document.get("sha256") == scope.source_sha256, "MATERIAL_PARSED_HASH_MISMATCH")
    matches = [(unit, table) for unit in document.get("units", []) for table in unit.get("blocks", [])
               if table.get("kind") == "table" and table.get("locator") == scope.locator]
    if len(matches) != 1:
        processing.update(status="scope_not_found", issues=["selected_table_not_unique_or_not_found"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    unit, table = matches[0]
    _check(unit.get("source_id") == scope.source_id and table.get("source_id") == scope.source_id, "MATERIAL_TABLE_SOURCE_MISMATCH")
    if adoptable_sources.get(scope.source_id) != scope.source_sha256:
        processing.update(status="not_authorized", issues=["explicit_hash_bound_fact_adoption_scope_required"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    if source.get("role") != "reference_bid" and source.get("provenance") != "derived_from_bid":
        processing.update(status="not_authorized", issues=["selected_source_is_not_supported_as_a_historical_bid_declaration"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    if unit.get("coverage", {}).get("status") != "read" or unit.get("coverage", {}).get("unread_objects"):
        processing.update(status="unread", issues=["whole_selected_native_table_must_be_read"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    if table.get("origin", "native") != "native":
        processing.update(status="unsupported_origin", issues=["image_and_ocr_records_are_not_automatically_adopted"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    rows, owners, spans = _grid(table)
    table_hash = _table_digest(rows, table.get("merges", []))
    layout = _headers(scope.kind, rows, owners)
    processing["table_sha256"] = table_hash
    if layout is None:
        processing.update(status="unknown_headers", issues=["header_geometry_or_labels_not_supported"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    height, headers = layout
    if any(owners.get((r, c), (r, c))[0] < height for r in range(height, len(rows)) for c in range(len(rows[0]))):
        processing.update(status="unknown_headers", issues=["header_merge_crosses_into_data_rows"])
        return {"processing": processing, "rows": [], "group": scope.merge_group or scope.source_sha256}
    processing.update(status="prepared", header_rows=height, header_map=headers, data_row_count=len(rows) - height, row_processing=[])
    prepared = []
    for r in range(height, len(rows)):
        if not any(value.strip() for value in rows[r]):
            processing["row_processing"].append({"row": r, "status": "empty"})
            continue
        mapped_owners = [owners.get((r, header["column"]), (r, header["column"])) for header in headers]
        if len(set(mapped_owners)) != len(mapped_owners):
            processing["status"] = "partial"
            processing["row_processing"].append({"row": r, "status": "unresolved", "reason": "horizontal_merge_crosses_distinct_fields"})
            continue
        fields = {header["field"]: _value(scope, unit, table, rows, owners, spans, header, r, table_hash) for header in headers}
        name_field = fields["name"] if scope.kind == "personnel_declaration" else fields["project_name"]
        if name_field["status"] == "missing":
            processing["status"] = "partial"
            processing["row_processing"].append({"row": r, "status": "missing_name", "reason": "no_unmerged_name_forward_fill"})
            continue
        person_group = None
        if scope.kind == "personnel_declaration":
            name_citation = name_field["citations"][0]
            if name_citation["row_span"] > 1:
                person_group = ["merged_name_cell", name_citation["row"], name_citation["column"]]
            else:
                person_group = ["native_unmerged_row", r]
        prepared.append({"fields": fields, "citation": _row_citation(scope, unit, table, rows, r, table_hash), "person_group": person_group})
        processing["row_processing"].append({"row": r, "status": "prepared"})
    return {"processing": processing, "rows": prepared, "group": scope.merge_group or scope.source_sha256,
            "table_evidence": _make_table_evidence(scope, unit, table, rows, height, headers, table_hash)}


def _unique_extend(target, values):
    known = {digest(value) for value in target}
    for value in values:
        if digest(value) not in known:
            target.append(copy.deepcopy(value)); known.add(digest(value))


def _merge_value(target, source):
    _check(target["value"] == source["value"] and target.get("unit") == source.get("unit"), "MATERIAL_VALUE_MERGE_MISMATCH")
    _unique_extend(target["citations"], source["citations"])


def _add_value(values, source):
    existing = next((value for value in values if value["value"] == source["value"] and value.get("unit") == source.get("unit")), None)
    if existing is None:
        values.append(copy.deepcopy(source))
    else:
        _merge_value(existing, source)


def _merge_records(outputs):
    people, projects = {}, {}
    scope_to_records = defaultdict(set)
    row_to_records = {}
    for output in outputs:
        kind, group = output["processing"]["kind"], output["group"]
        for row in output["rows"]:
            fields, citation = row["fields"], row["citation"]
            if kind == "personnel_declaration":
                key = (group, citation["table_sha256"], digest(row["person_group"]), fields["name"]["value"])
                record_id = _id("MR-P-", list(key))
                record = people.get(key)
                if record is None:
                    record = {"record_id": record_id, "kind": kind, "status": "usable_provisional", "fields": {
                        "name": copy.deepcopy(fields["name"]), "roles": [], "titles": [], "certificate_declarations": []}, "citations": [], "table_evidence": [], "issues": []}
                    people[key] = record
                else:
                    _merge_value(record["fields"]["name"], fields["name"])
                _add_value(record["fields"]["roles"], fields["role"])
                _add_value(record["fields"]["titles"], fields["title"])
                declaration_fields = {name: fields[name] for name in ("certificate_name", "level", "number", "specialty", "note")}
                declaration_key = [[name, value["value"], value.get("unit")] for name, value in declaration_fields.items()]
                declaration_id = _id("MD-", [record_id, declaration_key])
                declaration = next((item for item in record["fields"]["certificate_declarations"] if item["declaration_id"] == declaration_id), None)
                if declaration is None:
                    declaration = {"declaration_id": declaration_id, **copy.deepcopy(declaration_fields),
                        "status": "unresolved" if any(declaration_fields[name]["status"] == "missing" for name in ("certificate_name", "number")) else "usable_provisional",
                        "citations": []}
                    record["fields"]["certificate_declarations"].append(declaration)
                else:
                    for name, value in declaration_fields.items():
                        _merge_value(declaration[name], value)
                _unique_extend(declaration["citations"], [citation])
                if declaration["status"] == "unresolved":
                    record["status"] = "unresolved"
                    _unique_extend(record["issues"], ["one_or_more_certificate_declarations_have_missing_native_fields"])
            else:
                content_key = [[name, fields[name]["value"], fields[name].get("unit")] for name in _HISTORY_FIELDS]
                key = (group, digest(content_key))
                record_id = _id("MR-H-", [group, content_key])
                record = projects.get(key)
                if record is None:
                    record = {"record_id": record_id, "kind": kind, "status": "unresolved" if any(fields[name]["status"] == "missing" for name in
                        ("project_name", "client", "service_date", "contract_amount")) or fields["contract_amount"].get("unit") is None else "usable_provisional",
                        "fields": {name: copy.deepcopy(fields[name]) for name in _HISTORY_FIELDS}, "citations": [], "table_evidence": [], "issues": []}
                    if fields["contract_amount"].get("unit") is None:
                        record["issues"].append("historical_amount_unit_not_stated_in_supported_source_header")
                    projects[key] = record
                else:
                    for name in _HISTORY_FIELDS:
                        _merge_value(record["fields"][name], fields[name])
            _unique_extend(record["citations"], [citation])
            _merge_table_evidence(record["table_evidence"], [output["table_evidence"]])
            scope_to_records[output["processing"]["scope_id"]].add(record_id)
            row_to_records[(output["processing"]["scope_id"], citation["row"])] = record_id
    # Only complete duplicate declarations from separate tables may be merged.
    # A name alone is never a global person identity.
    unique_people, duplicates, replacements = [], {}, {}
    for key, record in people.items():
        def native_signature(value):
            return [value["value"], value.get("unit")]
        fields = record["fields"]
        signature = [native_signature(fields["name"]), [native_signature(value) for value in fields["roles"]],
                     [native_signature(value) for value in fields["titles"]],
                     [[native_signature(declaration[name]) for name in ("certificate_name", "level", "number", "specialty", "note")]
                      for declaration in fields["certificate_declarations"]]]
        duplicate_key = (key[0], digest(signature))
        existing = next((other for table_hash, other in duplicates.get(duplicate_key, []) if table_hash != key[1]), None)
        if existing is None:
            unique_people.append((key[0], record))
            duplicates.setdefault(duplicate_key, []).append((key[1], record))
            continue
        replacements[record["record_id"]] = existing["record_id"]
        _merge_value(existing["fields"]["name"], fields["name"])
        for field in ("roles", "titles"):
            for value in fields[field]:
                _add_value(existing["fields"][field], value)
        for target, source in zip(existing["fields"]["certificate_declarations"], fields["certificate_declarations"]):
            for field in ("certificate_name", "level", "number", "specialty", "note"):
                _merge_value(target[field], source[field])
            _unique_extend(target["citations"], source["citations"])
        _unique_extend(existing["citations"], record["citations"])
        _merge_table_evidence(existing["table_evidence"], record["table_evidence"])
    for scope_id, identities in scope_to_records.items():
        scope_to_records[scope_id] = {replacements.get(identity, identity) for identity in identities}
    row_to_records = {key: replacements.get(identity, identity) for key, identity in row_to_records.items()}
    name_groups = defaultdict(list)
    for group, record in unique_people:
        name_groups[(group, record["fields"]["name"]["value"])].append(record)
    for candidates in name_groups.values():
        if len(candidates) > 1:
            for record in candidates:
                record["status"] = "conflict"
                _unique_extend(record["issues"], ["same_native_name_in_distinct_source_groups_not_resolved"])
    records = [*(record for _, record in unique_people), *projects.values()]
    # Different declared certificates may coexist. A reused nonempty number
    # with incompatible declarations/names remains a visible conflict.
    certificate_index = defaultdict(list)
    for _, record in unique_people:
        for declaration in record["fields"]["certificate_declarations"]:
            if declaration["number"]["status"] == "declared":
                certificate_index[declaration["number"]["value"]].append((record, declaration))
    for entries in certificate_index.values():
        signatures = {(record["fields"]["name"]["value"], declaration["certificate_name"]["value"], declaration["level"]["value"], declaration["specialty"]["value"]) for record, declaration in entries}
        if len(signatures) > 1:
            for record, declaration in entries:
                declaration["status"] = record["status"] = "conflict"
                _unique_extend(record["issues"], ["certificate_number_has_conflicting_native_declarations"])
    project_index = defaultdict(list)
    for (group, _), record in projects.items():
        project_index[(group, *(record["fields"][name]["value"] for name in ("project_name", "client", "service_date")))].append(record)
    for candidates in project_index.values():
        if len(candidates) > 1:
            for record in candidates:
                record["status"] = "conflict"
                _unique_extend(record["issues"], ["same_historical_project_key_has_multiple_original_declarations"])
    processing = []
    table_seen = {}
    for output in outputs:
        item = copy.deepcopy(output["processing"])
        item["record_ids"] = sorted(scope_to_records[item["scope_id"]])
        for row in item.get("row_processing", []):
            identity = row_to_records.get((item["scope_id"], row["row"]))
            if identity:
                row["record_ids"] = [identity]
        if any(record["status"] == "conflict" for record in records if record["record_id"] in item["record_ids"]):
            item["status"] = "conflict"
        table_key = (item["kind"], output["group"], item.get("table_sha256"))
        if item.get("table_sha256"):
            if table_key in table_seen:
                item["duplicate_table_of"] = table_seen[table_key]
            else:
                table_seen[table_key] = item["scope_id"]
        processing.append(item)
    status = "conflict" if any(record["status"] == "conflict" for record in records) else "partial" if any(
        item["status"] != "prepared" for item in processing) or any(record["status"] == "unresolved" for record in records) else "prepared"
    return MaterialPreparationResult(status=status, material_records=records, record_processing=processing).model_dump(mode="json")


def prepare_material_records(ctx, registered_sources: list[dict], parsed: dict, scopes: list[dict | RecordScope], adoptable_sources: dict[str, str]) -> dict:
    """Prepare only explicit native scopes; values are private checkpoint data."""
    try:
        selected = [scope if isinstance(scope, RecordScope) else RecordScope.model_validate(scope) for scope in scopes]
    except Exception as exc:
        raise MaterialRecordError("MATERIAL_RECORD_SCOPE_INVALID") from exc
    if not selected:
        return MaterialPreparationResult(status="prepared").model_dump(mode="json")
    source_map = {source["source_id"]: source for source in registered_sources}
    documents = {document["source_id"]: document for document in parsed.get("documents", [])}
    _check(len({digest(scope.model_dump()) for scope in selected}) == len(selected), "MATERIAL_DUPLICATE_SCOPE")
    outputs = []
    for scope in selected:
        _check(scope.source_id in source_map and source_map[scope.source_id]["sha256"] == scope.source_sha256,
               "MATERIAL_SCOPE_HASH_OR_SOURCE_MISMATCH")
        document = documents.get(scope.source_id)
        matches = [(unit, block) for unit in document.get("units", []) for block in unit.get("blocks", []) if block.get("locator") == scope.locator] if document else []
        inputs = {"producer_version": MATERIAL_RECORD_VERSION, "scope": scope.model_dump(), "source_sha256": scope.source_sha256,
                  "document_sha256": document.get("sha256") if document else None, "selected_units": matches,
                  "fact_adoption_allowed": adoptable_sources.get(scope.source_id) == scope.source_sha256}
        output = ctx.run_unit(_id("material-scope-", scope.model_dump()), inputs,
                             lambda scope=scope, document=document: _scope_rows(scope, source_map[scope.source_id], document, adoptable_sources))
        outputs.append(output)
    result = ctx.run_unit("material-records-assemble", {"producer_version": MATERIAL_RECORD_VERSION, "scope_outputs": outputs}, lambda: _merge_records(outputs))
    # Revalidate a reused checkpoint too; these records can enter Intake later.
    validated = MaterialPreparationResult.model_validate(result).model_dump(mode="json")
    ctx.capture("material-records-report", {"producer_version": MATERIAL_RECORD_VERSION, "status": validated["status"],
        "scope_count": len(selected), "record_count": len(validated["material_records"]), "record_processing": validated["record_processing"]})
    return validated
