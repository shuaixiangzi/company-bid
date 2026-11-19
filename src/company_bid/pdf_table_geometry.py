"""Native table discovery with evidence for decorative text backgrounds."""
from __future__ import annotations

from collections import defaultdict
from ctypes import byref, c_double, c_float, c_int, create_string_buffer
from dataclasses import dataclass
import re

from pdfplumber.table import Table, TableSettings
from pypdfium2 import PdfDocument, PdfiumError
from pypdfium2 import raw as pdfium

from .pdf_text_lines import horizontal_matrix


def object_box(obj):
    return tuple(obj[key] for key in ("x0", "top", "x1", "bottom"))


def _contains(outer, inner, padding=0):
    return (outer[0] - padding <= inner[0] <= inner[2] <= outer[2] + padding
            and outer[1] - padding <= inner[1] <= inner[3] <= outer[3] + padding)


def _colored_unstroked_fill(rect):
    color = rect.get("non_stroking_color")
    return (rect.get("fill") is True and rect.get("stroke") is False
            and isinstance(color, (tuple, list)) and len(color) in (3, 4)
            and max(color[:3]) - min(color[:3]) > .01)


def _text_backgrounds(page):
    strips = {}
    for rect in page.rects:
        if _colored_unstroked_fill(rect):
            strips.setdefault((rect["top"], rect["bottom"], tuple(rect["non_stroking_color"])), []).append(rect)
    if not strips or page.rotation != 0:
        return [], []
    glyphs = []
    for char in page.chars:
        matrix = horizontal_matrix(char)
        if char["text"].strip() and matrix is not None and char["size"] > 0:
            _, baseline = page.point2coord(matrix[4:])
            glyphs.append((char["x0"], char["x1"], baseline, char["size"]))
    measured = []
    for (top, bottom, color), rects in strips.items():
        bands = []
        right = float("-inf")
        for rect in sorted(rects, key=lambda item: item["x0"]):
            if rect["x0"] > right + .15:
                bands.append([])
            bands[-1].append(rect)
            right = max(right, rect["x1"])
        for band in bands:
            left, right = band[0]["x0"], max(rect["x1"] for rect in band)
            inside = [(baseline, size) for x0, x1, baseline, size in glyphs
                      if left <= (x0 + x1) / 2 < right and top <= baseline < bottom]
            if (len(inside) >= 2 and max(y for y, _ in inside) - min(y for y, _ in inside) <= .15
                    and all(.8 * size <= bottom - top <= 1.7 * size for _, size in inside)):
                baseline, size = inside[0]
                inline = any(abs(y - baseline) <= .15 and abs(em - size) <= .25
                             and ((x0 + x1) / 2 < left and abs(left - x1) <= size * .55
                                  or (x0 + x1) / 2 >= right and abs(x0 - right) <= size * .55)
                             for x0, x1, y, em in glyphs)
                measured.append((band, (left, top, right, bottom), color, baseline, size, inline))
    # A continuation can occupy the full physical line between two portions
    # where contiguous unhighlighted text proves inline paragraph treatment.
    inline_bands = [box for _, box, color, y, size, inline in measured if inline or (
        any(other_color == color and other_inline and .9 * size <= y - other_y <= 1.8 * size
            for _, _, other_color, other_y, _, other_inline in measured)
        and any(other_color == color and other_inline and .9 * size <= other_y - y <= 1.8 * size
                for _, _, other_color, other_y, _, other_inline in measured))]
    return [rect for band, *_ in measured for rect in band], inline_bands


def _inline_highlight_only(page, table, bands):
    if len(table.rows) != 1 or not any(_contains(band, table.bbox, .75) for band in bands):
        return False
    # Keep genuine grid rules, even when a text highlight crosses them.
    x0, top, x1, bottom = table.bbox
    return not any(edge.get("stroke") is not False and edge["x0"] <= x1 + .75 and edge["x1"] >= x0 - .75
                   and edge["top"] <= bottom + .75 and edge["bottom"] >= top - .75 for edge in page.edges)


def _table_grid(table):
    xs = sorted({x for left, _, right, _ in table.cells for x in (left, right)})
    ys = sorted({y for _, top, _, bottom in table.cells for y in (top, bottom)})
    return sorted((xs.index(left), ys.index(top), xs.index(right), ys.index(bottom))
                  for left, top, right, bottom in table.cells)


def _overlaps(first, second):
    return (max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3]))


def _fully_covered_grid(table):
    cells = table.cells
    if any(_overlaps(cell, other) for index, cell in enumerate(cells) for other in cells[index + 1:]):
        return False
    left, top, right, bottom = table.bbox
    return abs(sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in cells) - (right - left) * (bottom - top)) <= .01


def _section_tables(page, tables, settings):
    # Page-wide x snapping can merge the outside borders of separate tables.
    # For a discovered page with text backgrounds, remeasure disjoint sections
    # using the same settings. Require the same cells and nearby boundaries.
    result = []
    for table in tables:
        _, top, _, bottom = table.bbox
        if any(other is not table and top < other.bbox[3] + 2 and bottom > other.bbox[1] - 2
               for other in tables):
            result.append(table)
            continue
        section = page.filter(lambda obj: obj["top"] <= bottom + 1 and obj["bottom"] >= top - 1)
        measured = section.find_tables(table_settings=settings)
        if len(measured) == 1 and _table_grid(measured[0]) == _table_grid(table) and all(
                abs(a - b) <= 1 for a, b in zip(table.bbox, measured[0].bbox)):
            result.append(measured[0])
        else:
            result.append(table)
    return result


@dataclass(frozen=True)
class PdfTableDiscovery:
    tables: list[Table]
    text_backgrounds: tuple[tuple[float, ...], ...] = ()
    prior_boxes: tuple[tuple[float, ...], ...] = ()
    diagonal_rules: tuple[tuple[tuple[float, float], ...], ...] = ()

    def transformation(self, locator):
        result = {"kind": "native_pdf_table_text_backgrounds" if self.text_backgrounds else "native_pdf_table_diagonal_rules",
                "locator": locator,
                "fill_bboxes": [list(box) for box in self.text_backgrounds],
                "prior_table_bboxes": [list(box) for box in self.prior_boxes],
                "table_bboxes": [list(table.bbox) for table in self.tables]}
        if self.diagonal_rules:
            result["diagonal_rule_endpoints"] = [[list(point) for point in rule] for rule in self.diagonal_rules]
        return result


def discover_pdf_tables(page, settings=None):
    settings = TableSettings.resolve(settings)
    original = page.find_tables(table_settings=settings)
    if not original or not {settings.vertical_strategy, settings.horizontal_strategy} & {"lines", "lines_strict"}:
        return PdfTableDiscovery(original)
    backgrounds, inline_bands = (_text_backgrounds(page) if "lines" in (settings.vertical_strategy, settings.horizontal_strategy)
                                 else ([], []))
    diagonals = [line for line in page.lines if len(line.get("pts", ())) == 2
                 and line["x1"] - line["x0"] > .5 and line["bottom"] - line["top"] > .5
                 and any(_contains(table.bbox, object_box(line), .75) for table in original)]
    if not backgrounds and not diagonals:
        return PdfTableDiscovery(original)
    keys = {id(obj) for obj in [*backgrounds, *diagonals]}
    filtered = page.filter(lambda obj: id(obj) not in keys)
    tables = filtered.find_tables(table_settings=settings)
    # Only change detection where the other drawing objects independently
    # recover a containing cell for every excluded background and preserve the
    # extent of each prior table. A fill-defined grid otherwise remains intact.
    boxes = tuple(object_box(rect) for rect in backgrounds)
    def contained(box):
        return any(_contains(cell, box, .75) for table in tables for cell in table.cells)

    if (not all(_fully_covered_grid(table) for table in tables)
            or any(_overlaps(table.bbox, other.bbox) for index, table in enumerate(tables) for other in tables[index + 1:])
            or any(not contained(box) and not any(_contains(band, box) for band in inline_bands) for box in boxes)
            or any(not contained(object_box(line)) for line in diagonals)
            or any(not any(_contains(table.bbox, old.bbox, .75) for table in tables)
                   and not _inline_highlight_only(page, old, inline_bands) for old in original)
            or [sorted(table.cells) for table in tables] == [sorted(table.cells) for table in original]):
        return PdfTableDiscovery(original)
    if settings.vertical_strategy == settings.horizontal_strategy == "lines" and len(tables) > 1:
        tables = _section_tables(filtered, tables, settings)
    return PdfTableDiscovery(tables, boxes, tuple(table.bbox for table in original),
                             tuple(tuple(line["pts"]) for line in diagonals))


def embedded_pdf_glyph_measurements(path, page, glyphs):
    """Cross-check exact character origins against native embedded-font outlines."""
    # The normal page coordinate spaces are independently compared below.
    if (page.mediabox[:2] != (0, 0) or page.mediabox != page.cropbox
            or page.page_obj.attrs.get("UserUnit", 1) != 1):
        return {}
    by_symbol = defaultdict(list)
    for glyph in glyphs:
        if len(glyph["text"]) == 1:
            by_symbol[ord(glyph["text"])].append(glyph)
    confirmed = defaultdict(list)
    try:
        with PdfDocument(path) as document:
            native_page = document[page.page_number - 1]
            if any(abs(left - right) > .03 for left, right in zip(native_page.get_bbox(), page.page_obj.mediabox)):
                return {}
            text_page = native_page.get_textpage()
            for index in range(text_page.count_chars()):
                peers = by_symbol.get(pdfium.FPDFText_GetUnicode(text_page, index))
                if not peers or pdfium.FPDFText_IsGenerated(text_page, index) != 0:
                    continue
                x, y = c_double(), c_double()
                if not pdfium.FPDFText_GetCharOrigin(text_page, index, byref(x), byref(y)):
                    continue
                origins = [glyph for glyph in peers if abs(glyph["matrix"][4] - x.value) <= .03
                           and abs(glyph["matrix"][5] - y.value) <= .03]
                if not origins:
                    continue
                obj = pdfium.FPDFText_GetTextObject(text_page, index)
                font = pdfium.FPDFTextObj_GetFont(obj) if obj else None
                if not font or pdfium.FPDFFont_GetIsEmbedded(font) != 1:
                    continue
                name, flags = create_string_buffer(256), c_int()
                length = pdfium.FPDFText_GetFontInfo(text_page, index, name, len(name), byref(flags))
                if not 0 < length <= len(name):
                    continue
                # PDFium drops the subset prefix for certain simple fonts.
                # Do not equate two distinct prefixes when both are reported.
                matches = [glyph for glyph in origins if glyph["fontname"].encode() == name.value
                           or re.sub(rb"^[A-Z]{6}\+", b"", glyph["fontname"].encode()) == name.value]
                if len(matches) == 1:
                    left, bottom, right, top = text_page.get_charbox(index)
                    stroke = c_float()
                    has_width = pdfium.FPDFPageObj_GetStrokeWidth(obj, byref(stroke))
                    confirmed[id(matches[0])].append({"ink_bbox": (left, page.height - top, right, page.height - bottom),
                        "text_render_mode": pdfium.FPDFTextObj_GetTextRenderMode(obj),
                        "stroke_width_pt": stroke.value if has_width else None})
    except (OSError, PdfiumError):
        return {}
    return {key: matches[0] for key, matches in confirmed.items() if len(matches) == 1}
