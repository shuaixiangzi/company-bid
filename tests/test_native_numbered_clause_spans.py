from __future__ import annotations

import copy
import hashlib

import pytest

from company_bid import native_numbered_clause_spans as spans


def _block(index, text, *, page=8, ordinal=None):
    return {"block_id": f"B{page}-{index}", "locator": f"pdf/page[{page}]/line[{index}]", "page": page,
            "ordinal": index if ordinal is None else ordinal, "text": text, "source_id": "S",
            "unit_id": f"page-{page}", "kind": "paragraph", "origin": "native"}


def _line(index, text, start, baseline):
    size = 12
    chars = []
    for n, letter in enumerate(text):
        latin = letter in "1234567890."
        top = 800 - baseline + (9.9 if latin else 0)
        x0 = start + n * size
        chars.append({"text": letter, "fontname": "Number" if latin else "Body", "size": size,
                      "matrix": (1, 0, 0, 1, x0, baseline), "upright": True,
                      "x0": x0, "x1": x0 + size, "top": top, "bottom": top + size})
    bbox = [start, min(char["top"] for char in chars), chars[-1]["x1"], max(char["bottom"] for char in chars)]
    return index, text, bbox, chars, {}


@pytest.fixture
def reviewed():
    parent = _block(32, "5.3.1 安全生产禁令", page=7, ordinal=0)
    previous = "电。 5.3.1.8 严禁未核实现场条件开展工"
    left = "程。 5.3.1.9 严禁以包代管或变相以包"
    blocks = [_block(0, "5.3.1.6 严禁私自拆除围挡。", ordinal=1),
              _block(1, "5.3.1.7 严禁无票使用临时用", ordinal=2), _block(2, previous, ordinal=3),
              _block(3, left, ordinal=4), _block(4, "代管现场。", ordinal=5),
              _block(5, "5.3.1.10 严禁擅自停工。", ordinal=6)]
    window = {"source_format": "pdf", "source_sha256": "1" * 64, "source_id": "S", "window_id": "W",
              "blocks": blocks, "adjacent_context": [parent]}
    return window, blocks, parent


def _resolve(window, blocks, parent):
    return spans.reviewed_numbered_clause_spans(window, blocks, parent=parent, before_id=blocks[0]["block_id"],
                                                after_id=blocks[-1]["block_id"])


def test_multilevel_cross_line_items_keep_exact_terminals_and_common_parent(reviewed):
    window, blocks, parent = reviewed
    clauses = _resolve(window, blocks, parent)
    assert [item["boundary_proof"]["clause_number"] for item in clauses] == ["5.3.1.7", "5.3.1.8", "5.3.1.9"]
    assert [item["quote"] for item in clauses] == ["5.3.1.7 严禁无票使用临时用\n电。",
                                                   "5.3.1.8 严禁未核实现场条件开展工\n程。",
                                                   "5.3.1.9 严禁以包代管或变相以包\n代管现场。"]
    for clause in clauses:
        proof = clause["boundary_proof"]
        assert proof["parent_number"] == "5.3.1" and proof["parent_heading"]["block_id"] == parent["block_id"]
        base = "\n".join(block["text"] for block in blocks if block["block_id"] in clause["block_ids"])
        assert base[clause["start"]:clause["end"]] == clause["quote"]


@pytest.mark.parametrize("fault", ["missing_sibling", "wrong_parent", "cross_page", "unclosed", "table", "wrong_frozen_text"])
def test_unproved_boundaries_do_not_create_a_exact_citation(reviewed, fault):
    window, blocks, parent = reviewed
    if fault == "missing_sibling":
        blocks[2]["text"] = blocks[2]["text"].replace("5.3.1.8", "5.3.1.11")
    elif fault == "wrong_parent":
        parent["text"] = "5.3.2 工程管理禁令"
    elif fault == "cross_page":
        blocks[4].update(page=9, locator="pdf/page[9]/line[4]", unit_id="page-9")
    elif fault == "unclosed":
        blocks[2]["text"] = blocks[2]["text"].replace("电。", "电（")
    elif fault == "table":
        blocks[4]["kind"] = "table"
    else:
        blocks = copy.deepcopy(blocks)
        blocks[2]["text"] += "他处文本"
    with pytest.raises(ValueError, match=spans.SOURCE_ERROR):
        _resolve(window, blocks, parent)


def _label_projection(reviewed):
    window, blocks, parent = reviewed
    blocks[3]["text"] = blocks[3]["text"].replace("以包代管或变相", "变相")
    clause = _resolve(window, blocks, parent)[-1]
    left, prior, right = blocks[3]["text"], blocks[2]["text"], blocks[4]["text"]
    lines = [_line(0, blocks[0]["text"], 90, 302), _line(1, blocks[1]["text"], 90, 279),
             _line(2, prior, 90 + (len(left) - len(prior)) * 12, 256),
             _line(3, left, 90, 233), _line(4, right, 90, 210)]
    return window, clause, lines, left + "\n" + right


def test_mixed_number_glyphs_ground_both_original_and_exact_citations(reviewed):
    window, clause, lines, quote = _label_projection(reviewed)
    boundary = spans.reviewed_numbered_label_boundary(window, lines, clause, label="以包代管")
    original = {"source_id": "S", "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "block_ids": clause["block_ids"], "basis": "consecutive_blocks_lf_v1"}
    focused = {**original, "quote": clause["quote"], "quote_sha256": clause["quote_sha256"],
               "basis": "frozen_clause_span_v1", "clause_id": clause["clause_id"],
               "clause_proof_sha256": clause["proof_sha256"]}
    for citation in (original, focused):
        checked = spans.reviewed_numbered_label_grounding(boundary, citation, candidate_index=5, label_index=1)
        joined = "".join(citation["quote"][a:b] for a, b in checked["grounding"]["matches"][0]["quote_spans"])
        assert joined == checked["label"] == "以包代管"
        assert checked["label_index"] == 1 and checked["candidate_index"] == 5


@pytest.mark.parametrize("fault", ["short_column", "different_font", "different_codepoint", "different_source_quote"])
def test_label_join_requires_exact_adjacent_glyphs(reviewed, fault):
    window, clause, lines, _ = _label_projection(reviewed)
    if fault == "short_column":
        lines[-1] = _line(4, lines[-1][1], 140, 210)
    elif fault == "different_font":
        lines[-1][3][0]["fontname"] = "Unexpected"
    elif fault == "different_codepoint":
        lines[-1][3][0]["text"] = "待"
    else:
        lines[-1] = _line(4, "代管其他事项。", 90, 210)
    with pytest.raises(ValueError, match=spans.SOURCE_ERROR):
        spans.reviewed_numbered_label_boundary(window, lines, clause, label="以包代管")
