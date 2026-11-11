"""Source clauses are exact program-derived views, never model substrings."""
import copy
import hashlib
import json

import pytest

from company_bid.source_clauses import (
    SourceClause, SourceClauseError, build_clause_catalog, validate_clause,
)


def window(body=None, *, source_sha="a" * 64):
    texts = ["一、服务范围", "1、采购内容：", *(body or ["1）未取得许可不得操作；2）费用为100万元；3）交付记录。"]), "2、服务周期："]
    blocks = [{"block_id": f"B{index}", "source_id": "S", "locator": f"pdf/page[1]/line[{index}]", "page": 1,
               "unit_id": "U1", "ordinal": index, "kind": "paragraph", "origin": "native", "text": text}
              for index, text in enumerate(texts)]
    return {"window_id": "W", "source_id": "S", "source_sha256": source_sha, "source_format": "pdf",
            "blocks": blocks, "adjacent_context": [], "native_pdf_line_edges": []}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_complete_native_list_preserves_each_own_condition_amount_exception_and_exact_quote():
    w = window(["1）除经书面授权外不得操作；2）报价为100万元（不含税）；3）交付记录。"])
    before = copy.deepcopy(w)
    catalog = build_clause_catalog(w)
    assert [c["quote"] for c in catalog] == ["1）除经书面授权外不得操作；", "2）报价为100万元（不含税）；", "3）交付记录。"]
    assert w == before
    by_id = {b["block_id"]: b for b in w["blocks"]}
    for index, clause in enumerate(catalog):
        assert SourceClause.model_validate(clause).model_dump(mode="json") == clause
        parent = "\n".join(by_id[b]["text"] for b in clause["block_ids"])
        assert parent[clause["start"]:clause["end"]] == clause["quote"]
        assert clause["quote_sha256"] == sha(clause["quote"])
        assert clause["parent_quote_sha256"] == sha(parent)
        assert clause["boundary_proof"]["item_number"] == index + 1
        assert clause["boundary_proof"]["item_count"] == 3
        assert validate_clause(w, clause) == clause


def test_multiblock_spans_keep_lf_and_use_minimal_contiguous_parent_blocks():
    w = window(["1）未取得", "许可不得操作；2）费用为100万元；", "3）交付记录。"])
    catalog = build_clause_catalog(w)
    assert len(catalog) == 3
    assert catalog[0]["quote"] == "1）未取得\n许可不得操作；"
    assert catalog[0]["block_ids"] == ["B2", "B3"]
    assert catalog[1]["block_ids"] == ["B3"]
    assert catalog[1]["quote"] == "2）费用为100万元；"  # No invented terminal LF.
    assert catalog[2]["block_ids"] == ["B4"] and catalog[2]["start"] == 0


def test_ascii_marker_family_is_supported_but_mixed_family_is_not():
    assert len(build_clause_catalog(window(["1)保留许可条件；2)交付记录。"]))) == 2
    assert build_clause_catalog(window(["1）保留许可条件；2)交付记录。"])) == []


@pytest.mark.parametrize("text", [
    "说明如下：1）保留条件；2）交付记录。", "1）例如说明某项；2）另一个例子。",
    "1）参见2）执行；2）交付记录。", "1）引用1）作为依据；2）交付记录。",
    "1）第一条；3）第三条。", "1）第一条；2）第二条；1）重新开始。",
    "1）第一条；2）第二条；2）重复第二条。", "1）第一条2）同句编号并非边界。",
    "（1）第一条；2）第二条。", "1）文字（尚未闭合；2）仍在括号中）；3）末项。",
    "1）文字“尚未闭合；2）仍在引号中”；3）末项。", "1）文字《尚未闭合；2）仍在书名号中》；3）末项。",
    "1）检查设备【备选配置；2）保留记录】；3）提交成果。", "1）检查设备[备选配置；2）保留记录]；3）提交成果。",
    "1）检查设备{备选配置；2）保留记录}；3）提交成果。", "1）检查设备〔备选配置；2）保留记录〕；3）提交成果。",
    "1）检查设备'备选配置；2）保留记录'；3）提交成果。", "1）检查设备⟦备选配置；2）保留记录⟧；3）提交成果。",
    "1）文字（错配];2）记录。", "1）只有一条完整内容。", "1）第一条；2）末项未结束",
])
def test_ambiguous_examples_references_numbering_nested_and_unknown_end_have_no_catalog(text):
    assert build_clause_catalog(window([text])) == []


@pytest.mark.parametrize("dependency", ["前述要求", "上述要求", "所有服务", "所有工作", "以上各项", "以下要求", "各工作", "共同条件"])
def test_cross_item_or_public_dependencies_are_not_isolated(dependency):
    assert build_clause_catalog(window([f"1）保留方法；2）{dependency}不得省略。"])) == []


@pytest.mark.parametrize("collective", ["全部作业", "所有活动", "每项工作", "每个任务", "每一阶段", "各阶段", "工作均"])
def test_collective_authorization_condition_is_not_assigned_only_to_last_item(collective):
    text = f"1）开展巡检；2）提交记录，{collective}在取得书面授权后方可进行。"
    assert build_clause_catalog(window([text])) == []


@pytest.mark.parametrize("tail", ["全部项目均须取得许可。", "费用与风险另行约定。", "无编号的补充正文。"])
def test_unknown_unnumbered_common_tail_after_last_sentence_rejects_whole_list(tail):
    assert build_clause_catalog(window(["1）检查设备；2）交付记录。", tail])) == []
    assert build_clause_catalog(window(["1）检查设备；2）交付记录。" + tail])) == []


def test_substantive_preamble_or_exception_termination_does_not_disappear():
    w = window()
    w["blocks"][1]["text"] = "1、仅在书面授权后适用："
    assert build_clause_catalog(w) == []
    w = window()
    w["blocks"].insert(1, {**w["blocks"][0], "block_id": "PRECONDITION", "text": "未取得许可不得执行以下工作。"})
    for i, b in enumerate(w["blocks"]):
        b.update(ordinal=i, locator=f"pdf/page[1]/line[{i}]")
    assert build_clause_catalog(w) == []
    w = window()
    w["blocks"][-1]["text"] = "2、特别约定："
    assert build_clause_catalog(w) == []


@pytest.mark.parametrize("title", ["事故发生后措施", "许可取得前措施", "授权后工作内容", "工作范围不含维修", "采购内容仅在许可后适用", "作业要求及相关条件"])
@pytest.mark.parametrize("where", ["outer", "parent"])
def test_temporal_conditional_or_negative_heading_cannot_be_treated_as_pure_scope(title, where):
    w = window(["1）立即停工；2）提交报告。"])
    if where == "outer": w["blocks"][0]["text"] = "一、" + title
    else: w["blocks"][1]["text"] = "1、" + title + "："
    assert build_clause_catalog(w) == []


@pytest.mark.parametrize("outer,parent", [("采购范围及相关要求", "采购内容"), ("工作范围", "巡检内容"), ("主要技术要求", "具体服务任务")])
def test_supported_pure_nominal_scope_categories(outer, parent):
    w = window()
    w["blocks"][0]["text"] = "一、" + outer
    w["blocks"][1]["text"] = "1、" + parent + "："
    assert len(build_clause_catalog(w)) == 3


@pytest.mark.parametrize("case", ["table", "image", "heading", "footer", "cross_page", "ocr", "line_gap", "unknown_tail"])
def test_no_catalog_crosses_objects_headings_footer_unknown_tail_or_page(case):
    w = window(["1）检查设备；", "2）交付记录。"])
    if case in {"table", "image"}:
        w["blocks"][3]["kind"] = case
    elif case == "heading":
        w["blocks"][3]["text"] = "二、其它事项"
    elif case == "footer":
        w["blocks"].insert(4, {**w["blocks"][3], "block_id": "FOOTER", "text": "- 1 -"})
        for i, b in enumerate(w["blocks"]): b.update(ordinal=i, locator=f"pdf/page[1]/line[{i}]")
    elif case == "cross_page":
        w["blocks"][3].update(page=2, unit_id="U2", locator="pdf/page[2]/line[0]")
    elif case == "ocr":
        w["blocks"][3]["origin"] = "ocr"
    elif case == "line_gap":
        w["blocks"][3]["locator"] = "pdf/page[1]/line[9]"
    else:
        w["blocks"].pop()
    assert build_clause_catalog(w) == []


def test_only_complete_available_list_with_target_ownership_is_cataloged():
    w = window(["1）检查设备；", "2）交付记录。"])
    all_blocks = w["blocks"]
    w["blocks"], w["adjacent_context"] = [all_blocks[2]], [*all_blocks[:2], *all_blocks[3:]]
    catalog = build_clause_catalog(w)
    assert len(catalog) == 1 and catalog[0]["boundary_proof"]["item_number"] == 1
    w["blocks"], w["adjacent_context"] = [all_blocks[0]], all_blocks[1:]
    assert build_clause_catalog(w) == []
    w = window(["1）检查设备；", "2）交付记录。"])
    w["blocks"] = w["blocks"][:-1]  # Independent end boundary unavailable.
    assert build_clause_catalog(w) == []


def test_stable_ids_do_not_depend_on_window_id_or_target_adjacent_partition():
    w = window()
    first = build_clause_catalog(w)
    w["window_id"] = "OTHER-WINDOW"
    moved = w["blocks"].pop(0)
    w["adjacent_context"] = [moved]
    assert build_clause_catalog(w) == first


@pytest.mark.parametrize("field", ["source_id", "source_sha256", "block_id", "locator", "block_ids", "start", "end", "quote", "quote_sha256", "parent_quote_sha256", "proof_sha256", "boundary_proof", "clause_id"])
def test_model_supplied_hashes_offsets_quotes_or_catalog_identity_cannot_replace_source(field):
    w = window()
    claim = copy.deepcopy(build_clause_catalog(w)[0])
    if field in {"start", "end"}: claim[field] += 1
    elif field == "block_ids": claim[field] = ["OTHER"]
    elif field == "boundary_proof": claim[field]["item_number"] = 99
    elif field.endswith("sha256"): claim[field] = "0" * 64
    else: claim[field] = "FORGED"
    with pytest.raises(SourceClauseError): validate_clause(w, claim)


def test_existing_source_catalog_is_ignored_and_forged_entry_cannot_be_adopted():
    w = window()
    expected = build_clause_catalog(w)
    forged = copy.deepcopy(expected[0]); forged["quote"] = "删掉否定后的虚假片段"
    w["source_clause_catalog"] = [forged]
    assert build_clause_catalog(w) == expected
    with pytest.raises(SourceClauseError): validate_clause(w, forged)


def test_even_self_consistent_unique_exact_substring_is_not_a_source_clause():
    w = window()
    claim = copy.deepcopy(build_clause_catalog(w)[0])
    parent = w["blocks"][2]["text"]
    claim["start"] = parent.index("不得操作")
    claim["end"] = claim["start"] + len("不得操作")
    claim["quote"] = parent[claim["start"]:claim["end"]]
    claim["quote_sha256"] = sha(claim["quote"])
    claim["boundary_proof"]["span_parts"][0].update(start=claim["start"], end=claim["end"])
    claim["proof_sha256"] = sha(json.dumps(claim["boundary_proof"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert SourceClause.model_validate(claim)  # Internally coherent is insufficient.
    with pytest.raises(SourceClauseError, match="SOURCE_CLAUSE_RECONSTRUCTION_MISMATCH"):
        validate_clause(w, claim)


@pytest.mark.parametrize("case", ["source", "hash", "text_hash", "ordinal"])
def test_wrong_frozen_source_identity_is_not_silently_used(case):
    w = window()
    if case == "source": w["blocks"][2]["source_id"] = "OTHER"
    elif case == "hash": w["blocks"][2]["source_sha256"] = "b" * 64
    elif case == "text_hash": w["blocks"][2]["text_sha256"] = "0" * 64
    else: w["blocks"][3]["ordinal"] = w["blocks"][2]["ordinal"]
    with pytest.raises(SourceClauseError): build_clause_catalog(w)


def test_valid_hash_change_needs_reconstruction_not_self_reported_adoption():
    w = window()
    old = build_clause_catalog(w)[0]
    w["source_sha256"] = "b" * 64
    assert build_clause_catalog(w)[0]["clause_id"] != old["clause_id"]
    with pytest.raises(SourceClauseError): validate_clause(w, old)
    w["source_sha256"] = "not-a-hash"
    with pytest.raises(SourceClauseError): build_clause_catalog(w)


def test_page_locator_mismatch_and_more_than_24_parent_blocks_fail_closed():
    w = window()
    w["blocks"][2]["page"] = 99
    assert build_clause_catalog(w) == []
    w = window(["1）第一项；", *["同条原生续行" for _ in range(23)], "2）第二项。"])
    assert build_clause_catalog(w) == []


def test_no_catalog_field_contains_a_free_form_source_path_or_model_fact():
    catalog = build_clause_catalog(window())
    assert all(set(c) == {"clause_id", "source_id", "source_sha256", "block_id", "locator", "block_ids", "start", "end",
                          "quote", "quote_sha256", "parent_quote_sha256", "proof_sha256", "boundary_proof"} for c in catalog)
    assert "fact_id" not in json.dumps(catalog)
