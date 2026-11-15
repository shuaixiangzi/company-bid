from copy import deepcopy

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

from company_bid.models import ListBlock, ParagraphBlock, Section
from company_bid.rendering import _base_document, _render_section_content
from company_bid.sample import create_synthetic_sample


@pytest.mark.parametrize("text", [
    "拟按招标要求执行，具体承诺仍待公司确认。\n响应人：示例公司\n法定代表人：【待签字】\n日期：【待填写】",
    "第一行\n\n保留空行后的正文，不凭内容改变对齐或顺序。",
    "没有显式换行的正常拟实施段落仍使用两端对齐。",
])
def test_justified_soft_breaks_keep_exact_editable_text_and_order(tmp_path, text):
    prepared = create_synthetic_sample(tmp_path / "input")
    section = Section(section_id="S-SOFT", title="拟实施章节", requirement_refs=["R-SOFT"], blocks=[
        ParagraphBlock(text=text, content_role="proposal"),
        ListBlock(items=["后续步骤仍在原位置。"]),
    ])
    before = deepcopy(section.model_dump())
    document = _base_document(prepared, "换行兼容性验证")
    _render_section_content(document, prepared, section)
    output = tmp_path / "soft-breaks.docx"
    document.save(output)

    reopened = Document(output)
    assert [p.text for p in reopened.paragraphs] == [text, "（1）后续步骤仍在原位置。"]
    paragraph = reopened.paragraphs[0]
    assert len(paragraph._p.xpath(".//w:br")) == text.count("\n")
    assert paragraph.style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    assert section.model_dump() == before
    assert not paragraph._p.xpath(".//w:drawing")
    settings = reopened.settings.element
    flags = settings.xpath("./w:compat/w:doNotExpandShiftReturn")
    assert len(flags) == 1 and flags[0].get(qn("w:val")) == "true"
    # Preserve the stock compatibility profile and automatic field updates.
    assert settings.xpath("./w:compat/w:compatSetting")
    assert settings.xpath("./w:updateFields")
    assert not settings.xpath("./w:documentProtection")
