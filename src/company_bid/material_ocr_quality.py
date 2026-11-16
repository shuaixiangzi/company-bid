"""Bounded OCR layout and numeric checks; machine text never establishes facts."""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import math
import re
import unicodedata

from .storage import digest

MATERIAL_OCR_QUALITY_VERSION = "material-ocr-quality.v1"
_REF = "<｜ref｜>"
_CLOSE_REF = "<｜/ref｜>"
_BOX = "<｜box｜>"
_CLOSE_BOX = "<｜/box｜>"
_REGION = re.compile(re.escape(_REF) + r"([^<>\r\n]{1,40})" + re.escape(_CLOSE_REF)
                     + r"\s*" + re.escape(_BOX) + r"([^<>]{1,500})" + re.escape(_CLOSE_BOX))
_SPECIAL = re.compile(r"<\|[^<>]*\|>|<｜[^<>]*｜>")
_UNIT = r"(?:mm|cm|km|m|毫米|厘米|千米|米)(?:\s*(?:\^?\s*[23]|[²³]))?|平方米|立方米|万元|元|%|％|kg|t|吨|个|处|站"
_MEASURE = re.compile(r"(?<![A-Za-z0-9.])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(" + _UNIT + r")(?![A-Za-z0-9])", re.I)
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(?:[+-]?\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9.])")
_DATE = re.compile(r"(?<!\d)(\d{4})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})(?:日)?(?!\d)")
_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-./][A-Za-z0-9]+)*(?![A-Za-z0-9])")


class MaterialOCRQualityFailure(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _require(value, code, **details):
    if not value:
        raise MaterialOCRQualityFailure(code, details)


def _normal(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _unit(value):
    value = _normal(value).replace("^", "")
    return {"平方米": "m2", "立方米": "m3", "米": "m", "千米": "km",
            "厘米": "cm", "毫米": "mm", "％": "%"}.get(value, value)


def _decimal(value):
    return Decimal(value.replace(",", ""))


class _Table(HTMLParser):
    """Restricted HTML reader: no DOM execution, external resolution or repair."""
    _inline = {"b", "i", "strong", "em", "u", "sub", "sup", "span", "p", "div"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.table_seen = False

    def handle_starttag(self, tag, attrs):
        _require(tag in {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "br"} | self._inline,
                 "MATERIAL_OCR_HTML_UNSAFE")
        _require(len(attrs) <= 8 and len(self.stack) < 20, "MATERIAL_OCR_TABLE_LIMIT")
        clean = {}
        for key, value in attrs:
            _require(key not in clean and key in {"rowspan", "colspan", "border", "align", "valign"},
                     "MATERIAL_OCR_HTML_UNSAFE")
            _require(isinstance(value, str) and len(value) <= 12, "MATERIAL_OCR_HTML_UNSAFE")
            clean[key] = value
        if tag == "table":
            _require(not self.table_seen and not self.stack, "MATERIAL_OCR_TABLE_INVALID")
            self.table_seen = True
        else:
            _require(self.stack and self.stack[0] == "table", "MATERIAL_OCR_TABLE_INVALID")
        if tag in {"thead", "tbody", "tfoot"}:
            _require(self.current_row is None and self.stack[-1] == "table", "MATERIAL_OCR_TABLE_INVALID")
        if tag == "tr":
            _require(self.current_row is None and self.stack[-1] in {"table", "thead", "tbody", "tfoot"},
                     "MATERIAL_OCR_TABLE_INVALID")
            self.current_row = []
        if tag in {"td", "th"}:
            _require(self.current_row is not None and self.current_cell is None and self.stack[-1] == "tr",
                     "MATERIAL_OCR_TABLE_INVALID")
            spans = {}
            for name in ("rowspan", "colspan"):
                value = clean.get(name, "1")
                _require(re.fullmatch(r"[1-9]\d?", value) and int(value) <= 40, "MATERIAL_OCR_TABLE_SPAN_INVALID")
                spans[name] = int(value)
            self.current_cell = {"tag": tag, **spans, "parts": []}
        if tag in self._inline or tag == "br":
            _require(self.current_cell is not None, "MATERIAL_OCR_TABLE_INVALID")
            if tag in {"br", "p", "div"}:
                self.current_cell["parts"].append("\n")
        if tag != "br":
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        _require(tag == "br", "MATERIAL_OCR_HTML_UNSAFE")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        _require(self.stack and self.stack[-1] == tag, "MATERIAL_OCR_TABLE_INVALID")
        self.stack.pop()
        if tag in {"td", "th"}:
            cell = self.current_cell
            cell["text"] = "".join(cell.pop("parts")).strip()
            cell["empty_cell"] = not bool(cell["text"])
            self.current_row.append(cell)
            self.current_cell = None
        if tag == "tr":
            _require(self.current_row and len(self.current_row) <= 40, "MATERIAL_OCR_TABLE_LIMIT")
            self.rows.append(self.current_row)
            self.current_row = None
            _require(len(self.rows) <= 200, "MATERIAL_OCR_TABLE_LIMIT")

    def handle_data(self, data):
        if self.current_cell is not None:
            self.current_cell["parts"].append(data)
        else:
            _require(not data.strip(), "MATERIAL_OCR_TABLE_INVALID")

    def handle_comment(self, data):
        raise MaterialOCRQualityFailure("MATERIAL_OCR_HTML_UNSAFE")

    def handle_decl(self, decl):
        raise MaterialOCRQualityFailure("MATERIAL_OCR_HTML_UNSAFE")

    def unknown_decl(self, data):
        raise MaterialOCRQualityFailure("MATERIAL_OCR_HTML_UNSAFE")

    def handle_pi(self, data):
        raise MaterialOCRQualityFailure("MATERIAL_OCR_HTML_UNSAFE")

    def finish(self):
        self.close()
        _require(self.table_seen and not self.stack and self.rows, "MATERIAL_OCR_TABLE_INVALID")
        grid, cells = {}, []
        for row_number, row in enumerate(self.rows):
            col = 0
            for cell in row:
                while (row_number, col) in grid:
                    col += 1
                _require(col + cell["colspan"] <= 40 and row_number + cell["rowspan"] <= len(self.rows),
                         "MATERIAL_OCR_TABLE_SPAN_INVALID")
                index = len(cells)
                cells.append({**cell, "row": row_number, "column": col, "cell_index": index,
                              "source_coordinate_status": "table_region_only_not_cell_grounded"})
                for r in range(row_number, row_number + cell["rowspan"]):
                    for c in range(col, col + cell["colspan"]):
                        _require((r, c) not in grid, "MATERIAL_OCR_TABLE_SPAN_OVERLAP")
                        grid[(r, c)] = index
                col += cell["colspan"]
        width = max(c for _, c in grid) + 1
        _require(len(cells) <= 4000 and all((r, c) in grid for r in range(len(self.rows)) for c in range(width)),
                 "MATERIAL_OCR_TABLE_GRID_INCOMPLETE")
        return {"row_count": len(self.rows), "column_count": width, "cells": cells,
                "grid": [[grid[r, c] for c in range(width)] for r in range(len(self.rows))],
                "table_relationships": "machine_proposed_unverified"}


def _content(text, region_index, tables):
    if re.search(r"<!|<\?|<(?:script|style|iframe|img|svg)\b", text, re.I):
        raise MaterialOCRQualityFailure("MATERIAL_OCR_HTML_UNSAFE")
    pieces, position = [], 0
    for match in re.finditer(r"<table\b[^>]*>.*?</table\s*>", text, re.I | re.S):
        _require(len(tables) < 10, "MATERIAL_OCR_TABLE_LIMIT")
        prefix = text[position:match.start()]
        _require(not re.search(r"</?[A-Za-z][^>]*>|</?(?:table|tr|td|th|thead|tbody|tfoot)\b", prefix, re.I),
                 "MATERIAL_OCR_MARKUP_INVALID")
        pieces.append(prefix)
        parser = _Table()
        parser.feed(match.group())
        table = parser.finish()
        table.update(table_index=len(tables), source_region_index=region_index)
        tables.append(table)
        # Physical cell text occurs once. Grid contains explicit span references.
        rows = [[cell["text"] for cell in table["cells"] if cell["row"] == row]
                for row in range(table["row_count"])]
        pieces.append("\n".join("\t".join(row) for row in rows))
        position = match.end()
    suffix = text[position:]
    _require(not re.search(r"</?[A-Za-z][^>]*>|</?(?:table|tr|td|th|thead|tbody|tfoot)\b", suffix, re.I),
             "MATERIAL_OCR_MARKUP_INVALID")
    pieces.append(suffix)
    return "".join(pieces).strip()


def _regions(raw, width, height):
    matches = list(_REGION.finditer(raw))
    _require(len(matches) <= 2048, "MATERIAL_OCR_REGION_LIMIT")
    if not matches:
        _require(not _SPECIAL.search(raw), "MATERIAL_OCR_MARKUP_INVALID")
        return [{"region_index": 0, "kind": "page_text", "normalized_boxes": [], "bounding_box": None, "raw_text": raw}]
    _require(not raw[:matches[0].start()].strip(), "MATERIAL_OCR_MARKUP_INVALID")
    regions = []
    for i, match in enumerate(matches):
        content = raw[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(raw)].strip()
        _require(not _SPECIAL.search(content), "MATERIAL_OCR_MARKUP_INVALID", region_index=i)
        try:
            boxes = json.loads(match.group(2))
        except (TypeError, ValueError):
            raise MaterialOCRQualityFailure("MATERIAL_OCR_COORDINATES_INVALID", {"region_index": i}) from None
        _require(isinstance(boxes, list) and 1 <= len(boxes) <= 16, "MATERIAL_OCR_COORDINATES_INVALID", region_index=i)
        for box in boxes:
            _require(isinstance(box, list) and len(box) == 4 and all(type(n) in (int, float) and math.isfinite(n) for n in box)
                     and 0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000,
                     "MATERIAL_OCR_COORDINATES_INVALID", region_index=i)
        pixel = [min(b[0] for b in boxes) * width / 1000, min(b[1] for b in boxes) * height / 1000,
                 max(b[2] for b in boxes) * width / 1000, max(b[3] for b in boxes) * height / 1000]
        regions.append({"region_index": i, "kind": match.group(1).strip(), "normalized_boxes": boxes,
                        "bounding_box": pixel, "raw_text": content})
    return regions


def _repetitions(regions):
    exact = Counter((json.dumps(r["normalized_boxes"]), _normal(r["raw_text"])) for r in regions)
    maximum = max(exact.values(), default=0)
    _require(maximum <= 4, "MATERIAL_OCR_REPETITIVE_OUTPUT", repeated_region_count=maximum)
    long_lines = Counter(_normal(line) for r in regions for line in r["raw_text"].splitlines()
                         if len(_normal(line)) >= 24 and not line.lstrip().startswith("<"))
    _require(max(long_lines.values(), default=0) <= 16, "MATERIAL_OCR_REPETITIVE_OUTPUT",
             repeated_long_line_count=max(long_lines.values(), default=0))
    # An unbroken repeated phrase is a loop too, not a reason to deduplicate.
    for region in regions:
        _require(not re.search(r"(.{24,160})\1{7}", region["raw_text"], re.S),
                 "MATERIAL_OCR_REPETITIVE_OUTPUT", region_index=region["region_index"])
    return {"maximum_identical_region_count": maximum, "deduplicated_or_repaired": False}


def _inventory(regions):
    result, findings = [], []
    for region in regions:
        text = region["text"]
        for kind, pattern in (("measure", _MEASURE), ("date", _DATE), ("number", _NUMBER), ("identifier", _CODE)):
            for match in pattern.finditer(text):
                if kind == "identifier" and not any(c.isdigit() for c in match.group()):
                    continue
                item = {"kind": kind, "text": match.group(), "text_span": list(match.span()),
                        "region_index": region["region_index"], "source_status": "pending_source_check"}
                if kind == "measure":
                    item.update(number=str(_decimal(match.group(1))), unit=_unit(match.group(2)))
                if kind == "date":
                    try:
                        item["calendar_value"] = date(*map(int, match.groups())).isoformat()
                    except ValueError:
                        findings.append({"code": "OCR_DATE_INVALID", "region_index": region["region_index"],
                                         "text_span": list(match.span()), "disposition": "needs_source_review_no_correction"})
                result.append(item)
                _require(len(result) <= 4000, "MATERIAL_OCR_NUMERIC_LIMIT")
        for match in re.finditer(r"(?<!\w)\d+[OoIl]\d*\s*(?:m[²³23]?|元|kg)(?!\w)", text):
            findings.append({"code": "OCR_NUMERIC_GLYPH_AMBIGUOUS", "region_index": region["region_index"],
                             "text_span": list(match.span()), "disposition": "needs_source_review_no_correction"})
    return result, findings


def _arithmetic(tables):
    findings, checks = [], []
    def number(value):
        value = _normal(value)
        if not re.fullmatch(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?", value):
            return None
        try:
            return _decimal(value)
        except InvalidOperation:
            return None
    for table in tables:
        grid, cells = table["grid"], table["cells"]
        headers = [_normal(cells[index]["text"]) for index in grid[0]]
        aliases = ({"数量", "工程数量"}, {"单价", "单价(元)"}, {"金额", "总费用", "合价", "金额(元)"})
        columns = [[i for i, h in enumerate(headers) if h in names] for names in aliases]
        if any(len(c) != 1 for c in columns):
            continue
        q, p, a = (column[0] for column in columns)
        for row, indices in enumerate(grid[1:], 1):
            selected = [cells[indices[c]] for c in (q, p, a)]
            if any(cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in selected):
                continue
            values = [number(cell["text"]) for cell in selected]
            if any(value is None for value in values):
                continue
            quantity, price, amount = values
            difference = abs(quantity * price - amount)
            # Explicit quantity × price checks only; never infer missing cells or taxes.
            status = "consistent_not_source_verified" if difference <= Decimal("0.01") else "inconsistent_pending_source_check"
            check = {"table_index": table["table_index"], "row": row, "columns": [q, p, a],
                     "status": status, "tolerance": "0.01", "basis": "explicit_headers_quantity_times_price"}
            checks.append(check)
            if difference > Decimal("0.01"):
                findings.append({"code": "OCR_TABLE_ARITHMETIC_INCONSISTENT", **check,
                                 "disposition": "needs_source_review_no_correction"})
    return checks, findings


def _references(reference, identity, regions, tables):
    if reference is None:
        return []
    _require(isinstance(reference, dict) and reference.get("image_sha256") == identity.get("image_sha256")
             and isinstance(reference.get("checks"), list) and 1 <= len(reference["checks"]) <= 200,
             "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
    checked, seen = [], set()
    for check in reference["checks"]:
        _require(isinstance(check, dict) and isinstance(check.get("field_id"), str) and
                 re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", check["field_id"]) and check["field_id"] not in seen,
                 "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
        seen.add(check["field_id"])
        if "table_index" in check:
            _require(all(type(check.get(k)) is int for k in ("table_index", "row", "column")),
                     "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
            ti, row, col = (check[k] for k in ("table_index", "row", "column"))
            _require(0 <= ti < len(tables) and 0 <= row < tables[ti]["row_count"] and 0 <= col < tables[ti]["column_count"],
                     "MATERIAL_OCR_NUMERIC_REFERENCE_SCOPE_MISSING", field_id=check["field_id"])
            table = tables[ti]
            scope = table["cells"][table["grid"][row][col]]["text"]
        else:
            index = check.get("region_index")
            _require(type(index) is int and 0 <= index < len(regions), "MATERIAL_OCR_NUMERIC_REFERENCE_SCOPE_MISSING",
                     field_id=check["field_id"])
            scope = regions[index]["text"]
        kind, expected = check.get("kind"), check.get("expected_text")
        _require(kind in {"measure", "number", "date", "identifier", "expected_blank"}
                 and isinstance(expected, str) and len(expected) <= 200, "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
        if kind == "expected_blank":
            _require("table_index" in check and expected == "", "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
            matched = not scope.strip()
        elif kind == "measure":
            match = _MEASURE.fullmatch(expected.strip())
            _require(match is not None, "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
            target = (_decimal(match.group(1)), _unit(match.group(2)))
            same_value_units = [_unit(m.group(2)) for m in _MEASURE.finditer(scope)
                                if _decimal(m.group(1)) == target[0]]
            _require(not (target[1] in same_value_units and any(unit != target[1] for unit in same_value_units)),
                     "MATERIAL_OCR_NUMERIC_REFERENCE_AMBIGUOUS", field_id=check["field_id"],
                     disposition="narrow_reference_scope_before_adoption")
            matched = target[1] in same_value_units
        elif kind == "number":
            _require(re.fullmatch(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?", expected), "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
            matched = any(_decimal(m.group()) == _decimal(expected) for m in _NUMBER.finditer(scope))
        elif kind == "date":
            match = _DATE.fullmatch(expected.strip())
            _require(match is not None, "MATERIAL_OCR_NUMERIC_REFERENCE_INVALID")
            try:
                target = date(*map(int, match.groups()))
                matched = any(tuple(map(int, m.groups())) == (target.year, target.month, target.day) for m in _DATE.finditer(scope))
            except ValueError:
                raise MaterialOCRQualityFailure("MATERIAL_OCR_NUMERIC_REFERENCE_INVALID") from None
        else:
            matched = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(_normal(expected)) + r"(?![A-Za-z0-9])", _normal(scope)))
        _require(matched, "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH", field_id=check["field_id"], kind=kind,
                 disposition="source_conflict_no_correction_no_fallback")
        checked.append({"field_id": check["field_id"], "kind": kind, "status": "matches_supplied_reviewed_reference_only"})
    return checked


def assess_ocr_response(payload, *, width, height, identity, numeric_reference=None):
    """Validate one selected PNG response. Caller authenticates reference provenance.

    Numeric reference matches cover only named fields, never the complete page.
    Without independent reviewed reference, consistency cannot establish accuracy.
    """
    _require(type(width) is int and type(height) is int and 0 < width <= 10000 and 0 < height <= 10000,
             "MATERIAL_OCR_DIMENSIONS_INVALID")
    _require(isinstance(identity, dict) and isinstance(identity.get("image_sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", identity["image_sha256"]), "MATERIAL_OCR_IMAGE_IDENTITY_INVALID")
    _require(isinstance(payload, dict) and "error" not in payload, "MATERIAL_OCR_RESPONSE_ERROR")
    pages = payload.get("pages")
    _require(type(payload.get("page_count")) is int and payload["page_count"] == 1 and isinstance(pages, list)
             and len(pages) == 1 and isinstance(pages[0], dict) and type(pages[0].get("index")) is int and pages[0]["index"] == 0,
             "MATERIAL_OCR_PAGE_COVERAGE_INVALID")
    page = pages[0]
    _require(page.get("finish_reason") == "stop", "MATERIAL_OCR_OUTPUT_INCOMPLETE", finish_reason=page.get("finish_reason"))
    raw = page.get("text")
    _require(isinstance(raw, str) and len(raw) <= 100000, "MATERIAL_OCR_TEXT_LIMIT")
    _require(not any(ord(c) < 32 and c not in "\t\r\n" for c in raw), "MATERIAL_OCR_CONTROL_CHARACTER_INVALID")
    regions = _regions(raw, width, height)
    repeat = _repetitions(regions)
    tables, lines = [], []
    for region in regions:
        region["text"] = _content(region.pop("raw_text"), region["region_index"], tables)
        if region["text"].strip():
            lines.append({"line_index": len(lines), "text": region["text"], "bounding_box": region["bounding_box"],
                          "region_index": region["region_index"], "words": []})
    for table in tables:
        region = regions[table["source_region_index"]]
        table.update(normalized_boxes=region["normalized_boxes"], bounding_box=region["bounding_box"],
                     empty_cells_are="machine_proposed_empty_not_independent_source_blank_confirmation")
    text = "\n".join(line["text"] for line in lines)
    inventory, findings = _inventory(regions)
    arithmetic, arithmetic_findings = _arithmetic(tables)
    findings.extend(arithmetic_findings)
    reviewed = _references(numeric_reference, identity, regions, tables)
    return {"schema_version": MATERIAL_OCR_QUALITY_VERSION, "width": width, "height": height,
            "image_sha256": identity["image_sha256"], "text": text, "lines": lines, "regions": regions, "tables": tables,
            "repetition_check": repeat, "quality_status": "machine_transcription_unverified",
            "reading_status": "candidate_text_unverified" if text.strip() else "ocr_no_text_unresolved",
            "reading_completeness": "not_established", "layout_status": "regions_and_tables_unverified",
            "qualification_result": "not_determined", "project_assignment": "not_made", "automatic_assembly_allowed": False,
            "numeric_validation": {"status": "review_required" if findings else "pending_source_check",
                "inventory": inventory, "inventory_sha256": digest(inventory), "findings": findings,
                "arithmetic_checks": arithmetic, "reviewed_reference_checks": reviewed,
                "source_accuracy_established_for_complete_page": False, "automatic_numeric_adoption": False,
                "independent_reference_status": "supplied_fields_only" if reviewed else "not_available",
                "reference_sha256": digest(numeric_reference) if numeric_reference is not None else None}}
