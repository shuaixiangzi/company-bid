"""Local, immutable company-library preparation from selected reader outputs.

This is deterministic preparation, not a company-identity verification service.
Registered files are not read implicitly. Only sources actually present in the
supplied N1 result are inspected and frozen. No model or OCR is called here.

Configuration example::

    {"authorized_bid_fact_sources": {"BID": "<original sha256>"},
     "table_scopes": {"BID": ["word/document.xml/table[4]"]},
     "proof_selections": [{"source_id": "LICENSE", "title": "营业执照",
                            "pages": [2]}]}

The native company-profile table scope is required even for an authorized bid.
Price, personnel, performance, bank and signature fields remain unprocessed.
Proof entries are source/page/package-part selections, never ready Attachments.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import Fact, SourceRef
from .material_records import MATERIAL_RECORD_VERSION, RecordScope, prepare_material_records
from .slots import has_fill_marker, whole_cell_slot
from .storage import digest


LIBRARY_VERSION = "company-library-v2"


class LibraryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LibraryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    authorized_bid_fact_sources: dict[str, str] = Field(default_factory=dict)
    table_scopes: dict[str, list[str]] = Field(default_factory=dict)
    proof_selections: list[dict] = Field(default_factory=list)
    record_scopes: list[RecordScope] = Field(default_factory=list)


_FIELD_ALIASES = {
    "公司名称": ("公司名称", "企业名称", "单位名称", "投标人名称", "响应人名称", "供应商名称"),
    "统一社会信用代码": ("统一社会信用代码", "营业执照号", "营业执照注册号", "工商注册号"),
    "注册地址": ("注册地址", "注册住所", "住所", "企业注册地址"),
    "注册资本": ("注册资本", "注册资金"),
    "成立日期": ("成立日期", "成立时间", "企业成立时间", "企业成立日期"),
    "企业类型": ("企业类型", "公司类型", "企业性质", "单位性质"),
    "经营范围": ("经营范围", "业务范围"),
    "邮政编码": ("邮政编码", "邮编"),
}
_UNSUPPORTED = {
    "personnel": re.compile(r"人员|员工|负责人|法人|代表人|联系人|姓名|电话|手机|职称|项目经理"),
    "performance": re.compile(r"业绩|履约|项目案例|合同项目|客户"),
    "bank": re.compile(r"开户|银行|账号|账户|联行"),
    "price": re.compile(r"报价|金额|总价|单价|税|价款|费用|费率"),
    "signature": re.compile(r"签字|签名|签章|盖章|授权"),
}
_COMPANY_TABLE = re.compile(r"^(?:\d+(?:\.\d+)*[、．.]?\s*|[一二三四五六七八九十]+[、．.])?(?:投标人|响应人|供应商|公司|企业|单位)基本情况(?:表)?$")
_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百0-9]+[章节篇部]|\d+(?:\.\d+)*[、．.]?\s+|[一二三四五六七八九十]+[、．.])")


def _json(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return str(value) if isinstance(value, Path) else value


def _id(prefix, value):
    return prefix + digest(_json(value))[:20]


def _check(condition, code):
    if not condition:
        raise LibraryError(code)


def _capture(ctx, name, data, *, partial=False):
    ctx.capture(name, data, partial=partial)


def _norm(value):
    return re.sub(r"[\s：:]+", "", value)


def _field(label):
    normalized = _norm(label)
    normalized = re.sub(r"[（(](?:人民币)?(?:万?元)[）)]$", "", normalized)
    return next((field for field, aliases in _FIELD_ALIASES.items() if normalized in aliases), None)


def _registered(values):
    result = []
    for value in _json(values):
        try:
            source = SourceRef.model_validate({key: item for key, item in value.items() if key in SourceRef.model_fields}).model_dump(mode="json")
        except Exception as exc:
            raise LibraryError("LIBRARY_REGISTRATION_INVALID") from exc
        source["role"] = value.get("role", "reference_bid" if source["provenance"] == "derived_from_bid" else "company_material")
        source["material_type"] = value.get("material_type", "unknown")
        source["format"] = value.get("format", Path(source["path"]).suffix.lower().lstrip("."))
        if source["format"] not in {"pdf", "docx", "png", "jpeg", "jpg", "unknown"}:
            source["format"] = "unknown"
        _check(source["role"] in {"company_material", "reference_bid", "provenance_only"}, "LIBRARY_SOURCE_ROLE_INVALID")
        _check(isinstance(source["material_type"], str), "LIBRARY_MATERIAL_TYPE_INVALID")
        result.append(source)
    _check(len({item["source_id"] for item in result}) == len(result), "LIBRARY_DUPLICATE_SOURCE")
    return result


def _source_ref(source):
    return {key: source[key] for key in SourceRef.model_fields if key in source}


def _freeze(ctx, source):
    try:
        data = Path(source["path"]).read_bytes()
    except OSError as exc:
        raise LibraryError("LIBRARY_SOURCE_UNAVAILABLE") from exc
    _check(hashlib.sha256(data).hexdigest() == source["sha256"], "LIBRARY_SOURCE_HASH_MISMATCH")
    ref = ctx.store_bytes("library-source" + Path(source["path"]).suffix.lower(), data)
    _check(ref.get("sha256") == source["sha256"], "LIBRARY_FREEZE_HASH_MISMATCH")
    frozen = {**_source_ref(source), "path": str(ctx.executor.store.resolve(ref["path"]))}
    return frozen, ref


def _grid(table):
    rows = table.get("rows", [])
    _check(rows and isinstance(rows[0], list) and rows[0] and
           all(isinstance(row, list) and len(row) == len(rows[0]) and all(isinstance(v, str) for v in row) for row in rows),
           "LIBRARY_TABLE_INVALID")
    covered, spans, occupied = set(), {}, set()
    for merge in table.get("merges", []):
        r, c, rs, cs = merge.get("row"), merge.get("column"), merge.get("row_span", 1), merge.get("col_span", 1)
        _check(all(type(v) is int for v in (r, c, rs, cs)) and min(r, c) >= 0 and min(rs, cs) >= 1 and
               r + rs <= len(rows) and c + cs <= len(rows[0]) and rs * cs > 1, "LIBRARY_TABLE_MERGE_INVALID")
        spans[(r, c)] = cs
        for ri in range(r, r + rs):
            for ci in range(c, c + cs):
                _check((ri, ci) not in occupied, "LIBRARY_TABLE_MERGE_INVALID")
                occupied.add((ri, ci))
                if (ri, ci) != (r, c):
                    _check(not rows[ri][ci], "LIBRARY_TABLE_MERGE_CONTENT_INVALID")
                    covered.add((ri, ci))
    return rows, covered, spans


def _quote(source, unit, block, row, label_column, value_column, label, value):
    # Cell values are retained verbatim. This is an explicit cell-tuple view,
    # never a claim of continuous PDF/DOCX prose.
    return {"source_id": source["source_id"], "source_sha256": source["sha256"], "unit_id": unit["unit_id"],
            "block_id": block["block_id"], "locator": block["locator"], "page": unit.get("page"),
            "basis": "native_table_cells_v1", "cells": [{"row": row, "column": label_column}, {"row": row, "column": value_column}],
            "label_quote": label, "value_quote": value, "quote_sha256": digest([label, value]),
            "value_span": {"row": row, "column": value_column, "start": 0, "end": len(value)}}


def _table_candidates(source, unit, block, scope):
    rows, covered, spans = _grid(block)
    candidates, unprocessed = [], []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if (r, c) in covered or not text.strip():
                continue
            inline = re.match(r"^([^：:\n]{1,40})[：:]([\s\S]+)$", text)
            field = _field(inline[1] if inline else text)
            if field:
                value_column = c if inline else c + spans.get((r, c), 1)
                if value_column >= len(row) or (r, value_column) in covered:
                    unprocessed.append({"category": "basic_field", "source_id": source["source_id"], "locator": block["locator"],
                                        "row": r, "column": c, "field": field, "reason": "unsupported_value_layout", "status": "listed_unprocessed"})
                    continue
                raw_value = inline[2] if inline else row[value_column]
                quote = _quote(source, unit, block, r, c, value_column, text, raw_value)
                if inline:
                    quote["value_span"] = {"row": r, "column": c, "start": inline.start(2), "end": inline.end(2)}
                status = "candidate"
                if whole_cell_slot(raw_value) or has_fill_marker(raw_value) or _field(raw_value):
                    status = "missing_value"
                value = raw_value.strip()
                unit_match = re.search(r"[（(]((?:人民币)?(?:万?元))[）)]$", text.strip())
                unit_basis = None
                if field == "注册资本" and unit_match and unit_match[1] not in value:
                    value = value + " " + unit_match[1]
                    unit_basis = {"unit_quote": unit_match[1], "basis": "value_plus_original_label_unit"}
                candidate = {"candidate_id": _id("LC-", [source["source_id"], quote, field]), "field": field, "label": field,
                             "value": value, "raw_value": raw_value, "status": status, "citation": quote, "scope": scope,
                             "provenance": source["provenance"], "fact_adoption_source_sha256": source["sha256"] if source["provenance"] == "derived_from_bid" else None}
                if unit_basis:
                    candidate["unit_basis"] = unit_basis
                candidates.append(candidate)
            elif c % 2 == 0:
                categories = [category for category, pattern in _UNSUPPORTED.items() if pattern.search(text)]
                for category in categories:
                    unprocessed.append({"category": category, "source_id": source["source_id"], "locator": block["locator"],
                                        "row": r, "column": c, "reason": "field_not_supported_for_automatic_adoption", "status": "listed_unprocessed"})
    return candidates, unprocessed


def _known_cell_ocr(unit, table, reader_version):
    coverage = unit.get("coverage", {})
    if coverage.get("status") != "needs_ocr":
        return False
    unread = coverage.get("unread_objects", [])
    if not unread or any(not isinstance(locator, str) for locator in unread):
        return False
    prefix = re.escape(table["locator"]) + r"/row\[\d+\]/cell\[\d+\]/"
    if coverage.get("error_code") == "OCR_REQUIRED":
        return all(re.match(prefix, locator) for locator in unread)
    # Frozen native-reading-2 optional reference images lacked the error code.
    # This compatibility rule is intentionally version- and object-specific.
    return (reader_version == "native-reading-2" and coverage.get("error_code") is None and
            all(re.fullmatch(prefix + r"(?:[^/]+/)*image\[\d+\]", locator) for locator in unread))


def _candidate_readability(unit, table, candidate, reader_version=None):
    coverage = unit.get("coverage", {})
    if coverage.get("status") == "read":
        return {"status": "native_unit_read", "unit_id": unit["unit_id"]}
    if not _known_cell_ocr(unit, table, reader_version):
        return None
    unread = coverage.get("unread_objects", [])
    if not unread or any(not isinstance(locator, str) for locator in unread):
        return None
    rows, _, _ = _grid(table)
    footprint = {(cell["row"], cell["column"]) for cell in candidate["citation"]["cells"]}
    for merge in table.get("merges", []):
        if (merge["row"], merge["column"]) in footprint:
            footprint.update((r, c) for r in range(merge["row"], merge["row"] + merge.get("row_span", 1))
                             for c in range(merge["column"], merge["column"] + merge.get("col_span", 1)))
    for locator in unread:
        match = re.match(re.escape(table["locator"]) + r"/row\[(\d+)\]/cell\[(\d+)\](?:/|$)", locator)
        if not match:
            return None
        position = (int(match[1]), int(match[2]))
        if position[0] >= len(rows) or position[1] >= len(rows[0]) or position in footprint:
            return None
    return {"status": "native_cells_read_with_unrelated_ocr_object", "unit_id": unit["unit_id"],
            "table_reading_status": "partial", "error_code": coverage.get("error_code"),
            "reader_version": reader_version, "compatibility_rule": "native-reading-2-optional-cell-image-v1" if coverage.get("error_code") is None else None,
            "covered_cells": [{"row": r, "column": c} for r, c in sorted(footprint)],
            "excluded_unread_objects": copy.deepcopy(unread)}


def _prepare_source(ctx, source, document, config, reader_version):
    _check(document.get("sha256") == source["sha256"] and document.get("role") == source["role"], "LIBRARY_PARSED_SOURCE_MISMATCH")
    _check(source["format"] == "unknown" or document.get("format") == source["format"], "LIBRARY_PARSED_FORMAT_MISMATCH")
    frozen, artifact = _freeze(ctx, source)
    fact_source = {**source, "provenance": "derived_from_bid"} if source["role"] == "reference_bid" else source
    if fact_source["provenance"] == "derived_from_bid":
        frozen["provenance"] = "derived_from_bid"
    candidates, unprocessed, block_index, unresolved_details = [], [], {}, []
    state_counts = defaultdict(int)
    pending_scope = None
    authorized = (source["provenance"] != "derived_from_bid" and source["role"] != "reference_bid") or config.authorized_bid_fact_sources.get(source["source_id"]) == source["sha256"]
    explicit_scopes = set(config.table_scopes.get(source["source_id"], []))
    seen_scopes = set()
    unit_ids = set()
    for unit in document.get("units", []):
        _check(unit.get("source_id") == source["source_id"] and isinstance(unit.get("unit_id"), str), "LIBRARY_PARSED_UNIT_INVALID")
        _check(unit["unit_id"] not in unit_ids and isinstance(unit.get("locator"), str), "LIBRARY_PARSED_UNIT_INVALID")
        unit_ids.add(unit["unit_id"])
        reading_status = unit.get("coverage", {}).get("status")
        _check(reading_status in {"read", "empty", "needs_ocr", "failed", "unsupported"}, "LIBRARY_READING_STATUS_INVALID")
        state_counts[reading_status] += 1
        if reading_status not in {"read", "empty"}:
            unresolved_details.append({"unit_id": unit["unit_id"], "locator": unit["locator"], "status": reading_status,
                "error_code": unit.get("coverage", {}).get("error_code"), "unread_objects": unit.get("coverage", {}).get("unread_objects", [])})
            unread = unit.get("coverage", {}).get("unread_objects", [])
            exact_table_cells = any(block.get("kind") == "table" and _known_cell_ocr(unit, block, reader_version) for block in unit.get("blocks", []))
            if not exact_table_cells:
                pending_scope = None
        for block in unit.get("blocks", []):
            _check(block.get("source_id") == source["source_id"] and isinstance(block.get("locator"), str) and
                   isinstance(block.get("block_id"), str) and bool(block["block_id"]) and block.get("block_id") not in block_index and
                   block.get("kind") in {"paragraph", "table", "image"},
                   "LIBRARY_PARSED_BLOCK_INVALID")
            block_index[block["block_id"]] = (unit, block)
            text = block.get("text", "").strip()
            if block["kind"] == "paragraph":
                if _COMPANY_TABLE.fullmatch(text) and reading_status == "read" and block.get("origin", "native") == "native":
                    pending_scope = {"basis": "native_company_profile_heading", "block_id": block["block_id"],
                                     "locator": block["locator"], "quote": text, "source_sha256": source["sha256"]}
                elif _HEADING.match(text) or re.search(r"合同|业主|采购人基本|客户基本", text[:40]):
                    pending_scope = None
                continue
            if block["kind"] != "table":
                continue
            rows, _, _ = _grid(block)
            explicit = block["locator"] in explicit_scopes
            if explicit:
                seen_scopes.add(block["locator"])
            own_title = next((cell.strip() for row in rows[:2] for cell in row if _COMPANY_TABLE.fullmatch(cell.strip())), None)
            scope = ({"basis": "explicit_selected_table", "locator": block["locator"], "source_sha256": source["sha256"]} if explicit else
                     {"basis": "native_company_profile_table_title", "locator": block["locator"], "quote": own_title, "source_sha256": source["sha256"]} if own_title else pending_scope)
            pending_scope = None  # A context grants one table, never the remainder of a contract.
            if not scope:
                unprocessed.append({"source_id": source["source_id"], "locator": block["locator"], "category": "table",
                                    "reason": "outside_company_profile_scope", "status": "listed_unprocessed"})
                continue
            cell_scoped_ocr = _known_cell_ocr(unit, block, reader_version)
            if not authorized or (reading_status != "read" and not cell_scoped_ocr) or block.get("origin", "native") != "native":
                unprocessed.append({"source_id": source["source_id"], "locator": block["locator"], "category": "basic_table",
                    "reason": "bid_fact_authorization_missing" if not authorized else "native_reading_required", "status": "listed_unprocessed"})
                continue
            if source["material_type"] in {"contract", "performance", "personnel"} and not explicit:
                unprocessed.append({"source_id": source["source_id"], "locator": block["locator"], "category": source["material_type"],
                                    "reason": "explicit_fact_scope_required_for_this_material_type", "status": "listed_unprocessed"})
                continue
            new_candidates, new_unprocessed = _table_candidates(fact_source, unit, block, scope)
            for candidate in new_candidates:
                readiness = _candidate_readability(unit, block, candidate, reader_version)
                if readiness is None:
                    candidate["status"] = "unread_cell"
                    new_unprocessed.append({"source_id": source["source_id"], "locator": block["locator"], "category": "basic_field",
                        "field": candidate["field"], "candidate_id": candidate["candidate_id"], "status": "listed_unprocessed",
                        "reason": "unread_object_overlaps_candidate_or_cannot_be_scoped"})
                else:
                    candidate["reading_evidence"] = readiness
            candidates.extend(new_candidates)
            unprocessed.extend(new_unprocessed)
    _check(explicit_scopes == seen_scopes, "LIBRARY_SELECTED_TABLE_NOT_FOUND")
    if source["material_type"] in {"personnel", "performance", "contract"}:
        unprocessed.append({"source_id": source["source_id"], "category": source["material_type"],
                            "reason": "automatic_material_fact_extraction_not_implemented", "status": "listed_unprocessed"})
    unresolved = sum(state_counts[key] for key in ("needs_ocr", "failed", "unsupported"))
    readable = state_counts["read"] + state_counts["empty"]
    status = "partial" if unresolved and readable else "unusable" if unresolved or not readable else "parsed"
    state = {"source_id": source["source_id"], "sha256": source["sha256"], "status": status, "registered": True,
             "unit_counts": dict(state_counts), "readable_units": readable, "unresolved_units": unresolved,
             "fact_candidate_count": len(candidates), "material_type": source["material_type"],
             "unresolved_unit_details": unresolved_details,
             "fact_adoption_status": "allowed_by_explicit_source_scope" if authorized else "not_allowed_for_company_facts"}
    _capture(ctx, _id("library-source-", source["source_id"]), {"state": state, "candidates": candidates, "listed_unprocessed": unprocessed}, partial=bool(unresolved))
    return {"source": frozen, "artifact": artifact, "state": state, "candidates": candidates, "listed_unprocessed": unprocessed}


def _proof_entries(selections, sources, documents):
    entries = []
    for selection in selections:
        _check(isinstance(selection, dict) and set(selection) <= {"source_id", "title", "pages", "package_parts"}, "LIBRARY_PROOF_SELECTION_INVALID")
        source_id, title = selection.get("source_id"), selection.get("title")
        _check(source_id in sources and isinstance(title, str) and title.strip(), "LIBRARY_PROOF_SELECTION_INVALID")
        pages, parts = selection.get("pages", []), selection.get("package_parts", [])
        _check(isinstance(pages, list) and all(type(page) is int and page > 0 for page in pages) and len(set(pages)) == len(pages) and
               isinstance(parts, list) and all(isinstance(part, str) and part.startswith("word/media/") and ".." not in Path(part).parts for part in parts) and
               len(set(parts)) == len(parts) and bool(pages) != bool(parts), "LIBRARY_PROOF_SELECTION_INVALID")
        source = sources[source_id]
        document = documents.get(source_id)
        entry = {"proof_id": _id("LP-", [source_id, source["sha256"], pages, parts]), "source_id": source_id, "sha256": source["sha256"],
                 "title": title, "format": document.get("format", "pdf" if pages else "docx") if document else source["format"],
                 "pages": pages, "package_parts": parts, "provenance": source["provenance"], "status": "registered_unparsed",
                 "prepared_attachment": False, "content_read": False, "scope_locators": []}
        if document:
            if pages:
                selected_units = [unit for unit in document.get("units", []) if unit.get("page") in pages]
                _check({unit.get("page") for unit in selected_units} == set(pages), "LIBRARY_PROOF_PAGE_NOT_FOUND")
            else:
                selected_units, found = [], set()
                for unit in document.get("units", []):
                    if any(block.get("image_ref", {}).get("package_part") in parts for block in unit.get("blocks", []) if isinstance(block.get("image_ref"), dict)):
                        selected_units.append(unit)
                        found.update(block["image_ref"]["package_part"] for block in unit["blocks"] if isinstance(block.get("image_ref"), dict) and block["image_ref"].get("package_part") in parts)
                _check(found == set(parts), "LIBRARY_PROOF_PART_NOT_FOUND")
            content_read = all(unit.get("coverage", {}).get("status") in {"read", "empty"} for unit in selected_units)
            entry.update(status="parsed_scope_not_prepared" if content_read else "parsed_scope_unread", content_read=content_read,
                         scope_locators=[unit["locator"] for unit in selected_units])
        entries.append(entry)
    _check(len({entry["proof_id"] for entry in entries}) == len(entries), "LIBRARY_DUPLICATE_PROOF_SELECTION")
    return entries


def prepare_library(ctx, registered_sources: list[dict], parsed: dict, config: dict) -> dict:
    """Prepare only selected reader outputs; return an immutable local snapshot."""
    try:
        options = LibraryConfig.model_validate(config)
    except Exception as exc:
        raise LibraryError("LIBRARY_CONFIG_INVALID") from exc
    registered = _registered(registered_sources)
    by_source = {source["source_id"]: source for source in registered}
    _check(set(options.authorized_bid_fact_sources) <= set(by_source) and set(options.table_scopes) <= set(by_source), "LIBRARY_CONFIG_SOURCE_UNKNOWN")
    for source_id, source_hash in options.authorized_bid_fact_sources.items():
        _check(source_hash == by_source[source_id]["sha256"], "LIBRARY_AUTHORIZATION_HASH_MISMATCH")
    documents = {}
    for document in parsed.get("documents", []):
        source_id = document.get("source_id")
        _check(source_id in by_source and source_id not in documents, "LIBRARY_PARSED_SOURCE_UNKNOWN_OR_DUPLICATE")
        documents[source_id] = document
    outputs, states = [], []
    for source in registered:
        document = documents.get(source["source_id"])
        if document is None:
            states.append({"source_id": source["source_id"], "sha256": source["sha256"], "status": "registered_unparsed", "registered": True,
                           "readable_units": 0, "unresolved_units": None, "material_type": source["material_type"], "fact_candidate_count": 0})
            continue
        inputs = {"producer_version": LIBRARY_VERSION, "source": source, "document": document, "config": options.model_dump(),
                  "reader_version": parsed.get("producer_version")}
        def process(source=source, document=document):
            try:
                return _prepare_source(ctx, source, document, options, parsed.get("producer_version"))
            except LibraryError as exc:
                _capture(ctx, _id("library-failure-", source["source_id"]), {"source_id": source["source_id"], "status": "failed", "error_code": exc.code}, partial=True)
                raise
        output = ctx.run_unit(_id("library-source-", source["source_id"]), inputs, process)
        outputs.append(output)
        states.append(copy.deepcopy(output["state"]))

    record_result = prepare_material_records(ctx, registered, parsed, options.record_scopes, options.authorized_bid_fact_sources)

    def assemble():
        sources = [output["source"] for output in outputs]
        source_map = {source["source_id"]: source for source in sources}
        candidates = [copy.deepcopy(candidate) for output in outputs for candidate in output["candidates"]]
        grouped = defaultdict(list)
        for candidate in candidates:
            if candidate["status"] == "candidate":
                grouped[candidate["field"]].append(candidate)
        facts, conflicts = [], []
        for field, field_candidates in grouped.items():
            values = {candidate["value"] for candidate in field_candidates}
            if len(values) > 1:
                conflicts.append({"field": field, "candidate_ids": [candidate["candidate_id"] for candidate in field_candidates],
                                  "source_ids": sorted({candidate["citation"]["source_id"] for candidate in field_candidates}), "status": "conflict"})
                for candidate in field_candidates:
                    candidate["status"] = "conflict"
                continue
            source_refs = []
            for candidate in field_candidates:
                citation = candidate["citation"]
                scoped_id = _id("LREF-", citation)
                parent = source_map[citation["source_id"]]
                scoped = {**parent, "source_id": scoped_id, "locator": citation["locator"] + "; basis=native_table_cells_v1; " +
                          ",".join(f"r{cell['row']}c{cell['column']}" for cell in citation["cells"]) + "; quote_sha256=" + citation["quote_sha256"]}
                if scoped_id not in source_map:
                    SourceRef.model_validate(scoped)
                    sources.append(scoped)
                    source_map[scoped_id] = scoped
                source_refs.append(scoped_id)
                candidate["status"] = "usable_provisional" if candidate["provenance"] == "derived_from_bid" else "usable_source_fact"
            facts.append(Fact(fact_id=_id("F-LIB-", [field, next(iter(values))]), label=field, value=next(iter(values)),
                              source_refs=list(dict.fromkeys(source_refs))).model_dump(mode="json"))
        conflict_sources = {source_id for conflict in conflicts for source_id in conflict["source_ids"]}
        final_states = [{**state, "reading_status": state["status"], "status": "conflict" if state["source_id"] in conflict_sources else state["status"]} for state in states]
        proofs = _proof_entries(options.proof_selections, by_source, documents)
        unprocessed = [entry for output in outputs for entry in output["listed_unprocessed"]]
        prepared_record_scopes = {(item["source_id"], item["locator"]) for item in record_result["record_processing"] if item["status"] == "prepared"}
        unprocessed = [entry for entry in unprocessed if not (entry.get("reason") == "outside_company_profile_scope" and
                        (entry.get("source_id"), entry.get("locator")) in prepared_record_scopes)]
        unprocessed.extend({"source_id": state["source_id"], "category": state["material_type"], "status": "listed_unprocessed",
                            "reason": "source_registered_but_not_read"} for state in final_states if state["status"] == "registered_unparsed")
        content = {"schema_version": "company.library.v1", "producer_version": LIBRARY_VERSION,
                   "status": "conflict" if conflicts or record_result["status"] == "conflict" else "partial" if record_result["status"] == "partial" or any(state["status"] in {"registered_unparsed", "partial", "unusable"} for state in final_states) else "prepared",
                   "registered_sources": registered, "sources": sources, "source_states": final_states,
                   "facts": facts, "fact_candidates": candidates, "proof_entries": proofs, "conflicts": conflicts,
                   "material_records_schema_version": record_result["schema_version"], "material_records_producer_version": MATERIAL_RECORD_VERSION,
                   "material_records": record_result["material_records"], "record_processing": record_result["record_processing"],
                   "listed_unprocessed": unprocessed, "source_artifacts": [output["artifact"] for output in outputs],
                   "parsed_input_sha256": digest(parsed), "preparation_config": options.model_dump(),
                   "counts": {"registered_sources": len(registered), "parsed_sources": len(outputs), "unparsed_sources": len(registered) - len(outputs),
                              "fully_read_sources": sum(state["reading_status"] == "parsed" for state in final_states),
                              "partial_sources": sum(state["reading_status"] == "partial" for state in final_states),
                              "unusable_sources": sum(state["reading_status"] == "unusable" for state in final_states),
                              "usable_facts": len(facts), "fact_candidates": len(candidates), "conflicts": len(conflicts), "proof_scopes": len(proofs),
                              "material_records": len(record_result["material_records"]), "record_scopes": len(record_result["record_processing"]),
                              "personnel_declaration_records": sum(record["kind"] == "personnel_declaration" for record in record_result["material_records"]),
                              "historical_performance_records": sum(record["kind"] == "historical_performance" for record in record_result["material_records"]),
                              "material_record_conflicts": sum(record["status"] == "conflict" for record in record_result["material_records"]),
                              "prepared_attachments": 0, "listed_unprocessed": len(unprocessed)}}
        content_hash = digest(content)
        snapshot = {**content, "snapshot_id": "LIB-" + content_hash[:24], "content_hash": content_hash}
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        reference = ctx.store_bytes("company-library-snapshot.json", serialized, "application/json")
        _capture(ctx, "library-snapshot-report", {"snapshot_id": snapshot["snapshot_id"], "status": snapshot["status"], "counts": snapshot["counts"],
                                                  "source_states": final_states, "conflicts": conflicts, "snapshot_artifact": reference})
        return {**snapshot, "snapshot_artifact": reference}

    return ctx.run_unit("library-snapshot", {"producer_version": LIBRARY_VERSION, "registered_sources": registered,
                        "parsed_input_sha256": digest(parsed), "config": options.model_dump(), "source_outputs": outputs,
                        "material_record_result": record_result}, assemble)
