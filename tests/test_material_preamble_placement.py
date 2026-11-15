from copy import deepcopy

import pytest

from company_bid.response_organization import _relocate_material_preamble


def fixture():
    title = "7.6、其他材料："
    texts = [title, "其中单位负责人为同一人或者存在控股、管理关系的不同单位，不得参加同一标",
             "段采购项目或者未划分标段的同一采购项目，格式如下："]
    blocks = {f"B{i}": {"block_id": f"B{i}", "text": text, "kind": "paragraph", "origin": "native",
        "source_id": "T", "locator": f"pdf/page[39]/line[{8+i}]"} for i,text in enumerate(texts)}
    heading = "单位负责人为同一人或存在控股、管理关系的单位情况说明"
    blocks["NEXT"] = {"block_id":"NEXT", "text":heading, "source_id":"T", "locator":"pdf/page[40]/line[0]"}
    fixed = [{"kind":"paragraph", "text":text, "source_refs":[f"REF{i}"]} for i,text in enumerate(texts[1:])]
    source = {"section_id":"PERFORMANCE", "fixed_content":[{"kind":"table_ref", "table_ref":"TABLE"}, *fixed],
              "requirement_refs":["R"]}
    target = {"section_id":"OTHER", "title":title, "fixed_content":[], "source_refs":[], "requirement_refs":[]}
    formats = {"PERFORMANCE":{"format_id":"F", "content_block_ids":["B0","B1","B2"]},
               "RELATION":{"format_id":"NEXT-F", "title":heading, "content_block_ids":["NEXT"]}}
    formats["PERFORMANCE"]["title"] = "响应人业绩统计表（后附业绩材料）"
    return source,target,formats,blocks,{"R":{"text":"\n".join(texts)}}


def relocate(source,target,formats,blocks,known):
    return _relocate_material_preamble([source], {"其他":target}, formats, blocks, known,
        lambda section,refs:section["requirement_refs"].extend(refs))


def test_exact_continuous_source_moves_introduction_after_its_real_heading():
    source,target,formats,blocks,known=fixture()
    original=deepcopy(source["fixed_content"])
    report,=relocate(source,target,formats,blocks,known)
    assert source["fixed_content"] == original[:1]
    assert target["fixed_content"] == original[1:] and target["source_refs"] == ["REF0","REF1"]
    assert target["requirement_refs"] == ["R"] and report["source_block_ids"] == ["B1","B2"]
    assert report["from_section_id"] == "PERFORMANCE" and report["to_section_id"] == "OTHER"


@pytest.mark.parametrize("mutation", ["foreign_source","gap","different_page","non_native","different_intro","unproven_next"])
def test_unproven_source_is_not_relocated(mutation):
    source,target,formats,blocks,known=fixture()
    if mutation=="foreign_source": blocks["B1"]["source_id"]="OTHER"
    if mutation=="gap": blocks["B2"]["locator"]="pdf/page[39]/line[12]"
    if mutation=="different_page": blocks["B2"]["locator"]="pdf/page[40]/line[10]"
    if mutation=="non_native": blocks["B1"]["origin"]="ocr"
    if mutation=="different_intro": blocks["B1"]["text"]="其他无关内容"
    if mutation=="unproven_next": blocks["NEXT"]["source_id"]="OTHER"
    before=deepcopy((source,target))
    assert relocate(source,target,formats,blocks,known)==[] and (source,target)==before


@pytest.mark.parametrize("mutation", ["duplicate","order","target_not_empty"])
def test_source_owner_or_order_conflict_is_explicit(mutation):
    source,target,formats,blocks,known=fixture()
    if mutation=="duplicate": source["fixed_content"].append(deepcopy(source["fixed_content"][-1]))
    if mutation=="order": source["fixed_content"][-2:]=reversed(source["fixed_content"][-2:])
    if mutation=="target_not_empty": target["fixed_content"]=[{"kind":"paragraph", "text":"Already present"}]
    with pytest.raises(ValueError,match="MATERIAL_PREAMBLE_"):
        relocate(source,target,formats,blocks,known)
