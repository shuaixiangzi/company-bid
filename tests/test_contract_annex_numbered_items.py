import copy

import pytest

from company_bid import contract_annex_numbered_items as numbered
from company_bid import extraction as ext
from company_bid.storage import digest
from test_source_selected_citations import block, fixture


def response(window, selections, unresolved=(), *, category="commercial"):
    return {"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
            "unresolved_block_ids": list(unresolved), "format_status": "none_verified", "format_chapters": [],
            "requirements": [{"citation": {"block_ids": ids}, "category": category, "response_mode": "procedure",
                              "mandatory": True, "format_status": "not_prescribed", "format_title": None, "confidence": "high",
                              "required_fact_labels": [], "attachment_keywords": [], "reference_keywords": []} for ids in selections]}


def execute(window, config, raw):
    return ext.run_extraction_window(None, window, config,
        lambda _ctx, _task, _unit, _payload, validate: validate(copy.deepcopy(raw)))


@pytest.fixture
def scenario(tmp_path):
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    source = [
        ("heading", "3.4.2 乙方违规处罚标准", True),
        ("intro", "因乙方原因发生下列情形，按以下标准处理：", True),
        ("a", "（1）首次丢失施工记录的，", True),
        ("b", "违约金为2万元。", False),
        ("c", "（2）再次丢失施工记录的，", True),
        ("d", "违约金为4万元。", False),
        ("e", "（3）第三次丢失施工记录的，", True),
        ("f", "违约金为6万元。", False),
        ("g", "（4）主动隐匿施工记录的，", True),
        ("h", "违约金为10万元。", False),
        ("boundary", "3.4.3 乙方责任追究", True),
        ("next", "乙方应按约定提交整改记录。", True),
    ]
    for index, (identity, line, indent) in enumerate(source):
        row = block(parsed, identity, line, origin="native",
                    bbox=[113.0 if indent else 89.0, 10 + 20 * index, 400.0, 28 + 20 * index])
        row["locator"] = f"pdf/page[1]/line[{index}]"
    blocks, _, docs = ext._index(parsed, manifest)
    config = ext.extraction_window_config({"window_max_blocks": 6})
    windows, _ = ext._windows(docs, blocks, config, manifest=manifest)
    assert [[row["block_id"] for row in value["blocks"]] for value in windows] == [
        ["heading", "intro", "a", "b", "c", "d"], ["e", "f", "g", "h", "boundary", "next"]]
    raw = response(windows[0], [["heading", "intro", "a", "b"], ["heading", "intro", "c", "d"]],
                   unresolved=["e", "f", "g"])
    scope = {"source_id": "T", "source_sha256": docs[0]["sha256"], "window_id": windows[0]["window_id"],
             "raw_candidate_sha256": digest(raw), "heading_block_ids": ["heading"],
             "reviewed_pages": [{"physical_page": 1, "rendered_rgb_sha256": "a" * 64}]}
    return config, windows, blocks, scope, raw


def bind(scenario):
    config, windows, blocks, scope, _ = scenario
    clean = ext.extraction_window_config({**config, numbered.SOURCE_CONFIG_KEY: scope})
    pages = [{"footer": {"block_id": "not_a_real_footer"}}]
    owners = {row["block_id"]: window["window_id"] for window in windows for row in window["blocks"]}
    groups = numbered._native_groups(list(blocks.values()), pages, scope, owners, *windows)
    bound = []
    for role, window in zip(("declaring_window", "following_owner"), windows, strict=True):
        proof = {key: window[key] for key in ("window_id", "source_id", "source_sha256")}
        proof.update(rule=numbered.RULE, scope=scope, reviewed_pages=pages, role=role, groups=groups)
        bound.append({**window, numbered.WINDOW_CONTEXT_KEY: {**proof, "proof_sha256": digest(proof)}})
    return clean, bound


def test_whole_children_keep_separate_parent_scope_across_owners(scenario):
    config, original, _, _, raw = scenario
    reviewed, (window, following) = bind(scenario)
    assert [ext.extraction_window_payload(row, config) for row in original] == [
        ext.extraction_window_payload(row, reviewed) for row in (window, following)]
    with pytest.raises(ext.PlanningError, match="CITATION_SPAN_NOT_CONTIGUOUS"):
        ext.validate_live_window(raw, original[0])
    result = execute(window, reviewed, raw)
    assert result["raw_model_candidate"] == raw
    future = result[numbered.RESOLUTION_KEY]["following_items"]
    assert [row["block_id"] for row in future[-1]["members"]] == ["g", "h"]
    assert result["processing"]["unresolved_block_ids"] == ["e", "f", "g"]
    assert len(result["validated"]["requirements"]) == 2
    raw_following = response(following, [["e", "f"], ["g", "h"]], category="procedure")
    successor = execute(following, reviewed, raw_following)
    assert successor["raw_model_candidate"] == raw_following
    with pytest.raises(ext.PlanningError, match="DEFERRED_CONTEXT_OWNER_INCOMPLETE"):
        ext._close_deferred_context([window, following], [result])
    closure = ext._close_deferred_context([window, following], [result, successor])
    assert [row["block_id"] for row in closure["items"]] == ["e", "f", "g"]
    assert all(row["resolution"] == "owner_target_validated_numbered_contract_item_with_scope" for row in closure["items"])
    merged, _ = ext._merge_results([result, successor])
    assert [row["citations"][0]["block_ids"] for row in merged] == [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]]
    assert [row["category"] for row in merged] == ["commercial", "commercial", "procedure", "procedure"]
    assert all(row["contract_scope_citations"][0]["block_ids"] == ["heading", "intro"] and not row["attachment_keywords"]
               for row in merged)
    assert all(f"违约金为{value}万元。" in item["text"] for value, item in zip([2, 4, 6, 10], merged, strict=True))
    removed = {key: value for key, value in successor.items() if key != numbered.RESOLUTION_KEY}
    with pytest.raises(ext.PlanningError, match="EXTRACTION_CONTRACT_ANNEX_NUMBERED_ITEMS_MISMATCH"):
        ext._close_deferred_context([window, following], [result, removed])


@pytest.mark.parametrize("change", ["omitted", "partial", "combined", "duplicate", "bid_attachment", "typing"])
def test_successor_requires_actual_complete_eligible_selection(scenario, change):
    reviewed, (_, following) = bind(scenario)
    raw = response(following, [["e", "f"], ["g", "h"]])
    if change == "omitted":
        raw["requirements"].pop()
    elif change == "partial":
        raw["requirements"][1]["citation"]["block_ids"] = ["g"]
    elif change == "combined":
        raw["requirements"][0]["citation"]["block_ids"] += ["g", "h"]
    elif change == "duplicate":
        raw["requirements"].append(raw["requirements"][1])
    elif change == "bid_attachment":
        raw["requirements"][1]["attachment_keywords"] = ["施工记录"]
    else:
        raw["requirements"][1]["category"] = "qualification"
    with pytest.raises(ext.PlanningError, match=numbered.CANDIDATE_ERROR) as error:
        execute(following, reviewed, raw)
    assert "block_ids" in error.value.diagnostic_context


@pytest.mark.parametrize("change", ["no_end_boundary", "truncated_preamble", "split_owner", "jumped_item"])
def test_invalid_native_geometry_or_numbering_cannot_authorize_a_split(scenario, change):
    _, (window, following), blocks, _, _ = scenario
    if change == "no_end_boundary":
        blocks["boundary"]["text"] = "乙方责任追究"
    elif change == "truncated_preamble":
        blocks["intro"]["text"] = "因乙方原因发生下列情形，"
    elif change == "split_owner":
        window["blocks"].append(following["blocks"].pop(0))
    else:
        blocks["g"]["text"] = blocks["g"]["text"].replace("（4）", "（5）")
    with pytest.raises(ValueError, match="CONTRACT_ANNEX_(?:NUMBERED_ITEMS|LIST)_SOURCE_MISMATCH"):
        bind(scenario)
