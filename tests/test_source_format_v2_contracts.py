"""Explicit page scopes and controlled identity slots, without PDF/model calls."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace as Obj

from pydantic import TypeAdapter, ValidationError
import pytest

from company_bid.models import TableCell
from company_bid.source_format_contracts import (
    NativeFormatLayout, NativeFormatLayoutV2, NativeFormatSpec, format_digest,
    validate_native_format,
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def seal(data):
    data["proof_sha256"] = format_digest({k:v for k,v in data.items() if k != "proof_sha256"})
    return data


def bounds(members):
    boxes = [m["bbox"] for m in members if m["role"] == "selected"]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def fixture(*, two_pages=True, heading_only=False, with_table=False):
    sources = {"T":Obj(sha256="a"*64, path="synthetic.pdf", kind="tender")}
    fixed, tables = [], {}

    def quote(block, locator, text):
        key = "Q-"+block
        sources[key] = Obj(sha256="a"*64,path="synthetic.pdf",kind="tender",
                          locator=f"{locator}; blocks={block}; quote_sha256={sha(text)}")
        return key

    def member(block, ordinal, page, line, text, box, role="selected", kind="paragraph"):
        return dict(block_id=block,ordinal=ordinal,physical_page=page,kind=kind,
                    locator=f"pdf/page[{page}]/{'line' if kind == 'paragraph' else 'table'}[{line}]",
                    text_sha256=sha(text),bbox=box,role=role)

    h = member("HEAD",11,7,1,"Synthetic fixed response",[100.,50.,260.,64.])
    body = member("BODY",12,7,2,"First original paragraph.",[40.,100.,300.,112.])
    first_members = [member("PREVIOUS",10,7,0,"Adjacent section",[40.,20.,200.,32.],"excluded"),h]
    elements = []

    def paragraph(m, text, index):
        fixed.append(Obj(kind="paragraph",text=text,source_refs=[quote(m["block_id"],m["locator"],text)],fact_refs=[]))
        return dict(kind="paragraph",physical_page=m["physical_page"],fixed_indices=[index],
                    source_block_ids=[m["block_id"]],source_locators=[m["locator"]],source_texts=[text],
                    output_texts_sha256=format_digest([text]),bbox=m["bbox"],font_family="SimSun",
                    font_size_pt=12.,bold=False,alignment="left",left_pt=40.,right_pt=40.,
                    first_line_indent_pt=0.,line_spacing_pt=18.,underlines=[],grouping_rule="one_native_line")

    if not heading_only:
        first_members.append(body)
        elements.append(paragraph(body,"First original paragraph.",0))
    first_members += [member("NEXT",13,7,3,"Next section",[40.,150.,250.,162.],"excluded"),
                      member("FOOT7",14,7,4,"7",[300.,750.,309.,759.],"page_furniture")]
    pages = [dict(physical_page=7,page_width_pt=600.,page_height_pt=800.,scope_kind="partial_page",
                  scope_bbox=bounds(first_members),members=first_members,fixed_indices=[] if heading_only else [0],
                  native_page_sha256="b"*64,frozen_page_sha256="c"*64)]
    if two_pages:
        assert not heading_only
        if with_table:
            m=member("GRID",15,8,0," ",[40.,100.,240.,160.],kind="table")
            rows=[[TableCell(text=" ")]]
            tables["TABLE"]=Obj(columns=["A"],rows=rows,merges=[],source_refs=[quote("GRID",m["locator"]," ")])
            fixed.append(Obj(kind="table_ref",table_ref="TABLE",source_refs=tables["TABLE"].source_refs))
            elements.append(dict(kind="table",physical_page=8,fixed_index=1,source_block_id="GRID",
                source_locator=m["locator"],table_id="TABLE",bbox=m["bbox"],column_widths_pt=[200.],row_heights_pt=[60.],
                cells=[dict(row=0,column=0,font_family="SimSun",font_size_pt=12.,bold=False,alignment="left",
                            vertical_alignment="center",left_inset_pt=0.,right_inset_pt=0.,top_inset_pt=0.,bottom_inset_pt=0.,
                            inset_basis="no_measured_inset",underlines=[dict(start=0,end=1,width_pt=40.,locator="pdf/page[8]/rule[0]")])],
                source_rows=[[" "]],source_merges=[],source_grid_sha256=format_digest([[" "]]),
                output_grid_sha256=format_digest([[cell.model_dump(mode="json") for cell in row]for row in rows])))
        else:
            m=member("CONTINUATION",15,8,0,"Continued original sentence.",[40.,60.,260.,72.])
            elements.append(paragraph(m,"Continued original sentence.",1))
        pm=[m,member("FOOT8",16,8,1,"8",[200.,600.,209.,609.],"page_furniture")]
        pages.append(dict(physical_page=8,page_width_pt=400.,page_height_pt=650.,scope_kind="whole_page",
                          scope_bbox=bounds(pm),members=pm,fixed_indices=[1],native_page_sha256="d"*64,frozen_page_sha256="e"*64))
    data=dict(schema_version="source-response-format-v2",source_id="T",source_sha256="a"*64,
        physical_page=7,page_width_pt=600.,page_height_pt=800.,heading_text="Synthetic fixed response",
        heading_source_ref=quote("HEAD",h["locator"],"Synthetic fixed response"),heading_source_block_id="HEAD",
        heading_source_locator=h["locator"],heading_bbox=h["bbox"],heading_font_family="SimSun",heading_font_size_pt=14.,
        heading_bold=True,heading_alignment="center",elements=elements,bindings=[],native_read_sha256="f"*64,
        frozen_blocks_sha256="0"*64,page_scopes=pages,form_role="response")
    return seal(data),fixed,tables,sources,{}


def check(case):
    data,fixed,tables,sources,facts=case
    spec=NativeFormatLayoutV2.model_validate(data)
    validate_native_format(spec,fixed,tables,sources,facts,title=data["heading_text"],
                           identities={"company_name":"Synthetic Co", "project_name":"Synthetic project"})
    return spec


def v1(data):
    result=deepcopy(data)
    result["schema_version"]="source-response-format-v1"
    result.pop("page_scopes");result.pop("form_role")
    for element in result["elements"]:element.pop("physical_page")
    return seal(result)


def test_v1_shape_seal_and_discriminator_remain_unchanged():
    data,*_=fixture(two_pages=False)
    original=v1(data)
    spec=TypeAdapter(NativeFormatSpec).validate_python(original)
    assert isinstance(spec,NativeFormatLayout) and not isinstance(spec,NativeFormatLayoutV2)
    assert spec.model_dump(mode="json")==original
    assert "page_scopes" not in type(spec).model_fields and "form_role" not in type(spec).model_fields
    assert "physical_page" not in type(spec.elements[0]).model_fields


@pytest.mark.parametrize("heading_only",[False,True])
def test_partial_page_keeps_neighbors_outside_exact_selected_scope(heading_only):
    case=fixture(two_pages=False,heading_only=heading_only)
    spec=check(case)
    assert isinstance(TypeAdapter(NativeFormatSpec).validate_python(case[0]),NativeFormatLayoutV2)
    assert len(spec.elements)==(0 if heading_only else 1)
    assert [m.block_id for m in spec.page_scopes[0].members if m.role=="excluded"]==["PREVIOUS","NEXT"]


@pytest.mark.parametrize("with_table",[False,True])
def test_each_continuation_element_and_quote_use_its_own_source_page(with_table):
    spec=check(fixture(with_table=with_table))
    assert [p.physical_page for p in spec.page_scopes]==[7,8]
    assert spec.elements[-1].physical_page==8
    assert spec.page_scopes[-1].page_width_pt==400.


@pytest.mark.parametrize("mutation",[
    "missing_page", "reversed_pages", "member_wrong_page", "member_wrong_locator_type",
    "member_wrong_hash", "member_duplicate_ordinal", "member_reorder", "member_duplicate_id",
    "selected_gap", "neighbor_selected", "missing_heading", "missing_selected_member",
    "element_wrong_page", "element_wrong_locator", "element_wrong_type", "element_bbox",
    "scope_bbox", "member_out_of_page", "whole_page_excludes", "duplicate_index", "index_gap",
    "scope_wrong_indices", "first_frame", "table_bbox", "table_wrong_text_hash",
])
def test_resealed_scope_omission_neighbor_page_and_geometry_changes_fail(mutation):
    data,*_=fixture(with_table=mutation.startswith("table_"))
    p=data["page_scopes"][0];e=data["elements"][0]
    if mutation=="missing_page":data["page_scopes"][1]["physical_page"]=9
    elif mutation=="reversed_pages":data["page_scopes"].reverse()
    elif mutation=="member_wrong_page":p["members"][2]["physical_page"]=8
    elif mutation=="member_wrong_locator_type":p["members"][2]["locator"]="pdf/page[7]/table[2]"
    elif mutation=="member_wrong_hash":p["members"][2]["text_sha256"]="1"*64
    elif mutation=="member_duplicate_ordinal":p["members"][2]["ordinal"]=11
    elif mutation=="member_reorder":p["members"][1:3]=reversed(p["members"][1:3])
    elif mutation=="member_duplicate_id":p["members"][3]["block_id"]="BODY"
    elif mutation=="selected_gap":p["members"][2]["role"]="excluded";p["members"][3]["role"]="selected";p["scope_bbox"]=bounds(p["members"])
    elif mutation=="neighbor_selected":p["members"][0]["role"]="selected";p["scope_bbox"]=bounds(p["members"])
    elif mutation=="missing_heading":p["members"][1]["role"]="excluded";p["scope_bbox"]=bounds(p["members"])
    elif mutation=="missing_selected_member":p["members"].pop(2);p["scope_bbox"]=bounds(p["members"])
    elif mutation=="element_wrong_page":e["physical_page"]=8
    elif mutation=="element_wrong_locator":e["source_locators"][0]="pdf/page[8]/line[0]"
    elif mutation=="element_wrong_type":p["members"][2]["kind"]="table";p["members"][2]["locator"]="pdf/page[7]/table[0]"
    elif mutation=="element_bbox":e["bbox"]=list(e["bbox"]);e["bbox"][2]+=2.
    elif mutation=="scope_bbox":p["scope_bbox"][2]+=2.
    elif mutation=="member_out_of_page":p["members"][3]["bbox"][2]=601.
    elif mutation=="whole_page_excludes":p["scope_kind"]="whole_page"
    elif mutation=="duplicate_index":data["elements"][1]["fixed_indices"]=[0]
    elif mutation=="index_gap":data["elements"][1]["fixed_indices"]=[2]
    elif mutation=="scope_wrong_indices":p["fixed_indices"]=[]
    elif mutation=="first_frame":data["page_width_pt"]=599.
    elif mutation=="table_bbox":data["elements"][1]["bbox"][2]+=1.
    else:data["page_scopes"][1]["members"][0]["text_sha256"]="1"*64
    with pytest.raises(ValidationError):NativeFormatLayoutV2.model_validate(seal(data))


@pytest.mark.parametrize("table",[False,True])
def test_source_quote_and_rule_checks_do_not_reuse_first_page(table):
    case=fixture(with_table=table);data,fixed,tables,sources,facts=case
    element=data["elements"][1]
    if table:
        element["cells"][0]["underlines"][0]["locator"]="pdf/page[7]/rule[0]"
    else:
        element["underlines"]=[dict(fixed_index=1,start=0,end=0,width_pt=30.,
            locator="pdf/page[7]/rule[0]",basis="native_vector_rule",bbox=[40.,72.,70.,72.])]
    seal(data)
    with pytest.raises(ValueError,match="source page|blank source slot"):check(case)


@pytest.mark.parametrize("table",[False,True])
def test_continuation_cannot_claim_a_quote_from_the_first_page(table):
    case=fixture(with_table=table)
    sources=case[3]
    key="Q-GRID" if table else "Q-CONTINUATION"
    sources[key].locator=sources[key].locator.replace("pdf/page[8]/","pdf/page[7]/")
    with pytest.raises(ValueError,match="source quote|original quote"):check(case)


@pytest.mark.parametrize("bbox",[None,[40.,700.,70.,700.]])
def test_continuation_rule_needs_geometry_inside_its_own_smaller_page(bbox):
    case=fixture();data=case[0]
    data["elements"][1]["underlines"]=[dict(fixed_index=1,start=0,end=0,width_pt=30.,
        locator="pdf/page[8]/rule[0]",basis="native_vector_rule",bbox=bbox)]
    seal(data)
    with pytest.raises(ValidationError,match="source rule geometry"):check(case)


def number_case(text="项目编号：____", *, rule=False):
    case=fixture(two_pages=False);data,fixed,tables,sources,facts=case
    start=text.rindex("：")+1;end=len(text)
    value="SYN-2026-42";after=text[:start]+value
    element=data["elements"][0];element["source_texts"]=[text];element["output_texts_sha256"]=format_digest([after])
    data["page_scopes"][0]["members"][2]["text_sha256"]=sha(text)
    sources["Q-BODY"].locator=f"pdf/page[7]/line[2]; blocks=BODY; quote_sha256={sha(text)}"
    fixed[0].text=after;fixed[0].fact_refs=["NUMBER"]
    facts["NUMBER"]=Obj(label="项目编号",value=value,source_refs=["EXPLICIT-INPUT"])
    binding=dict(fixed_index=0,source_block_id="BODY",start=start,end=end,original=text[start:end],replacement=value,
        label="project_number",fact_id="NUMBER",fact_source_refs=["EXPLICIT-INPUT"],before_text=text,after_text=after,
        before_sha256=sha(text),after_sha256=sha(after),protected_text_sha256=sha(text[:start]+"<slot>"+text[end:]),source_rule=None)
    if rule:
        source_rule=dict(fixed_index=0,start=start,end=end,width_pt=60.,locator="pdf/page[7]/rule[0]",
                         basis="native_vector_rule",bbox=[100.,112.,160.,112.])
        element["underlines"]=[source_rule];binding["source_rule"]=deepcopy(source_rule)
    data["bindings"]=[binding];seal(data)
    return case


@pytest.mark.parametrize("text",["项目编号：", "项目编号： ", "项目编号：____"])
@pytest.mark.parametrize("rule",[False,True])
def test_v2_project_number_requires_explicit_blank_and_one_exact_fact(text,rule):
    check(number_case(text,rule=rule))


def test_project_number_on_combined_identity_line_preserves_existing_project_words():
    check(number_case("项目名称：原有项目 项目编号：____"))


def test_v1_rejects_new_number_token_even_after_valid_reseal():
    data,*_=number_case()
    with pytest.raises(ValidationError,match="not part of the v1"):
        NativeFormatLayout.model_validate(v1(data))


@pytest.mark.parametrize("mutation",["conflict","wrong_label","wrong_value","wrong_refs","missing_fact","wrong_rule_range","no_rule_for_midline"])
def test_project_number_does_not_use_ambiguous_fact_or_unbound_rule(mutation):
    case=number_case(rule=True);data,fixed,tables,sources,facts=case
    if mutation=="conflict":facts["OTHER"]=Obj(label="项目编号",value="OTHER-VALUE",source_refs=["EXPLICIT-INPUT"])
    elif mutation=="wrong_label":facts["NUMBER"].label="采购编号"
    elif mutation=="wrong_value":facts["NUMBER"].value="OTHER-VALUE"
    elif mutation=="wrong_refs":facts["NUMBER"].source_refs=["OTHER-SOURCE"]
    elif mutation=="missing_fact":facts.clear()
    elif mutation=="wrong_rule_range":data["bindings"][0]["source_rule"]["start"]+=1
    else:
        # Without a measured rule an arbitrary mid-line insertion is not a
        # colon-to-end blank slot, even when all local hashes are resealed.
        b=data["bindings"][0];b["end"]-=1;b["original"]=b["before_text"][b["start"]:b["end"]]
        b["after_text"]=b["before_text"][:b["start"]]+b["replacement"]+b["before_text"][b["end"]:]
        b["after_sha256"]=sha(b["after_text"]);b["protected_text_sha256"]=sha(b["before_text"][:b["start"]]+"<slot>"+b["before_text"][b["end"]:]);b["source_rule"]=None
    seal(data)
    with pytest.raises(ValueError):check(case)


@pytest.mark.parametrize("text",["项目编号：EXISTING","法定代表人：____","日期：____","说明：项目编号：____","不得填写 项目编号：____"])
def test_number_slot_cannot_overwrite_value_fill_person_date_or_instruction(text):
    with pytest.raises(ValueError):check(number_case(text))

