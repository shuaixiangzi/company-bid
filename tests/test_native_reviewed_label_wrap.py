from __future__ import annotations

import copy

import pytest

from company_bid import native_reviewed_label_wrap as wrap
from company_bid.contract_attachment_citation_split import _member
from company_bid.storage import digest


def _line(index, text, start, baseline):
    size = 12
    chars = []
    for offset, letter in enumerate(text):
        latin = letter.isascii()
        top = 800 - baseline + (9.9 if latin else 0)
        x0 = start + offset * size
        chars.append({"text": letter, "fontname": "Latin" if latin else "BodyCJK", "size": size,
                      "matrix": (1, 0, 0, 1, x0, baseline), "upright": True,
                      "x0": x0, "x1": x0 + size, "top": top, "bottom": top + size})
    bbox = [start, min(char["top"] for char in chars), chars[-1]["x1"], max(char["bottom"] for char in chars)]
    native = {"block_id": f"B{index}", "source_id": "source", "unit_id": "page", "page": 4,
              "locator": f"pdf/page[4]/line[{index}]", "ordinal": index, "text": text, "bbox": bbox}
    return (index, text, bbox, chars, {}), native


@pytest.fixture
def reviewed():
    left = "预防为主持续改进HSE方案保障安全生"
    previous = ("我方依据制度严格落实作业管理规定认真执行"[:len(left) - 3] + "、")
    lines, native = map(list, zip(_line(0, previous, 114, 200), _line(1, left, 90, 177),
                                 _line(2, "产责任制落实到位。", 90, 154), _line(3, "5.3 下一条", 114, 131), strict=True))
    requirement = {"response_mode": "writing", "reference_keywords": ["安全生产责任制"]}
    raw = {"requirements": [requirement]}
    scope = {"source_id": "source", "source_sha256": "1" * 64, "window_id": "window",
             "physical_page": 4, "rendered_rgb_sha256": "2" * 64,
             "raw_candidate_sha256": digest(raw), "candidate_index": 0, "field": "reference_keywords",
             "label_index": 0, "left_block_id": "B1", "right_block_id": "B2"}
    boundary = wrap._prove_boundary(lines, native, 1)
    proof = {"rule": wrap.RULE, "scope": scope, "native_coverage_sha256": "3" * 64,
             "left": _member(native[1]), "right": _member(native[2]), "boundary": boundary}
    window = {key: scope[key] for key in ("source_id", "source_sha256", "window_id")}
    window["blocks"] = native
    window[wrap.WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    quote = "\n".join(block["text"] for block in native[:3])
    citation = {"source_id": "source", "quote": quote, "quote_sha256": digest(quote),
                "block_ids": ["B0", "B1", "B2"], "basis": "consecutive_blocks_lf_v1"}
    return lines, native, raw, window, citation, scope


def test_mixed_font_geometry_binds_a_unique_source_label_without_editing_it(reviewed):
    _, _, raw, window, citation, _ = reviewed
    resolution = wrap.resolve_reviewed_label_wrap(raw, raw["requirements"][0], window, citation)
    label = resolution["label"]
    match, = resolution["grounding"]["matches"]
    (a, b), (c, d) = match["quote_spans"]
    assert citation["quote"][a:b] + citation["quote"][c:d] == label == "安全生产责任制"
    assert citation["quote"][b:c] == "\n"
    assert resolution["model_candidate_mutation"] is False
    assert digest(raw) == resolution["raw_candidate_sha256"]


@pytest.mark.parametrize("fault", ["column", "heading", "font", "unwrapped_predecessor"])
def test_refuses_geometry_without_same_natural_paragraph(reviewed, fault):
    lines, native, *_ = reviewed
    if fault == "column":
        lines[2], native[2] = _line(2, native[2]["text"], 140, 154)
    elif fault == "heading":
        lines[2], native[2] = _line(2, "5.3 产责任制。", 90, 154)
    elif fault == "font":
        lines[2][3][0]["fontname"] = "DifferentChineseFont"
    else:
        lines[0], native[0] = _line(0, native[0]["text"][:-1] + "。", 114, 200)
    with pytest.raises(ValueError, match=wrap.SOURCE_ERROR):
        wrap._prove_boundary(lines, native, 1)


@pytest.mark.parametrize("fault", ["edited_raw", "skipped_words", "wrong_window", "wrong_locator", "duplicate_label"])
def test_refuses_mutation_and_ambiguous_source_matching(reviewed, fault):
    _, _, raw, window, citation, scope = reviewed
    requirement = raw["requirements"][0]
    if fault == "edited_raw":
        raw["new_field"] = True
    elif fault == "skipped_words":
        requirement["reference_keywords"] = ["安全责任制"]
        scope["raw_candidate_sha256"] = digest(raw)
    elif fault == "wrong_window":
        window["window_id"] = "elsewhere"
    elif fault == "wrong_locator":
        citation["block_ids"] = ["B0", "B1", "B3"]
    else:
        citation["quote"] += "\n安全生\n产责任制。"
        citation["block_ids"] += ["other_1", "other_2"]
    if fault == "skipped_words":
        proof = window[wrap.WINDOW_CONTEXT_KEY]
        proof["proof_sha256"] = digest({key: value for key, value in proof.items() if key != "proof_sha256"})
    with pytest.raises(ValueError):
        wrap.resolve_reviewed_label_wrap(raw, requirement, window, citation)


def test_scope_accepts_two_distinct_reviewed_windows_with_exact_reference_use(reviewed):
    *_, scope = reviewed
    next_scope = {**scope, "window_id": "next"}
    assert wrap.normalize_reviewed_label_wrap_config(scope) == scope
    assert wrap.normalize_reviewed_label_wrap_config([scope, next_scope]) == [scope, next_scope]
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        wrap.normalize_reviewed_label_wrap_config([scope, copy.deepcopy(scope)])
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        wrap.normalize_reviewed_label_wrap_config({**scope, "field": "attachment_keywords"})


@pytest.fixture
def numbered_item():
    left = "（5）组织事故演练并建立应急救援资源及应急"
    following = "（6）" + "继续执行适用的工程质量要求和管理程序"[:len(left) - 3]
    lines, native = map(list, zip(_line(0, "（4）完整列明风险控制事项。", 114, 200), _line(1, left, 114, 177),
                                 _line(2, "救援体系，及时落实。", 90, 154), _line(3, following, 114, 131), strict=True))
    return lines, native


def test_numbered_item_glyph_proof_and_frozen_legacy_scope_can_coexist(reviewed, numbered_item):
    *_, legacy = reviewed
    lines, native = numbered_item
    requirement = {"response_mode": "writing", "reference_keywords": ["应急救援体系"]}
    raw = {"requirements": [requirement]}
    scope = {**legacy, "window_id": "numbered-item", "raw_candidate_sha256": digest(raw), "list_item_number": 5}
    assert wrap.normalize_reviewed_label_wrap_config([legacy, scope]) == [legacy, scope]
    assert "list_item_number" not in wrap.normalize_reviewed_label_wrap_config(legacy)
    boundary = wrap._prove_numbered_item_boundary(lines, native, 1, 5)
    assert boundary["paragraph_start"]["block_id"] == "B1" and boundary["following_boundary"]["block_id"] == "B3"
    proof = {"rule": wrap.RULE, "scope": scope, "native_coverage_sha256": "3" * 64,
             "left": _member(native[1]), "right": _member(native[2]), "boundary": boundary}
    window = {key: scope[key] for key in ("source_id", "source_sha256", "window_id")}
    window["blocks"] = native
    window[wrap.WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    quote = "\n".join(block["text"] for block in native[1:3])
    citation = {"source_id": "source", "quote": quote, "quote_sha256": digest(quote),
                "block_ids": ["B1", "B2"], "basis": "consecutive_blocks_lf_v1"}
    resolution = wrap.resolve_reviewed_label_wrap(raw, requirement, window, citation)
    (a, b), (c, d) = resolution["grounding"]["matches"][0]["quote_spans"]
    assert quote[a:b] + quote[c:d] == resolution["label"] == "应急救援体系"
    citation["block_ids"] = ["B1", "B2", "B3"]
    citation["quote"] += "\n" + native[3]["text"]
    with pytest.raises(ValueError, match=wrap.SOURCE_ERROR):
        wrap.resolve_reviewed_label_wrap(raw, requirement, window, citation)


@pytest.mark.parametrize("fault", ["nonconsecutive_sibling", "wrong_ordinal", "new_item", "previous_open", "wrong_column"])
def test_numbered_item_requires_same_parent_and_exact_wrap(numbered_item, fault):
    lines, native = numbered_item
    number = 5
    if fault == "nonconsecutive_sibling":
        lines[3], native[3] = _line(3, native[3]["text"].replace("（6）", "（7）"), 114, 131)
    elif fault == "wrong_ordinal":
        number = 6
    elif fault == "new_item":
        lines[2], native[2] = _line(2, "（6）救援体系，及时落实。", 90, 154)
    elif fault == "previous_open":
        lines[0], native[0] = _line(0, native[0]["text"][:-1], 114, 200)
    else:
        lines[2], native[2] = _line(2, native[2]["text"], 151, 154)
    with pytest.raises(ValueError, match=wrap.SOURCE_ERROR):
        wrap._prove_numbered_item_boundary(lines, native, 1, number)
