"""Prove the layout and boundaries of individually reviewed contract annex pages."""
from __future__ import annotations

import hashlib
import io
import math
import re
from pathlib import Path
from typing import Annotated

import pdfplumber
import pypdfium2
from PIL import Image, ImageChops, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .response_cover_sources import _native_lines
from .storage import digest


SOURCE_CONFIG_KEY = "contract_heading_only_annex_source"
WINDOW_CONTEXT_KEY = "native_contract_heading_only_annex_context"
RESOLUTION_KEY = "native_contract_heading_only_annex_resolution"
SOURCE_ERROR = "CONTRACT_HEADING_ONLY_ANNEX_SOURCE_MISMATCH"
RULE = "reviewed_native_contract_heading_only_annex_v1"
_SCALE = 2
_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_HEAD = re.compile(r"附件[ \t　]*([1-9][0-9]{0,2})[ \t　]*[：:][ \t　]*([^。；;：:!?！？\n]{1,65})")
_INVENTORY = re.compile(r"^本合同附件(?:为|是|属于)本合同(?:的)?组成部分")
_SIGNATURE = re.compile(r"[（(]本页无(?:合同)?正文[，,]\s*(?:为|系)签[章署]页[）)]")


class _ReviewedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page: int = Field(ge=4)
    heading_block_id: _ID
    rendered_rgb_sha256: _SHA


class _ReviewScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _ID
    source_sha256: _SHA
    window_id: _ID
    reviewed_pages: list[_ReviewedPage] = Field(min_length=1, max_length=8)


def normalize_heading_only_annex_config(value):
    try:
        config = _ReviewScope.model_validate(value).model_dump()
    except ValidationError:
        raise ValueError("CONTRACT_HEADING_ONLY_ANNEX_CONFIG_INVALID") from None
    pages = config["reviewed_pages"]
    if ([row["page"] for row in pages] != list(range(pages[0]["page"], pages[0]["page"] + len(pages))) or
            len({row["heading_block_id"] for row in pages}) != len(pages)):
        raise ValueError("CONTRACT_HEADING_ONLY_ANNEX_CONFIG_INVALID")
    return config


def _require(condition):
    if not condition:
        raise ValueError(SOURCE_ERROR)


def _rgb_page(page):
    bitmap = page.render(scale=_SCALE)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def rendered_page_rgb_sha256(source, page_number):
    """Hash a 144 DPI full-page review image's exact RGB pixels."""
    with pypdfium2.PdfDocument(source) as pdf:
        page = pdf[page_number - 1]
        try:
            return hashlib.sha256(_rgb_page(page).tobytes()).hexdigest()
        finally:
            page.close()


def _member(block):
    return {key: block[key] for key in (
        "block_id", "source_id", "locator", "unit_id", "ordinal", "page", "text", "bbox", "kind", "origin")}


def _heading(block):
    match = _HEAD.fullmatch(block["text"])
    return (int(match[1]), match[2]) if match else None


def _within(box, outer, margin=0):
    return outer[0] - margin <= box[0] <= box[2] <= outer[2] + margin and (
        outer[1] - margin <= box[1] <= box[3] <= outer[3] + margin)


def _complete_page(document, units, blocks, page):
    unit = units.get(page)
    _require(unit is not None and unit["locator"] == f"pdf/page[{page}]")
    coverage = unit["coverage"]
    _require(coverage["status"] == "read" and coverage["origin"] == "native" and not any(
        coverage[key] for key in ("unread_objects", "ignored_objects", "unmapped_glyphs", "transformations",
                                  "error_code", "warnings", "table_count", "image_count")))
    lines = [blocks[source["block_id"]] for source in unit["blocks"]]
    _require(bool(lines) and _native_lines(lines, document["source_id"], page, range(len(lines))) and
             all(line["text"] == source["text"] and line["bbox"] == source.get("bbox")
                 for line, source in zip(lines, unit["blocks"], strict=True)) and
             all(left["bbox"][3] <= right["bbox"][1] for left, right in zip(lines, lines[1:])))
    return lines, digest(coverage)


def _native_glyph_counts(page, lines):
    _require(page.rotation == 0 and page.bbox == page.cropbox == page.mediabox and
             page.bbox[0] == page.bbox[1] == 0 and set(page.objects) == {"char"} and not page.annots)
    groups = [[] for line in lines]
    for char in page.chars:
        bounds = [char["x0"], char["top"], char["x1"], char["bottom"]]
        matches = [index for index, line in enumerate(lines) if _within(bounds, line["bbox"], margin=0.5)]
        _require(len(matches) == 1 and char["upright"])
        groups[matches[0]].append(char)
    for line, chars in zip(lines, groups, strict=True):
        glyphs = "".join(char["text"] for char in sorted(chars, key=lambda char: char["x0"]))
        _require(bool(chars) and re.sub(r"\s", "", glyphs) == re.sub(r"\s", "", line["text"]))
    return [len(group) for group in groups]


def _raster_layout(page, review, lines):
    number = review["page"]
    width, height = page.get_size()
    heading, footer = [line["bbox"] for line in lines]
    _require(heading[3] < height / 4 and footer[1] > height * 0.85 and
             all(_within(box, (0, 0, width, height)) for box in (heading, footer)) and
             abs((footer[0] + footer[2]) / 2 - width / 2) <= 5)
    native_positions = [[], []]
    for obj in page.get_objects():
        _require(obj.type == pypdfium2.raw.FPDF_PAGEOBJ_TEXT)
        left, bottom, right, top = obj.get_bounds()
        bounds = left, height - top, right, height - bottom
        # PDFMiner's font boxes and PDFium's painted digit bounds use different
        # vertical metrics. Match the footer to its narrow horizontal range.
        matches = [index for index, box in enumerate((heading, footer))
                   if _within(bounds, (box[0], box[1] - 8 * index, box[2], box[3]), margin=3)]
        _require(len(matches) == 1)
        native_positions[matches[0]].append(bounds)
    rendered = _rgb_page(page)
    rendered_hash = hashlib.sha256(rendered.tobytes()).hexdigest()
    _require(rendered_hash == review["rendered_rgb_sha256"])
    ink = ImageChops.difference(rendered, Image.new("RGB", rendered.size, "white"))
    paint = ImageDraw.Draw(ink)
    for positions in native_positions:
        _require(bool(positions))
        left = min(box[0] for box in positions)
        top = min(box[1] for box in positions)
        right = max(box[2] for box in positions)
        bottom = max(box[3] for box in positions)
        region = (math.floor(_SCALE * (left - 3)), math.floor(_SCALE * (top - 3)),
                  math.ceil(_SCALE * (right + 3)), math.ceil(_SCALE * (bottom + 3)))
        _require(ink.crop(region).getbbox() is not None)
        paint.rectangle(region, fill=(0, 0, 0))
    _require(ink.getbbox() is None)
    return {"physical_page": number, "render_dpi": 72 * _SCALE, "pixel_size": list(rendered.size),
            "rendered_rgb_sha256": rendered_hash, "native_text_objects": [len(boxes) for boxes in native_positions]}


def _contract_listing(indexes, document, units, blocks, first_page, last_number):
    source_id = document["source_id"]
    matches = [(index, section) for index in indexes for section in index["sections"]
               if index["source_id"] == source_id and index["source_sha256"] == document["sha256"] and
               section["next_boundary"]["kind"] == "contract_body_end" and
               section["next_boundary"]["page"] <= first_page - 3]
    _require(bool(matches))
    listing_page = max(section["next_boundary"]["page"] for _, section in matches)
    matches = [(index, section) for index, section in matches if section["next_boundary"]["page"] == listing_page]
    _require(len(matches) == 1)
    index, section = matches[0]
    lines, coverage = _complete_page(document, units, blocks, listing_page)
    _require(len(lines) >= 3 and all(section["next_boundary"][key] == lines[-2][key]
                                     for key in ("block_id", "locator", "text")))
    entries = [entry for entry in index["entries"] if entry["section_id"] == section["section_id"]]
    declarations = [entry for entry in entries if entry["complete"] and entry["intro_spans"] and
                    _INVENTORY.match(re.sub(r"^\d+\.\d+\s*", "", entry["intro_spans"][0]["text"]))]
    _require(len(declarations) == 1)
    declaration = declarations[0]
    listing = [blocks[span["block_id"]] for span in declaration["spans"] if _HEAD.fullmatch(span["text"])]
    _require(len(listing) >= last_number + 1 and [line["block_id"] for line in listing] ==
             [line["block_id"] for line in lines if _heading(line)] and
             [_heading(line)[0] for line in listing] == list(range(1, len(listing) + 1)) and
             all(right["ordinal"] == left["ordinal"] + 1 for left, right in zip(listing, listing[1:])))
    return index, section, entries, declaration, listing, lines[-2:], coverage, listing_page


def _reference_entries(entry, pattern):
    if not pattern.search(entry["quote"]):
        return []
    children = [candidate for child in entry["items"] for candidate in _reference_entries(child, pattern)]
    if children:
        return children
    _require(entry["complete"])
    return [{key: entry[key] for key in ("block_ids", "quote", "quote_sha256", "start", "end", "proof_sha256")} |
            {"number": entry.get("clause_number") or entry.get("parent_clause_number"),
             "subitem": entry.get("item_number")}]


def _footer_proof(footer, footer_skips):
    proof = footer_skips.get(footer["block_id"])
    _require(bool(proof) and proof["rule"] == "pdf-page-footer-v1" and
             str(proof["printed_page"]) == footer["text"] and
             all(proof[key] == footer[key] for key in ("block_id", "source_id", "page", "bbox", "locator")))
    return proof


def _build_proof(config, manifest, documents, indexes, blocks, footer_skips):
    sources = [source for source in (manifest or {}).get("sources", []) if source["source_id"] == config["source_id"]]
    matches = [document for document in documents if document["source_id"] == config["source_id"]]
    _require(len(sources) == len(matches) == 1)
    source, document = sources[0], matches[0]
    _require(document["format"] == "pdf" and source["sha256"] == document["sha256"] == config["source_sha256"])
    units = {unit["page"]: unit for unit in document["units"]}
    _require(len(units) == len(document["units"]))
    reviews = config["reviewed_pages"]
    checklist = [_complete_page(document, units, blocks, review["page"]) for review in reviews]
    _require(all(len(lines) == 2 and _heading(lines[0]) and lines[0]["block_id"] == review["heading_block_id"]
                 for review, (lines, _) in zip(reviews, checklist, strict=True)))
    numbers = [_heading(lines[0])[0] for lines, _ in checklist]
    _require(numbers[0] >= 2 and numbers == list(range(numbers[0], numbers[0] + len(numbers))))
    first, last = reviews[0]["page"], reviews[-1]["page"]
    index, section, entries, declaration, listing, end, listing_coverage, listing_page = _contract_listing(
        indexes, document, units, blocks, first, numbers[-1])
    signature, signature_coverage = _complete_page(document, units, blocks, listing_page + 1)
    _require(len(signature) >= 4 and _SIGNATURE.fullmatch(signature[0]["text"]) and
             any(line["text"].startswith("甲方") for line in signature[1:-1]) and
             any(line["text"].startswith("乙方") for line in signature[1:-1]))
    prior = [_complete_page(document, units, blocks, page) for page in range(listing_page + 2, first)]
    prior_lines = [line for lines, _ in prior for line in lines[:-1]]
    prior_headings = [line for line in prior_lines if _heading(line)]
    _require(_heading(prior[0][0][0]) == _heading(listing[0]) and
             [_heading(line) for line in prior_headings] ==
             [_heading(line) for line in listing[:numbers[0] - 1]])
    preceding = prior_lines[prior_lines.index(prior_headings[-1]):]
    following, following_coverage = _complete_page(document, units, blocks, last + 1)
    _require(len(preceding) >= 2 and len(following) >= 3 and
             _heading(following[0]) == _heading(listing[numbers[-1]]) and
             not any(_heading(line) for line in following[1:-1]))
    _require(all(_heading(lines[0]) == _heading(listing[number - 1])
                 for (lines, _), number in zip(checklist, numbers, strict=True)))
    footers = [line[-1] for line in [end, signature, *(lines for lines, _ in prior),
                                    *(lines for lines, _ in checklist), following]]
    proofs = [_footer_proof(line, footer_skips) for line in footers]
    _require(len({row["printed_page"] - row["page"] for row in proofs}) == 1)

    raw = Path(source["path"]).read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == document["sha256"])
    verified_pages = []
    with pdfplumber.open(io.BytesIO(raw)) as parser, pypdfium2.PdfDocument(raw) as renderer:
        _require(len(parser.pages) == len(renderer) == len(units))
        for review, (lines, coverage), number in zip(reviews, checklist, numbers, strict=True):
            native_page = parser.pages[review["page"] - 1]
            page = renderer[review["page"] - 1]
            try:
                counts = _native_glyph_counts(native_page, lines)
                layout = _raster_layout(page, review, lines)
            finally:
                page.close()
            pattern = re.compile(rf"附件[ \t　]*{number}(?![0-9])(?!(?:[ \t　]*[：:]))")
            references = [reference for entry in entries if entry is not declaration
                          for reference in _reference_entries(entry, pattern)]
            _require(bool(references))
            verified_pages.append({"attachment_number": number, "heading": _member(lines[0]), "footer": _member(lines[1]),
                                   "listing_entry": _member(listing[number - 1]), "native_coverage_sha256": coverage,
                                   "layout": layout | {"native_glyph_counts": counts}, "contract_references": references})

    proof = {"rule": RULE, "source_id": document["source_id"], "source_sha256": document["sha256"],
             "contract_index_sha256": index["index_sha256"], "contract_section": section["heading"],
             "listing": {"clause_number": declaration["clause_number"], "quote_sha256": declaration["quote_sha256"],
                         "proof_sha256": declaration["proof_sha256"], "native_coverage_sha256": listing_coverage,
                         "first_entry": _member(listing[0]), "body_end": _member(end[0])},
             "signing_page_boundary": {"marker": _member(signature[0]), "native_coverage_sha256": signature_coverage},
             "neighboring_annexes": [{"heading": _member(lines[0]), "first_body": _member(lines[1]),
                                     "last_body": _member(lines[-1]), "native_coverage_sha256": coverage}
                                    for lines, coverage in ((preceding, [coverage for _, coverage in prior]),
                                                            (following[:-1], [following_coverage]))],
             "page_footer_proofs": proofs, "pages": verified_pages, "window_id": config["window_id"],
             "scope": "original_pages_have_headings_and_footers_only",
             "disposition": "retain_contract_annex_headings_and_references_without_inventing_content_or_response_formats"}
    return proof | {"proof_sha256": digest(proof)}


def bind_heading_only_annex(windows, documents, indexes, blocks, footer_skips, manifest, config):
    """Attach source review only to the window that owns all reviewed headings."""
    matches = [window for window in windows if window["window_id"] == config["window_id"]]
    _require(len(matches) == 1 and matches[0]["source_id"] == config["source_id"])
    window = matches[0]
    target_ids = {block["block_id"] for block in window["blocks"]}
    _require({row["heading_block_id"] for row in config["reviewed_pages"]} <= target_ids)
    try:
        proof = _build_proof(config, manifest, documents, indexes, blocks, footer_skips)
    except (OSError, KeyError, TypeError, IndexError, pypdfium2.PdfiumError):
        raise ValueError(SOURCE_ERROR) from None
    window[WINDOW_CONTEXT_KEY] = proof


def resolve_heading_only_annex(raw, normalized, window, critical):
    """Account only for the reviewed source headings and retain the raw choices."""
    proof = window.get(WINDOW_CONTEXT_KEY)
    if proof is None or not critical:
        return None
    selected = {identity for row in [*normalized["requirements"], *normalized["format_chapters"]]
                for identity in (row["citation"]["block_ids"] or [row["citation"]["block_id"]])}
    selected.update(identity for row in normalized["format_chapters"] for identity in row["content_block_ids"])
    headings = [page["heading"] for page in proof["pages"]]
    targets = {block["block_id"]: block for block in window["blocks"]}
    if (critical != [heading["block_id"] for heading in headings] or set(critical) & selected or
            any(proof[key] != window[key] for key in ("source_id", "source_sha256", "window_id")) or
            proof["proof_sha256"] != digest({key: value for key, value in proof.items() if key != "proof_sha256"}) or
            any(not all(targets.get(heading["block_id"], {}).get(key) == value for key, value in heading.items() if key != "bbox")
                for heading in headings)):
        return None
    return {"rule": RULE, "window_id": window["window_id"], "proof": proof,
            "resolved_block_ids": list(critical), "raw_candidate_sha256": digest(raw),
            "model_candidate_mutation": False, "independent_requirements": "retain_separate_source_review",
            "semantic_review": "requires_source_review"}
