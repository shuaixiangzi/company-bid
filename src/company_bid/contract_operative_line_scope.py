"""Bind reviewed HSE performance roles and a mixed-font paragraph label."""
from __future__ import annotations

import copy
import hashlib
import re

from .native_reviewed_label_wrap import _CJK, _boundary_glyphs, _crossings, _glyph, _visible
from .pdf_text_lines import horizontal_matrix
from .source_clauses import _adjacent, _identity, _native
from .storage import digest


SOURCE_ERROR = "REVIEWED_HSE_OPERATIVE_SCOPE_MISMATCH"
PERFORMANCE_RULE = "reviewed_hse_contractor_performance_conditions_v1"
LABEL_WRAP_RULE = "reviewed_hse_numbered_paragraph_label_wrap_v1"
_ANNEX = re.compile(r"附件\s*[1-9]\d*\s*[：:]\s*健康、安全与环境[（(]\s*HSE\s*[）)]管理条款(?:【[^】]+】)?")
_GOVERNING = re.compile(r"([1-9]\d*)[.．]\s*对乙方\s*HSE\s*总体要求")
_NUMBERED = re.compile(r"([1-9]\d*)[.．]([1-9]\d*)(?![\d.．])\s*\S")
_SUBMISSION = re.compile(r"投标|响应|报价|报名|截止|资格(?:审查|预审)|随附|递交|提交|证明材料")


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _native_paragraph(window, blocks):
    targets = {block["block_id"]: block for block in window["blocks"]}
    keys = ("block_id", "locator", "text", "source_id", "unit_id", "page", "ordinal", "origin", "kind")
    _require(window["source_format"] == "pdf" and 2 <= len(blocks) <= 12 and
             all(_native(block) and not block.get("read_disposition") and block["source_id"] == window["source_id"] and
                 block["block_id"] in targets and all(targets[block["block_id"]].get(key) == block.get(key) for key in keys)
                 for block in blocks) and all(_adjacent(left, right) for left, right in zip(blocks, blocks[1:])))


def _paragraph_number(blocks, following):
    start = _NUMBERED.match(blocks[0]["text"])
    end = _NUMBERED.match(following["text"])
    _require(start and end and start[1] == end[1] and int(start[2]) + 1 == int(end[2]) and
             not any(_NUMBERED.match(block["text"]) for block in blocks[1:]) and
             blocks[-1]["text"].endswith("。"))
    return f"{start[1]}.{start[2]}", start[1]


def reviewed_hse_performance_role(window, clause_blocks, *, annex_heading, governing_heading, following, labels):
    """Prove that the complete conditions apply to the named contract performer."""
    _native_paragraph(window, [*clause_blocks, following])
    number, section = _paragraph_number(clause_blocks, following)
    headings = (annex_heading, governing_heading)
    authority = _GOVERNING.fullmatch(governing_heading["text"])
    _require(all(_native(block) and not block.get("read_disposition") and block["source_id"] == window["source_id"]
                 for block in headings) and _ANNEX.fullmatch(annex_heading["text"]) and authority and authority[1] == section and
             annex_heading["ordinal"] < governing_heading["ordinal"] < clause_blocks[0]["ordinal"])
    quote = "\n".join(block["text"] for block in clause_blocks)
    continuous = re.sub(r"\s+", "", quote)
    _require(re.match(r"[1-9]\d*[.．][1-9]\d*乙方应具备《", continuous) and
             "标准规定的安全生产、环境保护、职业卫生的生产和生活条件" in continuous and
             not _SUBMISSION.search(quote) and labels == ["专业人员", "资质", "技术装备"] and
             continuous.endswith("；具备与本项目相适应的" + "、".join(labels) + "。"))
    proof = {"rule": PERFORMANCE_RULE, "stage": "contract_performance", "source_id": window["source_id"],
             "source_sha256": window["source_sha256"], "window_id": window["window_id"], "clause_number": number,
             "annex_heading": _identity(annex_heading), "governing_heading": _identity(governing_heading),
             "first_block": _identity(clause_blocks[0]), "following_sibling": _identity(following),
             "block_ids": [block["block_id"] for block in clause_blocks], "quote_sha256": _sha(quote),
             "original_fact_labels": list(labels), "actor": "contractor", "capability_status": "not_checked"}
    return proof | {"proof_sha256": digest(proof)}


def project_hse_performance_role(proof, original):
    """Keep the source obligation and retire only the inferred bid fact route."""
    citation = original["citation"]
    _require(proof["rule"] == PERFORMANCE_RULE and
             digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"] and
             (original["category"], original["response_mode"], original["mandatory"], original["format_status"],
              original["format_title"]) == ("qualification", "factual_response", True, "not_prescribed", None) and
             original["required_fact_labels"] == proof["original_fact_labels"] and
             not original["attachment_keywords"] and not original["reference_keywords"] and
             (citation["source_id"], citation["block_ids"], citation["block_id"], citation["locator"]) ==
             (proof["source_id"], proof["block_ids"], proof["first_block"]["block_id"], proof["first_block"]["locator"]) and
             citation["quote_sha256"] == proof["quote_sha256"] == _sha(citation["quote"]))
    result = copy.deepcopy(original)
    result.update(category="commercial", response_mode="procedure", required_fact_labels=[],
                  contract_evidence_scope={key: proof[key] for key in ("stage", "rule", "actor", "capability_status", "proof_sha256")})
    return result


def _paragraph_wrap_glyphs(native, left_pos, label, split):
    opening, left, right, following = native[0], native[left_pos], native[left_pos + 1], native[-1]
    tail = _boundary_glyphs(_visible(left[3]), tail=True)
    head = _boundary_glyphs(_visible(right[3]), tail=False)
    last, first = tail[-1], head[0]
    size = last["size"]
    spacing = last["matrix"][5] - first["matrix"][5]
    mixed = [char for char in _visible(left[3]) if re.fullmatch(r"[A-Za-z]", char["text"]) and
             horizontal_matrix(char) is not None and abs(char["matrix"][5] - last["matrix"][5]) <= .15 and
             abs(char["size"] - size) < .05 and char["fontname"] != last["fontname"] and
             char["bottom"] > last["bottom"] + .5 * size]
    _require(mixed and last["fontname"] == first["fontname"] and abs(first["size"] - size) < .05 and
             size <= spacing <= 2.5 * size and abs(left[2][0] - right[2][0]) <= .15 * size and
             all(abs(line[2][0] - right[2][0]) <= .15 * size for line in native[1:-1]) and
             all(1.5 * size <= line[2][0] - right[2][0] <= 3 * size for line in (opening, following)) and
             abs(last["x1"] - opening[2][2]) <= size and last["x1"] - right[2][0] >= 10 * size and
             abs(last["x1"] - left[2][2]) <= .05 * size and abs(first["x0"] - right[2][0]) <= .05 * size and
             right[2][2] < last["x1"] - size and
             "".join(char["text"] for char in tail[-split:]) == label[:split] and
             "".join(char["text"] for char in head[:len(label) - split]) == label[split:])
    return {"left_glyphs": [_glyph(char) for char in tail[-split:]],
            "right_glyphs": [_glyph(char) for char in head[:len(label) - split]],
            "mixed_glyphs": [_glyph(char) for char in mixed]}


def reviewed_hse_paragraph_label_boundary(window, page_lines, *, expected_blocks, label):
    """Locate a unique literal source LF within the complete numbered paragraph."""
    try:
        last = expected_blocks[-1]
        following, = [block for block in window["blocks"] if _native(block) and _adjacent(last, block)]
        _native_paragraph(window, [*expected_blocks, following])
        number, _ = _paragraph_number(expected_blocks, following)
        quote = "\n".join(block["text"] for block in expected_blocks)
        _require(2 <= len(label) <= 32 and _CJK.fullmatch(label) and label not in quote)
        (offset, split), = _crossings(label, quote)
        left_pos = quote[:offset].count("\n")
        _require(0 < left_pos == len(expected_blocks) - 2 and
                 not any(edge["blocks"] == [item["block_id"] for item in expected_blocks[left_pos:left_pos + 2]]
                         for edge in window.get("native_pdf_line_edges", [])))
        index = int(expected_blocks[0]["locator"].rsplit("[", 1)[1].rstrip("]"))
        native = page_lines[index:index + len(expected_blocks) + 1]
        _require([line[:2] for line in native] == [(index + n, block["text"])
                 for n, block in enumerate([*expected_blocks, following])])
        glyphs = _paragraph_wrap_glyphs(native, left_pos, label, split)
        proof = {"rule": LABEL_WRAP_RULE, "clause_number": number, "source_id": window["source_id"],
                 "source_sha256": window["source_sha256"], "window_id": window["window_id"], "label": label,
                 "block_ids": [block["block_id"] for block in expected_blocks], "quote_sha256": _sha(quote),
                 "following_sibling": _identity(following), "offset": offset, "split_at": split, **glyphs}
        return proof | {"proof_sha256": digest(proof)}
    except (IndexError, KeyError, TypeError, ValueError):
        raise ValueError(SOURCE_ERROR) from None


def reviewed_hse_paragraph_label_grounding(proof, citation, *, candidate_index, label_index):
    """Return strict label grounding compatible with the original validator."""
    label = proof["label"]
    quote = citation["quote"]
    (offset, split), = _crossings(label, quote)
    _require(proof["rule"] == LABEL_WRAP_RULE and citation["source_id"] == proof["source_id"] and
             citation["block_ids"] == proof["block_ids"] and citation["basis"] == "consecutive_blocks_lf_v1" and
             citation["quote_sha256"] == proof["quote_sha256"] == _sha(quote) and
             (offset, split) == (proof["offset"], proof["split_at"]) and
             digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"])
    field = "reference_keywords"
    return {"candidate_index": candidate_index, "label_index": label_index, "field": field, "label": label,
            "quote_sha256": proof["quote_sha256"], "block_ids": proof["block_ids"],
            "grounding": {"field": field, "label": label, "basis": LABEL_WRAP_RULE,
                          "matches": [{"quote_spans": [[offset - split, offset], [offset + 1, offset + 1 + len(label) - split]],
                                       "edge_sha256s": [proof["proof_sha256"]]}]}}
