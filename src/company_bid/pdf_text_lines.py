"""Read horizontal native PDF text on proven baselines across font metrics."""
from __future__ import annotations

from collections import defaultdict
import math

from pdfplumber.utils import extract_words


def horizontal_matrix(char):
    value = char.get("matrix")
    if (char.get("upright") is not True or not isinstance(value, (list, tuple)) or len(value) != 6
            or not all(isinstance(part, (float, int)) and math.isfinite(part) for part in value)):
        return None
    a, b, c, d, _, _ = value
    if a > 0 and d > 0 and abs(b) < .0001 and abs(c) < .0001:
        return value
    return None


def native_baseline_tops(chars, order_tops=None):
    """Override extraction tops only for mixed fonts on the same text matrix baseline."""
    tops = dict(order_tops or {})
    # Do not use glyph rectangles to bridge fonts. Their font-specific descents
    # can differ by most of an em even when the matrices share a baseline.
    measured = []
    for char in chars:
        matrix = horizontal_matrix(char)
        if matrix is not None:
            measured.append((matrix[5], char))
    measured.sort(key=lambda item: item[0])
    line_baseline, line = None, []
    for baseline, char in measured:
        if line_baseline is None or baseline - line_baseline > .15:
            _project_mixed_line(line, tops)
            line_baseline, line = baseline, []
        line.append(char)
    _project_mixed_line(line, tops)
    return tops


def _project_mixed_line(line, tops):
    fonts = defaultdict(list)
    for char in line:
        if char["text"].strip():
            fonts[char.get("fontname")].append(char)
    if len(fonts) < 2:
        return
    visible = [char for group in fonts.values() for char in group]
    original_tops = [tops.get(id(char), char["top"]) for char in visible]
    if max(original_tops) - min(original_tops) <= 3:
        return
    sizes = [char["size"] for char in visible]
    if min(sizes) < .8 * max(sizes):
        return
    # The established line stays on the measured top of its dominant font.
    # Prefer using a tall glyph's upper edge on tied font populations.
    anchor = max(fonts.values(), key=lambda group: (len(group), -min(char["top"] for char in group)))
    top = min(tops.get(id(char), char["top"]) for char in anchor)
    tops.update((id(char), top) for char in line)


def projected_chars(chars, tops):
    result = []
    for char in chars:
        if id(char) in tops:
            top = tops[id(char)]
            delta = top - char["top"]
            char = {**char, "top": top, "bottom": char["bottom"] + delta,
                    "doctop": char["doctop"] + delta, "_native_char": char}
        result.append(char)
    return result


def _inside(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _outside(boxes, item):
    return not any(_inside(box, (item["x0"] + item["x1"]) / 2, (item["top"] + item["bottom"]) / 2)
                   for box in boxes)


def pdf_page_text_lines(page, table_boxes, claimed_table_chars=()):
    """Share projected line text and exact original glyph geometry between N1 and N3."""
    chars = [char for char in page.chars if id(char) not in claimed_table_chars] if claimed_table_chars else page.chars
    tops = native_baseline_tops([char for char in chars if _outside(table_boxes, char)])
    words = (extract_words(projected_chars(chars, tops), return_chars=True)
             if tops or claimed_table_chars else page.extract_words(return_chars=True))
    groups = []
    for word in sorted(words, key=lambda item: (round(item["top"], 1), item["x0"])):
        chars = [char.get("_native_char", char) for char in word["chars"]]
        geometry = {"x0": min(char["x0"] for char in chars), "x1": max(char["x1"] for char in chars),
                    "top": min(char["top"] for char in chars), "bottom": max(char["bottom"] for char in chars)}
        if not _outside(table_boxes, geometry):
            continue
        entry = {**word, "native_chars": chars, "geometry": geometry}
        if groups and abs(groups[-1][0]["top"] - word["top"]) <= 3:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    for index, group in enumerate(groups):
        group.sort(key=lambda word: word["x0"])
        text = " ".join(str(word["text"]) for word in group).strip()
        if text:
            boxes = [word["geometry"] for word in group]
            bbox = [min(box["x0"] for box in boxes), min(box["top"] for box in boxes),
                    max(box["x1"] for box in boxes), max(box["bottom"] for box in boxes)]
            chars = [char for word in group for char in word["native_chars"]]
            yield index, text, bbox, chars, tops
