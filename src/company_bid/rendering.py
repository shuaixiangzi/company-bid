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
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
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
            formatting.space_before = formatting.space_after = Pt(6)
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


def _format_table(document, table, weights, style, header_rows=1):
    page = document.sections[-1]
    width_twips = round((page.page_width - page.left_margin - page.right_margin) / 635)
    total = sum(weights)
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
        margin.set(qn("w:w"), "90")
        margin.set(qn("w:type"), "dxa")
        margins.append(margin)
    table_pr.append(margins)
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


def _render_table(document, spec, issues, style, *, show_title=True):
    if show_title:
        _plain(document, spec.title, style, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, keep_next=True)
    offset = int(spec.header_from_columns)
    table = document.add_table(rows=offset, cols=len(spec.columns))
    if spec.header_from_columns:
        for cell, label in zip(table.rows[0].cells, spec.columns):
            cell.text = label
    seen_issues = set()
    for values in spec.rows:
        row = table.add_row()
        for cell, value in zip(row.cells, values):
            text = value.text
            for issue_id in value.issue_refs:
                text = re.sub(r"【待补\s+" + re.escape(issue_id) + r"(?:\s*[:：][^】]*)?】", "", text)
            cell.text = text.strip()
            for issue_id in value.issue_refs:
                _issue_text(cell.paragraphs[0], issues[issue_id], style, compact=issue_id in seen_issues)
                seen_issues.add(issue_id)
    weights = spec.column_widths or ([1, 2.4] if len(spec.columns) == 2 else [1] * len(spec.columns))
    _format_table(document, table, weights, style, spec.repeat_header_rows)
    for merge in spec.merges:
        anchor = table.cell(merge.row + offset, merge.column)
        paragraph_count = len(anchor.paragraphs)
        merged = anchor.merge(table.cell(merge.row + merge.row_span - 1 + offset, merge.column + merge.col_span - 1))
        # python-docx appends the covered empty cells' paragraphs. They are
        # geometry, not source content, and otherwise inflate the form's row.
        for paragraph in merged.paragraphs[paragraph_count:]:
            if not paragraph.text:
                paragraph._p.getparent().remove(paragraph._p)
    spacer = _plain(document, "", style)
    spacer.paragraph_format.space_after = Pt(3)
    spacer.paragraph_format.line_spacing = 0.3


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
    issues = {issue.issue_id: issue for issue in package.issues}
    attachments = {item.attachment_id: item for item in package.attachments}
    body_sections_seen = 0
    for section_index, section in enumerate(package.sections):
        if not section.render_body or section.section_id == cover_id:
            continue
        preceding = section_index if cover_id is None else body_sections_seen
        if preceding and section.start_on_new_page:
            document.add_page_break()
        body_sections_seen += 1
        document.add_paragraph(section.title, f"Heading {section.level}")
        for block in section.blocks:
            if isinstance(block, HeadingBlock):
                document.add_paragraph(block.text, f"Heading {block.level}")
            elif isinstance(block, ParagraphBlock):
                if block.content_role == "proposal":
                    document.add_paragraph(block.text)
                else:
                    _plain(document, block.text, style)
            elif isinstance(block, ListBlock):
                for index, item in enumerate(block.items, 1):
                    _plain(document, f"（{index}）{item}" if block.ordered else f"• {item}", style)
            elif isinstance(block, TableBlock):
                _render_table(document, block.table, issues, style, show_title=block.table.title != section.title)
            elif isinstance(block, IssueBlock):
                paragraph = _plain(document, "", style)
                issue = issues[block.issue_id]
                _issue_text(paragraph, issue, style)
                _font(paragraph.add_run(f" {issue.action} 详见待补清单同编号。"), name=style.font_family, size=style.body_pt)
            elif isinstance(block, AttachmentBlock):
                attachment = attachments[block.attachment_id]
                if attachment.title != section.title:
                    document.add_paragraph(attachment.title, f"Heading {min(section.level + 1, 3)}")
                if attachment.synthetic:
                    paragraph = _plain(document, "以下图片为合成测试用途，不是实际公司证明或证照。", style, bold=True, keep_next=True)
                    _font(paragraph.runs[0], name=style.font_family, color="A80000")
                with Image.open(attachment.path) as image:
                    image_width, image_height = image.size
                page = document.sections[-1]
                max_width = page.page_width.cm - 2 * style.margin_cm
                max_height = page.page_height.cm - 2 * style.margin_cm - 5
                scale = min(max_width / image_width, max_height / image_height)
                paragraph = _plain(document, "", style, align=WD_ALIGN_PARAGRAPH.CENTER)
                picture = paragraph.add_run().add_picture(str(attachment.path), width=Cm(image_width * scale), height=Cm(image_height * scale))
                picture._inline.docPr.set("descr", attachment.description)
            for issue_id in block.issue_refs:
                if isinstance(block, IssueBlock) and issue_id == block.issue_id:
                    continue  # The explicit issue block already includes this marker and its action.
                _issue_text(_plain(document, "", style), issues[issue_id], style)
    return document


def _trace_appendix(document, package):
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

    trace("正文位置与要求映射", bold=True, keep_next=True)
    for section in package.sections:
        trace(f"{section.section_id} {section.title}；要求 {'、'.join(section.requirement_refs)}。")
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
        trace("本次正文和表格没有采用来源事实值；未采用的资料保留在受控输入中。")
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
    trace("实际装配附件", bold=True, keep_next=True)
    for attachment in package.attachments:
        trace(f"{attachment.attachment_id} {attachment.title}；正文位置 {attachment.section_id}；来源 {'、'.join(attachment.source_refs)}。")
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
        _plain(document, "需线下补充的事项如下。正文和清单使用同一待补编号。", style, bold=True)
    elif not package.review_actions:
        _plain(document, "本次未自动检出待补项。提交前仍须线下核对原件、报价及签字盖章。", style)
    titles = {section.section_id: section.title for section in package.sections}
    for issue in package.issues:
        heading = document.add_paragraph(f"{issue.issue_id} {issue.title}", "Heading 2")
        heading.paragraph_format.space_before = Pt(12)
        _issue_text(_plain(document, "", style), issue, style)
        _plain(document, f"正文位置：{titles[issue.section_id]}（{issue.section_id}）。在投标文件中搜索同编号即可定位。", style)
        _plain(document, f"对应要求：{'、'.join(issue.requirement_refs)}", style)
        _plain(document, f"缺项原因：{issue.reason}", style)
        _plain(document, f"来源要求：{issue.source_requirement}", style)
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
            _plain(document, f"来源要求：{action.source_requirement}", style)
            _plain(document, f"办理或核对动作：{action.instruction}", style)
            for ref in action.source_refs:
                source = sources[ref]
                _plain(document, f"来源定位：{ref}；{source.title}；{source.locator}", style)
    _trace_appendix(document, package)
    return document


def render_package(package: DocumentPackage, output_dir: Path) -> dict:
    """Assemble both files completely and exclusively; never overwrite results.

    Pagination uses real Word fields. The caller must visually verify the
    target Word/WPS render. M1 embeds only verified PNG/JPEG attachment bytes.
    """
    package = DocumentPackage.model_validate(package.model_dump())
    output_dir = Path(output_dir)
    targets = {"bid_docx": output_dir / "投标文件.docx", "issues_docx": output_dir / "待补清单.docx"}
    if any(path.exists() for path in targets.values()):
        raise FileExistsError("output already exists; use a new immutable result directory")
    _validate_files(package)
    output_dir.mkdir(parents=True, exist_ok=True)
    published = []
    try:
        with tempfile.TemporaryDirectory(prefix=".render-", dir=output_dir) as staging:
            staged = {key: Path(staging) / path.name for key, path in targets.items()}
            _render_bid(package).save(staged["bid_docx"])
            _render_issues(package).save(staged["issues_docx"])
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
    return result
