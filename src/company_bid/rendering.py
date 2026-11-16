"""Editable documents assembled from a frozen package and explicit typography.

Reference-derived layout and OOXML compatibility decisions are documented in
our design records; no original signatures, prices or document package parts
are copied into a new bid.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from PIL import Image

from .models import (AttachmentBlock, DocumentPackage, HeadingBlock, IssueBlock,
                     ListBlock, ParagraphBlock, TableBlock, file_sha256)


class RenderingError(ValueError):
    """Input or assembly failed; no successful manifest is returned."""


def _typeface(r_pr, name, size=None):
    fonts = r_pr.get_or_add_rFonts()
    for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn(f"w:{attribute}"), None)
    for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{attribute}"), name)
    fonts.set(qn("w:hint"), "eastAsia")
    if size is not None:
        for tag in ("sz", "szCs"):
            element = r_pr.find(qn(f"w:{tag}"))
            if element is None:
                element = OxmlElement(f"w:{tag}")
                r_pr.append(element)
            element.set(qn("w:val"), str(round(size * 2)))


def _font(run, *, name="宋体", size=None, bold=None, color=None):
    run.font.name = name
    if size is not None:
        run.font.size = Pt(size)
    _typeface(run._element.get_or_add_rPr(), name, size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)
    return run


def _indent(element, chars, size):
    properties = element.get_or_add_pPr()
    indent = properties.find(qn("w:ind"))
    if indent is None:
        indent = OxmlElement("w:ind")
        properties.append(indent)
    for tag in ("firstLine", "firstLineChars", "hanging", "hangingChars", "left", "leftChars", "right", "rightChars"):
        indent.attrib.pop(qn(f"w:{tag}"), None)
    indent.set(qn("w:firstLineChars"), str(round(chars * 100)))
    indent.set(qn("w:firstLine"), str(round(chars * size * 20)))
    indent.set(qn("w:left"), "0")
    indent.set(qn("w:right"), "0")


def _field(paragraph, instruction, placeholder="", *, font="宋体", size=12):
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = f" {instruction} "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, code, separate):
        _font(paragraph.add_run(), name=font, size=size)._r.append(element)
    if placeholder:
        _font(paragraph.add_run(placeholder), name=font, size=size)
    _font(paragraph.add_run(), name=font, size=size)._r.append(end)


def _geometry(section, style):
    if style.page_size == "A4":
        section.page_width, section.page_height = Cm(21), Cm(29.7)
    else:
        section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.left_margin = section.right_margin = Cm(style.margin_cm)
    section.top_margin = section.bottom_margin = Cm(style.margin_cm)
    section.header_distance = Cm(style.header_cm)
    section.footer_distance = Cm(style.footer_cm)


def _page_furniture(section, style, *, numbered, start=None):
    section.different_first_page_header_footer = False
    section.header.is_linked_to_previous = False
    section.footer.is_linked_to_previous = False
    for part in (section.header, section.footer):
        for paragraph in part.paragraphs:
            paragraph.clear()
            _indent(paragraph._p, 0, style.body_pt)
    if numbered:
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _font(footer.add_run("第 "), name=style.font_family, size=10.5)
        _field(footer, "PAGE", font=style.font_family, size=10.5)
        _font(footer.add_run(" 页"), name=style.font_family, size=10.5)
        numbering = section._sectPr.find(qn("w:pgNumType"))
        if numbering is None:
            numbering = OxmlElement("w:pgNumType")
            section._sectPr.insert_element_before(numbering, "w:cols", "w:docGrid")
        if start is None:
            numbering.attrib.pop(qn("w:start"), None)
        else:
            numbering.set(qn("w:start"), str(start))
        numbering.set(qn("w:fmt"), "decimal")


def _base_document(package, title):
    document = Document()
    style = package.style
    document.core_properties.title = title
    document.core_properties.subject = package.project.name
    document.core_properties.author = "公司内部标书制作系统"
    document.core_properties.comments = ""
    _geometry(document.sections[0], style)
    defaults = document.styles.element.find(qn("w:docDefaults"))
    if defaults is None:
        defaults = OxmlElement("w:docDefaults")
        document.styles.element.insert(0, defaults)
    r_default = defaults.find(qn("w:rPrDefault"))
    if r_default is None:
        r_default = OxmlElement("w:rPrDefault")
        defaults.append(r_default)
    r_pr = r_default.find(qn("w:rPr"))
    if r_pr is None:
        r_pr = OxmlElement("w:rPr")
        r_default.append(r_pr)
    _typeface(r_pr, style.font_family, style.body_pt)
    for name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        paragraph_style = document.styles[name]
        paragraph_style.font.name = style.font_family
        paragraph_style.font.color.rgb = RGBColor(0, 0, 0)
        size = style.body_pt
        if name.startswith("Heading"):
            size = style.heading_pt[int(name[-1]) - 1]
        elif name == "Title":
            size = style.cover_title_pt
        paragraph_style.font.size = Pt(size)
        _typeface(paragraph_style._element.get_or_add_rPr(), style.font_family, size)
        formatting = paragraph_style.paragraph_format
        formatting.space_before = formatting.space_after = Pt(0)
        formatting.line_spacing = style.body_line_spacing
        formatting.widow_control = True
        formatting.keep_with_next = name != "Normal"
        formatting.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if name == "Normal" else WD_ALIGN_PARAGRAPH.LEFT
        _indent(paragraph_style._element, style.body_first_line_chars if name == "Normal" else 0, size)
        if name.startswith("Heading"):
            paragraph_style.font.bold = True
            if name == "Heading 1":
                formatting.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for border in list(paragraph_style._element.xpath("./w:pPr/w:pBdr")):
            border.getparent().remove(border)
    content_width = document.sections[0].page_width.cm - 2 * style.margin_cm
    for level in (1, 2, 3):
        name = f"TOC {level}"
        if name not in document.styles:
            document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        toc_style = document.styles[name]
        toc_style.font.name = style.font_family
        toc_style.font.size = Pt(style.body_pt)
        toc_style.font.color.rgb = RGBColor(0, 0, 0)
        _typeface(toc_style._element.get_or_add_rPr(), style.font_family, style.body_pt)
        formatting = toc_style.paragraph_format
        formatting.line_spacing = style.body_line_spacing
        formatting.space_before = formatting.space_after = Pt(0)
        _indent(toc_style._element, 0, style.body_pt)
        formatting.left_indent = Cm((level - 1) * 0.74)
        formatting.tab_stops.add_tab_stop(Cm(content_width), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    document.settings.element.append(update)
    # Keep explicit soft-break lines readable inside justified editable prose.
    # This changes layout only; candidate text and paragraph order stay intact.
    compat = document.settings.element.find(qn("w:compat"))
    if compat is None:
        compat = OxmlElement("w:compat")
        document.settings.element.append(compat)
    no_expand = compat.find(qn("w:doNotExpandShiftReturn"))
    if no_expand is None:
        no_expand = OxmlElement("w:doNotExpandShiftReturn")
        compat.insert(0, no_expand)
    no_expand.set(qn("w:val"), "true")
    return document


def _plain(document, text, style, *, size=None, bold=False, align=WD_ALIGN_PARAGRAPH.LEFT, keep_next=False):
    paragraph = document.add_paragraph()
    _indent(paragraph._p, 0, style.body_pt)
    paragraph.alignment = align
    paragraph.paragraph_format.keep_with_next = keep_next
    _font(paragraph.add_run(text), name=style.font_family, size=size or style.body_pt, bold=bold)
    return paragraph


def _cover(document, package):
    style = package.style
    _page_furniture(document.sections[0], style, numbered=True, start=1)
    title = document.add_paragraph("投 标 文 件", "Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(60)
    title.paragraph_format.space_after = Pt(84)
    title.paragraph_format.keep_with_next = False
    _font(title.runs[0], name=style.font_family, size=style.cover_title_pt, bold=True)
    project = _plain(document, f"项目名称：{package.project.name}", style, size=style.cover_meta_pt, align=WD_ALIGN_PARAGRAPH.CENTER)
    project.paragraph_format.space_after = Pt(72)
    _plain(document, f"投标人：{package.company.name}", style, size=style.cover_meta_pt, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    sign = _plain(document, "签字盖章按招标文件要求线下办理", style, align=WD_ALIGN_PARAGRAPH.CENTER)
    sign.paragraph_format.space_before = Pt(12)
    if package.issues:
        paragraph = _plain(document, "本文件含待补事项 详见配套待补清单", style, align=WD_ALIGN_PARAGRAPH.CENTER)
        paragraph.paragraph_format.space_before = Pt(24)
    if package.synthetic:
        paragraph = _plain(document, "合成测试样本 仅用于系统验证 不可用于投标", style, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        _font(paragraph.runs[0], name=style.font_family, color="A80000")


def _issue_text(paragraph, issue, style, *, compact=False):
    marker = f"【待补 {issue.issue_id}】" if compact else issue.marker
    _font(paragraph.add_run(marker), name=style.font_family, size=style.body_pt, bold=True, color="A80000")


def _prescribed_cover(document, package, section):
    if section.native_format is not None:
        from .source_format_rendering import render_native_section
        render_native_section(document, package, section, cover=True)
        return
    style = package.style
    _page_furniture(document.sections[0], style, numbered=True, start=1)
    title = document.add_paragraph(section.title, "Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(60)
    title.paragraph_format.space_after = Pt(84)
    title.paragraph_format.keep_with_next = False
    _font(title.runs[0], name=style.font_family, size=style.cover_title_pt, bold=True)
    for block in section.blocks:
        if isinstance(block, ParagraphBlock):
            paragraph = _plain(document, block.text, style, size=style.cover_meta_pt,
                               align=WD_ALIGN_PARAGRAPH.CENTER)
            paragraph.paragraph_format.space_after = Pt(18)
    issues = {issue.issue_id: issue for issue in package.issues}
    issue_ids = list(dict.fromkeys(ref for block in section.blocks
                                  for ref in [*block.issue_refs, *([block.issue_id] if isinstance(block, IssueBlock) else [])]))
    if issue_ids:
        paragraph = _plain(document, "", style, align=WD_ALIGN_PARAGRAPH.CENTER)
        paragraph.paragraph_format.space_before = Pt(12)
        for index, issue_id in enumerate(issue_ids):
            if index:
                paragraph.add_run(" ")
            _issue_text(paragraph, issues[issue_id], style, compact=True)
        _plain(document, "详见待补清单同编号。", style, align=WD_ALIGN_PARAGRAPH.CENTER)
    if package.synthetic:
        paragraph = _plain(document, "合成测试样本 仅用于系统验证 不可用于投标", style,
                           bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        _font(paragraph.runs[0], name=style.font_family, color="A80000")


def _format_table(document, table, weights, style, header_rows=1, native_layout=None):
    page = document.sections[-1]
    width_twips = round((page.page_width - page.left_margin - page.right_margin) / 635)
    total = sum(weights)
    if native_layout is not None:
        width_twips = round(sum(native_layout.column_widths_pt) * 20)
        widths = [round(width * 20) for width in native_layout.column_widths_pt]
    else:
        widths = [round(width_twips * weight / total) for weight in weights]
    widths[-1] += width_twips - sum(widths)
    table.autofit = False
    table_pr = table._tbl.tblPr
    table_width = table_pr.find(qn("w:tblW"))
    if table_width is None:
        table_width = OxmlElement("w:tblW")
        table_pr.append(table_width)
    table_width.set(qn("w:type"), "dxa")
    table_width.set(qn("w:w"), str(width_twips))
    for column, width in zip(table.columns, widths):
        column.width = Pt(width / 20)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        for attr, value in (("val", "single"), ("sz", "4"), ("color", style.table_border_color)):
            element.set(qn(f"w:{attr}"), value)
        borders.append(element)
    table_pr.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge in ("top", "left", "bottom", "right"):
        margin = OxmlElement(f"w:{edge}")
        margin.set(qn("w:w"), "0" if native_layout is not None else "90")
        margin.set(qn("w:type"), "dxa")
        margins.append(margin)
    table_pr.append(margins)
    if native_layout is not None:
        indent = OxmlElement("w:tblInd")
        indent.set(qn("w:type"), "dxa")
        indent.set(qn("w:w"), str(round(native_layout.bbox[0] * 20)))
        table_pr.append(indent)
    for row_index, row in enumerate(table.rows):
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        if row_index < header_rows:
            repeat = OxmlElement("w:tblHeader")
            repeat.set(qn("w:val"), "true")
            row._tr.get_or_add_trPr().append(repeat)
        for column_index, cell in enumerate(row.cells):
            cell.width = Pt(widths[column_index] / 20)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "FFFFFF")
            cell._tc.get_or_add_tcPr().append(shade)
            for paragraph in cell.paragraphs:
                _indent(paragraph._p, 0, style.table_pt)
                paragraph.paragraph_format.space_after = paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.line_spacing = style.body_line_spacing
                paragraph.paragraph_format.keep_with_next = row_index < header_rows
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if row_index < header_rows else WD_ALIGN_PARAGRAPH.LEFT
                for run in paragraph.runs:
                    _font(run, name=style.font_family, size=style.table_pt, bold=True if row_index < header_rows else None)


def _render_table(document, spec, issues, style, *, show_title=True, native_layout=None):
    if show_title:
        _plain(document, spec.title, style, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, keep_next=True)
    offset = int(spec.header_from_columns)
    table = document.add_table(rows=offset, cols=len(spec.columns))
    if spec.header_from_columns:
        for cell, label in zip(table.rows[0].cells, spec.columns):
            cell.text = label
    for values in spec.rows:
        row = table.add_row()
        for cell, value in zip(row.cells, values):
            text = value.text
            for issue_id in value.issue_refs:
                text = re.sub(r"【待补\s+" + re.escape(issue_id) + r"(?:\s*[:：][^】]*)?】", "", text)
            cell.text = text.strip()
            if native_layout is None:
                for issue_id in value.issue_refs:
                    _issue_text(cell.paragraphs[0], issues[issue_id], style, compact=True)
    weights = spec.column_widths or ([1, 2.4] if len(spec.columns) == 2 else [1] * len(spec.columns))
    _format_table(document, table, weights, style, spec.repeat_header_rows, native_layout)
    for merge in spec.merges:
        anchor = table.cell(merge.row + offset, merge.column)
        paragraph_count = len(anchor.paragraphs)
        merged = anchor.merge(table.cell(merge.row + merge.row_span - 1 + offset, merge.column + merge.col_span - 1))
        # python-docx appends the covered empty cells' paragraphs. They are
        # geometry, not source content, and otherwise inflate the form's row.
        for paragraph in merged.paragraphs[paragraph_count:]:
            if not paragraph.text:
                paragraph._p.getparent().remove(paragraph._p)
    if native_layout is not None:
        from .source_format_rendering import native_font, _underlined_run
        for row, height in zip(table.rows, native_layout.row_heights_pt):
            row.height, row.height_rule = Pt(height), WD_ROW_HEIGHT_RULE.AT_LEAST
        for cell_spec in native_layout.cells:
            cell = table.cell(cell_spec.row, cell_spec.column)
            cell.vertical_alignment = {"top": WD_CELL_VERTICAL_ALIGNMENT.TOP,
                "center": WD_CELL_VERTICAL_ALIGNMENT.CENTER, "bottom": WD_CELL_VERTICAL_ALIGNMENT.BOTTOM}[cell_spec.vertical_alignment]
            value = spec.rows[cell_spec.row][cell_spec.column]
            if cell_spec.underlines:
                paragraph = cell.paragraphs[0]
                paragraph.clear()
                cursor = 0
                for rule in sorted(cell_spec.underlines, key=lambda r:r.start):
                    _font(paragraph.add_run(value.text[cursor:rule.start]), name=native_font(cell_spec.font_family),
                          size=cell_spec.font_size_pt, bold=cell_spec.bold)
                    _underlined_run(paragraph, value.text[rule.start:rule.end], rule.width_pt,
                                    native_font(cell_spec.font_family), cell_spec.font_size_pt, cell_spec.bold)
                    cursor = rule.end
                _font(paragraph.add_run(value.text[cursor:]), name=native_font(cell_spec.font_family),
                      size=cell_spec.font_size_pt, bold=cell_spec.bold)
            for index, paragraph in enumerate(cell.paragraphs):
                paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                    "right": WD_ALIGN_PARAGRAPH.RIGHT}[cell_spec.alignment]
                _indent(paragraph._p, 0, cell_spec.font_size_pt)
                pf = paragraph.paragraph_format
                pf.line_spacing = Pt(cell_spec.font_size_pt * 1.15)
                pf.space_before = Pt(cell_spec.top_inset_pt if index == 0 else 0.)
                pf.space_after = Pt(cell_spec.bottom_inset_pt if index == len(cell.paragraphs)-1 else 0.)
                pf.left_indent, pf.right_indent = Pt(cell_spec.left_inset_pt), Pt(cell_spec.right_inset_pt)
                snap = OxmlElement("w:snapToGrid")
                snap.set(qn("w:val"), "0")
                paragraph._p.get_or_add_pPr().append(snap)
                for run in paragraph.runs:
                    _font(run, name=native_font(cell_spec.font_family), size=cell_spec.font_size_pt, bold=cell_spec.bold)
            for identity in value.issue_refs:
                run = cell.paragraphs[0].add_run((" " if cell.paragraphs[0].text else "") + identity)
                _font(run, name=native_font(cell_spec.font_family), size=9, color="A80000")
    else:
        spacer = _plain(document, "", style)
        spacer.paragraph_format.space_after = Pt(3)
        spacer.paragraph_format.line_spacing = 0.3
    return table


def _validate_files(package):
    for source in package.sources:
        if not source.path.is_file():
            raise RenderingError(f"source file missing: {source.source_id}")
        if file_sha256(source.path) != source.sha256:
            raise RenderingError(f"source hash mismatch: {source.source_id}")
    for attachment in package.attachments:
        if attachment.path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise RenderingError(f"unsupported attachment format: {attachment.attachment_id}; M1 accepts PNG/JPEG only")
        if not attachment.path.is_file() or file_sha256(attachment.path) != attachment.sha256:
            raise RenderingError(f"attachment missing or hash mismatch: {attachment.attachment_id}")
        try:
            with Image.open(attachment.path) as image:
                image_format = image.format
                image.verify()
            expected = "PNG" if attachment.path.suffix.lower() == ".png" else "JPEG"
            if image_format != expected:
                raise RenderingError(f"attachment extension/content mismatch: {attachment.attachment_id}")
        except RenderingError:
            raise
        except Exception as exc:
            raise RenderingError(f"attachment unreadable: {attachment.attachment_id}") from exc


def _render_section_content(document, package, section, *, seen_attachment_groups=None):
    """Render the same editable content in its declared destination document."""
    style = package.style
    issues = {issue.issue_id: issue for issue in package.issues}
    attachments = {item.attachment_id: item for item in package.attachments}
    if seen_attachment_groups is None:
        seen_attachment_groups = set()
    table_issue_ids = {ref for block in section.blocks if isinstance(block, TableBlock)
                       for row in block.table.rows for cell in row for ref in cell.issue_refs}
    for block in section.blocks:
        if isinstance(block, HeadingBlock):
            document.add_paragraph(block.text, f"Heading {block.level}")
        elif isinstance(block, ParagraphBlock):
            # Content provenance does not change the house body indentation.
            # Prescribed source geometry is rendered separately before this path.
            document.add_paragraph(block.text)
        elif isinstance(block, ListBlock):
            for index, item in enumerate(block.items, 1):
                document.add_paragraph(f"（{index}）{item}" if block.ordered else f"• {item}")
        elif isinstance(block, TableBlock):
            _render_table(document, block.table, issues, style, show_title=block.table.title != section.title)
        elif isinstance(block, IssueBlock):
            if block.issue_id not in table_issue_ids:
                paragraph = _plain(document, "", style)
                issue = issues[block.issue_id]
                _issue_text(paragraph, issue, style)
                destination = "本清单" if section.document_role == "review" else "待补清单"
                _font(paragraph.add_run(f" {issue.action} 详见{destination}同编号。"), name=style.font_family, size=style.body_pt)
        elif isinstance(block, AttachmentBlock):
            attachment = attachments[block.attachment_id]
            group = (("bundle", attachment.bundle_id) if attachment.bundle_id else
                     ("single", attachment.attachment_id))
            continuation = group in seen_attachment_groups
            if not continuation:
                seen_attachment_groups.add(group)
                if attachment.title != section.title:
                    document.add_paragraph(attachment.title, f"Heading {min(section.level + 1, 3)}")
            else:
                _plain(document, f"{attachment.title}（第{attachment.page_index}/{attachment.page_count}页）",
                       style, bold=True, keep_next=True)
            if attachment.synthetic:
                paragraph = _plain(document, "以下图片为合成测试用途，不是实际公司证明或证照。", style, bold=True, keep_next=True)
                _font(paragraph.runs[0], name=style.font_family, color="A80000")
            with Image.open(attachment.path) as image:
                image_width, image_height = image.size
            page = document.sections[-1]
            max_width = page.page_width.cm - 2 * style.margin_cm
            max_height = page.page_height.cm - 2 * style.margin_cm - (2 if continuation else 5)
            scale = min(max_width / image_width, max_height / image_height)
            paragraph = _plain(document, "", style, align=WD_ALIGN_PARAGRAPH.CENTER)
            picture = paragraph.add_run().add_picture(str(attachment.path), width=Cm(image_width * scale), height=Cm(image_height * scale))
            picture._inline.docPr.set("descr", attachment.description)
        for issue_id in block.issue_refs:
            if isinstance(block, IssueBlock) and issue_id == block.issue_id:
                continue  # The explicit issue block already includes its marker and action.
            _issue_text(_plain(document, "", style), issues[issue_id], style)


def _render_bid(package):
    document = _base_document(package, package.title)
    style = package.style
    cover_id = package.prescribed_cover_section_id
    if cover_id is None:
        _cover(document, package)
    else:
        _prescribed_cover(document, package, next(section for section in package.sections if section.section_id == cover_id))
    toc_page = document.add_section(WD_SECTION_START.NEW_PAGE)
    _geometry(toc_page, style)
    _page_furniture(toc_page, style, numbered=True)
    _plain(document, "目 录", style, size=style.heading_pt[0], bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    _field(_plain(document, "", style), 'TOC \\o "1-3" \\h \\z \\u', "请在 Word 或 WPS 中更新目录字段以显示实际页码。", font=style.font_family, size=style.body_pt)
    body_page = document.add_section(WD_SECTION_START.NEW_PAGE)
    _geometry(body_page, style)
    _page_furniture(body_page, style, numbered=True)
    body_sections_seen = 0
    seen_attachment_groups = set()
    native_page = False
    prior_partial = None
    for section in package.sections:
        if section.document_role != "response" or not section.render_body or section.section_id == cover_id:
            continue
        if section.native_format is not None:
            layout = section.native_format
            scopes = getattr(layout, "page_scopes", ())
            partial = scopes[0] if len(scopes) == 1 and scopes[0].scope_kind == "partial_page" else None
            first = next((m for m in partial.members if m.role == "selected"), None) if partial else None
            append = bool(prior_partial and partial and first and
                          prior_partial[0] == layout.source_sha256 and prior_partial[1] == partial.physical_page and
                          prior_partial[2] + 1 == first.ordinal and section.level > 1 and not section.start_on_new_page)
            if body_sections_seen and not append:
                document.add_section(WD_SECTION_START.NEW_PAGE)
            from .source_format_rendering import render_native_section
            render_native_section(document, package, section, append=append,
                                  previous_source_bottom=prior_partial[3] if append else None)
            fixed_count = sum(len(e.fixed_indices) if e.kind == "paragraph" else 1
                              for e in section.native_format.elements)
            remaining = [block for block in section.blocks[fixed_count:] if not isinstance(block, IssueBlock)]
            if remaining:
                page = document.add_section(WD_SECTION_START.NEW_PAGE)
                _geometry(page, style)
                _page_furniture(page, style, numbered=True)
                continuation = section.model_copy(update={"blocks": remaining, "native_format": None})
                _render_section_content(document, package, continuation,
                                        seen_attachment_groups=seen_attachment_groups)
            body_sections_seen += 1
            native_page = not bool(remaining)
            last = next((m for m in reversed(partial.members) if m.role == "selected"), None) if partial else None
            prior_partial = (layout.source_sha256, partial.physical_page, last.ordinal, partial.scope_bbox[3]) if partial and not remaining else None
            continue
        prior_partial = None
        if native_page:
            page = document.add_section(WD_SECTION_START.NEW_PAGE)
            _geometry(page, style)
            _page_furniture(page, style, numbered=True)
            native_page = False
        elif body_sections_seen and (section.level == 1 or section.start_on_new_page):
            document.add_page_break()
        body_sections_seen += 1
        document.add_paragraph(section.title, f"Heading {section.level}")
        _render_section_content(document, package, section, seen_attachment_groups=seen_attachment_groups)
    return document


def _source_quote_catalog(package):
    """Point repeated source text to its verbatim appendix, retaining extras."""
    requirements = {item.requirement_id: item.text for item in package.requirements}
    links, supplemental = {}, {}
    quotes = [(item.source_requirement, item.requirement_refs)
              for item in [*package.issues, *package.review_actions]]
    quotes.extend((context.text, []) for action in package.review_actions for context in action.source_scope_contexts)
    for text, refs in quotes:
        if text not in links:
            matching = [rid for rid, original in requirements.items() if text == original]
            if not matching:
                matching = [rid for rid, original in requirements.items() if text in original]
            if not matching and any(text == separator.join(requirements[ref] for ref in refs)
                                    for separator in ("\n", "；", "\n\n")):
                matching = list(refs)
            if matching:
                links[text] = f"见“响应要求”原文附录：{'、'.join(matching)}。"
            else:
                identity = f"SRCQ-{len(supplemental) + 1:03d}"
                supplemental[text] = {"quote_id": identity, "text": text, "requirement_refs": []}
                links[text] = f"见“补充来源原文”附录：{identity}。"
        if text in supplemental:
            supplemental[text]["requirement_refs"] = list(dict.fromkeys(
                [*supplemental[text]["requirement_refs"], *refs]))
    return {"links": links, "supplemental": list(supplemental.values())}


def _trace_appendix(document, package, source_quote_catalog=None):
    style = package.style
    document.add_page_break()
    document.add_paragraph("制作范围与来源追溯", "Heading 1")
    _plain(document, package.preparation_note, style)
    _plain(document, f"样式配置：{style.profile_id}。参考来源：{'、'.join(style.reference_source_refs) or '默认样式'}。", style)
    _plain(document, "下列编号对应冻结输入中的原件、定位和校验值。正文事实与技术拟实施方法分开记录。", style)

    def trace(text, *, bold=False, keep_next=False, size=None):
        paragraph = _plain(document, text, style, size=size or style.table_pt, bold=bold, keep_next=keep_next)
        paragraph.paragraph_format.space_after = Pt(4)
        paragraph.paragraph_format.line_spacing = 1.2
        return paragraph

    trace("文档位置与要求映射", bold=True, keep_next=True)
    for section in package.sections:
        destination = "投标正文" if section.document_role == "response" else "配套核对清单"
        trace(f"{section.section_id} {section.title}；位置 {destination}；要求 {'、'.join(section.requirement_refs)}。")
        if section.document_role == "review":
            trace("保留来源原文及待核事项，不计为已完成投标响应，也未认定满足。")
        if not section.render_body:
            trace("该项无需单独投标正文；办理或约束核对状态保留于清单，不代表已经满足。")
        for block in section.blocks:
            if isinstance(block, (HeadingBlock, TableBlock, AttachmentBlock)):
                if isinstance(block, HeadingBlock):
                    label = block.text
                elif isinstance(block, TableBlock):
                    label = f"表格 {block.table.table_id} {block.table.title}"
                else:
                    label = f"附件 {block.attachment_id}"
                trace(f"位置 {label}；要求 {'、'.join(block.requirement_refs) or '见所属章节'}。")
    trace("采用事实", bold=True, keep_next=True)
    used_fact_ids = set()
    for section in package.sections:
        if not section.render_body:
            continue
        for block in section.blocks:
            used_fact_ids.update(getattr(block, "fact_refs", []))
            if isinstance(block, TableBlock):
                used_fact_ids.update(ref for row in block.table.rows for cell in row for ref in cell.fact_refs)
    for fact in package.facts:
        if fact.fact_id in used_fact_ids:
            trace(f"{fact.fact_id} {fact.label}：{fact.value}；来源 {'、'.join(fact.source_refs)}。")
    if not used_fact_ids:
        trace("本次文档和表格没有采用来源事实值；未采用的资料保留在受控输入中。")
    groups = {}
    for source in package.sources:
        groups.setdefault(source.sha256, []).append(source)
    for group_index, (digest, sources) in enumerate(groups.items(), 1):
        trace(f"原件组 {group_index} 文件 {sources[0].path.name}", bold=True, keep_next=True)
        trace(f"SHA-256：{digest}", size=8, keep_next=True)
        for source in sources:
            trace(f"{source.source_id} {source.title}；定位：{source.locator}；来源类型：{source.provenance}。")
    trace("响应要求", bold=True, keep_next=True)
    for requirement in package.requirements:
        trace(f"{requirement.requirement_id} {requirement.text} 来源 {'、'.join(requirement.source_refs)}。")
    source_quote_catalog = source_quote_catalog or _source_quote_catalog(package)
    if source_quote_catalog["supplemental"]:
        trace("补充来源原文", bold=True, keep_next=True)
        for quote in source_quote_catalog["supplemental"]:
            trace(f"{quote['quote_id']} 对应要求 {'、'.join(quote['requirement_refs']) or '见事项来源定位'}：{quote['text']}")
    trace("实际装配附件", bold=True, keep_next=True)
    for attachment in package.attachments:
        destination = next(item.document_role for item in package.sections if item.section_id == attachment.section_id)
        trace(f"{attachment.attachment_id} {attachment.title}；{'正文' if destination == 'response' else '配套清单'}位置 {attachment.section_id}；来源 {'、'.join(attachment.source_refs)}。")
        trace(attachment.description)
        trace(f"装配图像 SHA-256：{attachment.sha256}", size=8)


def _render_issues(package):
    title = f"{package.project.name} 待补清单"
    document = _base_document(package, title)
    style = package.style
    _page_furniture(document.sections[0], style, numbered=True, start=1)
    heading = document.add_paragraph(title, "Title")
    _font(heading.runs[0], name=style.font_family, size=18, bold=True)
    heading.paragraph_format.space_after = Pt(12)
    if package.synthetic:
        paragraph = _plain(document, "合成测试样本 仅用于系统验证 不可用于投标", style, bold=True)
        _font(paragraph.runs[0], name=style.font_family, color="A80000")
    _plain(document, f"项目名称：{package.project.name}", style)
    _plain(document, f"投标人：{package.company.name}", style)
    if package.issues:
        location_note = ("各待补编号按下列投标正文或配套清单位置查找。" if
                         any(section.document_role == "review" for section in package.sections) else
                         "正文和清单使用同一待补编号。")
        _plain(document, f"需线下补充的事项如下。{location_note}", style, bold=True)
    elif not package.review_actions and not any(section.document_role == "review" for section in package.sections):
        _plain(document, "本次未自动检出待补项。提交前仍须线下核对原件、报价及签字盖章。", style)
    titles = {section.section_id: section.title for section in package.sections}
    roles = {section.section_id: section.document_role for section in package.sections}
    source_quotes = _source_quote_catalog(package)
    for issue in package.issues:
        heading = document.add_paragraph(f"{issue.issue_id} {issue.title}", "Heading 2")
        heading.paragraph_format.space_before = Pt(12)
        if roles[issue.section_id] == "review":
            _plain(document, f"配套清单位置：{titles[issue.section_id]}（{issue.section_id}）。在本清单中搜索同编号即可定位。", style)
        else:
            _plain(document, f"正文位置：{titles[issue.section_id]}（{issue.section_id}）。在投标文件中搜索同编号即可定位。", style)
        _plain(document, f"对应要求：{'、'.join(issue.requirement_refs)}", style)
        _plain(document, f"缺项原因：{issue.reason}", style)
        _plain(document, f"来源要求：{source_quotes['links'][issue.source_requirement]}", style)
        _plain(document, f"补充动作：{issue.action}", style)
    if package.review_actions:
        document.add_paragraph("待办理事项与待核验商业约束", "Heading 1")
        _plain(document, "以下保留招标来源规则，不属于公司事实、报价或履约承诺；无需单独正文不代表已经满足。", style)
        sources = {source.source_id: source for source in package.sources}
        for action in package.review_actions:
            label = "商业约束待核验" if action.kind == "commercial_constraint" else "程序事项待办理"
            status = "尚未核验" if action.status == "not_checked" else "待线下办理"
            document.add_paragraph(f"{action.action_id} {label}", "Heading 2")
            _plain(document, f"状态：{status}；尚未认定满足。", style)
            _plain(document, f"对应要求：{'、'.join(action.requirement_refs)}", style)
            _plain(document, f"来源要求：{source_quotes['links'][action.source_requirement]}", style)
            _plain(document, f"办理或核对动作：{action.instruction}", style)
            if action.source_scope_status != "not_applicable":
                _plain(document, "适用范围尚未确定；该事项仅待线下核对，未据此判定正文、报价或资格。", style)
                for context in action.source_scope_contexts:
                    _plain(document, f"候选上层原文：{source_quotes['links'][context.text]}", style)
                    source = sources[context.source_ref]
                    _plain(document, f"上层来源定位：{context.source_ref}；{source.title}；{source.locator}", style)
            for ref in action.source_refs:
                source = sources[ref]
                _plain(document, f"来源定位：{ref}；{source.title}；{source.locator}", style)
    review_sections = [section for section in package.sections
                       if section.document_role == "review" and section.render_body]
    if review_sections:
        document.add_page_break()
        document.add_paragraph("来源原文与待核对内容", "Heading 1")
        _plain(document, "以下内容保留完整原文、表格和缺项，仅供配套核对；不计为正式投标响应，尚未认定满足。", style)
        seen_attachment_groups = set()
        for index, section in enumerate(review_sections):
            if index and section.start_on_new_page:
                document.add_page_break()
            document.add_paragraph(section.title, f"Heading {section.level}")
            _plain(document, f"核对位置：{section.section_id}；对应要求：{'、'.join(section.requirement_refs)}。", style)
            _render_section_content(document, package, section, seen_attachment_groups=seen_attachment_groups)
    _trace_appendix(document, package, source_quotes)
    return document


DIAGNOSTIC_MARKER = "诊断回放产物 · 仅供诊断核对 · 不可作为正式投标文件"


def _mark_diagnostic(document, package):
    """Mark the actual first page, including a source-prescribed cover."""
    document.core_properties.title = "【诊断回放】" + document.core_properties.title
    paragraph = document.paragraphs[0].insert_paragraph_before(DIAGNOSTIC_MARKER)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.keep_with_next = True
    _font(paragraph.runs[0], name=package.style.font_family, size=14, bold=True, color="A80000")
    for section in document.sections:
        for header in (section.header, section.first_page_header, section.even_page_header):
            header.is_linked_to_previous = False
            if any(paragraph.text == DIAGNOSTIC_MARKER for paragraph in header.paragraphs):
                continue
            paragraph = header.add_paragraph(DIAGNOSTIC_MARKER)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _font(paragraph.runs[0], name=package.style.font_family, size=9, bold=True, color="A80000")


def render_package(package: DocumentPackage, output_dir: Path, *, diagnostic: bool = False,
                   review_document_mode: str = "full_trace") -> dict:
    """Assemble both files completely and exclusively; never overwrite results.

    Pagination uses real Word fields. The caller must visually verify the
    target Word/WPS render. M1 embeds only verified PNG/JPEG attachment bytes.
    """
    package = DocumentPackage.model_validate(package.model_dump())
    if review_document_mode not in {"full_trace", "task_list"}:
        raise ValueError("REVIEW_DOCUMENT_MODE_INVALID")
    output_dir = Path(output_dir)
    prefix = "诊断回放-" if diagnostic else ""
    targets = {"bid_docx": output_dir / (prefix + "投标文件.docx"), "issues_docx": output_dir / (prefix + "待补清单.docx")}
    if any(path.exists() for path in targets.values()):
        raise FileExistsError("output already exists; use a new immutable result directory")
    _validate_files(package)
    output_dir.mkdir(parents=True, exist_ok=True)
    published = []
    try:
        with tempfile.TemporaryDirectory(prefix=".render-", dir=output_dir) as staging:
            staged = {key: Path(staging) / path.name for key, path in targets.items()}
            bid = _render_bid(package)
            review_index = None
            if review_document_mode == "task_list":
                from .review_document import render_task_list
                issues, review_index = render_task_list(package)
            else:
                issues = _render_issues(package)
            if diagnostic:
                _mark_diagnostic(bid, package)
                _mark_diagnostic(issues, package)
            bid.save(staged["bid_docx"])
            issues.save(staged["issues_docx"])
            for key, target in targets.items():
                with target.open("xb") as dest:
                    published.append(target)
                    with staged[key].open("rb") as source:
                        shutil.copyfileobj(source, dest)
                    dest.flush()
                    os.fsync(dest.fileno())
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    result = {key: {"path": str(path.resolve()), "sha256": file_sha256(path), "size_bytes": path.stat().st_size} for key, path in targets.items()}
    result["warnings"] = ["目录与页码使用 Word 字段。请在 Word/WPS 中更新全部字段并人工检查排版；不提供估算页码。", "来源定位、事实映射及制作范围见待补清单的追溯附录。"]
    result["attachment_count"] = len(package.attachments)
    result["issue_ids"] = [issue.issue_id for issue in package.issues]
    if review_index is not None:
        result["review_document_index"] = review_index
        result["warnings"][1] = "按编号核对待补与办理事项；完整来源原文见清单中的原文附录。"
    return result
