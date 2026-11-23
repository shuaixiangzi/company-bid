"""Authenticate a reviewed native Chinese label crossing one mixed-font PDF wrap."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Literal

import pdfplumber
import pypdfium2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract_attachment_citation_split import _ID, _SHA, _member, _page_lines
from .pdf_text_lines import horizontal_matrix, pdf_page_text_lines
from .storage import digest


SOURCE_CONFIG_KEY = "native_reviewed_label_wrap_source"
WINDOW_CONTEXT_KEY = "native_reviewed_label_wrap_context"
RESOLUTION_KEY = "native_reviewed_label_wrap_resolution"
SOURCE_ERROR = "NATIVE_REVIEWED_LABEL_WRAP_SOURCE_MISMATCH"
RULE = "reviewed_native_mixed_font_label_wrap_v1"
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_NUMBERED = re.compile(r"^\s*(?:[0-9]+(?:[.．][0-9]+)*[.．、）)]?|第[一二三四五六七八九十百0-9]+[章节条款])\s*")
_LIST_ITEM = re.compile(r"(?:（([1-9]\d{0,2})）|\(([1-9]\d{0,2})\))(?=\s*\S)")
_TERMINATORS = ("。", "！", "？", "；", ".", "!", "?", ";")


class _WrapScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    raw_candidate_sha256: _SHA
    physical_page: int = Field(ge=1)
    rendered_rgb_sha256: _SHA
    left_block_id: _ID
    right_block_id: _ID
    candidate_index: int = Field(ge=0)
    field: Literal["reference_keywords"]
    label_index: int = Field(ge=0)
    list_item_number: int | None = Field(default=None, ge=2, le=999)


def normalize_reviewed_label_wrap_config(value):
    try:
        if not isinstance(value, list):
            return _WrapScope.model_validate(value).model_dump(exclude_none=True)
        scopes = [_WrapScope.model_validate(scope).model_dump(exclude_none=True) for scope in value]
    except ValidationError:
        raise ValueError("NATIVE_REVIEWED_LABEL_WRAP_CONFIG_INVALID") from None
    if not scopes or len({scope["window_id"] for scope in scopes}) != len(scopes):
        raise ValueError("NATIVE_REVIEWED_LABEL_WRAP_CONFIG_INVALID")
    return scopes


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _visible(chars):
    return [char for char in chars if char["text"].strip()]


def _cjk_on_baseline(char, anchor):
    matrix = horizontal_matrix(char)
    return (matrix is not None and _CJK.fullmatch(char["text"]) and len(char["text"]) == 1 and
            char["fontname"] == anchor["fontname"] and abs(char["size"] - anchor["size"]) < .05 and
            abs(matrix[5] - anchor["matrix"][5]) <= .15)


def _boundary_glyphs(chars, *, tail):
    ordered = list(reversed(chars)) if tail else chars
    anchor = ordered[0]
    _require(_cjk_on_baseline(anchor, anchor) and anchor["size"] > 0)
    span = [anchor]
    for char in ordered[1:]:
        previous = span[-1]
        gap = previous["x0"] - char["x1"] if tail else char["x0"] - previous["x1"]
        if not _cjk_on_baseline(char, anchor) or not -.05 * anchor["size"] <= gap <= .2 * anchor["size"]:
            break
        span.append(char)
    return list(reversed(span)) if tail else span


def _glyph(char):
    return {"text": char["text"], "font": char["fontname"], "size": char["size"],
            "bbox": [char["x0"], char["top"], char["x1"], char["bottom"]], "matrix": list(char["matrix"])}


def _prove_boundary(lines, native, left_index):
    # An indented preceding line, a full continuing line, and an unindented
    # closing line establish the same native paragraph and column.
    previous, left, right, following = native[left_index - 1:left_index + 3]
    previous_chars, left_chars, right_chars = [_visible(lines[i][3]) for i in range(left_index - 1, left_index + 2)]
    tail, head = _boundary_glyphs(left_chars, tail=True), _boundary_glyphs(right_chars, tail=False)
    last, first = tail[-1], head[0]
    size = last["size"]
    left_baseline, right_baseline = last["matrix"][5], first["matrix"][5]
    spacing = left_baseline - right_baseline
    mixed_latin = [char for char in left_chars if re.fullmatch(r"[A-Za-z]", char["text"]) and
                   horizontal_matrix(char) is not None and abs(char["matrix"][5] - left_baseline) <= .15 and
                   char["fontname"] != last["fontname"] and abs(char["size"] - size) < .05 and
                   char["bottom"] > last["bottom"] + .5 * size]
    para_start = previous_chars[-1]
    _require(mixed_latin and _cjk_on_baseline(first, first) and last["fontname"] == first["fontname"] and
             abs(first["size"] - size) < .05 and size <= spacing <= 2.5 * size and
             horizontal_matrix(para_start) is not None and
             abs((para_start["matrix"][5] - left_baseline) - spacing) <= .1 * size and
             1.5 * size <= previous["bbox"][0] - right["bbox"][0] <= 3 * size and
             abs(left["bbox"][0] - right["bbox"][0]) <= .15 * size and
             abs(last["x1"] - previous["bbox"][2]) <= size and
             abs(last["x1"] - left["bbox"][2]) <= .05 * size and
             abs(first["x0"] - right["bbox"][0]) <= .05 * size and
             last["x1"] - right["bbox"][0] >= 10 * size and
             right["bbox"][2] < last["x1"] - size and
             not previous["text"].endswith(_TERMINATORS) and right["text"].endswith(_TERMINATORS) and
             not any(_NUMBERED.match(block["text"]) for block in (previous, left, right)) and
             (following["bbox"][0] - right["bbox"][0] >= 1.5 * size or _NUMBERED.match(following["text"])))
    left_text, right_text = ("".join(char["text"] for char in glyphs) for glyphs in (tail, head))
    _require(left["text"].endswith(left_text) and right["text"].startswith(right_text))
    return {"paragraph_start": _member(previous), "following_boundary": _member(following),
            "left_cjk_glyphs": [_glyph(char) for char in tail], "right_cjk_glyphs": [_glyph(char) for char in head],
            "left_mixed_latin_glyphs": [_glyph(char) for char in mixed_latin]}


def _prove_numbered_item_boundary(lines, native, left_index, number):
    # The label crosses only the opening line and its unnumbered, final
    # continuation. Consecutive siblings bracket the reviewed list item.
    previous, left, right, following = native[left_index - 1:left_index + 3]
    left_marker = _LIST_ITEM.match(left["text"])
    next_marker = _LIST_ITEM.match(following["text"])
    before = next((block for block in reversed(native[max(0, left_index - 12):left_index])
                   if _LIST_ITEM.match(block["text"]) or _NUMBERED.match(block["text"])), None)
    prior_marker = _LIST_ITEM.match(before["text"]) if before else None
    _require(left_marker and prior_marker and next_marker and
             tuple(int(marker[1] or marker[2]) for marker in (prior_marker, left_marker, next_marker)) ==
             (number - 1, number, number + 1) and
             len({marker[1] is not None for marker in (prior_marker, left_marker, next_marker)}) == 1 and
             previous["text"].endswith(_TERMINATORS) and not left["text"].endswith(_TERMINATORS) and
             right["text"].endswith(_TERMINATORS) and not _LIST_ITEM.match(right["text"]) and
             not _NUMBERED.match(right["text"]))
    prior_chars, left_chars, right_chars = [_visible(lines[i][3]) for i in range(left_index - 1, left_index + 2)]
    tail, head = _boundary_glyphs(left_chars, tail=True), _boundary_glyphs(right_chars, tail=False)
    last, first = tail[-1], head[0]
    size = last["size"]
    baseline = last["matrix"][5]
    spacing = baseline - first["matrix"][5]
    marker = left_chars[:len(left_marker[0])]
    digits = marker[1:-1]
    _require("".join(char["text"] for char in marker) == left_marker[0] and
             all(horizontal_matrix(char) is not None and abs(char["matrix"][5] - baseline) <= .15 for char in marker) and
             all(char["fontname"] != last["fontname"] and abs(char["size"] - size) < .05 and
                 char["bottom"] > last["bottom"] + .5 * size for char in digits) and
             last["fontname"] == first["fontname"] and abs(first["size"] - size) < .05 and
             size <= spacing <= 2.5 * size and horizontal_matrix(prior_chars[-1]) is not None and
             abs((prior_chars[-1]["matrix"][5] - baseline) - spacing) <= .1 * size and
             1.5 * size <= left["bbox"][0] - right["bbox"][0] <= 3 * size and
             all(abs(left["bbox"][0] - sibling["bbox"][0]) <= .15 * size for sibling in (before, following)) and
             abs(last["x1"] - following["bbox"][2]) <= size and last["x1"] - right["bbox"][0] >= 10 * size and
             abs(last["x1"] - left["bbox"][2]) <= .05 * size and abs(first["x0"] - right["bbox"][0]) <= .05 * size and
             right["bbox"][2] < last["x1"] - size)
    left_text, right_text = ("".join(char["text"] for char in glyphs) for glyphs in (tail, head))
    _require(left["text"].endswith(left_text) and right["text"].startswith(right_text))
    return {"paragraph_start": _member(left), "previous_sibling": _member(before),
            "previous_sibling_end": _member(previous), "following_boundary": _member(following), "list_item_number": number,
            "left_cjk_glyphs": [_glyph(char) for char in tail], "right_cjk_glyphs": [_glyph(char) for char in head],
            "left_item_marker_glyphs": [_glyph(char) for char in marker]}


def bind_reviewed_label_wrap(windows, documents, blocks, manifest, config):
    """Reread the exact native page and its raster for a single frozen candidate."""
    try:
        source_id, page = config["source_id"], config["physical_page"]
        source, = [source for source in (manifest or {}).get("sources", []) if source["source_id"] == source_id]
        document, = [document for document in documents if document["source_id"] == source_id]
        window, = [window for window in windows if window["window_id"] == config["window_id"]]
        _require(document["format"] == window["source_format"] == "pdf" and window["source_id"] == source_id and
                 source["sha256"] == document["sha256"] == window["source_sha256"] == config["source_sha256"])
        native, coverage_hash = _page_lines(config, document, blocks, source)
        left_id, right_id = config["left_block_id"], config["right_block_id"]
        left_index, = [i for i, block in enumerate(native) if block["block_id"] == left_id]
        _require(0 < left_index < len(native) - 2)
        left, right = native[left_index:left_index + 2]
        _require(right["block_id"] == right_id and right["ordinal"] == left["ordinal"] + 1 and
                 {left_id, right_id} <= {block["block_id"] for block in window["blocks"]} and
                 not any(edge["blocks"] == [left_id, right_id] for edge in window.get("native_pdf_line_edges", [])))
        payload = Path(source["path"]).read_bytes()
        _require(hashlib.sha256(payload).hexdigest() == config["source_sha256"])
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            lines = list(pdf_page_text_lines(pdf.pages[page - 1], ()))
            _require([(i, text, bbox) for i, text, bbox, _, _ in lines] ==
                     [(i, block["text"], block["bbox"]) for i, block in enumerate(native)])
            if number := config.get("list_item_number"):
                boundary = _prove_numbered_item_boundary(lines, native, left_index, number)
            else:
                boundary = _prove_boundary(lines, native, left_index)
        proof = {"rule": RULE, "scope": config, "native_coverage_sha256": coverage_hash,
                 "left": _member(left), "right": _member(right), "boundary": boundary}
        window[WINDOW_CONTEXT_KEY] = proof | {"proof_sha256": digest(proof)}
    except (KeyError, TypeError, IndexError, OSError, ValueError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None


def _crossings(label, quote):
    offset, matches = 0, []
    lines = quote.split("\n")
    for left, right in zip(lines, lines[1:]):
        offset += len(left)
        matches.extend((offset, split) for split in range(1, len(label))
                       if left.endswith(label[:split]) and right.startswith(label[split:]))
        offset += 1
    return matches


def resolve_reviewed_label_wrap(raw, requirement, window, citation):
    proof = window[WINDOW_CONTEXT_KEY]
    scope = proof["scope"]
    index, field = scope["label_index"], scope["field"]
    selected = citation["block_ids"]
    left_id, right_id = scope["left_block_id"], scope["right_block_id"]
    _require(digest(raw) == scope["raw_candidate_sha256"] and requirement["response_mode"] == "writing" and
             scope["source_id"] == window["source_id"] == citation["source_id"] and
             scope["source_sha256"] == window["source_sha256"] and scope["window_id"] == window["window_id"] and
             scope["left_block_id"] == proof["left"]["block_id"] and scope["right_block_id"] == proof["right"]["block_id"] and
             left_id in selected and right_id in selected and selected.index(right_id) == selected.index(left_id) + 1 and
             proof["boundary"]["paragraph_start"]["block_id"] in selected and
             citation["basis"] == "consecutive_blocks_lf_v1" and index < len(requirement[field]) and
             proof["rule"] == RULE and digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"])
    if number := scope.get("list_item_number"):
        _require(selected == [left_id, right_id] and proof["boundary"].get("list_item_number") == number)
    label = requirement[field][index]
    quote = citation["quote"]
    blocks = {block["block_id"]: block for block in window["blocks"]}
    left, right = blocks[left_id], blocks[right_id]
    _require(proof["left"] == _member(left) and proof["right"] == _member(right) and
             len(citation["block_ids"]) == len(quote.split("\n")) and
             quote.split("\n")[selected.index(left_id):selected.index(right_id) + 1] == [left["text"], right["text"]] and
             2 <= len(label) <= 32 and _CJK.fullmatch(label) and label not in quote)
    (offset, split), = _crossings(label, quote)
    expected_offset = sum(len(line) + 1 for line in quote.split("\n")[:selected.index(left_id) + 1]) - 1
    left_text = "".join(char["text"] for char in proof["boundary"]["left_cjk_glyphs"])
    right_text = "".join(char["text"] for char in proof["boundary"]["right_cjk_glyphs"])
    _require(offset == expected_offset and left_text.endswith(label[:split]) and right_text.startswith(label[split:]))
    grounding = {"field": field, "label": label, "basis": RULE,
                 "matches": [{"quote_spans": [[offset - split, offset], [offset + 1, offset + 1 + len(label) - split]],
                              "edge_sha256s": [proof["proof_sha256"]]}]}
    return {"rule": RULE, "candidate_index": scope["candidate_index"], "field": field, "label_index": index,
            "label": label, "quote_sha256": citation["quote_sha256"], "block_ids": selected,
            "raw_candidate_sha256": scope["raw_candidate_sha256"], "proof": proof, "grounding": grounding,
            "model_candidate_mutation": False}
