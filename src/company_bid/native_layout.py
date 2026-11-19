"""Source-bound native PDF geometry for an N3 response-format recovery unit.

This reads immutable source bytes, independently of the N1 checkpoint schema.
It reuses the current reader's table/text selection rules so N3 can cross-check
each measured object against frozen N1 blocks. It does not infer paragraph
semantics, authorize a fill, select a person, perform OCR, or alter source text.

The narrow contract follows docs/08's reviewed layout/geometry lessons from
legacy baseline 53f20a3a; no legacy module or runtime dependency is imported.
"""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path
import re
import unicodedata

import pdfplumber

from .reading import PdfTableCellOwnershipError, SourceDocument, _inside, _pdf_table, _pdf_table_cell_characters
from .storage import digest


NATIVE_FORMAT_LAYOUT_VERSION = "native-format-layout-v1"


class NativeLayoutError(ValueError):
    """A measured layout cannot safely be represented by this native adapter."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise NativeLayoutError(code)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), "NATIVE_LAYOUT_INVALID_GEOMETRY")
    return float(value)


def _box(value, width, height):
    _require(isinstance(value, (list, tuple)) and len(value) == 4, "NATIVE_LAYOUT_INVALID_GEOMETRY")
    result = [_number(item) for item in value]
    x0, top, x1, bottom = result
    _require(-.01 <= x0 < x1 <= width + .01 and -.01 <= top < bottom <= height + .01,
             "NATIVE_LAYOUT_INVALID_GEOMETRY")
    return result


def _char_box(char):
    return [float(char[key]) for key in ("x0", "top", "x1", "bottom")]


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _selected_chars(chars, bbox):
    return [char for char in chars if _inside(bbox, (char["x0"] + char["x1"]) / 2,
                                            (char["top"] + char["bottom"]) / 2)]


def _font(char):
    name = str(char.get("fontname", ""))
    size = _number(char.get("size"))
    _require(bool(name.strip()) and size > 0, "NATIVE_LAYOUT_UNKNOWN_FONT")
    bold = bool(re.search(r"bold|black|heavy|demi", name, re.IGNORECASE))
    italic = bool(re.search(r"italic|oblique", name, re.IGNORECASE))
    family = re.sub(r"^[A-Z]{6}\+", "", name)
    family = re.sub(r"[,\-](?:BoldItalic|Bold|Italic|Oblique)(?:MT)?$", "", family, flags=re.IGNORECASE)
    return {"font_name": name, "font_family": family, "font_size_pt": size,
            "bold": bold, "italic": italic, "style_basis": "native_font_name"}


def _runs(chars, *, order_tops=None):
    """Keep native character positions, including spaces, in font-homogeneous runs."""
    order_tops = order_tops or {}
    result = []
    for char in sorted(chars, key=lambda value: (round(order_tops.get(id(value), value["top"]), 1), value["x0"])):
        font = _font(char)
        box = _char_box(char)
        entry = {"text": char["text"], "bbox": box}
        same = (result and all(result[-1][key] == value for key, value in font.items()) and
                abs(result[-1]["bbox"][1] - box[1]) <= 1 and
                box[0] - result[-1]["bbox"][2] <= font["font_size_pt"] * .6)
        if same:
            run = result[-1]
            run["text"] += char["text"]
            run["chars"].append(entry)
            run["bbox"] = _union([run["bbox"], box])
        else:
            result.append({**font, "text": char["text"], "bbox": box, "chars": [entry]})
    return result


def _alignment(chars, container):
    """Measured gaps are evidence; suggestions are not logical paragraph claims."""
    visible = [char for char in chars if char["text"].strip()]
    if not visible:
        return {"basis": "no_native_glyphs", "horizontal": "unresolved", "vertical": "unresolved"}
    glyphs = _union([_char_box(char) for char in visible])
    left, top = glyphs[0] - container[0], glyphs[1] - container[1]
    right, bottom = container[2] - glyphs[2], container[3] - glyphs[3]
    center_x, center_y = (left - right) / 2, (top - bottom) / 2
    horizontal = "center" if abs(center_x) <= 3 else "unresolved"
    vertical = "center" if abs(center_y) <= 3 else ("top" if top < bottom else "bottom")
    return {"basis": "native_glyph_bounds_relative_to_container", "container_bbox": list(container),
            "glyph_bbox": glyphs, "left_gap_pt": left, "right_gap_pt": right,
            "top_gap_pt": top, "bottom_gap_pt": bottom, "center_delta_x_pt": center_x,
            "center_delta_y_pt": center_y, "horizontal": horizontal, "vertical": vertical,
            "suggestion_only": True}


def _physical_lines(page, table_boxes, number):
    # Same word partition and locator numbering as the immutable N1 reader.
    outside = [word for word in page.extract_words() if not any(
        _inside(box, (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2)
        for box in table_boxes)]
    groups = []
    for word in sorted(outside, key=lambda item: (round(item["top"], 1), item["x0"])):
        if groups and abs(groups[-1][0]["top"] - word["top"]) <= 3:
            groups[-1].append(word)
        else:
            groups.append([word])
    lines = []
    for index, words in enumerate(groups):
        words.sort(key=lambda word: word["x0"])
        text = " ".join(str(word["text"]) for word in words).strip()
        if not text:
            continue
        bbox = _box([min(w["x0"] for w in words), min(w["top"] for w in words),
                     max(w["x1"] for w in words), max(w["bottom"] for w in words)], page.width, page.height)
        chars = _selected_chars(page.chars, bbox)
        _require(chars, "NATIVE_LAYOUT_LINE_WITHOUT_NATIVE_GLYPHS")
        alignment = _alignment(chars, [0., 0., float(page.width), float(page.height)])
        footer_number = (bbox[1] >= page.height * .9 and
                         abs((bbox[0] + bbox[2]) / 2 - page.width / 2) <= page.width * .1 and
                         bool(re.fullmatch(r"\s*[-–—]?\s*\d+\s*[-–—]?\s*", text)))
        lines.append({"locator": f"pdf/page[{number}]/line[{index}]", "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "bbox": bbox, "runs": _runs(chars),
            "alignment_evidence": alignment, "page_furniture_candidate": "page_number" if footer_number else None,
            "furniture_policy": "retained_for_cross_check_not_automatically_form_content"})
    return lines


def _table_layout(source, page, table, number, index):
    bbox = _box(list(table.bbox), page.width, page.height)
    locator = f"pdf/page[{number}]/table[{index}]"
    try:
        assignment = _pdf_table_cell_characters(page, table, locator)
    except PdfTableCellOwnershipError as exc:
        raise NativeLayoutError("NATIVE_LAYOUT_TABLE_CELL_OWNERSHIP_AMBIGUOUS") from exc
    block = _pdf_table(source, page, table, locator, assignment=assignment)
    groups, order_tops = assignment
    boxes = list(groups)
    xs = sorted({round(value, 3) for box in boxes for value in (box[0], box[2])})
    ys = sorted({round(value, 3) for box in boxes for value in (box[1], box[3])})
    cells = []
    for raw_box in sorted(boxes, key=lambda box: (box[1], box[0])):
        cell_box = _box(list(raw_box), page.width, page.height)
        left, top, right, bottom = (round(value, 3) for value in raw_box)
        column, row = xs.index(left), ys.index(top)
        chars = groups[raw_box]
        cells.append({"row": row, "column": column,
            "row_span": ys.index(bottom) - row, "col_span": xs.index(right) - column,
            "bbox": cell_box, "text": block.rows[row][column], "runs": _runs(chars, order_tops=order_tops),
            "alignment_evidence": _alignment(chars, cell_box)})
    heights = [ys[index + 1] - ys[index] for index in range(len(ys) - 1)]
    _require(all(value > 0 for value in heights) and
             abs(sum(heights) - (bbox[3] - bbox[1])) <= .01, "NATIVE_LAYOUT_TABLE_HEIGHT_MISMATCH")
    # The native reader stores raw rows with an empty ReadingBlock.text. N3
    # consumes extraction._index's immutable rows-as-TSV view, so measure the
    # same exact text projection without mutating the original N1 checkpoint.
    text = "\n".join("\t".join(row) for row in block.rows)
    result = {"locator": locator, "text": text, "text_basis": "table_rows_tsv_v1",
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "rows": block.rows,
        "merges": [merge.model_dump() for merge in block.merges], "bbox": bbox,
        "column_widths_pt": block.column_widths, "row_heights_pt": heights,
        "width_pt": bbox[2] - bbox[0], "height_pt": bbox[3] - bbox[1], "cells": cells,
        "geometry_policy": "source_absolute_points_row_height_is_minimum",
        "grid_text_sha256": digest(block.rows)}
    result["layout_sha256"] = digest(result)
    return result


def _horizontal_rules(page, tables, number):
    result = []
    for edge in sorted(page.edges, key=lambda value: (value["top"], value["x0"])):
        x0, x1, top, bottom = (_number(edge[key]) for key in ("x0", "x1", "top", "bottom"))
        if x1 <= x0 or abs(bottom - top) > .3:
            continue
        _require(-.01 <= x0 < x1 <= page.width + .01 and -.01 <= top <= page.height + .01,
                 "NATIVE_LAYOUT_INVALID_RULE")
        is_table = any(any(abs(top - cell["bbox"][side]) <= .75 for side in (1, 3)) and
                       x1 >= cell["bbox"][0] - .75 and x0 <= cell["bbox"][2] + .75
                       for table in tables for cell in table["cells"])
        result.append({"locator": f"pdf/page[{number}]/rule[{len(result)}]", "bbox": [x0, top, x1, bottom],
            "x0": x0, "x1": x1, "top": top, "length_pt": x1 - x0,
            "line_width_pt": _number(edge.get("linewidth") or 0),
            "kind": "table_boundary" if is_table else "fillable_rule",
            "semantic_role": "unassigned_source_geometry"})
    return result


def _page_layout(source, page, number):
    _require(page.rotation == 0, "NATIVE_LAYOUT_UNSUPPORTED_ROTATION")
    _require(_number(page.page_obj.attrs.get("UserUnit", 1)) == 1, "NATIVE_LAYOUT_UNSUPPORTED_USER_UNIT")
    _require(page.width > 0 and page.height > 0, "NATIVE_LAYOUT_INVALID_PAGE")
    _require(not page.images and not page.curves, "NATIVE_LAYOUT_UNSUPPORTED_NONNATIVE_OBJECT")
    _require(page.chars, "NATIVE_LAYOUT_NO_NATIVE_TEXT")
    for char in page.chars:
        text = char.get("text")
        _require(isinstance(text, str) and text and not any(
            symbol == "\ufffd" or unicodedata.category(symbol) in {"Co", "Cs", "Cc"} for symbol in text),
            "NATIVE_LAYOUT_UNRESOLVED_GLYPH")
        _require(char.get("upright") is not False, "NATIVE_LAYOUT_UNSUPPORTED_TEXT_TRANSFORM")
        matrix = char.get("matrix")
        _require(not matrix or (len(matrix) == 6 and abs(matrix[1]) < .0001 and abs(matrix[2]) < .0001
                               and matrix[0] > 0 and matrix[3] > 0), "NATIVE_LAYOUT_UNSUPPORTED_TEXT_TRANSFORM")
        _box(_char_box(char), page.width, page.height)
        _font(char)
    native_tables = page.find_tables()
    tables = [_table_layout(source, page, table, number, index) for index, table in enumerate(native_tables)]
    lines = _physical_lines(page, [table["bbox"] for table in tables], number)
    result = {"page_number": number, "locator": f"pdf/page[{number}]",
        "width_pt": float(page.width), "height_pt": float(page.height), "rotation": 0,
        "coordinate_system": "top_left_pdf_points", "lines": lines, "tables": tables,
        "horizontal_rules": _horizontal_rules(page, tables, number), "images": [],
        "native_character_count": len(page.chars),
        "layout_semantics": "physical_lines_and_measured_geometry_only_no_paragraph_or_fill_authorization"}
    result["layout_sha256"] = digest(result)
    return result


def read_native_format_layout(source_path, source_sha256, page_numbers):
    """Read exact native pages; unsupported content raises a bounded failure.

    Pages are one-based and must be explicitly unique. Page-number furniture is
    retained under its original N1-compatible locator and flagged as a candidate,
    so N3 can select only its frozen format block span without relabeling a page
    footer as response text. No page number, title, form, company or fill is baked
    into this adapter.
    """
    _require(isinstance(source_sha256, str) and bool(re.fullmatch(r"[0-9a-f]{64}", source_sha256)),
             "NATIVE_LAYOUT_SOURCE_HASH_INVALID")
    _require(isinstance(page_numbers, (list, tuple)) and page_numbers and len(page_numbers) <= 100 and
             all(type(value) is int and value >= 1 for value in page_numbers) and
             len(set(page_numbers)) == len(page_numbers), "NATIVE_LAYOUT_PAGE_SCOPE_INVALID")
    path = Path(source_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NativeLayoutError("NATIVE_LAYOUT_SOURCE_UNREADABLE") from exc
    _require(hashlib.sha256(raw).hexdigest() == source_sha256, "NATIVE_LAYOUT_SOURCE_HASH_MISMATCH")
    _require(raw.lstrip().startswith(b"%PDF-"), "NATIVE_LAYOUT_SOURCE_NOT_PDF")
    source = SourceDocument(source_id="native-format-layout", path=path, sha256=source_sha256,
                            role="tender", title="Native response-format source")
    try:
        with pdfplumber.open(io.BytesIO(raw)) as document:
            _require(max(page_numbers) <= len(document.pages), "NATIVE_LAYOUT_PAGE_SCOPE_INVALID")
            pages = [_page_layout(source, document.pages[number - 1], number) for number in page_numbers]
    except NativeLayoutError:
        raise
    except Exception as exc:
        raise NativeLayoutError("NATIVE_LAYOUT_READ_FAILED") from exc
    try:
        _require(hashlib.sha256(path.read_bytes()).hexdigest() == source_sha256, "NATIVE_LAYOUT_SOURCE_CHANGED")
    except OSError as exc:
        raise NativeLayoutError("NATIVE_LAYOUT_SOURCE_CHANGED") from exc
    result = {"schema_version": NATIVE_FORMAT_LAYOUT_VERSION, "source_sha256": source_sha256,
        "source_size_bytes": len(raw), "page_numbers": list(page_numbers), "pages": pages,
        "table_settings": {}, "reading_policy": "native_only_no_ocr_no_source_mutation"}
    result["layout_sha256"] = digest(result)
    return result
