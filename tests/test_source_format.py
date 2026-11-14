"""Source-native geometry and bounded identity fills through the same pipeline."""
from copy import deepcopy
import hashlib

from docx import Document
from docx.oxml.ns import qn
from PIL import Image
import pdfplumber
from pydantic import ValidationError
import pytest

from company_bid.models import PreparedBid, SourceRef
from company_bid.extraction import _citation
from company_bid.pipeline import build_executor
from company_bid.reading import SourceDocument, ReadingConfig, _read_pdf_page
from company_bid.runtime import Executor, NodeSpec
from company_bid.source_format import _fill_line, _recover_one, _can_join, _centered, _cell_underlines, _cell_insets
from company_bid.source_format_contracts import NativeFormatLayout, NativeSlotBinding, NativeUnderline, format_digest
from test_native_layout import native_pdf, sha


class Context:
    def __init__(self):
        self.units, self.captures = {}, {}
    def run_unit(self, uid, payload, fn):
        self.units[uid] = payload
        return fn()
    def capture(self, name, payload):
        self.captures[name] = payload
        return {"status": "captured"}


def source_format_fixture(tmp_path):
    path = native_pdf(tmp_path)
    source = {"source_id": "T", "path": str(path), "sha256": sha(path), "kind": "tender",
        "title": "Synthetic source", "locator": "original", "provenance": "synthetic", "synthetic": True}
    doc = SourceDocument(source_id="T", path=path, sha256=sha(path), role="tender", title="Synthetic")
    with pdfplumber.open(path) as pdf:
        unit = _read_pdf_page(doc, pdf.pages[0], 1, ReadingConfig(), None)
    original = sorted([b.model_dump(mode="json") for b in unit.blocks if not b.text.strip().startswith("- 1 -")],
                      key=lambda b: b["bbox"][1])
    for block in original:
        block["page"] = 1  # read_documents supplies this enclosing-page projection.
        block["unit_id"] = unit.unit_id
        if block["kind"] == "table":
            block["text"] = "\n".join("\t".join(row) for row in block["rows"])
    blocks = {b["block_id"]: b for b in original}
    sources = {"T": source}
    def scoped(citation):
        # Match production singleton behavior: the blocks suffix exists only
        # when the caller explicitly asks to retain block membership.
        checked = _citation({**citation, "source_id": "T"}, blocks)
        identity = "Q-" + format_digest(checked)[:12]
        sources[identity] = {**source, "source_id": identity,
            "locator": citation["locator"] + "; quote_sha256=" + hashlib.sha256(citation["quote"].encode()).hexdigest() +
                       ("; blocks=" + ",".join(citation["block_ids"]) if citation.get("block_ids") else "")}
        return identity
    section = {"section_id": "FORM", "title": original[0]["text"], "kind": "facts", "requirement_refs": ["R"],
        "table_refs": ["TABLE"], "fixed_content": [], "source_refs": ["T"]}
    tables = []
    for block in original[1:]:
        ref = scoped({"quote": block["text"], "locator": block["locator"], "block_id": block["block_id"]})
        if block["kind"] == "paragraph":
            section["fixed_content"].append({"kind": "paragraph", "text": block["text"], "source_refs": [ref]})
        else:
            tables.append({"table_id": "TABLE", "section_id": "FORM", "title": "Source table",
                "columns": ["A", "B"], "rows": [[{"text": x, "fact_refs": [], "issue_refs": []} for x in row] for row in block["rows"]],
                "column_widths": block["column_widths"], "merges": block["merges"], "header_from_columns": False,
                "repeat_header_rows": 0, "source_refs": [ref], "requirement_refs": ["R"]})
            section["fixed_content"].append({"kind": "table_ref", "table_ref": "TABLE", "source_refs": [ref]})
    chapter = {"title": original[0]["text"], "content_block_ids": list(blocks)}
    manifest = {"company": {"name": "Synthetic company"}, "project": {"name": "Synthetic project"}}
    ctx = Context()
    spec, reason = _recover_one(ctx, section, chapter, blocks, source, tables, [], manifest, scoped)
    assert spec is not None and reason is None and len(ctx.units) == 1, reason
    prepared = {"synthetic": True, "preparation_note": "Synthetic editable native format test.",
        "project": {"project_id": "P", "name": "Synthetic project", "source_refs": ["T"]},
        "company": {"company_id": "C", "name": "Synthetic company", "source_refs": ["T"]},
        "sources": list(sources.values()), "requirements": [{"requirement_id": "R", "text": "Preserve this exact source form.",
            "category": "fixed_table", "response_mode": "table", "source_refs": ["T"]}],
        "sections": [section], "tables": tables, "facts": [], "attachments": [], "issues": []}
    return PreparedBid.model_validate(prepared).model_dump(mode="json"), ctx


def test_source_native_layout_is_carried_to_editable_docx_without_house_width_replacement(tmp_path):
    data, ctx = source_format_fixture(tmp_path)
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}})
    run = executor.create_run({"prepared": data})
    done = executor.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    result = executor.output(run)
    doc = Document(executor.store.root / result["files"]["bid_docx"]["path"])
    table = doc.tables[0]
    assert sum(int(x.get(qn("w:w"))) for x in table._tbl.tblGrid) == 320 * 20
    assert int(table._tbl.tblPr.find(qn("w:tblInd")).get(qn("w:w"))) == 40 * 20
    assert [row.height.pt for row in table.rows] == [50, 100, 100]
    assert all(row.height_rule.name == "AT_LEAST" for row in table.rows)
    assert table.cell(0, 1)._tc is table.cell(1, 1)._tc
    assert table.cell(0, 0).paragraphs[0].runs[0].font.size.pt == 12
    assert all(node.get(qn("w:w")) == "0" for node in table._tbl.tblPr.find(qn("w:tblCellMar")))
    assert table.cell(0,0).paragraphs[0].paragraph_format.left_indent.pt == 10.
    assert data["sections"][0]["native_format"] == executor.output(run, "N4")["package"]["sections"][0]["native_format"]
    assert "n3-native-source-layout" in ctx.captures


def test_native_form_keeps_its_table_and_attached_proof_after_the_form(tmp_path):
    data, _ = source_format_fixture(tmp_path)
    image = tmp_path / "attached-proof.png"
    Image.new("RGB", (80, 100), "orange").save(image)
    data["sources"].append({**data["sources"][0], "source_id": "PROOF", "path": str(image), "sha256": sha(image),
                            "kind": "company_material", "locator": "Synthetic proof"})
    data["attachments"] = [{"attachment_id": "A", "section_id": "FORM", "title": "Attached source proof",
        "path": str(image), "sha256": sha(image), "source_refs": ["PROOF"], "requirement_refs": ["R"],
        "description": "Synthetic proof, no real qualification.", "synthetic": True}]
    data["sections"][0]["attachment_refs"] = ["A"]
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}})
    run = executor.create_run({"prepared": data})
    done = executor.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    doc = Document(executor.store.root / executor.output(run)["files"]["bid_docx"]["path"])
    assert len(doc.tables) == len(doc.inline_shapes) == 1
    assert "Attached source proof" in [p.text for p in doc.paragraphs]
    body = list(doc._element.body)
    assert next(i for i,e in enumerate(body) if e.tag == qn("w:tbl")) < next(i for i,e in enumerate(body) if e.xpath('.//w:drawing'))


def test_native_form_preserves_additional_fact_and_non_native_table(tmp_path):
    data, _ = source_format_fixture(tmp_path)
    data["facts"] = [{"fact_id": "F", "label": "Explicit extra fact", "value": "Confirmed synthetic value", "source_refs": ["T"]}]
    data["sections"][0]["fact_refs"] = ["F"]
    extra = deepcopy(data["tables"][0])
    extra.update(table_id="EXTRA", title="Additional source table")
    data["tables"].append(extra)
    data["sections"][0]["table_refs"].append("EXTRA")
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}})
    run = executor.create_run({"prepared": data})
    done = executor.execute(run)
    assert done["status"] == "succeeded", done.get("error_code")
    doc = Document(executor.store.root / executor.output(run)["files"]["bid_docx"]["path"])
    assert len(doc.tables) == 2
    assert "Explicit extra fact：Confirmed synthetic value" in [p.text for p in doc.paragraphs]
    body = list(doc._element.body)
    table_positions = [i for i,e in enumerate(body) if e.tag == qn("w:tbl")]
    fact_position = next(i for i,e in enumerate(body) if "Confirmed synthetic value" in ''.join(e.itertext()))
    assert table_positions[0] < fact_position < table_positions[1]


@pytest.mark.parametrize("kind", ["partial_page", "multiple_pages", "changed_text"])
def test_recovery_does_not_silently_invent_partial_cross_page_or_changed_source(tmp_path, kind):
    data, ctx = source_format_fixture(tmp_path)
    blocks = deepcopy(next(iter(ctx.units.values()))["frozen_blocks"])
    if kind == "partial_page":blocks = blocks[:-1]
    elif kind == "multiple_pages":blocks[-1]["page"] = 2
    else:blocks[1]["text"] += "Altered source"
    section = deepcopy(data["sections"][0]);chapter = {"title": section["title"], "content_block_ids": [b["block_id"] for b in blocks]}
    source = next(s for s in data["sources"] if s["source_id"] == "T")
    def recover():return _recover_one(Context(), section, chapter, {b['block_id']:b for b in blocks}, source,
        data["tables"], [], {"company":{"name":"Synthetic company"},"project":{"name":"Synthetic project"}}, lambda _: "unused")
    if kind == "changed_text":
        with pytest.raises(ValueError, match="FROZEN_BLOCK_MISMATCH"):recover()
    else:
        spec, reason = recover()
        assert spec is None and reason == ("unsupported_partial_native_page" if kind == "partial_page" else "unsupported_multiple_physical_pages")


@pytest.mark.parametrize("mutation", ["drop_native", "reseal_geometry"])
def test_n5_rejects_native_geometry_removed_or_resealed_after_frozen_plan(tmp_path, mutation):
    data, _ = source_format_fixture(tmp_path)
    config = {"provider": {"mode": "prepared"}}
    original = build_executor(tmp_path / "runtime", config)
    nodes = []
    for node in original.nodes:
        if node.key != "N4":
            nodes.append(node);continue
        def changed(ctx, value, handler=node.handler):
            result = handler(ctx, value)
            section = result["package"]["sections"][0]
            if mutation == "drop_native":
                section["native_format"] = None
            else:
                native = section["native_format"]
                table = next(e for e in native["elements"] if e["kind"] == "table")
                table["row_heights_pt"][0] += 10
                table["bbox"][3] += 10
                native["proof_sha256"] = format_digest({k:v for k,v in native.items() if k != "proof_sha256"})
            return result
        nodes.append(NodeSpec("N4", "test-native-tamper-v1", changed, node.config_keys))
    executor = Executor(original.store.root, nodes, config)
    run = executor.create_run({"prepared": data})
    done = executor.execute(run)
    assert done["status"] == "failed" and done["error_code"] == "NATIVE_FORMAT_PLAN_MISMATCH"


@pytest.mark.parametrize("mutation", ["extra_output_row", "source_column_count", "row_height_count", "missing_cell", "protected_label", "wrong_locator"])
def test_resealed_native_grid_tampering_is_rejected_before_zip_can_hide_it(tmp_path, mutation):
    data, _ = source_format_fixture(tmp_path)
    native = data["sections"][0]["native_format"]
    table = data["tables"][0]
    geometry = next(e for e in native["elements"] if e["kind"] == "table")
    if mutation == "extra_output_row":table["rows"].append(deepcopy(table["rows"][-1]))
    elif mutation == "source_column_count":geometry["source_rows"][0].append("")
    elif mutation == "row_height_count":geometry["row_heights_pt"].append(1.)
    elif mutation == "missing_cell":geometry["cells"].pop()
    elif mutation == "protected_label":table["rows"][0][0]["text"] = "Changed printed label"
    else:geometry["source_locator"] = "pdf/page[2]/table[0]"
    geometry["output_grid_sha256"] = format_digest(table["rows"])
    native["proof_sha256"] = format_digest({k:v for k,v in native.items() if k != "proof_sha256"})
    with pytest.raises(ValidationError):PreparedBid.model_validate(data)


def fill_case(text, *, company="A company", project="A project", rules=()):
    item = {"text": text, "source_refs": ["T"]}
    facts = [{"fact_id": "C", "label": "公司名称", "value": company, "source_refs": ["C-SOURCE"]},
             {"fact_id": "P", "label": "项目名称", "value": project, "source_refs": ["P-SOURCE"]},
             {"fact_id": "PERSON", "label": "法定代表人姓名", "value": "Never select this person", "source_refs": ["C-SOURCE"]}]
    manifest = {"company": {"name": company}, "project": {"name": project}}
    bindings = _fill_line(item, text, "BLOCK", 0, facts, manifest, source_rules=rules)
    return item, bindings


@pytest.mark.parametrize("kind", ["native_vector_rule", "native_underscore_glyphs"])
def test_known_project_colon_slot_requires_its_exact_measured_rule_and_preserves_unknown_number(kind):
    original="项目名称： 项目编号：____" if kind=="native_vector_rule" else "项目名称：________ 项目编号：____"
    end=6 if kind=="native_vector_rule" else 13
    rule={"fixed_index":0,"start":5,"end":end,"width_pt":96.,"bbox":[150.,100.,246.,100. if kind=="native_vector_rule" else 112.],
          "locator":"pdf/page[1]/rule[0]" if kind=="native_vector_rule" else "pdf/page[1]/line[0]; chars[5:13]","basis":kind}
    unchanged,none=fill_case(original)
    assert unchanged["text"]==original and not none
    item,bindings=fill_case(original,rules=[rule])
    assert item["text"].startswith("项目名称：A project") and item["text"].endswith("项目编号：____")
    assert len(bindings)==1 and bindings[0]["source_rule"]==rule
    assert bindings[0]["start"]==5 and bindings[0]["end"]==end
    NativeSlotBinding.model_validate(bindings[0])


def test_company_declaration_placeholder_requires_measured_rule_and_keeps_its_printed_label():
    text="（响应人） 已认真阅读了询比文件。"
    rule={"fixed_index":0,"start":0,"end":6,"width_pt":90.,"bbox":[80.,100.,170.,100.],
          "locator":"pdf/page[1]/rule[1]","basis":"native_vector_rule"}
    item,bindings=fill_case(text,rules=[rule])
    assert item["text"]=="A company"+text and len(bindings)==1
    assert bindings[0]["start"]==bindings[0]["end"]==0 and bindings[0]["source_rule"]==rule
    assert fill_case(text)[1]==[]
    for existing in ["（响应人） Existing company 已认真阅读了文件。", "说明：（响应人） 已认真阅读。", "（响应人） 无需填写公司名称。"]:
        unchanged,none=fill_case(existing,rules=[rule])
        assert unchanged["text"]==existing and not none


@pytest.mark.parametrize("text", ["项目名称：Existing project 项目编号：____", "项目编号：____", "法定代表人：____", "日期：____", "注：不得填写项目名称：____"])
def test_measured_line_does_not_authorize_overwriting_existing_values_unknown_fields_or_instructions(text):
    rule={"fixed_index":0,"start":5,"end":9,"width_pt":96.,"bbox":[150.,100.,246.,100.],
          "locator":"pdf/page[1]/rule[0]","basis":"native_vector_rule"}
    item,bindings=fill_case(text,rules=[rule])
    assert item["text"]==text and not bindings


def test_explicit_company_project_slots_only_preserve_printed_words_and_unknown_people():
    text = "本人 （姓名）系 （响应人名称）的法定代表人，现委托 （姓名）办理（项目名称）。"
    item, bindings = fill_case(text)
    assert "A company（响应人名称）" in item["text"] and "A project（项目名称）" in item["text"]
    assert item["text"].count("（姓名）") == 2 and "Never select" not in item["text"]
    assert set(item["fact_refs"]) == {"C", "P"}
    assert all(NativeSlotBinding.model_validate(b) for b in bindings)
    unknown, empty = fill_case("法定代表人： （签字或盖章）；委托代理人： （签字）；委托日期： 年 月 日")
    assert not empty and unknown["text"].endswith("年 月 日")


@pytest.mark.parametrize("text", ["注：不得填写（项目名称），保持原字。", "说明：（项目名称）仅是引用。", "禁止填写 （响应人名称）。"])
def test_instruction_or_negative_placeholder_is_not_fill_authorization(text):
    item, bindings = fill_case(text)
    assert not bindings and item["text"] == text


def test_symmetric_long_body_lines_are_not_centered_titles_and_can_join():
    def line(text, x0, x1, top, bold=False):
        return {"text":text,"bbox":[x0,top,x1,top+12.],"runs":[{"font_family":"SimSun","font_size_pt":12.,"bold":bold}],
                "alignment_evidence":{"horizontal":"center","container_bbox":[0.,0.,600.,800.]}}
    a=line("An incomplete source sentence continues",60.,540.,100.)
    b=line("with its remaining protected original text.",60.,540.,123.)
    assert not _centered(a) and _can_join(a,b)
    assert _centered(line("Native title",240.,360.,50.,True))


@pytest.mark.parametrize("text", ["法定代表人或其授权委托人： （签字）", "项目负责人： （签字）", "日期： 年 月 日", "说明："])
def test_signatures_dates_and_explanation_labels_remain_separate_source_paragraphs(text):
    def line(text, top):
        return {"text":text,"bbox":[60.,top,540.,top+12.],"runs":[{"font_family":"SimSun","font_size_pt":12.,"bold":False}],
                "alignment_evidence":{"horizontal":"unresolved","container_bbox":[0.,0.,600.,800.]}}
    assert not _can_join(line("响应人： （全称、盖章）",100.), line(text,123.))
    assert not _can_join(line(text,100.), line("A following body line",123.))


def cell_rule_case():
    chars = [{"text":c,"bbox":[x,y,x+6.,y+12.]} for c,x,y in [("A",10.,10.),("B",50.,10.),("C",10.,40.),("D",50.,40.)]]
    cell = {"bbox":[0.,0.,100.,80.],"text":"A B\nC D","runs":[{"chars":chars}]}
    rules = [{"kind":"fillable_rule","x0":20.,"x1":45.,"top":y,"length_pt":25.,"locator":f"pdf/page[1]/rule[{i}]"}
             for i,y in enumerate([24.,54.])]
    return cell, rules


def test_cell_vector_rules_map_to_exact_normalized_source_blanks_across_lines():
    cell, rules = cell_rule_case()
    found = _cell_underlines(cell,rules)
    assert [(r["start"],r["end"],r["width_pt"]) for r in found] == [(1,2,25.),(5,6,25.)]
    assert all(cell["text"][r["start"]:r["end"]] == " " for r in found)
    cell["text"] = "A XX B\nC D"
    with pytest.raises(ValueError,match="CHARACTER_ALIGNMENT"):_cell_underlines(cell,rules)


def test_centered_and_empty_native_cells_do_not_double_count_vertical_whitespace():
    evidence={"horizontal":"center","vertical":"center","left_gap_pt":30.,"right_gap_pt":30.,"top_gap_pt":100.,"bottom_gap_pt":100.}
    centered = _cell_insets({"runs":[{}],"alignment_evidence":evidence},4.)
    assert centered["top_inset_pt"] == centered["bottom_inset_pt"] == 0.
    assert centered["left_inset_pt"] == centered["right_inset_pt"] == 0.
    empty = _cell_insets({"runs":[],"alignment_evidence":{}},4.)
    assert empty["inset_basis"] == "same_table_minimum_horizontal_inset"
    assert empty["top_inset_pt"] == empty["bottom_inset_pt"] == 0.


@pytest.mark.parametrize("mutation", ["past_source_end", "protected_text", "wrong_page", "too_wide", "oversized_inset"])
def test_resealed_cell_rule_or_inset_tampering_cannot_claim_a_different_source_slot(tmp_path, mutation):
    data,_=source_format_fixture(tmp_path)
    native=data["sections"][0]["native_format"]
    table=next(e for e in native["elements"] if e["kind"]=="table")
    cell=next(c for c in table["cells"] if c["row"]==c["column"]==0)
    rule={"start":8,"end":9,"width_pt":20.,"locator":"pdf/page[1]/rule[1]"}
    if mutation=="past_source_end":rule["end"]=999
    elif mutation=="protected_text":rule["start"]=0
    elif mutation=="wrong_page":rule["locator"]="pdf/page[2]/rule[1]"
    elif mutation=="too_wide":rule["width_pt"]=999.
    else:cell["left_inset_pt"]=999.
    cell["underlines"]=[rule]
    native["proof_sha256"]=format_digest({k:v for k,v in native.items() if k!="proof_sha256"})
    with pytest.raises(ValidationError):PreparedBid.model_validate(data)


def test_long_explicit_fact_is_not_truncated_or_shrunk_and_protected_change_is_rejected():
    value = "长名称" * 120
    item, bindings = fill_case("响应人： （盖单位章）", company=value)
    assert item["text"] == "响应人：" + value + "（盖单位章）"
    bad = deepcopy(bindings[0]);bad["after_text"] += "extra duty"
    with pytest.raises(ValidationError):NativeSlotBinding.model_validate(bad)


def test_long_native_underlined_fill_remains_complete_editable_text_without_shrink():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph
    text = "响应人： （盖单位章）"
    value = "长公司名称" * 80
    item, bindings = fill_case(text, company=value)
    document = Document()
    element = SimpleNamespace(left_pt=56.7, right_pt=56.7, first_line_indent_pt=0., line_spacing_pt=23.4,
        alignment="left", font_family="SimSun", font_size_pt=12., bold=False, fixed_indices=[0],
        underlines=[SimpleNamespace(fixed_index=0, start=4, end=5, width_pt=80.)])
    section = SimpleNamespace(native_format=SimpleNamespace(bindings=[NativeSlotBinding.model_validate(b) for b in bindings]),
                              blocks=[SimpleNamespace(text=item["text"])])
    paragraph = _native_paragraph(document, section, element, 0.)
    assert paragraph.text == "响应人：" + value + "（盖单位章）"
    assert all(run.font.size.pt == 12. for run in paragraph.runs)
    assert not paragraph._p.xpath('.//w:fitText|.//w:w')
    assert all(int(node.get(qn("w:val"))) >= 0 for node in paragraph._p.xpath('.//w:rPr/w:spacing'))
    assert paragraph.paragraph_format.left_indent.pt == paragraph.paragraph_format.right_indent.pt == 56.7


def test_native_rule_covering_blank_and_caption_keeps_filled_company_contiguous():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph
    text="响应人： （全称、盖章）"
    item, bindings=fill_case(text,company="A company")
    element=SimpleNamespace(left_pt=60.,right_pt=60.,first_line_indent_pt=0.,line_spacing_pt=23.4,
        alignment="left",font_family="SimSun",font_size_pt=12.,bold=False,fixed_indices=[0],
        underlines=[SimpleNamespace(fixed_index=0,start=4,end=len(text),width_pt=210.)])
    section=SimpleNamespace(native_format=SimpleNamespace(bindings=[NativeSlotBinding.model_validate(b) for b in bindings]),
                            blocks=[SimpleNamespace(text=item["text"])])
    paragraph=_native_paragraph(Document(),section,element,0.)
    assert "A company（全称、盖章）" in paragraph.text
    assert not any("company" in run.text and "（全称" not in run.text for run in paragraph.runs)


def test_source_trailing_rule_uses_visible_editable_nbsp_without_changing_a_name_or_date():
    from company_bid.source_format_rendering import _underlined_run
    paragraph=Document().add_paragraph("Signature:")
    _underlined_run(paragraph,"",114.,"宋体",12.,False)
    assert paragraph.text == "Signature:\u00a0"
    assert paragraph.runs[-1].underline and paragraph.runs[-1].font.size.pt == 12.
    assert int(paragraph.runs[-1]._r.xpath('./w:rPr/w:spacing')[0].get(qn("w:val"))) == 2160


def test_native_same_baseline_fragments_share_a_paragraph_with_distinct_source_fonts():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph, _same_baseline
    common=dict(kind="paragraph",alignment="left",right_pt=60.,first_line_indent_pt=0.,underlines=[])
    bold=SimpleNamespace(**common,left_pt=88.7,bbox=[88.7,493.292,185.02,509.292],fixed_indices=[0],
                         line_spacing_pt=20.,font_family="SimSun",font_size_pt=16.,bold=True)
    plain=SimpleNamespace(**common,left_pt=185.08,bbox=[185.08,496.728,425.08,508.728],fixed_indices=[1],
                          line_spacing_pt=15.,font_family="SimSun",font_size_pt=12.,bold=False)
    assert _same_baseline(bold,plain)
    document=Document();section=SimpleNamespace(native_format=SimpleNamespace(bindings=[]),
        blocks=[SimpleNamespace(text="Example."),SimpleNamespace(text="Following description.")])
    paragraph=_native_paragraph(document,section,bold,0.)
    _native_paragraph(document,section,plain,0.,paragraph=paragraph)
    assert len(document.paragraphs)==1 and paragraph.text=="Example.Following description."
    assert [(r.font.size.pt,r.bold) for r in paragraph.runs] == [(16.,True),(12.,False)]


def test_short_signature_uses_common_source_form_width_without_changing_evidence_bbox():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph
    element=SimpleNamespace(kind="paragraph",bbox=[60.,500.,300.,512.],left_pt=60.,right_pt=300.,first_line_indent_pt=0.,
        line_spacing_pt=15.,alignment="left",font_family="SimSun",font_size_pt=12.,bold=False,fixed_indices=[0],underlines=[])
    section=SimpleNamespace(native_format=SimpleNamespace(bindings=[],page_width_pt=600.,elements=[element,SimpleNamespace(bbox=[60.,100.,540.,400.])]),
                            blocks=[SimpleNamespace(text="Signature:__________________")])
    paragraph=_native_paragraph(Document(),section,element,0.)
    assert paragraph.paragraph_format.left_indent.pt==paragraph.paragraph_format.right_indent.pt==60.
    assert element.bbox==[60.,500.,300.,512.] and element.right_pt==300.


def test_leading_company_fill_starts_at_source_rule_left_edge_not_original_placeholder_glyph():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph
    source="（响应人） 已认真阅读了文件。"
    rule={"fixed_index":0,"start":0,"end":6,"width_pt":90.,"bbox":[80.,100.,170.,100.],
          "locator":"pdf/page[1]/rule[1]","basis":"native_vector_rule"}
    item,bindings=fill_case(source,rules=[rule])
    element=SimpleNamespace(kind="paragraph",bbox=[100.,90.,540.,102.],left_pt=60.,right_pt=60.,first_line_indent_pt=40.,
        line_spacing_pt=23.4,alignment="left",font_family="SimSun",font_size_pt=12.,bold=False,fixed_indices=[0],underlines=[])
    section=SimpleNamespace(native_format=SimpleNamespace(bindings=[NativeSlotBinding.model_validate(b) for b in bindings]),
                            blocks=[SimpleNamespace(text=item["text"])])
    paragraph=_native_paragraph(Document(),section,element,0.)
    assert paragraph.paragraph_format.first_line_indent.pt==20.
    assert element.first_line_indent_pt==40. and element.bbox[0]==100.


@pytest.mark.parametrize("owns_rule", [True, False])
def test_zero_length_leading_fill_is_underlined_only_inside_its_exact_source_rule(owns_rule):
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _native_paragraph
    text="（响应人） 已认真阅读了文件。"
    rule={"fixed_index":0,"start":0,"end":6,"width_pt":90.,"bbox":[80.,100.,170.,100.],
          "locator":"pdf/page[1]/rule[1]","basis":"native_vector_rule"}
    item,bindings=fill_case(text,rules=[rule])
    if not owns_rule:
        bindings[0]["source_rule"]=None
    element=SimpleNamespace(kind="paragraph",bbox=[92.,90.,540.,102.],left_pt=60.,right_pt=60.,first_line_indent_pt=32.,
        line_spacing_pt=23.4,alignment="left",font_family="SimSun",font_size_pt=12.,bold=False,fixed_indices=[0],
        underlines=[NativeUnderline.model_validate(rule)])
    section=SimpleNamespace(native_format=SimpleNamespace(bindings=[NativeSlotBinding.model_validate(b) for b in bindings]),
                            blocks=[SimpleNamespace(text=item["text"])])
    paragraph=_native_paragraph(Document(),section,element,0.)
    company_run=next(run for run in paragraph.runs if "A company" in run.text)
    assert bool(company_run.underline) is owns_rule
    assert paragraph.text.replace("\u00a0","").startswith("A company（响应人）")
    assert "已认真阅读了文件。" in paragraph.text


def test_native_missing_annotations_are_small_ids_with_a_full_explicit_legend(tmp_path):
    data,_=source_format_fixture(tmp_path)
    data["issues"]=[{"issue_id":"M-001","section_id":"FORM","title":"Missing synthetic detail",
        "reason":"No authorized fact.","action":"Provide this input.","source_requirement":"Preserve this exact source form.","requirement_refs":["R"]}]
    data["tables"][0]["rows"][0][0]["issue_refs"]=["M-001"]
    native=data["sections"][0]["native_format"]
    next(e for e in native["elements"] if e["kind"]=="table")["output_grid_sha256"]=format_digest(data["tables"][0]["rows"])
    native["proof_sha256"]=format_digest({k:v for k,v in native.items() if k!="proof_sha256"})
    executor=build_executor(tmp_path/"runtime",{"provider":{"mode":"prepared"}})
    run=executor.create_run({"prepared":data});done=executor.execute(run)
    assert done["status"]=="succeeded",done.get("error_code")
    doc=Document(executor.store.root/executor.output(run)["files"]["bid_docx"]["path"])
    paragraph=doc.tables[0].cell(0,0).paragraphs[0]
    assert [(r.text,r.font.size.pt) for r in paragraph.runs] == [("Identity area",12.),(" M-001",9.)]
    assert sum(p.text=="红色 M 编号表示待补，详见待补清单。" for p in doc.paragraphs)==1


@pytest.mark.parametrize("mutation", ["unknown_fixed_index", "wrong_source_block"])
def test_binding_must_belong_to_the_exact_element_member(tmp_path, mutation):
    data, _ = source_format_fixture(tmp_path)
    _, bindings = fill_case("响应人： （盖单位章）", company="Synthetic company")
    binding = bindings[0]
    native = data["sections"][0]["native_format"]
    paragraph = next(e for e in native["elements"] if e["kind"] == "paragraph")
    binding["fixed_index"] = 999 if mutation == "unknown_fixed_index" else paragraph["fixed_indices"][0]
    binding["source_block_id"] = "different-frozen-block"
    data["facts"] = [{"fact_id":"C","label":"公司名称","value":"Synthetic company","source_refs":["T"]}]
    binding["fact_source_refs"] = ["T"]
    native["bindings"] = [binding]
    native["proof_sha256"] = format_digest({k:v for k,v in native.items() if k != "proof_sha256"})
    with pytest.raises(ValidationError, match="native fill must retain"):
        PreparedBid.model_validate(data)
    bad = deepcopy(bindings[0]);bad.update(start=len(bad["before_text"])+1, end=len(bad["before_text"])+1)
    with pytest.raises(ValidationError):NativeSlotBinding.model_validate(bad)
