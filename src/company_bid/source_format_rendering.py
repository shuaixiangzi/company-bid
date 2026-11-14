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


def render_native_section(document, package, section):
    from .rendering import _font, _geometry, _indent, _issue_text, _page_furniture, _plain, _render_table
    layout = section.native_format
    page = document.sections[-1]
    page.page_width, page.page_height = Pt(layout.page_width_pt), Pt(layout.page_height_pt)
    page.left_margin = page.right_margin = Pt(0)
    page.top_margin = Pt(max(0., layout.heading_bbox[1] - 2))
    page.bottom_margin = Pt(36)
    _page_furniture(page, package.style, numbered=True)
    heading = document.add_paragraph(layout.heading_text, f"Heading {section.level}")
    heading.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                         "right": WD_ALIGN_PARAGRAPH.RIGHT}[layout.heading_alignment]
    pf = heading.paragraph_format
    _indent(heading._p, 0, layout.heading_font_size_pt)
    pf.space_before = pf.space_after = Pt(0)
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
    for element in layout.elements:
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
