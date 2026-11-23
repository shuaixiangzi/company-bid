"""Derive exact sibling clauses inside a reviewed native PDF page."""
from __future__ import annotations

import hashlib
import re
import unicodedata

from .native_reviewed_label_wrap import _CJK, _boundary_glyphs, _crossings, _glyph, _visible
from .pdf_text_lines import horizontal_matrix
from .source_clauses import SourceClause, _adjacent, _identity, _local_spans, _native, _not_nested
from .storage import digest


RULE = "reviewed_consecutive_native_multilevel_clauses_v1"
LABEL_WRAP_RULE = "reviewed_native_numbered_clause_label_wrap_v1"
SOURCE_ERROR = "REVIEWED_NUMBERED_CLAUSE_SPAN_MISMATCH"
_NUMBER = re.compile(r"(?<![\d.．])([1-9]\d*(?:[.．][1-9]\d*)+)(?![\d.．])(?=[ \t]*[^\s\d.．])")
_TERMINATORS = ("。", "！", "？", "；", "!", "?", ";")


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _number(tuple_match):
    return tuple(map(int, tuple_match[1].replace("．", ".").split(".")))


def _number_text(parts):
    return ".".join(map(str, parts))


def _source_member(block, available, source_id):
    identity = block["block_id"]
    if identity not in available:
        return False
    observed = available[identity]
    keys = ("block_id", "kind", "origin", "locator", "ordinal", "source_id", "unit_id", "page", "text")
    return (_native(block) and not block.get("read_disposition") and block["source_id"] == source_id and
            all(block.get(key) == observed.get(key) for key in keys) and
            not any(char == "\ufffd" or unicodedata.category(char) in {"Co", "Cc", "Cf", "Cs", "Cn"}
                    for char in block["text"] if char != "\t"))


def _span_catalog(envelope, markers, full_quote, parent, source_id, source_sha256, parent_number):
    ranges, offset = [], 0
    for block in envelope:
        length = len(block["text"])
        ranges.append((offset, offset + length))
        offset += length + 1
    common = {"rule": RULE, "source_sha256": source_sha256, "source_id": source_id,
              "parent_number": _number_text(parent_number), "parent_heading": _identity(parent),
              "previous_sibling": _identity(envelope[0]), "following_sibling": _identity(envelope[-1]),
              "reading_envelope": [_identity(block) for block in envelope],
              "reading_envelope_sha256": _sha(full_quote), "join_basis": "frozen_native_blocks_lf_v1"}
    spans = []
    for marker, following in zip(markers[1:-1], markers[2:], strict=True):
        start = marker.start()
        quote = full_quote[start:following.start()].rstrip()
        _require(quote.endswith(_TERMINATORS))
        parts = _local_spans(envelope, ranges, start, start + len(quote))
        positions = {part["block_id"] for part in parts}
        blocks = [block for block in envelope if block["block_id"] in positions]
        original = "\n".join(block["text"] for block in blocks)
        local_start = parts[0]["start"]
        local_end = local_start + len(quote)
        _require(original[local_start:local_end] == quote and original.count(quote) == 1)
        proof = {**common, "clause_number": _number_text(_number(marker)), "span_parts": parts}
        proof_sha256 = digest(proof)
        quote_sha256 = _sha(quote)
        identity = digest({"source_sha256": source_sha256, "proof_sha256": proof_sha256, "quote_sha256": quote_sha256})
        spans.append(SourceClause(clause_id="CLAUSE-" + identity[:24], source_id=source_id, source_sha256=source_sha256,
            block_id=parts[0]["block_id"], locator=parts[0]["locator"], block_ids=[part["block_id"] for part in parts],
            start=local_start, end=local_end, quote=quote, quote_sha256=quote_sha256,
            parent_quote_sha256=_sha(original), proof_sha256=proof_sha256, boundary_proof=proof).model_dump(mode="json"))
    return spans


def reviewed_numbered_clause_spans(window, page_blocks, *, parent, before_id, after_id):
    """Return only the complete consecutive siblings inside the authenticated anchors."""
    try:
        source_id, source_sha256 = window["source_id"], window["source_sha256"]
        supplied = [*window["blocks"], *window.get("adjacent_context", [])]
        available = {block["block_id"]: block for block in supplied}
        _require(window["source_format"] == "pdf" and len(available) == len(supplied) and
                 _source_member(parent, available, source_id))
        first, = [i for i, block in enumerate(page_blocks) if block["block_id"] == before_id]
        last, = [i for i, block in enumerate(page_blocks) if block["block_id"] == after_id]
        envelope = page_blocks[first:last + 1]
        _require(first < last and len(envelope) <= 24 and
                 all(_source_member(block, available, source_id) for block in envelope) and
                 all(_adjacent(a, b) for a, b in zip(envelope, envelope[1:])) and
                 0 < envelope[0]["ordinal"] - parent["ordinal"] <= 40)
        targets = {block["block_id"] for block in window["blocks"]}
        _require(all(block["block_id"] in targets for block in envelope))
        parent_marker = _NUMBER.match(parent["text"])
        _require(parent_marker and not parent["text"].endswith(_TERMINATORS))
        parent_number = _number(parent_marker)
        full_quote = "\n".join(block["text"] for block in envelope)
        markers = list(_NUMBER.finditer(full_quote))
        marked = [_number(marker) for marker in markers]
        _require(len(markers) >= 3 and markers[0].start() == 0 and
                 markers[-1].start() == len(full_quote) - len(envelope[-1]["text"]) and
                 all(number[:-1] == parent_number for number in marked) and
                 [number[-1] for number in marked] == list(range(marked[0][-1], marked[-1][-1] + 1)) and
                 all(full_quote[:marker.start()].rstrip().endswith(_TERMINATORS) for marker in markers[1:]) and
                 _not_nested(full_quote, markers))
        return _span_catalog(envelope, markers, full_quote, parent, source_id, source_sha256, parent_number)
    except (IndexError, KeyError, TypeError, ValueError):
        raise ValueError(SOURCE_ERROR) from None


def _mixed_number_glyphs(left, right, prior, quote):
    left_chars, right_chars = _visible(left[3]), _visible(right[3])
    tail = _boundary_glyphs(left_chars, tail=True)
    head = _boundary_glyphs(right_chars, tail=False)
    last, first = tail[-1], head[0]
    size = last["size"]
    left_baseline, right_baseline = last["matrix"][5], first["matrix"][5]
    spacing = left_baseline - right_baseline
    number = _NUMBER.match(quote)[1]
    digits = [char for char in left_chars if re.fullmatch(r"[0-9.．]", char["text"])]
    _require("".join(char["text"] for char in digits) == number and
             all(horizontal_matrix(char) is not None and abs(char["matrix"][5] - left_baseline) <= .15 and
                 abs(char["size"] - size) < .05 and char["fontname"] != last["fontname"] and
                 char["bottom"] > last["bottom"] + .5 * size for char in digits) and
             last["fontname"] == first["fontname"] and abs(first["size"] - size) < .05 and
             size <= spacing <= 2.5 * size and abs(left[2][0] - right[2][0]) <= .15 * size and
             abs(last["x1"] - left[2][2]) <= .05 * size and abs(first["x0"] - right[2][0]) <= .05 * size and
             abs(last["x1"] - prior[2][2]) <= size and last["x1"] - right[2][0] >= 10 * size and
             right[2][2] < last["x1"] - size)
    return tail, head, digits


def reviewed_numbered_label_boundary(window, page_lines, clause, *, label):
    """Bind one literal CJK keyword to glyphs on both sides of a native wrap."""
    try:
        _require(2 <= len(label) <= 32 and _CJK.fullmatch(label) and
                 window["source_format"] == "pdf" and clause["source_id"] == window["source_id"] and
                 clause["source_sha256"] == window["source_sha256"] and clause["boundary_proof"]["rule"] == RULE and
                 SourceClause.model_validate(clause).model_dump() == clause)
        by_id = {block["block_id"]: block for block in window["blocks"]}
        left, right = (by_id[unit] for unit in clause["block_ids"])
        _require(_adjacent(left, right) and all(_native(block) and not block.get("read_disposition") for block in (left, right)) and
                 not any(edge["blocks"] == clause["block_ids"] for edge in window.get("native_pdf_line_edges", [])))
        first_part, last_part = clause["boundary_proof"]["span_parts"]
        _require(first_part["start"] > 0 and first_part["end"] == len(left["text"]) and
                 last_part["start"] == 0 and last_part["end"] == len(right["text"]) and
                 left["text"][:first_part["start"]].rstrip().endswith(_TERMINATORS) and
                 right["text"].endswith(_TERMINATORS))
        parent_quote = left["text"] + "\n" + right["text"]
        _require(_sha(parent_quote) == clause["parent_quote_sha256"] and label not in parent_quote and
                 parent_quote[clause["start"]:clause["end"]] == clause["quote"])
        (offset, split), = _crossings(label, parent_quote)
        _require(offset == len(left["text"]) and offset - split >= clause["start"] and
                 offset + len(label) - split < clause["end"])
        left_index = int(left["locator"].rsplit("[", 1)[1].rstrip("]"))
        prior, left_line, right_line = page_lines[left_index - 1:left_index + 2]
        previous, = [block for block in window["blocks"] if _native(block) and _adjacent(block, left)]
        _require(left_index > 0 and prior[:2] == (left_index - 1, previous["text"]) and
                 left_line[:2] == (left_index, left["text"]) and right_line[:2] == (left_index + 1, right["text"]))
        tail, head, digits = _mixed_number_glyphs(left_line, right_line, prior, clause["quote"])
        _require("".join(char["text"] for char in tail[-split:]) == label[:split] and
                 "".join(char["text"] for char in head[:len(label) - split]) == label[split:])
        proof = {"rule": LABEL_WRAP_RULE, "clause_id": clause["clause_id"], "source_id": window["source_id"],
                 "source_sha256": window["source_sha256"], "block_ids": clause["block_ids"], "label": label,
                 "split_at": split, "parent_quote_sha256": clause["parent_quote_sha256"],
                 "focused_quote_sha256": clause["quote_sha256"], "clause_proof_sha256": clause["proof_sha256"],
                 "left_glyphs": [_glyph(char) for char in tail[-split:]],
                 "right_glyphs": [_glyph(char) for char in head[:len(label) - split]],
                 "inline_number_glyphs": [_glyph(char) for char in digits]}
        return proof | {"proof_sha256": digest(proof)}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ValueError(SOURCE_ERROR) from None


def reviewed_numbered_label_grounding(boundary, citation, *, candidate_index, label_index):
    """Prove the identical literal in the original block citation or focused clause."""
    try:
        quote, quote_sha256 = citation["quote"], citation["quote_sha256"]
        _require(boundary["rule"] == LABEL_WRAP_RULE and citation["source_id"] == boundary["source_id"] and
                 citation["block_ids"] == boundary["block_ids"] and _sha(quote) == quote_sha256 and
                 ((citation["basis"] == "consecutive_blocks_lf_v1" and quote_sha256 == boundary["parent_quote_sha256"]) or
                  (citation["basis"] == "frozen_clause_span_v1" and quote_sha256 == boundary["focused_quote_sha256"] and
                   citation["clause_id"] == boundary["clause_id"] and
                   citation["clause_proof_sha256"] == boundary["clause_proof_sha256"])) and
                 digest({key: value for key, value in boundary.items() if key != "proof_sha256"}) == boundary["proof_sha256"])
        label = boundary["label"]
        (offset, split), = _crossings(label, quote)
        _require(split == boundary["split_at"])
        field = "reference_keywords"
        return {"candidate_index": candidate_index, "label_index": label_index, "field": field, "label": label,
                "quote_sha256": quote_sha256, "block_ids": boundary["block_ids"],
                "grounding": {"field": field, "label": label, "basis": LABEL_WRAP_RULE,
                              "matches": [{"quote_spans": [[offset - split, offset],
                                                           [offset + 1, offset + 1 + len(label) - split]],
                                           "edge_sha256s": [boundary["proof_sha256"]]}]}}
    except (KeyError, TypeError, ValueError):
        raise ValueError(SOURCE_ERROR) from None
