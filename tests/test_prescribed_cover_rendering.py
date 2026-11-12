"""Source-only prescribed covers replace the generic cover exactly once."""

import copy
import hashlib
from zipfile import ZipFile

import pytest
from docx import Document
from lxml import etree
from pydantic import ValidationError

from company_bid.models import DocumentPackage, PreparedBid, file_sha256
from company_bid.rendering import render_package


COVER = ["响应文件", "项目编号：", "响应人：（盖单位章）",
         "法定代表人或其委托代理人：（签字或盖章）", "年 月 日"]
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def prepared_data(tmp_path):
    original = tmp_path / "synthetic-tender.txt"
    original.write_text("\n".join([*COVER, "正文来源文字。"]), encoding="utf-8")
    source = {"source_id": "S-BASE", "title": "合成招标来源", "path": str(original),
              "sha256": file_sha256(original), "locator": "合成原件", "kind": "tender",
              "provenance": "synthetic", "synthetic": True}

    def scoped(identity, text):
        return {**source, "source_id": identity,
                "locator": f"page 1 line 1; quote_sha256={hashlib.sha256(text.encode('utf-8')).hexdigest()}"}

    full = "\n".join(COVER)
    sources = [source, scoped("S-COVER", full),
               *(scoped(f"S-LINE{i}", text) for i, text in enumerate(COVER[1:], 1))]
    requirements = [{"requirement_id": "R-COVER", "text": full, "category": "procedure",
                     "response_mode": "fixed_text", "source_refs": ["S-COVER"]},
                    {"requirement_id": "R-BODY", "text": "正文来源文字。", "category": "procedure",
                     "response_mode": "fixed_text", "source_refs": ["S-BASE"]}]
    cover = {"section_id": "COVER", "title": COVER[0], "kind": "facts",
             "requirement_refs": ["R-COVER"], "source_refs": ["S-COVER"],
             "fixed_content": [{"kind": "paragraph", "text": text, "source_refs": [f"S-LINE{i}"]}
                               for i, text in enumerate(COVER[1:], 1)]}
    body = {"section_id": "BODY", "title": "正文标题", "kind": "facts", "start_on_new_page": True,
            "requirement_refs": ["R-BODY"], "fixed_content": [
                {"kind": "paragraph", "text": "正文来源文字。", "source_refs": ["S-BASE"]}]}
    issues = [{"issue_id": f"M-{i:03d}", "title": f"封面字段{i}待补", "section_id": "COVER",
               "requirement_refs": ["R-COVER"], "reason": "保留原文空项或签章。", "action": "线下补齐并签章。",
               "source_requirement": full} for i in range(1, 5)]
    return {"synthetic": True, "project": {"project_id": "P", "name": "不应自动填入的项目名称", "source_refs": ["S-BASE"]},
            "company": {"company_id": "C", "name": "不应自动填入的公司", "source_refs": ["S-BASE"]},
            "sources": sources, "requirements": requirements, "issues": issues,
            "preparation_note": "合成封面合同测试。", "sections": [cover, body],
            "prescribed_cover_section_id": "COVER"}


def package_data(data):
    value = copy.deepcopy(data)
    value["title"] = "合成文件"
    value["sections"] = [
        {"section_id": plan["section_id"], "title": plan["title"], "requirement_refs": plan["requirement_refs"],
         "start_on_new_page": plan.get("start_on_new_page", False),
         "blocks": [{"kind": "paragraph", "text": item["text"], "content_role": "neutral",
                     "source_refs": item["source_refs"], "requirement_refs": plan["requirement_refs"]}
                    for item in plan["fixed_content"]] + [
                        {"kind": "issue", "issue_id": issue["issue_id"], "requirement_refs": issue["requirement_refs"]}
                        for issue in value["issues"] if issue["section_id"] == plan["section_id"]]}
        for plan in value["sections"]]
    return value


def test_default_none_accepts_legacy_prepared_and_package(tmp_path):
    data = prepared_data(tmp_path)
    data.pop("prescribed_cover_section_id")
    prepared = PreparedBid.model_validate(data)
    package = DocumentPackage.model_validate(package_data(data))
    assert prepared.prescribed_cover_section_id is package.prescribed_cover_section_id is None
    output = render_package(package, tmp_path / "legacy-output")
    text = "\n".join(paragraph.text for paragraph in Document(output["bid_docx"]["path"]).paragraphs)
    assert "投 标 文 件" in text
    assert f"项目名称：{package.project.name}" in text
    assert f"投标人：{package.company.name}" in text
    assert "响应文件" in text


def test_complete_cover_and_distinct_full_and_paragraph_scopes_are_preserved(tmp_path):
    data = prepared_data(tmp_path)
    prepared = PreparedBid.model_validate(data)
    package = DocumentPackage.model_validate(package_data(data))
    assert prepared.sections[0].source_refs == ["S-COVER"]
    assert prepared.sections[0].fixed_content[0].source_refs == ["S-LINE1"]
    assert package.sections[0].blocks[0].source_refs == ["S-LINE1"]
    assert package.sections[0].blocks[0].requirement_refs == ["R-COVER"]
    assert [issue.issue_id for issue in package.issues] == [f"M-{i:03d}" for i in range(1, 5)]


@pytest.mark.parametrize("foreign", [False, True])
def test_duplicate_full_cover_requirements_need_one_original_identity(tmp_path, foreign):
    data = prepared_data(tmp_path)
    duplicate = copy.deepcopy(data["requirements"][0])
    duplicate["requirement_id"] = "R-COVER-OBSERVATION"
    source = copy.deepcopy(data["sources"][1])
    source["source_id"] = "S-COVER-OBSERVATION"
    if foreign:
        source["path"] += ".different-original"
    data["sources"].append(source)
    duplicate["source_refs"] = [source["source_id"]]
    data["requirements"].append(duplicate)
    data["sections"][0]["requirement_refs"].append(duplicate["requirement_id"])
    if foreign:
        with pytest.raises(ValidationError, match="one original source identity"):
            PreparedBid.model_validate(data)
        with pytest.raises(ValidationError, match="one original source identity"):
            DocumentPackage.model_validate(package_data(data))
    else:
        assert PreparedBid.model_validate(data).prescribed_cover_section_id == "COVER"
        assert DocumentPackage.model_validate(package_data(data)).prescribed_cover_section_id == "COVER"


@pytest.mark.parametrize("model,make", [(PreparedBid, lambda value: value), (DocumentPackage, package_data)])
@pytest.mark.parametrize("change", ["unknown", "duplicate", "wrong_title", "changed_text", "swapped",
                                    "dropped", "missing_scope", "wrong_scope", "wrong_original",
                                    "wrong_kind", "wrong_provenance", "ambiguous_scope", "malformed_scope", "wrong_issue"])
def test_cover_source_contract_rejects_missing_or_changed_original(tmp_path, model, make, change):
    data = prepared_data(tmp_path)
    if change == "unknown":
        data["prescribed_cover_section_id"] = "ABSENT"
    elif change == "duplicate":
        data["sections"].append(copy.deepcopy(data["sections"][0]))
    elif change == "wrong_title":
        data["sections"][0]["title"] = "擅自替换标题"
    elif change == "changed_text":
        data["sections"][0]["fixed_content"][0]["text"] = "项目编号：猜填-123"
    elif change == "swapped":
        data["sections"][0]["fixed_content"][:2] = reversed(data["sections"][0]["fixed_content"][:2])
    elif change == "dropped":
        data["sections"][0]["fixed_content"].pop()
    elif change == "missing_scope":
        data["sources"][1]["locator"] = "page 1 line 1"
    elif change == "wrong_scope":
        data["sources"][2]["locator"] = data["sources"][3]["locator"]
    elif change == "wrong_original":
        data["sources"][2]["sha256"] = "0" * 64
    elif change == "wrong_kind":
        data["sources"][2]["kind"] = "company_material"
    elif change == "wrong_provenance":
        data["sources"][2]["provenance"] = "derived_from_bid"
    elif change == "ambiguous_scope":
        data["sources"][1]["locator"] += "; " + data["sources"][1]["locator"]
    elif change == "malformed_scope":
        data["sources"][1]["locator"] += "; quote_sha256=invalid"
    elif change == "wrong_issue":
        data["issues"][0]["source_requirement"] = "别处来源或已填值"
    with pytest.raises(ValidationError):
        model.model_validate(make(data))


@pytest.mark.parametrize("change", ["hidden", "facts", "writing", "no_sources", "no_paragraphs", "table", "attachment"])
def test_prepared_cover_rejects_other_content(tmp_path, change):
    data = prepared_data(tmp_path)
    cover = data["sections"][0]
    if change == "hidden":
        cover.update(render_body=False, fixed_content=[])
    elif change == "facts":
        data["facts"] = [{"fact_id": "F", "label": "真实值", "value": "合成值", "source_refs": ["S-BASE"]}]
        cover["fact_refs"] = ["F"]
    elif change == "writing":
        cover.update(kind="technical", writing_tasks=[{"unit_id": "T", "title": "拟实施", "objective": "拟实施方法",
            "must_cover": ["合成方法"], "requirement_refs": ["R-COVER"], "forbidden_fields": ["事实"],
            "completion_criteria": ["完整"], "operation": "bounded_new_draft"}])
    elif change == "no_sources":
        cover["source_refs"] = []
    elif change == "no_paragraphs":
        cover["fixed_content"] = []
    elif change == "table":
        cover["table_refs"] = ["T"]
        data["tables"] = [{"table_id": "T", "section_id": "COVER", "title": "另加表格", "columns": ["列"],
                           "rows": [[{"text": "格"}]], "source_refs": ["S-COVER"], "requirement_refs": ["R-COVER"]}]
    elif change == "attachment":
        cover["attachment_refs"] = ["A"]
        data["attachments"] = [{"attachment_id": "A", "section_id": "COVER", "title": "另加附件",
            "path": data["sources"][0]["path"], "sha256": data["sources"][0]["sha256"], "source_refs": ["S-BASE"],
            "requirement_refs": ["R-COVER"], "description": "不应成为封面附件。", "synthetic": True}]
    with pytest.raises(ValidationError):
        PreparedBid.model_validate(data)


@pytest.mark.parametrize("change", ["hidden", "proposal", "fact", "fact_refs", "heading", "list", "table", "attachment",
                                    "no_sources", "no_requirements", "foreign_requirements", "foreign_issue"])
def test_package_cover_rejects_non_original_blocks_and_lost_references(tmp_path, change):
    data = package_data(prepared_data(tmp_path))
    cover = data["sections"][0]
    block = cover["blocks"][0]
    if change == "hidden":
        cover.update(render_body=False, blocks=[])
    elif change in {"proposal", "fact"}:
        block["content_role"] = change
    elif change == "fact_refs":
        data["facts"] = [{"fact_id": "F", "label": "真实值", "value": "合成值", "source_refs": ["S-BASE"]}]
        block["fact_refs"] = ["F"]
    elif change == "heading":
        cover["blocks"].append({"kind": "heading", "text": "另加标题"})
    elif change == "list":
        cover["blocks"].append({"kind": "list", "items": ["另加条目"]})
    elif change == "table":
        cover["blocks"].append({"kind": "table", "table": {"table_id": "T", "section_id": "COVER", "title": "另加表格",
            "columns": ["列"], "rows": [[{"text": "格"}]], "source_refs": ["S-COVER"], "requirement_refs": ["R-COVER"]}})
    elif change == "attachment":
        data["attachments"] = [{"attachment_id": "A", "section_id": "COVER", "title": "另加附件",
            "path": data["sources"][0]["path"], "sha256": data["sources"][0]["sha256"], "source_refs": ["S-BASE"],
            "requirement_refs": ["R-COVER"], "description": "不应成为封面附件。", "synthetic": True}]
        cover["blocks"].append({"kind": "attachment", "attachment_id": "A"})
    elif change == "no_sources":
        block["source_refs"] = []
    elif change == "no_requirements":
        block["requirement_refs"] = []
    elif change == "foreign_requirements":
        block["requirement_refs"] = ["R-BODY"]
    elif change == "foreign_issue":
        data["issues"][0].update(section_id="BODY", requirement_refs=["R-BODY"], source_requirement="正文来源文字。")
        data["sections"][1]["blocks"].append(copy.deepcopy(cover["blocks"][4]))
        data["sections"][1]["blocks"][-1]["requirement_refs"] = ["R-BODY"]
    with pytest.raises(ValidationError):
        DocumentPackage.model_validate(data)


def test_prescribed_cover_first_once_original_order_and_same_id_checklist(tmp_path):
    data = package_data(prepared_data(tmp_path))
    # The designated section is deliberately not the first planned section.
    data["sections"].reverse()
    package = DocumentPackage.model_validate(data)
    frozen = package.model_dump(mode="json")
    output = render_package(package, tmp_path / "prescribed-output")
    bid = Document(output["bid_docx"]["path"])
    lines = [paragraph.text for paragraph in bid.paragraphs]
    assert lines[:5] == COVER
    assert all(lines.count(text) == 1 for text in COVER)
    assert lines.index("目 录") > lines.index(COVER[-1])
    assert lines.index("正文标题") > lines.index("目 录")
    assert "投 标 文 件" not in lines
    assert not any(package.project.name in text or package.company.name in text for text in lines)
    assert [paragraph.text for paragraph in bid.paragraphs if paragraph.style.name.startswith("Heading")] == ["正文标题"]
    assert len(bid.sections) == 3
    assert all(abs(section.page_width.cm - 21) < .01 and abs(section.page_height.cm - 29.7) < .01 for section in bid.sections)
    assert bid.paragraphs[0].style.name == "Title"
    assert bid.paragraphs[0].runs[0].font.size.pt == package.style.cover_title_pt
    assert bid.paragraphs[1].runs[0].font.size.pt == package.style.cover_meta_pt
    text = "\n".join(lines)
    issues_text = "\n".join(paragraph.text for paragraph in Document(output["issues_docx"]["path"]).paragraphs)
    for issue in package.issues:
        assert text.count(f"【待补 {issue.issue_id}】") == 1
        assert issue.marker in issues_text
        assert issue.action in issues_text
    assert "详见待补清单同编号。" in text
    assert package.model_dump(mode="json") == frozen
    with ZipFile(output["bid_docx"]["path"]) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
        assert not root.xpath("//w:br[@w:type='page']", namespaces=NS)  # no blank page before the first body section
        assert len(root.xpath("//w:sectPr", namespaces=NS)) == 3
        assert 'TOC \\o "1-3"' in archive.read("word/document.xml").decode()


def test_body_section_order_and_page_breaks_remain_after_cover_skip(tmp_path):
    data = package_data(prepared_data(tmp_path))
    extra = copy.deepcopy(data["sections"][1])
    extra.update(section_id="BODY2", title="第二正文标题")
    data["sections"].append(extra)
    output = render_package(DocumentPackage.model_validate(data), tmp_path / "ordered-output")
    bid = Document(output["bid_docx"]["path"])
    assert [p.text for p in bid.paragraphs if p.style.name.startswith("Heading")] == ["正文标题", "第二正文标题"]
    with ZipFile(output["bid_docx"]["path"]) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
        assert len(root.xpath("//w:br[@w:type='page']", namespaces=NS)) == 1
