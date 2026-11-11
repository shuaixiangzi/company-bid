"""Only explicit Arabic-comma short-heading siblings narrow forward context."""
import copy
import hashlib
import json

import pytest

from company_bid.extraction import (
    PlanningError, _hash, _heading_level, _index, _windows, _citation,
    _validate_window, extract_requirements,
)
from company_bid.planning import plan_bid
from test_source_selected_citations import Context, fixture, block, selected, candidate, live_candidate


def source(tmp_path, *, parent="二、服务内容", title="4、实施地点：", body="按项目现场位置实施。", preceding="估算额为100万元。", request=False):
    manifest, parsed = fixture(tmp_path)
    if request:
        block(parsed, "REQUEST", "应说明巡检和记录交付方法。")
    block(parsed, "PARENT", parent)
    block(parsed, "ANCHOR", "3、采购估算额：")
    block(parsed, "CONTINUATION", preceding)
    boundary = block(parsed, "BOUNDARY", title)
    block(parsed, "FOLLOWING", body)
    block(parsed, "LATER", "5、主要技术要求或技术方案：")
    block(parsed, "LATER_BODY", "应按本项目要求交付成果。")
    blocks, _, documents = _index(parsed, manifest)
    windows, ledger = _windows(documents, blocks, {"window_max_blocks": 3 if request else 2})
    return manifest, parsed, blocks, windows, ledger, boundary


def forward_ids(window):
    end = window["blocks"][-1]["ordinal"]
    return [b["block_id"] for b in window["adjacent_context"] if b["ordinal"] > end]


def test_only_consecutive_same_parent_arabic_colon_heading_cuts_forward(tmp_path):
    manifest, parsed, blocks, windows, ledger, boundary = source(tmp_path)
    first = windows[0]
    assert forward_ids(first) == ["CONTINUATION"]
    assert [[b["block_id"] for b in w["blocks"]] for w in windows] == [
        ["PARENT", "ANCHOR"], ["CONTINUATION", "BOUNDARY"], ["FOLLOWING", "LATER"], ["LATER_BODY"]]
    assert [i for row in ledger for i in row["block_ids"]] == list(blocks)
    assert len({b["block_id"] for w in windows for b in w["blocks"]}) == 7
    assert [b["block_id"] for b in windows[1]["adjacent_context"] if b["ordinal"] < windows[1]["blocks"][0]["ordinal"]] == ["PARENT", "ANCHOR"]
    proof = first["context_boundary_decisions"][0]
    assert proof["decision"] == "omitted_independent_forward_context"
    assert proof["anchor"]["block_id"] == "ANCHOR" and proof["boundary"]["block_id"] == "BOUNDARY"
    assert proof["parent"]["block_id"] == "PARENT" and proof["numbers"] == [3, 4]
    assert proof["proof_sha256"] == _hash({k: v for k, v in proof.items() if k != "proof_sha256"})
    assert proof["boundary"]["text_sha256"] == hashlib.sha256(boundary["text"].encode()).hexdigest()
    assert blocks["BOUNDARY"]["text"] == boundary["text"]
    assert _heading_level(boundary["text"]) is None  # No global/catalog rule change.
    assert "BOUNDARY" not in {row["block_id"] for row in first["source_heading_catalog"]}
    assert "按项目现场位置实施。" not in json.dumps(first, ensure_ascii=False)
    assert all("text" not in row for row in proof["checked_following"])


@pytest.mark.parametrize("title", [
    "4）按照规定说明地点。", "4、应当提供完整说明：", "4、报价不得超过100万元：",
    "4、实施地点：按项目现场实施。", "4、实施地点", "4、实施\n地点：", "5、实施地点：",
    "4.1 实施地点：", "4、" + "很长的标题" * 15 + "：", "4、投标人根据现场条件编制项目方案：",
    "4、按照现场条件和自身能力确定方案：",
])
def test_ordinary_numbered_prose_nonfamily_discontinuous_or_inline_body_does_not_cut(tmp_path, title):
    _, _, _, windows, _, _ = source(tmp_path, title=title)
    assert "BOUNDARY" in forward_ids(windows[0])
    assert windows[0]["context_boundary_decisions"] == []


@pytest.mark.parametrize("parent", ["服务内容", "本段应按以下事项处理。", "2.1 服务内容"])
def test_no_explicit_chinese_parent_is_not_a_boundary(tmp_path, parent):
    _, _, _, windows, _, _ = source(tmp_path, parent=parent)
    assert "BOUNDARY" in forward_ids(windows[0])


@pytest.mark.parametrize("title", ["例外", "补充规定", "适用条件", "特别约定", "其他说明"])
def test_exception_and_dependency_titles_keep_forward_condition(tmp_path, title):
    _, _, _, windows, _, _ = source(tmp_path, title=f"4、{title}：", body="前述工期遇特殊情况顺延。")
    assert "BOUNDARY" in forward_ids(windows[0]) and "FOLLOWING" in forward_ids(windows[0])
    decision = windows[0]["context_boundary_decisions"][0]
    assert decision["decision"] == "retained_context" and decision["reason"] == "dependency_or_exception_title"


@pytest.mark.parametrize("body", ["前述工期遇特殊情况顺延。", "但前款价格不包括新增范围。", "如遇停工，按照前条执行。",
                                  "本范围不适用前述上限。", "遇新增范围，须按上述价格另行约定。",
                                  "除前一编号所列费用外，其余费用包含在内。", "除采购估算额已含费用外，其余范围另行约定。"])
def test_independent_looking_title_with_body_back_reference_is_retained(tmp_path, body):
    _, _, _, windows, _, _ = source(tmp_path, body=body)
    assert "FOLLOWING" in forward_ids(windows[0])
    assert windows[0]["context_boundary_decisions"][0]["reason"] == "explicit_backward_reference_body"


@pytest.mark.parametrize("preceding", ["本项必须先取得许可，", "未获得授权时不得", "本项例外条件如下："])
def test_unfinished_preceding_condition_is_not_cut(tmp_path, preceding):
    _, _, _, windows, _, _ = source(tmp_path, preceding=preceding)
    assert "BOUNDARY" in forward_ids(windows[0])
    assert windows[0]["context_boundary_decisions"][0]["reason"] == "unfinished_preceding_condition"


def test_parent_to_child_and_unknown_following_body_keep_context(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "PARENT", "二、服务内容")
    block(parsed, "FIRST", "1、采购内容：")
    block(parsed, "BODY", "按项目要求实施。")
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 1})[0][0]
    assert "FIRST" in forward_ids(first) and first["context_boundary_decisions"] == []
    manifest, parsed, blocks, windows, _, _ = source(tmp_path)
    # End the existing six-paragraph lookahead exactly on the potential sibling.
    anchor = blocks["ANCHOR"]
    from company_bid.extraction import _narrow_forward_context, _arabic_forward_markers
    forward = [blocks["CONTINUATION"], blocks["BOUNDARY"]]
    retained, decisions = _narrow_forward_context([anchor], forward, _arabic_forward_markers(list(blocks.values())), {}, [], "a" * 64)
    assert retained == forward and decisions[0]["reason"] == "following_body_unknown"


@pytest.mark.parametrize("kind", ["table", "image"])
def test_existing_table_or_image_hard_boundary_does_not_supply_extra_context(tmp_path, kind):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "PARENT", "二、服务内容")
    block(parsed, "ANCHOR", "3、采购估算额：")
    extra = {"rows": [["字段", "数值"]], "merges": [], "column_widths": [1, 1]} if kind == "table" else {}
    block(parsed, "OBJECT", "", kind=kind, **extra)
    block(parsed, "BOUNDARY", "4、实施地点：")
    block(parsed, "BODY", "按项目现场实施。")
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert forward_ids(first) == [] and first["context_boundary_decisions"] == []


def test_changed_chinese_parent_prevents_cross_parent_sibling_cut(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "PARENT", "二、服务内容")
    block(parsed, "ANCHOR", "3、采购估算额：")
    block(parsed, "AMOUNT", "估算额为100万元。")
    block(parsed, "NEW_PARENT", "三、其它事项")
    block(parsed, "BOUNDARY", "4、实施地点：")
    block(parsed, "BODY", "按项目现场实施。")
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert "BOUNDARY" in forward_ids(first) and first["context_boundary_decisions"] == []


def test_first_same_parent_marker_out_of_order_does_not_search_later_for_a_cut(tmp_path):
    manifest, parsed = fixture(tmp_path)
    for identity, text in [("PARENT", "二、服务内容"), ("ANCHOR", "3、采购估算额："), ("PREVIOUS", "估算额为100万元。"),
                           ("FIFTH", "5、主要技术要求："), ("FIFTH_BODY", "按本项目要求实施。"),
                           ("FOURTH", "4、实施地点："), ("FOURTH_BODY", "按项目现场实施。")]:
        block(parsed, identity, text)
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert forward_ids(first) == ["PREVIOUS", "FIFTH", "FIFTH_BODY", "FOURTH", "FOURTH_BODY"]
    assert first["context_boundary_decisions"] == []


@pytest.mark.parametrize("new_scope", ["三、必须提交的资料：", "三、按指定格式提交资料。"])
def test_operative_or_ambiguous_chinese_scope_cannot_inherit_prior_parent(tmp_path, new_scope):
    manifest, parsed = fixture(tmp_path)
    for identity, text in [("PARENT", "二、服务内容"), ("ANCHOR", "3、采购估算额："),
                           ("AMOUNT", "估算额为100万元。"), ("NEW_SCOPE", new_scope),
                           ("BOUNDARY", "4、实施地点："), ("BODY", "按项目现场实施。")]:
        block(parsed, identity, text)
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert "NEW_SCOPE" in forward_ids(first) and "BOUNDARY" in forward_ids(first)
    assert first["context_boundary_decisions"] == []


@pytest.mark.parametrize("tail,reason", [("本范围不适用前述上限。", "explicit_backward_reference_body"),
                                          ("新增范围包括：", "following_body_unknown")])
def test_third_body_block_back_reference_or_unfinished_condition_is_not_cut(tmp_path, tail, reason):
    manifest, parsed = fixture(tmp_path)
    values = [("PARENT", "二、服务内容"), ("ANCHOR", "3、采购估算额："), ("PREVIOUS", "估算额为100万元。"),
              ("BOUNDARY", "4、实施地点："), ("BODY1", "第一行现场说明。"), ("BODY2", "第二行现场说明。"),
              ("BODY3", tail), ("LATER", "5、主要技术要求：")]
    for identity, text in values: block(parsed, identity, text)
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert "BODY3" in forward_ids(first) and "BOUNDARY" in forward_ids(first)
    decision = first["context_boundary_decisions"][0]
    assert decision["decision"] == "retained_context" and decision["reason"] == reason
    assert [r["block_id"] for r in decision["checked_following"]] == ["BODY1", "BODY2", "BODY3"]


def test_native_continuation_from_last_body_to_unprovided_source_retains_context(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    values = [("PARENT", "二、服务内容"), ("ANCHOR", "3、采购估算额："), ("PREVIOUS", "估算额为100万元。"),
              ("BOUNDARY", "4、实施地点："), ("BODY1", "第一行现场说明。"), ("BODY2", "第二行现场说明。"),
              ("BODY3", "第三行现场说明。"), ("BODY4", "仍须具备完整前提"), ("BODY5", "条件后方可实施。")]
    for index, (identity, text) in enumerate(values):
        y = 20 + index * 18
        value = block(parsed, identity, text, bbox=[40, y, 300 if identity == "BODY4" else 220, y + 10], origin="native")
        value["locator"] = f"pdf/page[1]/line[{index}]"
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert "BODY4" in forward_ids(first) and "BODY5" not in forward_ids(first)
    decision = first["context_boundary_decisions"][0]
    assert decision["reason"] == "following_body_unknown"
    assert decision["following_native_edge_sha256s"] and not decision["following_body_incomplete"]


def test_cross_page_footer_does_not_hide_prior_clause_exception(tmp_path):
    manifest, parsed = fixture(tmp_path)
    block(parsed, "PARENT", "二、服务内容", bbox=[60, 50, 200, 62])
    anchor = block(parsed, "ANCHOR", "3、工期：", bbox=[60, 80, 180, 92])
    amount = block(parsed, "PERIOD", "工期为30日历天。", bbox=[60, 100, 250, 112])
    for page in (1, 2, 3):
        if page == 2:
            title = block(parsed, "EXCEPTION", "4、适用条件：", page=2, bbox=[60, 60, 200, 72])
            body = block(parsed, "BODY", "前述工期遇不可抗力顺延。", page=2, bbox=[60, 90, 300, 102])
        elif page == 3:
            block(parsed, "OTHER", "其它正文。", page=3, bbox=[60, 60, 200, 72])
        block(parsed, f"F{page}", f"- {page} -", page=page, bbox=[290, 773, 305, 782])
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 2})[0][0]
    assert {"F1", "EXCEPTION", "BODY"} <= set(forward_ids(first))
    available = {b["block_id"]: b for b in first["blocks"] + first["adjacent_context"]}
    quote = _citation(selected(anchor, amount, title, body), available, {r["block_id"]: r for r in first["safe_skip_blocks"]})
    assert "30日历天" in quote["quote"] and "不可抗力顺延" in quote["quote"]
    assert quote["skipped_block_ids"] == ["F1"]


def test_verified_native_continuation_before_sibling_remains_available(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    texts = ["二、服务内容", "3、作业要求：", "未取得书面许", "可不得操作。", "4、实施地点：", "按项目现场实施。"]
    ids = ["PARENT", "ANCHOR", "PREFIX", "CONTINUATION", "BOUNDARY", "BODY"]
    values = []
    for index, (identity, text) in enumerate(zip(ids, texts)):
        y = 20 + index * 18
        value = block(parsed, identity, text, bbox=[40, y, 300 if identity == "PREFIX" else 220, y + 10], origin="native")
        value["locator"] = f"pdf/page[1]/line[{index}]"
        values.append(value)
    blocks, _, docs = _index(parsed, manifest)
    first = _windows(docs, blocks, {"window_max_blocks": 3})[0][0]
    assert forward_ids(first) == ["CONTINUATION"]
    assert ["PREFIX", "CONTINUATION"] in [edge["blocks"] for edge in first["native_pdf_line_edges"]]
    quote = _citation(selected(values[2], values[3]), {b["block_id"]: b for b in first["blocks"] + first["adjacent_context"]})
    assert quote["quote"] == "未取得书面许\n可不得操作。"


def test_boundary_source_proofs_change_context_hash_without_resupplying_omitted_body(tmp_path):
    manifest, parsed, blocks, windows, _, _ = source(tmp_path)
    before = windows[0]
    parsed["documents"][0]["units"][0]["blocks"][4]["text"] = "被省略正文PRIVATE-CANARY。"
    indexed, _, docs = _index(parsed, manifest)
    after = _windows(docs, indexed, {"window_max_blocks": 2})[0][0]
    assert before["window_id"] == after["window_id"]
    assert before["context_identity"] != after["context_identity"]
    assert "PRIVATE-CANARY" not in json.dumps(after, ensure_ascii=False)


def test_n3_rebuilds_boundary_context_identity_and_pure_neighbor_citation_stays_rejected(tmp_path):
    manifest, parsed, _, _, _, boundary = source(tmp_path, request=True)
    def call(ctx, task, unit, payload, validate):
        raw = live_candidate(payload, selected(payload["window"]["blocks"][0]))
        if not any(b["block_id"] == "REQUEST" for b in payload["window"]["blocks"]):
            raw["requirements"] = []
        return validate(raw)
    output = extract_requirements(Context(), parsed, manifest, call, {"window_max_blocks": 3})
    def no_call(*args): pytest.fail("No further model call is needed")
    assert plan_bid(Context(), output, parsed, manifest, no_call, {})["requirements"]
    indexed, _, docs = _index(parsed, manifest)
    first = _windows(docs, indexed, {"window_max_blocks": 3})[0][0]
    with pytest.raises(PlanningError, match="CITATION_OUTSIDE_REVIEW_WINDOW"):
        _validate_window(candidate({"window": first}, selected(boundary)), first)
    changed = copy.deepcopy(output)
    changed["window_results"][0]["processing"]["context_identity"]["sha256"] = "0" * 64
    with pytest.raises(PlanningError, match="EXTRACTION_PROCESSING_LEDGER_MISMATCH"):
        plan_bid(Context(), changed, parsed, manifest, no_call, {})
