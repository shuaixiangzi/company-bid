"""Render measured editable response forms; never rasterize or shrink text."""
from __future__ import annotations

import unicodedata

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


def native_font(name):
    return {"SimSun": "宋体", "SimHei": "黑体", "TimesNewRomanPS": "Times New Roman"}.get(name, name)


def _estimated_width(text, size):
    return sum(size if unicodedata.east_asian_width(c) in {"W", "F"} else size * .5 for c in text)


def _underlined_run(paragraph, text, width, family, size, bold):
    from .rendering import _font
    # Word does not paint underline on ordinary trailing spaces. A single
    # editable NBSP with positive character spacing preserves the source rule.
    shown = text if text.strip() else "\u00a0"
    run = paragraph.add_run(shown)
    _font(run, name=family, size=size, bold=bold)
    run.underline = True
    # Blank strokes are editable whitespace, not image overlays. Long known
    # fills wrap normally: never apply negative spacing/shrink-to-fit.
    padding = max(0., width - _estimated_width(shown, size))
    if not text.strip() or padding >= size * .5:
        pad = paragraph.add_run("\u00a0") if text.strip() else run
        _font(pad, name=family, size=size, bold=bold)
        pad.underline = True
        spacing = OxmlElement("w:spacing")
        spacing.set(qn("w:val"), str(round(max(0., padding - (size * .5 if text.strip() else 0.)) * 20)))
        pad._r.get_or_add_rPr().append(spacing)


def _fills_blank_rule_right_edge(binding, line, element, index):
    """An insertion at a proved blank's end belongs inside that source rule."""
    member = element.fixed_indices.index(index)
    member_ids = getattr(element, "source_block_ids", ())
    source_texts = getattr(element, "source_texts", ())
    return (member < len(member_ids) and member < len(source_texts) and
            binding.source_block_id == member_ids[member] and
            binding.before_text == source_texts[member] and
            getattr(line, "basis", "native_vector_rule") == "native_vector_rule" and
            (binding.source_rule is None or binding.source_rule == line) and
            binding.start == binding.end == line.end and line.start < line.end and
            not binding.before_text[line.start:line.end].strip(" \t"))


def _native_paragraph(document, section, element, gap, *, paragraph=None):
    from .rendering import _font, _indent
    if paragraph is None:
        paragraph = document.add_paragraph()
        _indent(paragraph._p, 0, element.font_size_pt)
        pf = paragraph.paragraph_format
        pf.space_before, pf.space_after = Pt(max(0., gap)), Pt(0)
        pf.left_indent, pf.right_indent = Pt(element.left_pt), Pt(element.right_pt)
        measured = getattr(section.native_format, "elements", ())
        if measured and element.alignment == "left":
            # A short signature's last glyph is evidence, not a text-box wall.
            # Use the form's measured common right edge for editable reflow.
            right_edge = max(e.bbox[2] for e in measured)
            pf.right_indent = Pt(min(element.right_pt, section.native_format.page_width_pt - right_edge))
        pf.first_line_indent = Pt(element.first_line_indent_pt)
        leading = next((b.source_rule for b in section.native_format.bindings
                        if b.fixed_index == element.fixed_indices[0] and b.start == b.end == 0 and
                        b.source_rule is not None and b.source_rule.basis == "native_vector_rule" and
                        b.source_rule.start == 0 and b.source_rule.bbox is not None), None)
        if leading is not None and element.alignment == "left":
            pf.first_line_indent = Pt(leading.bbox[0] - element.left_pt)
        pf.line_spacing = Pt(element.line_spacing_pt)
        pf.keep_together = False
        pf.keep_with_next = False
        paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                               "right": WD_ALIGN_PARAGRAPH.RIGHT}[element.alignment]
        # Center/right alignment uses the page interval, not the tiny glyph box.
        if element.alignment == "center":
            pf.left_indent = pf.right_indent = Pt(0)
        elif element.alignment == "right":
            pf.left_indent = Pt(0)
    else:
        paragraph.paragraph_format.line_spacing = Pt(max(paragraph.paragraph_format.line_spacing.pt, element.line_spacing_pt))
    family = native_font(element.font_family)
    bindings = section.native_format.bindings
    for index in element.fixed_indices:
        text = section.blocks[index].text
        spans = []
        for line in [rule for rule in element.underlines if rule.fixed_index == index]:
            if getattr(line,"basis","native_vector_rule") == "native_underscore_glyphs" and not any(
                    binding.fixed_index == index and binding.source_rule == line for binding in bindings):
                continue  # Unfilled underscore glyphs already remain in source text.
            start, end = line.start, line.end
            for binding in [b for b in bindings if b.fixed_index == index]:
                delta = len(binding.replacement) - (binding.end - binding.start)
                if binding.start == binding.end == start and binding.source_rule == line:
                    # A leading identity is inserted into its own measured
                    # rule, even when that rule also underlines the caption.
                    end += len(binding.replacement)
                elif _fills_blank_rule_right_edge(binding, line, element, index):
                    # PDF glyph extraction can place the insertion just after
                    # the measured blank. Consume that same editable rule;
                    # do not print an empty stroke before the known value.
                    end += len(binding.replacement)
                elif start == end == binding.start == binding.end:
                    end = start + len(binding.replacement)
                else:
                    # A native rule can also cover a printed caption after its
                    # blank. Map both boundaries through the complete fill;
                    # never insert padding into the middle of a known fact.
                    if start >= binding.end:
                        start += delta
                    elif start > binding.start:
                        start = binding.start
                    if end > binding.start:
                        end = end + delta if end >= binding.end else binding.start + len(binding.replacement)
            spans.append((start, end, line.width_pt))
        cursor = 0
        for start, end, width in sorted(spans):
            if start < cursor or end > len(text):
                raise ValueError("NATIVE_RENDER_SLOT_RANGE_CHANGED")
            _font(paragraph.add_run(text[cursor:start]), name=family, size=element.font_size_pt, bold=element.bold)
            _underlined_run(paragraph, text[start:end], width, family, element.font_size_pt, element.bold)
            cursor = end
        _font(paragraph.add_run(text[cursor:]), name=family, size=element.font_size_pt, bold=element.bold)
    return paragraph


def _same_baseline(previous, following):
    return (previous is not None and previous.kind == following.kind == "paragraph" and
            len(previous.fixed_indices) == len(following.fixed_indices) == 1 and
            abs(previous.bbox[3] - following.bbox[3]) <= 2 and
            previous.bbox[2] <= following.bbox[0] + .1 and
            min(previous.bbox[3], following.bbox[3]) > max(previous.bbox[1], following.bbox[1]))


def render_native_section(document, package, section, *, cover=False, append=False, previous_source_bottom=None):
    from .rendering import _font, _geometry, _indent, _issue_text, _page_furniture, _plain, _render_table
    layout = section.native_format
    page = document.sections[-1]
    page.page_width, page.page_height = Pt(layout.page_width_pt), Pt(layout.page_height_pt)
    page.left_margin = page.right_margin = Pt(0)
    scopes = getattr(layout, "page_scopes", ())
    partial = bool(scopes and scopes[0].scope_kind == "partial_page")
    if not append:
        page.top_margin = Pt(70 if partial else max(0., layout.heading_bbox[1] - 2))
    page.bottom_margin = Pt(36)
    if not append:
        _page_furniture(page, package.style, numbered=True, start=1 if cover else None)
    heading = document.add_paragraph(layout.heading_text, "Title" if cover else f"Heading {section.level}")
    heading.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                         "right": WD_ALIGN_PARAGRAPH.RIGHT}[layout.heading_alignment]
    pf = heading.paragraph_format
    _indent(heading._p, 0, layout.heading_font_size_pt)
    pf.space_before = Pt(max(0., layout.heading_bbox[1] - previous_source_bottom)) if append and previous_source_bottom is not None else Pt(0)
    pf.space_after = Pt(0)
    pf.keep_with_next = False
    pf.first_line_indent = pf.left_indent = pf.right_indent = Pt(0)
    if layout.heading_alignment == "left":
        pf.left_indent = Pt(layout.heading_bbox[0])
    pf.line_spacing = Pt(layout.heading_font_size_pt * 1.25)
    for run in heading.runs:
        _font(run, name=native_font(layout.heading_font_family), size=layout.heading_font_size_pt,
              bold=layout.heading_bold)
    cursor = layout.heading_bbox[1] + layout.heading_font_size_pt * 1.25
    issues = {i.issue_id: i for i in package.issues}
    previous, paragraph = None, None
    physical_page = layout.physical_page
    for element in layout.elements:
        element_page = getattr(element, "physical_page", layout.physical_page)
        if element_page != physical_page:
            from docx.enum.section import WD_SECTION_START
            scope = next(s for s in scopes if s.physical_page == element_page)
            page = document.add_section(WD_SECTION_START.NEW_PAGE)
            page.page_width, page.page_height = Pt(scope.page_width_pt), Pt(scope.page_height_pt)
            page.left_margin = page.right_margin = Pt(0)
            page.top_margin = Pt(max(0., scope.scope_bbox[1] - 2))
            page.bottom_margin = Pt(36)
            _page_furniture(page, package.style, numbered=True)
            physical_page, cursor, previous, paragraph = element_page, scope.scope_bbox[1], None, None
        gap = max(0., element.bbox[1] - cursor)
        if element.kind == "paragraph":
            inline = _same_baseline(previous, element)
            paragraph = _native_paragraph(document, section, element, gap, paragraph=paragraph if inline else None)
            cursor = max(cursor if inline else 0., element.bbox[1] + element.line_spacing_pt * len(element.fixed_indices))
        else:
            if gap > .5:
                spacer = document.add_paragraph()
                spacer.paragraph_format.space_before = spacer.paragraph_format.space_after = Pt(0)
                spacer.paragraph_format.line_spacing = Pt(gap)
                _font(spacer.add_run(""), size=1)
            table = section.blocks[element.fixed_index].table
            _render_table(document, table, issues, package.style, show_title=False, native_layout=element)
            cursor = element.bbox[3]
            paragraph = None
        previous = element
    # Preserve every unknown authorization/signature marker, without allowing
    # the generic long issue/action paragraphs to overwrite the printed form.
    cell_issues = {ref for b in section.blocks if b.kind == "table" for row in b.table.rows for cell in row for ref in cell.issue_refs}
    pending = list(dict.fromkeys(b.issue_id for b in section.blocks if b.kind == "issue" and b.issue_id not in cell_issues))
    if cell_issues or pending:
        paragraph = _plain(document, "", package.style)
        paragraph.paragraph_format.left_indent = Pt(56.7)
        paragraph.paragraph_format.right_indent = Pt(28)
        paragraph.paragraph_format.line_spacing = Pt(11)
        paragraph.paragraph_format.space_before = paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.keep_with_next = paragraph.paragraph_format.keep_together = False
        for index, identity in enumerate(pending):
            if index:
                _font(paragraph.add_run(" "), size=9)
            _font(paragraph.add_run(identity), name=package.style.font_family, size=9, color="A80000")
        _font(paragraph.add_run((" " if pending else "") + "红色 M 编号表示待补，详见待补清单。"),
              name=package.style.font_family, size=9)

# This appendix recovery is deliberately independent of response form filling.
REVIEW_SOURCE_TABLE_VERSION = "review-source-tables-v4"
_REVIEW_NATIVE_GRID_VERSION = "review-native-pdf-grid-v1"
_REVIEW_AGGREGATE_HASH_BYTE_BUDGET = 8 * 1024 * 1024


def _review_native_table(page, table, source_id, locator):
    """Read only native rectangles and glyph centres into an editable grid.

    Independent local implementation of this appendix's geometry contract.
    Equivalence baseline: this project's reading._pdf_table, native-reading-3;
    there is deliberately no runtime import of that N1 implementation. Native
    column boundaries, blank merged continuations and unassigned text checks
    must agree. Glyph interpretation, OCR and semantic fills are unsupported.
    """
    import hashlib
    import math
    import pdfplumber

    boxes = [tuple(float(value) for value in box) for box in table.cells if box is not None]
    if not boxes or any(len(box) != 4 or not all(math.isfinite(v) for v in box) for box in boxes):
        raise ValueError("REVIEW_NATIVE_GRID_INVALID_CELL")
    xs = sorted({round(value, 3) for box in boxes for value in (box[0], box[2])})
    ys = sorted({round(value, 3) for box in boxes for value in (box[1], box[3])})
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("REVIEW_NATIVE_GRID_EMPTY")
    rows = [["" for _ in xs[:-1]] for _ in ys[:-1]]
    merges, occupied = [], set()
    def inside(box, x, y):
        return box[0] <= x < box[2] and box[1] <= y < box[3]
    for box in boxes:
        left, top, right, bottom = (round(value, 3) for value in box)
        col, row = xs.index(left), ys.index(top)
        col_span, row_span = xs.index(right) - col, ys.index(bottom) - row
        if col_span < 1 or row_span < 1:
            raise ValueError("REVIEW_NATIVE_GRID_INVALID_SPAN")
        cells = {(r, c) for r in range(row, row + row_span) for c in range(col, col + col_span)}
        if cells & occupied:
            raise ValueError("REVIEW_NATIVE_GRID_OVERLAP")
        occupied.update(cells)
        chars = [char for char in page.chars if inside(box, (char["x0"] + char["x1"]) / 2,
                                                     (char["top"] + char["bottom"]) / 2)]
        rows[row][col] = (pdfplumber.utils.extract_text(chars) or "").strip() if chars else ""
        if row_span > 1 or col_span > 1:
            merges.append({"row": row, "column": col, "row_span": row_span, "col_span": col_span})
    for char in page.chars:
        x, y = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
        if (str(char.get("text", "")).strip() and inside(table.bbox, x, y) and
                not any(inside(box, x, y) for box in boxes)):
            raise ValueError("REVIEW_NATIVE_GRID_UNASSIGNED_TEXT")
    return {"block_id": "b-" + hashlib.sha256((source_id + "::" + locator).encode()).hexdigest()[:24],
            "source_id": source_id, "locator": locator, "rows": rows, "merges": merges,
            "column_widths": [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]}


def _recover_review_source_aggregate(text, source_refs, sources, cache, recover):
    """Compose only uniquely located, source-bound scopes covering the quote.

    LF separators are preserved as explicit fragments. An unresolved child grid
    remains a marked paragraph; a bad source fails the whole aggregate. Repeated
    quote matches, overlap, uncovered prose or missing scopes never get guessed.
    """
    import copy
    import hashlib
    import re
    from pathlib import Path
    from .models import file_sha256

    def value(source, key):
        return source[key] if isinstance(source, dict) else getattr(source, key)
    def fallback(reason, status="unsupported"):
        return {"version": REVIEW_SOURCE_TABLE_VERSION, "status": status, "reason": reason,
                "fragments": [{"kind": "paragraph", "text": text}], "proofs": []}
    refs = list(dict.fromkeys(source_refs))
    if not 2 <= len(refs) <= 32 or len(text) > 100000:
        return fallback("aggregate_scope_limit")
    parts = []
    authenticated = set()
    line_starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
    hash_bytes = 0
    # File authentication precedes fragment selection, including plain-text
    # children which the ordinary no-TSV branch intentionally does not parse.
    for ref in refs:
        if ref not in sources:
            return fallback("aggregate_source_ref_missing", "failed")
        source = sources[ref]
        path, expected = Path(value(source, "path")), value(source, "sha256")
        if value(source, "kind") != "tender":
            return fallback("aggregate_non_tender_scope", "failed")
        try:
            identity = (str(path.resolve()), expected)
            if identity not in authenticated:
                if file_sha256(path) != expected:
                    return fallback("aggregate_source_hash_mismatch", "failed")
                authenticated.add(identity)
        except OSError:
            return fallback("aggregate_source_unreadable", "failed")
        locator = value(source, "locator")
        hashes = re.findall(r"(?:^|;\s*)quote_sha256=([0-9a-f]{64})(?=;|$)", locator)
        spans = re.findall(r"(?:^|;\s*)(?:selected_row_view_)?chars\[(\d+):(\d+)\](?=;|$)", locator)
        if len(hashes) != 1 or len(spans) != 1:
            return fallback("aggregate_precise_quote_scope_required")
        length = int(spans[0][1]) - int(spans[0][0])
        if not 0 < length <= len(text):
            return fallback("aggregate_quote_length_invalid")
        positions = []
        # Complete composition permits only LF separators. Test native scope
        # boundaries, never every character of a potentially large source cell.
        # Bound cumulative actual UTF-8 hash work across all candidate scopes.
        for start in line_starts:
            end = start + length
            if end > len(text) or (end < len(text) and text[end] != "\n"):
                continue
            candidate = text[start:end].encode("utf-8")
            hash_bytes += len(candidate)
            if hash_bytes > _REVIEW_AGGREGATE_HASH_BYTE_BUDGET:
                return fallback("aggregate_hash_work_limit")
            if hashlib.sha256(candidate).hexdigest() == hashes[0]:
                positions.append(start)
        if len(positions) != 1:
            return fallback("aggregate_child_quote_ambiguous" if positions else "aggregate_child_quote_absent")
        start = positions[0]
        child_quote = text[start:start + length]
        # Retain the earlier conservative ambiguity boundary even if another
        # occurrence lies inside a larger cell rather than at an LF boundary.
        # Exact string search adds no unbounded sliding-window hash work.
        if text.find(child_quote) != text.rfind(child_quote):
            return fallback("aggregate_child_quote_ambiguous")
        parts.append({"source_ref": ref, "source_sha256": expected, "locator": locator,
                      "quote_sha256": hashes[0], "start": start, "end": start + length})
    parts.sort(key=lambda part: (part["start"], part["end"]))
    cursor = 0
    for part in parts:
        if part["start"] < cursor or text[cursor:part["start"]] not in ("", "\n"):
            return fallback("aggregate_scopes_do_not_cover_quote")
        cursor = part["end"]
    if cursor != len(text) or parts[0]["start"] != 0:
        return fallback("aggregate_scopes_do_not_cover_quote")
    aggregate_hash = hashlib.sha256(text.encode()).hexdigest()
    fragments, proofs, scope_parts = [], [], []
    cursor = 0
    unsupported = False
    for part in parts:
        start, end = part["start"], part["end"]
        if start > cursor:
            fragments.append({"kind": "paragraph", "text": text[cursor:start],
                              "quote_start": cursor, "quote_end": start})
        child_text = text[start:end]
        child = recover(child_text, [part["source_ref"]], sources, cache)
        if child["status"] == "failed":
            return fallback("aggregate_child_source_validation_failed", "failed")
        scope_parts.append({**part, "layout_status": child["status"], "layout_reason": child["reason"]})
        unsupported = unsupported or child["status"] == "unsupported"
        offset = start
        rebuilt = []
        for original in child["fragments"]:
            fragment = copy.deepcopy(original)
            rendered = fragment["text"] if fragment["kind"] == "paragraph" else "\n".join(
                "\t".join(row) for row in fragment["rows"])
            fragment.update(quote_start=offset, quote_end=offset + len(rendered))
            if child["status"] == "unsupported":
                fragment.update(layout_status="unsupported", layout_reason=child["reason"])
            if fragment["kind"] == "table":
                proof = fragment["proof"]
                proof.update(child_start=proof["start"], child_end=proof["end"],
                             aggregate_quote_sha256=aggregate_hash,
                             source_scope_start=start, source_scope_end=end)
                proof["start"] += start
                proof["end"] += start
                proofs.append(proof)
            fragments.append(fragment)
            rebuilt.append(rendered)
            offset += len(rendered)
        if "".join(rebuilt) != child_text or offset != end:
            return fallback("aggregate_child_roundtrip_failed", "failed")
        cursor = end
    if not proofs:
        return fallback("aggregate_has_no_restored_tables")
    return {"version": REVIEW_SOURCE_TABLE_VERSION,
            "status": "partially_restored" if unsupported else "restored",
            "reason": "aggregate_child_layout_unresolved" if unsupported else None,
            "fragments": fragments, "proofs": proofs, "scope_parts": scope_parts,
            "composition_rule": "unique_sha256_scopes_lf_cover_v1",
            "aggregate_quote_sha256": aggregate_hash,
            "hash_work_bytes": hash_bytes,
            "hash_work_byte_budget": _REVIEW_AGGREGATE_HASH_BYTE_BUDGET}

def recover_review_source_tables(text, source_refs, sources, cache=None):
    """Recover exact native PDF row views, or retain the complete quoted text.

    ``sources`` is the package SourceRef mapping (or sequence). A caller-owned
    cache may be shared across appendix entries. No OCR, inference, number
    correction or source mutation occurs. Returned fragments are ordered and
    replace only byte-identical TSV intervals. Proofs belong in the controlled
    review index; they establish layout provenance, never business acceptance.
    """
    import hashlib
    import re
    from pathlib import Path
    import pdfplumber
    from .models import context_digest, file_sha256

    source_map = sources if isinstance(sources, dict) else {s.source_id: s for s in sources}
    def value(source, key):
        return source[key] if isinstance(source, dict) else getattr(source, key)
    def fallback(reason, status="unsupported"):
        return {"version": REVIEW_SOURCE_TABLE_VERSION, "status": status, "reason": reason,
                "fragments": [{"kind": "paragraph", "text": text}], "proofs": []}
    if "\t" not in text:
        return fallback("no_tsv_quote", "not_applicable")
    quote_hash = hashlib.sha256(text.encode()).hexdigest()
    candidates = []
    for ref in dict.fromkeys(source_refs):
        if ref not in source_map:
            return fallback("aggregate_source_ref_missing", "failed") if len(set(source_refs)) > 1 else fallback("source_ref_missing")
        source = source_map[ref]
        locator = value(source, "locator")
        hashes = re.findall(r"(?:^|;\s*)quote_sha256=([0-9a-f]{64})(?=;|$)", locator)
        if hashes == [quote_hash]:
            candidates.append((ref, source))
    if not candidates:
        if len(set(source_refs)) > 1:
            return _recover_review_source_aggregate(text, source_refs, source_map, cache, recover_review_source_tables)
        return fallback("exact_quote_scope_unavailable")
    # Multiple identical quote locations are not interchangeable provenance.
    if len(candidates) != 1:
        return fallback("ambiguous_quote_scope")
    ref, source = candidates[0]
    locator = value(source, "locator")
    pages = [int(x) for x in re.findall(r"pdf/page\[(\d+)\]", locator)]
    if (value(source, "kind") != "tender" or not pages or
            max(pages) - min(pages) > 8 or min(pages) < 1):
        return fallback("unsupported_native_scope")
    fields = {}
    for item in locator.split(";")[1:]:
        key, sep, val = item.strip().partition("=")
        if sep:
            if key in fields:
                return fallback("duplicate_scope_field")
            fields[key] = val
    if fields.get("basis") not in {"consecutive_mixed_blocks_lf_v1", "frozen_table_rows_tsv_v1",
                                    "native_table_tsv_v1", "consecutive_table_blocks_lf_v1"}:
        return fallback("unsupported_quote_basis")
    blocks = fields.get("blocks", "").split(",")
    cells = fields.get("cells", "").split(",")
    selected = {}
    for cell in cells:
        match = re.fullmatch(r"(b-[0-9a-f]{24}):r(\d+)c(\d+)", cell)
        if not match:
            return fallback("explicit_native_cell_scope_required")
        coordinate = (int(match[2]), int(match[3]))
        if coordinate in selected.setdefault(match[1], set()):
            return fallback("duplicate_cell_scope")
        selected[match[1]].add(coordinate)
    if not set(selected) <= set(blocks):
        return fallback("cell_block_outside_scope")
    path, sha = Path(value(source, "path")), value(source, "sha256")
    try:
        if file_sha256(path) != sha:
            return fallback("source_hash_mismatch", "failed")
        # IDs are reproduced from a source identity already present in the
        # package, never reverse engineered from guessed document titles.
        originals = [s for s in source_map.values() if value(s, "sha256") == sha and
                     Path(value(s, "path")).resolve() == path.resolve()]
        key = (REVIEW_SOURCE_TABLE_VERSION, _REVIEW_NATIVE_GRID_VERSION, pdfplumber.__version__,
               str(path.resolve()), sha, tuple(pages), tuple(sorted(selected)),
               tuple(sorted(value(s, "source_id") for s in originals)))
        saved = cache.get(key) if cache is not None else None
        if saved is not None and saved[0] == context_digest(saved[1]):
            import copy
            native = copy.deepcopy(saved[1])
        else:
            native = []
            with pdfplumber.open(path) as pdf:
                if max(pages) > len(pdf.pages):
                    return fallback("source_page_out_of_range", "failed")
                for number in range(min(pages), max(pages) + 1):
                    page = pdf.pages[number - 1]
                    for index, table in enumerate(page.find_tables()):
                        table_locator = f"pdf/page[{number}]/table[{index}]"
                        identities = [(s, "b-" + hashlib.sha256((value(s, "source_id") + "::" +
                                      table_locator).encode()).hexdigest()[:24]) for s in originals]
                        matches = [(s, bid) for s, bid in identities if bid in selected]
                        if len(matches) > 1:
                            return fallback("ambiguous_native_table_identity")
                        if not matches:
                            continue
                        original, bid = matches[0]
                        block = _review_native_table(page, table, value(original, "source_id"), table_locator)
                        native.append(block)
            if cache is not None:
                if len(cache) >= 128:
                    cache.clear()
                import copy
                payload = copy.deepcopy(native)
                cache[key] = (context_digest(payload), payload)
        if {b["block_id"] for b in native} != set(selected):
            return fallback("native_table_identity_unresolved")
        replacements = []
        for block in native:
            coordinates = selected[block["block_id"]]
            rows = sorted({r for r, _ in coordinates})
            columns = len(block["rows"][0])
            if (rows != list(range(rows[0], rows[-1] + 1)) or rows[-1] >= len(block["rows"]) or
                    any(c >= columns for _, c in coordinates)):
                return fallback("nonrectangular_row_scope")
            # Empty/covered cells may be omitted by a frozen citation selector;
            # all nonempty anchors must still be explicitly cited.
            if any(block["rows"][r][c] and (r, c) not in coordinates for r in rows for c in range(columns)):
                return fallback("partial_row_scope")
            merges = []
            for merge in block["merges"]:
                end = merge["row"] + merge["row_span"] - 1
                if merge["row"] <= rows[-1] and end >= rows[0]:
                    if merge["row"] < rows[0] or end > rows[-1]:
                        return fallback("merge_crosses_quote_boundary")
                    merges.append({**merge, "row": merge["row"] - rows[0]})
            grid = [list(block["rows"][r]) for r in rows]
            tsv = "\n".join("\t".join(row) for row in grid)
            if not tsv or text.count(tsv) != 1:
                return fallback("native_grid_quote_mismatch")
            start = text.index(tsv)
            # Never replace a matching interior string of a larger source cell.
            if (start and text[start - 1] != "\n") or (start + len(tsv) < len(text) and text[start + len(tsv)] != "\n"):
                return fallback("native_grid_quote_boundary_mismatch")
            proof = {"rule": REVIEW_SOURCE_TABLE_VERSION, "source_ref": ref, "source_sha256": sha,
                     "quote_sha256": quote_hash, "native_source_id": block["source_id"],
                     "block_id": block["block_id"], "locator": block["locator"], "source_rows": rows,
                     "selected_cells": [list(pair) for pair in sorted(coordinates)],
                     "grid_sha256": context_digest({"rows": grid, "merges": merges,
                                                    "column_widths": block["column_widths"]}),
                     "parser": "pdfplumber", "parser_version": pdfplumber.__version__,
                     "reader_contract": "source_format_rendering._review_native_table", "reader_version": _REVIEW_NATIVE_GRID_VERSION, "start": start, "end": start + len(tsv)}
            replacements.append((start, start + len(tsv), {"kind": "table", "rows": grid,
                "column_widths": block["column_widths"], "merges": merges,
                "header_rows": [], "proof": proof}))
        replacements.sort(key=lambda r: r[0])
        fragments, proofs, cursor = [], [], 0
        for start, end, fragment in replacements:
            if start < cursor:
                return fallback("overlapping_table_intervals")
            if start > cursor:
                fragments.append({"kind": "paragraph", "text": text[cursor:start]})
            fragments.append(fragment)
            proofs.append(fragment["proof"])
            cursor = end
        if cursor < len(text):
            fragments.append({"kind": "paragraph", "text": text[cursor:]})
        return {"version": REVIEW_SOURCE_TABLE_VERSION, "status": "restored", "reason": None,
                "fragments": fragments, "proofs": proofs}
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return fallback("native_source_parse_failed", "failed")


def render_review_source_table(document, fragment, style):
    """Render the proved complete source rows without inventing a header."""
    from .models import TableSpec
    from .rendering import _render_table
    proof = fragment["proof"]
    table = TableSpec(table_id="REVIEW-" + proof["grid_sha256"][:24], section_id="review-source-appendix",
        title="原文表格", columns=[f"列{i + 1}" for i in range(len(fragment["rows"][0]))],
        rows=[[{"text": text} for text in row] for row in fragment["rows"]],
        source_refs=[proof["source_ref"]], requirement_refs=["review-source-quote"],
        column_widths=fragment["column_widths"], merges=fragment["merges"],
        header_from_columns=False, repeat_header_rows=0)
    _render_table(document, table, {}, style, show_title=False)
