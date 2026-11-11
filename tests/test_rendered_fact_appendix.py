"""The Word appendix reports adopted facts, not every frozen library value."""
from docx import Document

from company_bid.models import DocumentPackage, file_sha256
from company_bid.rendering import render_package


def test_body_and_table_facts_remain_in_appendix_but_unused_library_values_do_not(tmp_path):
    body_value = "合成正文事实值-ALPHA-17"
    table_value = "合成表格事实值-BETA-23"
    unused_value = "合成未采用库值-GAMMA-41"
    source = tmp_path / "synthetic-facts.txt"
    source.write_text("\n".join((body_value, table_value, unused_value)), encoding="utf8")
    package = DocumentPackage.model_validate({
        "project": {"project_id": "P", "name": "合成附录验证", "source_refs": ["S"]},
        "company": {"company_id": "C", "name": "合成测试公司", "source_refs": ["S"]},
        "title": "合成来源事实附录验证", "preparation_note": "仅供本地合成验证，不可投标。", "synthetic": True,
        "sources": [{"source_id": "S", "title": "合成事实来源", "path": str(source), "sha256": file_sha256(source),
                     "locator": "document", "kind": "manual_input", "provenance": "manual_input", "synthetic": True}],
        "facts": [{"fact_id": "F-BODY", "label": "正文事实字段", "value": body_value, "source_refs": ["S"]},
                  {"fact_id": "F-TABLE", "label": "表格事实字段", "value": table_value, "source_refs": ["S"]},
                  {"fact_id": "F-UNUSED", "label": "未采用字段", "value": unused_value, "source_refs": ["S"]}],
        "requirements": [{"requirement_id": "R-BODY", "text": "合成正文事实响应要求。", "category": "qualification",
                          "response_mode": "factual_response", "source_refs": ["S"]},
                         {"requirement_id": "R-TABLE", "text": "合成表格事实响应要求。", "category": "fixed_table",
                          "response_mode": "table", "source_refs": ["S"]}],
        "sections": [{"section_id": "SEC-BODY", "title": "正文事实", "requirement_refs": ["R-BODY"], "blocks": [
            {"kind": "paragraph", "text": "正文事实字段：" + body_value, "content_role": "fact", "fact_refs": ["F-BODY"],
             "source_refs": ["S"], "requirement_refs": ["R-BODY"]}]},
            {"section_id": "SEC-TABLE", "title": "表格事实", "requirement_refs": ["R-TABLE"], "blocks": [
                {"kind": "table", "requirement_refs": ["R-TABLE"], "table": {
                    "table_id": "T", "section_id": "SEC-TABLE", "title": "合成事实表", "columns": ["字段", "值"],
                    "rows": [[{"text": "表格事实字段"}, {"text": table_value, "fact_refs": ["F-TABLE"]}]],
                    "source_refs": ["S"], "requirement_refs": ["R-TABLE"]}}]}],
    })
    assert any(fact.fact_id == "F-UNUSED" for fact in package.facts)
    rendered = render_package(package, tmp_path / "output")
    bid = Document(rendered["bid_docx"]["path"])
    body = "\n".join([*(paragraph.text for paragraph in bid.paragraphs),
                       *(cell.text for table in bid.tables for row in table.rows for cell in row.cells)])
    assert body_value in body and table_value in body and unused_value not in body
    checklist = Document(rendered["issues_docx"]["path"])
    appendix = "\n".join(paragraph.text for paragraph in checklist.paragraphs)
    assert "采用事实" in appendix and body_value in appendix and table_value in appendix
    assert "F-BODY" in appendix and "F-TABLE" in appendix
    assert unused_value not in appendix and "F-UNUSED" not in appendix
