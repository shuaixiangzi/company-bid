import copy

import pytest

from company_bid.extraction import (
    RESPONSE_CELL_REFLOW_CONFIG_KEY,
    RESPONSE_LETTER_CONFIG_KEY,
    PlanningError,
    _merge_results,
    reviewed_authority_tail,
)
from company_bid.response_authority_tail import authority_tail_proof
from company_bid.storage import digest


@pytest.fixture
def authority_form():
    page = 70
    source_id = "synthetic-tender"
    title = "97. 授权委托书"
    rows = [["法人正面（示意）", "委托代理人正面（示意）"], ["法人反面（示意）", "委托代理人反面（示意）"]]
    paragraphs = [
        title, "法定代表人授权委托书", "本段在前一窗。", "合成授权事项。", "合成授权期限。",
        "合成权限说明。", "合成签署提示。", "证件粘贴区如下。", "证件粘贴区",
        "响应人：____（盖单位章）", "法定代表人：____（签字或盖章）",
        "委托代理人：____（签字或盖章）", "年 月 日", "51",
    ]
    locators = [f"line[{index}]" for index in range(8)] + ["table[0]"] + [f"line[{index}]" for index in range(8, 13)]
    blocks = [{"block_id": f"synthetic-{index}", "source_id": source_id, "origin": "native", "page": page,
               "unit_id": "synthetic-page", "unit_ordinal": index, "ordinal": 1000 + index,
               "locator": f"pdf/page[{page}]/{locator}", "text": text, "kind": "paragraph"}
              for index, (locator, text) in enumerate(zip(locators, paragraphs, strict=True))]
    blocks[8].update(kind="table", rows=rows, merges=[])
    next_heading = {"block_id": "synthetic-next", "page": page + 1, "source_id": source_id, "origin": "native",
                    "locator": f"pdf/page[{page + 1}]/line[0]", "ordinal": 1000 + len(blocks), "text": "98. 企业基本概况"}
    footer = blocks[-1]
    window = {"window_id": "synthetic-tail", "source_id": source_id, "source_format": "pdf",
              "source_sha256": "1" * 64, "adjacent_context": blocks[:3], "blocks": [*blocks[3:], next_heading],
              "safe_skip_blocks": [{"block_id": footer["block_id"], "rule": "pdf-page-footer-v1",
                                    "printed_page": 51, "locator": footer["locator"]}]}
    tail_ids = [block["block_id"] for block in blocks[3:-1]]
    raw = {"format_chapters": [{"title": title, "level": 2, "status": "prescribed", "citation": {"block_ids": tail_ids},
                                "content_block_ids": tail_ids, "requirement_indices": [0]}]}
    return window, raw


def test_authority_proof_binds_owner_tail_footer_and_following_heading(authority_form):
    window, raw = authority_form
    head = window["adjacent_context"]
    tail = window["blocks"][:-2]
    footer, following = window["blocks"][-2:]
    proof = reviewed_authority_tail(window, raw)
    assert proof["rule"] == "native_authorization_form_tail_v1"
    assert proof["owner_ids"] == [block["block_id"] for block in head]
    assert proof["tail_ids"] == [block["block_id"] for block in tail]
    assert proof["physical_page"] == head[0]["page"]
    assert proof["footer_id"] == footer["block_id"]
    assert proof["following_heading_id"] == following["block_id"]
    assert proof["source_block_digests"] == [digest(block) for block in [*head, *tail, footer, following]]
    assert proof["proof_sha256"] == digest({key: value for key, value in proof.items() if key != "proof_sha256"})


@pytest.mark.parametrize(("path", "replacement"), [
    (("blocks", 0, "page"), 71),
    (("blocks", 11, "page"), 72),
    (("blocks", 11, "text"), "98. 合成其他附件"),
    (("blocks", 6, "text"), "响应人：这条为填表说明。"),
    (("blocks", 0, "block_id"), "unexpected-content-block"),
    (("blocks", 5, "rows", 1, 0), "法人正面（错误示意）"),
    (("blocks", 5, "merges"), [{"row": 0, "column": 0, "rowspan": 2, "colspan": 1}]),
    (("safe_skip_blocks", 0, "printed_page"), 50),
    (("safe_skip_blocks", 0, "rule"), "incorrect-footer-rule"),
    (("adjacent_context", 0, "locator"), "pdf/page[69]/line[0]"),
    (("adjacent_context", 1, "source_id"), "different-tender"),
    (("adjacent_context", 1, "block_id"), "synthetic-0"),
])
def test_authority_proof_rejects_scope_mutations(authority_form, path, replacement):
    window, raw = authority_form
    parent = window
    for field in path[:-1]:
        parent = parent[field]
    parent[path[-1]] = replacement
    assert authority_tail_proof(window, raw) is None
    with pytest.raises(PlanningError, match="RESPONSE_AUTHORITY_TAIL_SOURCE_MISMATCH"):
        reviewed_authority_tail(window, raw)


@pytest.mark.parametrize("target", ["following_heading", "printed_footer", "owner_heading"])
def test_authority_proof_rejects_blocks_outside_its_tail(authority_form, target):
    window, raw = authority_form
    replacement = {"following_heading": window["blocks"][-1], "printed_footer": window["blocks"][-2],
                   "owner_heading": window["adjacent_context"][0]}[target]
    chapter = raw["format_chapters"][0]
    chapter["content_block_ids"][-1] = replacement["block_id"]
    assert authority_tail_proof(window, raw) is None


def test_authority_proof_rejects_duplicate_footer_disposition(authority_form):
    window, raw = authority_form
    dispositions = window["safe_skip_blocks"]
    dispositions.append(copy.deepcopy(dispositions[0]))
    assert authority_tail_proof(window, raw) is None


@pytest.fixture
def authority_merge(authority_form):
    window, raw = authority_form
    proof = reviewed_authority_tail(window, raw)

    def chapter(ids):
        return {"title": proof["title"], "level": 2, "status": "prescribed", "content_block_ids": ids,
                "citation": {"block_ids": ids, "source_id": proof["source_id"]}, "requirement_indices": [0]}

    owner = {"window_id": "synthetic-owner", RESPONSE_LETTER_CONFIG_KEY: {"format_index": 1},
             "validated": {"requirements": [{"citation": {"quote": "前窗合成要求"}}],
                           "format_chapters": [chapter(proof["owner_ids"])]}}
    tail = {"window_id": window["window_id"], RESPONSE_CELL_REFLOW_CONFIG_KEY: {"authority_tail": proof},
            "validated": {"requirements": [{"citation": {"quote": "续文合成要求"}}],
                          "format_chapters": [chapter(proof["tail_ids"])]}}
    return owner, tail, proof


def test_authority_merge_keeps_one_chapter_in_source_order(authority_merge):
    owner, tail, proof = authority_merge
    before = copy.deepcopy([owner, tail])
    requirements, formats = _merge_results([owner, tail])
    result, = formats
    assert result["content_block_ids"] == proof["owner_ids"] + proof["tail_ids"]
    assert result["citation"] == owner["validated"]["format_chapters"][0]["citation"]
    assert result["requirement_refs"] == [requirement["requirement_id"] for requirement in requirements]
    assert result["source_continuation_derivation"] == proof
    assert [owner, tail] == before


def test_authority_merge_rejects_absent_or_wrong_owner_checkpoint(authority_merge):
    owner, tail, _ = authority_merge
    error = "RESPONSE_AUTHORITY_TAIL_OWNER_CHECKPOINT_MISMATCH"
    with pytest.raises(PlanningError, match=error):
        _merge_results([tail])
    with pytest.raises(PlanningError, match=error):
        _merge_results([{key: value for key, value in owner.items() if key != RESPONSE_LETTER_CONFIG_KEY}, tail])
    owner["validated"]["format_chapters"][0]["citation"]["source_id"] = "different-tender"
    with pytest.raises(PlanningError, match=error):
        _merge_results([owner, tail])
