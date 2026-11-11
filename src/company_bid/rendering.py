"""Deterministic M1 DOCX assembly; images are embedded, not linked placeholders."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

from .models import (
    AttachmentBlock,
    DocumentPackage,
    HeadingBlock,
    IssueBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
    file_sha256,
)


class RenderingError(ValueError):
    """Input or assembly failed; no successful result manifest is returned."""


def _font(run, *, name="宋体", size=None, bold=None, color=None):
    run.font.name = name
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    return run


def _field(paragraph, instruction: str, placeholder: str = ""):
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
    for element in [begin, code, separate]:
        paragraph.add_run()._r.append(element)
    if placeholder:
        _font(paragraph.add_run(placeholder), size=10)
    paragraph.add_run()._r.append(end)


def _base_document(package: DocumentPackage, title: str):
    document = Document()
    document.core_properties.title = title
    document.core_properties.subject = package.project.name
    document.core_properties.author = "公司内部标书制作系统"
    document.core_properties.comments = ""
    page = document.sections[0]
    page.page_width = Inches(8.5)
    page.page_height = Inches(11)
    page.left_margin = page.right_margin = Inches(0.8)
    page.top_margin = page.bottom_margin = Inches(0.75)
    page.header_distance = page.footer_distance = Inches(0.3)
    normal = document.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.widow_control = True
    for style_name in ["Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"]:
        style = document.styles[style_name]
        style.font.name = "黑体"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "黑体")
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(7)
        for border in list(style._element.xpath("./w:pPr/w:pBdr")):
            border.getparent().remove(border)
    document.styles["Title"].font.size = Pt(22)
    document.styles["Heading 1"].font.size = Pt(16)
    document.styles["Heading 2"].font.size = Pt(13)
    document.styles["Heading 3"].font.size = Pt(11)
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    document.settings.element.append(update)
    footer = page.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(footer.add_run("第 "), size=9)
    _field(footer, "PAGE")
    _font(footer.add_run(" 页"), size=9)
    title_paragraph = document.add_paragraph(title, "Title")
    for border in list(title_paragraph._p.xpath("./w:pPr/w:pBdr")):
        border.getparent().remove(border)
    if package.synthetic:
        _font(document.add_paragraph().add_run("合成测试样本 仅用于系统验证 不可用于投标"), bold=True, color="A80000")
    document.add_paragraph(f"项目名称：{package.project.name}")
    document.add_paragraph(f"投标主体：{package.company.name}")
    document.add_paragraph(package.preparation_note)
    if package.issues:
        _font(document.add_paragraph().add_run("生成完成，需线下补充。正文待补编号与待补清单逐项对应。"), bold=True, color="A80000")
    else:
        document.add_paragraph("系统检查完成。提交前请由使用者线下审阅、核对并办理签字盖章。")
    return document


def _refs(document, *, fact_refs=(), source_refs=(), requirement_refs=()):
    parts = []
    if fact_refs:
        parts.append("事实 " + "、".join(dict.fromkeys(fact_refs)))
    if source_refs:
        parts.append("来源 " + "、".join(dict.fromkeys(source_refs)))
    if requirement_refs:
        parts.append("响应要求 " + "、".join(dict.fromkeys(requirement_refs)))
    if parts:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(8)
        _font(paragraph.add_run("；".join(parts) + "。对应原件及定位见文末来源与事实追溯。"), size=9, color="555555")


def _issue_text(paragraph, issue, *, compact=False):
    marker = f"【待补 {issue.issue_id}】" if compact else issue.marker
    _font(paragraph.add_run(marker), bold=True, color="A80000")


def _format_table(table, weights):
    table.autofit = False
    width = 6.9
    total = sum(weights)
    for column, weight in zip(table.columns, weights):
        column.width = Inches(width * weight / total)
    table_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ["top", "left", "bottom", "right", "insideH", "insideV"]:
        element = OxmlElement(f"w:{edge}")
        for attr, value in [("val", "single"), ("sz", "5"), ("color", "D9D9D9")]:
            element.set(qn(f"w:{attr}"), value)
        borders.append(element)
    table_pr.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge in ["top", "left", "bottom", "right"]:
        margin = OxmlElement(f"w:{edge}")
        margin.set(qn("w:w"), "100")
        margin.set(qn("w:type"), "dxa")
        margins.append(margin)
    table_pr.append(margins)
    for row_index, row in enumerate(table.rows):
        cant_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(cant_split)
        if row_index == 0:
            repeat = OxmlElement("w:tblHeader")
            repeat.set(qn("w:val"), "true")
            row._tr.get_or_add_trPr().append(repeat)
        for cell_index, cell in enumerate(row.cells):
            cell.width = Inches(width * weights[cell_index] / total)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "E8EEF3" if row_index == 0 else "FFFFFF")
            cell._tc.get_or_add_tcPr().append(shade)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(3)
                paragraph.paragraph_format.space_before = Pt(3)
                paragraph.paragraph_format.line_spacing = 1.15
                for run in paragraph.runs:
                    _font(run, size=10, bold=True if row_index == 0 else None)


def _render_table(document, spec, issues):
    caption = document.add_paragraph(spec.title)
    caption.paragraph_format.keep_with_next = True
    _font(caption.runs[0], bold=True)
    table = document.add_table(rows=1, cols=len(spec.columns))
    for cell, label in zip(table.rows[0].cells, spec.columns):
        cell.text = label
    fact_refs = []
    table_issue_ids = set()
    for values in spec.rows:
        row = table.add_row()
        for cell, value in zip(row.cells, values):
            text = value.text
            # Prepared inputs may already contain a human-readable placeholder.
            # Render the canonical marker exactly once, preserving other text.
            for issue_id in value.issue_refs:
                text = re.sub(r"【待补\s+" + re.escape(issue_id) + r"(?:\s*[:：][^】]*)?】", "", text)
            cell.text = text.strip()
            for issue_id in value.issue_refs:
                _issue_text(cell.paragraphs[0], issues[issue_id], compact=issue_id in table_issue_ids)
                table_issue_ids.add(issue_id)
            fact_refs.extend(value.fact_refs)
    weights = spec.column_widths or ([1, 2.4] if len(spec.columns) == 2 else [1] * len(spec.columns))
    _format_table(table, weights)
    for merge in spec.merges:
        table.cell(merge.row + 1, merge.column).merge(table.cell(merge.row + merge.row_span, merge.column + merge.col_span - 1))
    return fact_refs


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
    _font(document.add_paragraph().add_run("目录"), name="黑体", size=16, bold=True)
    _field(document.add_paragraph(), 'TOC \\o "1-3" \\h \\z \\u', "请在 Word 或 WPS 中更新目录字段以显示实际页码。")
    document.add_page_break()
    issues = {issue.issue_id: issue for issue in package.issues}
    attachments = {item.attachment_id: item for item in package.attachments}
    for section_index, section in enumerate(package.sections):
        if section_index and any(isinstance(block, AttachmentBlock) for block in section.blocks):
            document.add_page_break()
        document.add_paragraph(section.title, "Heading 1")
        section_fact_refs = []
        section_source_refs = []
        section_requirement_refs = list(section.requirement_refs)
        for block in section.blocks:
            if isinstance(block, HeadingBlock):
                document.add_paragraph(block.text, f"Heading {block.level}")
            elif isinstance(block, ParagraphBlock):
                document.add_paragraph(block.text)
            elif isinstance(block, ListBlock):
                for index, item in enumerate(block.items, 1):
                    document.add_paragraph(f"{index}. {item}" if block.ordered else f"• {item}")
            elif isinstance(block, TableBlock):
                section_fact_refs.extend(_render_table(document, block.table, issues))
                section_source_refs.extend(block.table.source_refs)
                section_requirement_refs.extend(block.table.requirement_refs)
            elif isinstance(block, IssueBlock):
                paragraph = document.add_paragraph()
                issue = issues[block.issue_id]
                _issue_text(paragraph, issue)
                paragraph.add_run(f" {issue.action} 详见待补清单同编号。")
            elif isinstance(block, AttachmentBlock):
                attachment = attachments[block.attachment_id]
                document.add_paragraph(attachment.title, "Heading 2")
                document.add_paragraph(attachment.description)
                if attachment.synthetic:
                    _font(document.add_paragraph().add_run("以下图片为合成测试用途，不是实际公司证明或证照。"), color="A80000", bold=True)
                with Image.open(attachment.path) as image:
                    image_width, image_height = image.size
                scale = min(6.7 / image_width, 6.2 / image_height)
                paragraph = document.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = paragraph.add_run()
                picture = run.add_picture(str(attachment.path), width=Inches(image_width * scale), height=Inches(image_height * scale))
                picture._inline.docPr.set("descr", attachment.description)
                section_source_refs.extend(attachment.source_refs)
                section_requirement_refs.extend(attachment.requirement_refs)
            for issue_id in block.issue_refs:
                _issue_text(document.add_paragraph(), issues[issue_id])
            section_fact_refs.extend(block.fact_refs)
            section_source_refs.extend(block.source_refs)
            section_requirement_refs.extend(block.requirement_refs)
        _refs(document, fact_refs=section_fact_refs, source_refs=section_source_refs, requirement_refs=section_requirement_refs)
    document.add_page_break()
    document.add_paragraph("来源与事实追溯", "Heading 1")
    document.add_paragraph("下列编号对应冻结输入中的原件、定位和文件校验值。事实采用与技术拟实施方法分开记录。")
    def trace_paragraph(text, *, bold=False, keep_next=False, size=9.5):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(3)
        paragraph.paragraph_format.line_spacing = 1.1
        paragraph.paragraph_format.keep_with_next = keep_next
        _font(paragraph.add_run(text), size=size, bold=bold)
        return paragraph

    trace_paragraph("采用事实", bold=True, keep_next=True)
    for fact in package.facts:
        trace_paragraph(f"{fact.fact_id} {fact.label}：{fact.value}；来源 {'、'.join(fact.source_refs)}。")
    # The frozen manifest can contain multiple locators for identical bytes.
    # Print each original hash once, without losing any source ID or locator.
    source_groups = {}
    for source in package.sources:
        source_groups.setdefault(source.sha256, []).append(source)
    for group_index, (digest, sources) in enumerate(source_groups.items(), 1):
        trace_paragraph(f"原件组 {group_index} 文件 {sources[0].path.name}", bold=True, keep_next=True)
        trace_paragraph(f"SHA-256：{digest}", size=8, keep_next=True)
        for source in sources:
            trace_paragraph(f"{source.source_id} {source.title}；定位：{source.locator}；来源类型：{source.provenance}。")
    trace_paragraph("响应要求", bold=True, keep_next=True)
    for requirement in package.requirements:
        trace_paragraph(f"{requirement.requirement_id} {requirement.text} 来源 {'、'.join(requirement.source_refs)}。")
    return document


def _render_issues(package):
    document = _base_document(package, f"{package.project.name} 待补清单")
    titles = {section.section_id: section.title for section in package.sections}
    if not package.issues:
        document.add_paragraph("本次未自动检出待补项。仍须线下核对原件、报价和签字盖章。")
    for issue in package.issues:
        document.add_paragraph(f"{issue.issue_id} {issue.title}", "Heading 1")
        _issue_text(document.add_paragraph(), issue)
        document.add_paragraph(f"正文位置：{titles[issue.section_id]}（{issue.section_id}）。在投标文件中搜索同编号即可定位。")
        document.add_paragraph(f"对应要求：{'、'.join(issue.requirement_refs)}")
        document.add_paragraph(f"缺项原因：{issue.reason}")
        document.add_paragraph(f"来源要求：{issue.source_requirement}")
        document.add_paragraph(f"补充动作：{issue.action}")
    return document


def render_package(package: DocumentPackage, output_dir: Path) -> dict:
    """Return paths/hashes after complete assembly; never overwrite existing output.

    Pagination and TOC numbers are Word fields, not estimated display numbers.
    The caller must visually verify a target Word/WPS render before claiming
    document acceptance. M1 supports real PNG/JPEG attachment bytes only.
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
    result["warnings"] = ["目录与页码使用 Word 字段。请在 Word/WPS 中更新全部字段并人工检查排版；不提供估算页码。"]
    result["attachment_count"] = len(package.attachments)
    result["issue_ids"] = [issue.issue_id for issue in package.issues]
    return result
