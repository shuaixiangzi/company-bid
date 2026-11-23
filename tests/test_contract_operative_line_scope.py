from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid import contract_operative_line_scope as scope
from company_bid.material_matching import _proof_request


def _block(index, text, *, page=9, ordinal=None):
    return {"block_id": f"B{page}-{index}", "locator": f"pdf/page[{page}]/line[{index}]", "page": page,
            "ordinal": index if ordinal is None else ordinal, "text": text, "source_id": "S",
            "unit_id": f"page-{page}", "kind": "paragraph", "origin": "native"}


def _window(blocks):
    return {"source_format": "pdf", "source_sha256": "1" * 64, "source_id": "S", "window_id": "W",
            "blocks": blocks}


@pytest.fixture
def performance():
    annex = _block(0, "附件9：健康、安全与环境（HSE）管理条款", page=7, ordinal=0)
    heading = _block(0, "5.对乙方 HSE 总体要求", page=8, ordinal=1)
    lines = [_block(0, "5.4 乙方应具备《适用规范》等有关标准规定的安全生产、环境保护、", ordinal=2),
             _block(1, "职业卫生的生产和生活条件；具备与本项目相适应的专业人员、资质、技术装备。", ordinal=3)]
    following = _block(2, "5.5 乙方制定现场作业指导书。", ordinal=4)
    window = _window([*lines, following])
    labels = ["专业人员", "资质", "技术装备"]
    quote = "\n".join(block["text"] for block in lines)
    citation = {"source_id": "S", "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "block_id": lines[0]["block_id"], "block_ids": [block["block_id"] for block in lines],
                "locator": lines[0]["locator"]}
    candidate = {"citation": citation, "category": "qualification", "response_mode": "factual_response",
                 "mandatory": True, "format_status": "not_prescribed", "format_title": None,
                 "required_fact_labels": labels, "attachment_keywords": [], "reference_keywords": []}
    proof_args = {"annex_heading": annex, "governing_heading": heading, "following": following, "labels": labels}
    return window, lines, candidate, proof_args


def test_keeps_contract_conditions_and_routes_only_reviewed_bid_fact_labels(performance):
    window, lines, candidate, proof_args = performance
    original = copy.deepcopy(candidate)
    proof = scope.reviewed_hse_performance_role(window, lines, **proof_args)
    result = scope.project_hse_performance_role(proof, candidate)
    assert result["category"] == "commercial" and result["response_mode"] == "procedure"
    assert not result["required_fact_labels"] and not result["attachment_keywords"]
    assert result["citation"] == original["citation"] and candidate == original
    assert result["contract_evidence_scope"]["capability_status"] == "not_checked"
    assert proof["original_fact_labels"] == original["required_fact_labels"]
    request = _proof_request({**result, "text": result["citation"]["quote"]})
    assert request["rule"] == "contract_performance_evidence_for_counterparty" and not request["kinds"]
    actual_bid = {"response_mode": "attachment", "text": "投标人须提供营业执照扫描件。"}
    assert _proof_request(actual_bid)["rule"] != request["rule"]


@pytest.mark.parametrize("fault", ["bid_stage", "different_actor", "missing_line", "unrelated_heading", "unrelated_label"])
def test_refuses_to_reroute_bid_stage_or_incomplete_source(performance, fault):
    window, lines, _, proof_args = performance
    if fault == "bid_stage":
        lines[0]["text"] += "投标时另需提交证明材料。"
    elif fault == "different_actor":
        lines[0]["text"] = lines[0]["text"].replace("乙方", "甲方")
    elif fault == "missing_line":
        lines.pop()
    elif fault == "unrelated_heading":
        proof_args["annex_heading"]["text"] = "第四章 资格材料"
    else:
        proof_args["labels"] = ["专业人员", "特种资质", "技术装备"]
    with pytest.raises(ValueError, match=scope.SOURCE_ERROR):
        scope.reviewed_hse_performance_role(window, lines, **proof_args)


def _line(index, text, start, baseline, *, right=None):
    size = 12
    step = (right - start) / len(text) if right is not None else size
    chars = []
    for n, letter in enumerate(text):
        latin = letter.isascii() and letter != " "
        top = 800 - baseline + (9.9 if latin else 0)
        x0 = start + n * step
        chars.append({"text": letter, "fontname": "Latin" if latin else "CJK", "size": size,
                      "matrix": (1, 0, 0, 1, x0, baseline), "upright": True,
                      "x0": x0, "x1": x0 + step, "top": top, "bottom": top + size})
    bbox = [start, min(char["top"] for char in chars), chars[-1]["x1"], max(char["bottom"] for char in chars)]
    return index, text, bbox, chars, {}


@pytest.fixture
def wrap():
    opening = "5.9 乙方实施现场培训并保持相应记录，"
    previous = "培训应覆盖适用条件并讲解所有项目资料，"
    left = "按规定核对HSE 计划和工地培训要求及应急"
    right = "预案并登记。"
    following = "5.10 乙方办理后续手续。"
    source = [opening, previous, left, right, following]
    blocks = [_block(i, text) for i, text in enumerate(source)]
    right_margin = 90 + len(left) * 12
    glyphs = [_line(0, opening, 114, 302, right=right_margin), _line(1, previous, 90, 279),
              _line(2, left, 90, 256), _line(3, right, 90, 233), _line(4, following, 114, 210)]
    return _window(blocks), blocks, glyphs


def test_full_numbered_paragraph_supports_scoped_glyph_wrap(wrap):
    window, blocks, glyphs = wrap
    scope_input = blocks[:-1]
    proof = scope.reviewed_hse_paragraph_label_boundary(window, glyphs, expected_blocks=scope_input, label="应急预案")
    quote = "\n".join(block["text"] for block in scope_input)
    citation = {"source_id": "S", "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "block_ids": [block["block_id"] for block in scope_input], "basis": "consecutive_blocks_lf_v1"}
    grounding = scope.reviewed_hse_paragraph_label_grounding(proof, citation, candidate_index=13, label_index=3)
    (a, b), (c, d) = grounding["grounding"]["matches"][0]["quote_spans"]
    assert quote[a:b] + quote[c:d] == grounding["label"] == "应急预案" and quote[b:c] == "\n"
    assert grounding["candidate_index"] == 13 and grounding["label_index"] == 3


@pytest.mark.parametrize("fault", ["wrong_word", "cross_clause", "glyph_missing", "new_column"])
def test_refuses_wrap_evidence_from_another_word_clause_or_column(wrap, fault):
    window, blocks, glyphs = wrap
    label = "应急预案"
    if fault == "wrong_word":
        label = "应急方案"
    elif fault == "cross_clause":
        blocks[3]["text"] = "5.10 预案编制完毕。"
        glyphs[3] = _line(3, blocks[3]["text"], 90, 233)
    elif fault == "glyph_missing":
        glyphs[3][3][0]["text"] = "计"
    else:
        glyphs[3] = _line(3, blocks[3]["text"], 140, 233)
    with pytest.raises(ValueError, match=scope.SOURCE_ERROR):
        scope.reviewed_hse_paragraph_label_boundary(window, glyphs, expected_blocks=blocks[:-1], label=label)
