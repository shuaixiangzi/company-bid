"""Bind N2 screenshot use to reviewed N1 regions and the frozen PDF."""
from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .storage import digest


CONFIG_KEY = "illustrative_ocr_regions"
_LOCATOR = r"^pdf/page\[([1-9]\d*)\]/image\[(?:0|[1-9]\d*)\]$"
_SHA = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_REVIEW_ROLES = frozenset({"instruction", "navigation", "interface", "example_input", "example_result", "context"})


class IllustrationError(ValueError):
    pass


ILLUSTRATION_SOURCE_CONFIG_KEY = "illustration_source_resolution"
_CONTEXT_RULE = "source_reviewed_query_illustration_only_v1"
_BLOCK_ID = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class _IllustrationContextScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _BLOCK_ID
    source_sha256: _SHA
    window_id: _BLOCK_ID
    page: int = Field(ge=2)
    predecessor_window_id: _BLOCK_ID
    predecessor_requirement_sha256: _SHA
    native_requirement_block_ids: list[_BLOCK_ID] = Field(min_length=2, max_length=24)
    candidate_block_ids: list[_BLOCK_ID] = Field(min_length=3, max_length=3)
    instruction_block_ids: list[_BLOCK_ID] = Field(min_length=2, max_length=12)
    raw_candidate_sha256: _SHA
    source_proof_sha256: _SHA


def normalize_illustration_context_scope(raw):
    try:
        scope = _IllustrationContextScope.model_validate(raw).model_dump()
    except ValidationError:
        raise IllustrationError("ILLUSTRATION_CONTEXT_SCOPE_INVALID") from None
    fields = ("native_requirement_block_ids", "candidate_block_ids", "instruction_block_ids")
    if scope["window_id"] == scope["predecessor_window_id"] or any(len(set(scope[key])) != len(scope[key]) for key in fields):
        raise IllustrationError("ILLUSTRATION_CONTEXT_SCOPE_INVALID")
    return scope


class IllustrativeOcrRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    source_sha256: _SHA
    locator: Annotated[str, StringConstraints(pattern=_LOCATOR)]
    image_sha256: _SHA
    ocr_blocks_sha256: _SHA
    render_dpi: int = Field(ge=72, le=300)


def normalize_regions(raw):
    if not isinstance(raw, list) or len(raw) > 100:
        raise IllustrationError("ILLUSTRATION_REGION_CONFIGURATION_INVALID")
    try:
        regions = [IllustrativeOcrRegion.model_validate(item).model_dump() for item in raw]
    except ValidationError:
        raise IllustrationError("ILLUSTRATION_REGION_CONFIGURATION_INVALID") from None
    if len({(row["source_id"], row["locator"]) for row in regions}) != len(regions):
        raise IllustrationError("ILLUSTRATION_REGION_CONFIGURATION_INVALID")
    return regions


def _ocr_children(blocks, locator):
    prefix = locator + "/ocr/block["
    return [block for block in blocks if block["locator"].startswith(prefix)]


def ocr_region_block_digest(blocks, locator):
    """Hash N1's ordered OCR children with their original content and boxes."""
    return digest(_ocr_children(blocks, locator))


def _use(role, basis, image_sha256, proof_sha256):
    return {"role": role, "basis": basis, "image_sha256": image_sha256, "proof_sha256": proof_sha256}


def _parent(unit, locator):
    matches = [block for block in unit["blocks"] if block["locator"] == locator]
    if (len(matches) != 1 or matches[0].get("kind") != "image" or matches[0].get("origin") != "native"
            or matches[0]["source_id"] != unit["source_id"]):
        raise IllustrationError("ILLUSTRATION_PARENT_IMAGE_MISMATCH")
    return matches[0]


def _reviewed_unit_uses(unit, document):
    from .reading_source_review import KIND, VERSION, review_box_on_page

    children = [block for block in unit.get("blocks", []) if block.get("origin") == "source_review"]
    positions = {block["locator"]: block for block in children}
    if len(positions) != len(children):
        raise IllustrationError("ILLUSTRATION_SOURCE_REVIEW_MISMATCH")
    uses = {}
    for record in unit.get("coverage", {}).get("transformations", []):
        if record.get("kind") != KIND:
            continue
        try:
            locator, image_sha = record["locator"], record["image_sha256"]
            parent = _parent(unit, locator)
            if (record["version"] != VERSION or record["status"] != "confirmed" or unit["coverage"]["status"] != "read"
                    or document.get("format") != "pdf" or not re.fullmatch(_LOCATOR, locator)
                    or locator.split("/image[", 1)[0] != unit["locator"]
                    or record["source_id"] != document["source_id"] or record["source_sha256"] != document["sha256"]
                    or record["page"] != unit["page"] or record["bbox"] != parent["bbox"]
                    or image_sha != record["reviewed_image"]["sha256"] or image_sha != record["renderer"]["image_sha256"]
                    or not re.fullmatch(r"[a-f0-9]{64}", image_sha) or parent["block_id"] in uses):
                raise IllustrationError("ILLUSTRATION_SOURCE_REVIEW_MISMATCH")
            proof_sha = digest(record)
            uses[parent["block_id"]] = _use("image", KIND, image_sha, proof_sha)
            for index, entry in enumerate(record["entries"]):
                expected = f"{locator}/source-review/entry[{index}]"
                child = positions.pop(expected)
                if (entry["locator"] != expected or entry["role"] not in _REVIEW_ROLES
                        or child["source_id"] != unit["source_id"] or child["kind"] != "paragraph"
                        or child["text"] != entry["text"] or child["bbox"] != review_box_on_page(record, entry["pixel_bbox"])):
                    raise IllustrationError("ILLUSTRATION_SOURCE_REVIEW_MISMATCH")
                uses[child["block_id"]] = _use(entry["role"], KIND, image_sha, proof_sha)
        except (KeyError, TypeError, IndexError) as exc:
            raise IllustrationError("ILLUSTRATION_SOURCE_REVIEW_MISMATCH") from None
    if positions:
        raise IllustrationError("ILLUSTRATION_SOURCE_REVIEW_MISMATCH")
    return uses


def _reviewed_ocr_uses(region, document, unit, source):
    from .ocr import DEEPSEEK_OCR_VERSION, RasterOptions, _prepare_image, _read_source, _Request
    from .provider import ProviderFailure

    locator = region["locator"]
    parent = _parent(unit, locator)
    children = _ocr_children(unit["blocks"], locator)
    if (source is None or document["sha256"] != region["source_sha256"] or source["sha256"] != document["sha256"]
            or document.get("format") != "pdf" or unit["coverage"]["status"] != "read"
            or not children or ocr_region_block_digest(unit["blocks"], locator) != region["ocr_blocks_sha256"]
            or any(child["locator"] != f"{locator}/ocr/block[{index}]" or child.get("origin") != "ocr"
                   or child["source_id"] != unit["source_id"] or child.get("bbox") != parent["bbox"]
                   for index, child in enumerate(children))):
        raise IllustrationError("ILLUSTRATION_OCR_REGION_MISMATCH")
    try:
        request = _Request(source_id=document["source_id"], sha256=document["sha256"], path=str(source["path"]),
                           unit_id=unit["unit_id"], locator=locator, page=unit["page"],
                           ocr_version=DEEPSEEK_OCR_VERSION, object_kind="pdf_region", bbox=parent["bbox"])
        options = RasterOptions(render_dpi=region["render_dpi"])
        _, raster = _prepare_image(request, _read_source(request, options), options)
    except (ValidationError, ProviderFailure, KeyError):
        raise IllustrationError("ILLUSTRATION_OCR_RASTER_MISMATCH") from None
    if raster["image_sha256"] != region["image_sha256"]:
        raise IllustrationError("ILLUSTRATION_OCR_RASTER_MISMATCH")
    proof_sha = digest(region)
    return {block["block_id"]: _use("image" if block["kind"] == "image" else "illustrative_ocr",
                                   "reviewed_ocr_region_v1", region["image_sha256"], proof_sha)
            for block in [parent, *children]}


def illustration_source_uses(documents, raw_regions, manifest):
    """Derive source roles without changing or re-reading N1's transcription."""
    regions = normalize_regions(raw_regions)
    sources = {row["source_id"]: row for row in (manifest or {}).get("sources", [])}
    by_target = {(row["source_id"], row["locator"].split("/image[", 1)[0]): [] for row in regions}
    for region in regions:
        by_target[region["source_id"], region["locator"].split("/image[", 1)[0]].append(region)
    uses = {}
    for document in documents:
        for unit in document.get("units", []):
            uses.update(_reviewed_unit_uses(unit, document))
            for region in by_target.pop((document["source_id"], unit["locator"]), []):
                annotated = _reviewed_ocr_uses(region, document, unit, sources.get(document["source_id"]))
                if uses.keys() & annotated.keys():
                    raise IllustrationError("ILLUSTRATION_OCR_REGION_MISMATCH")
                uses.update(annotated)
    if by_target:
        raise IllustrationError("ILLUSTRATION_OCR_REGION_MISMATCH")
    return uses


def is_context_only(block):
    use = block.get("illustration_source_use")
    role = use.get("role") if isinstance(use, dict) else use
    return bool(role and role != "instruction")


def _context_source(condition):
    if not condition:
        raise IllustrationError("ILLUSTRATION_CONTEXT_SOURCE_PROOF_MISMATCH")


def _proof_block(block):
    return {**{key: block[key] for key in ("block_id", "locator", "kind", "origin")},
            "use": block["illustration_source_use"], "text_sha256": digest(block["text"])}


def _reviewed_screenshot_rows(unit, document, annotated, image_locators):
    from .reading_source_review import KIND

    uses = _reviewed_unit_uses(unit, document)
    transformations = unit["coverage"].get("transformations", [])
    records = {row["locator"]: row for row in transformations if row.get("kind") == KIND}
    _context_source(len(records) == len(transformations) == 2 and len(image_locators) == 2 and
                    set(records) == set(image_locators) and
                    all(not any(item.get("critical") is not False for item in row["unknown"]) for row in records.values()))
    _context_source(set(annotated) == set(uses) and
                    all(block.get("illustration_source_use") == uses[key] for key, block in annotated.items()))
    return [{"locator": locator, "bbox": records[locator]["bbox"], "image_sha256": records[locator]["image_sha256"],
             "proof_sha256": digest(records[locator])} for locator in image_locators]


def build_illustration_context_proof(scope, window, predecessor, document, blocks, footer_skips):
    """Bind one saved illustration-only candidate to its reviewed page and the native owner."""
    page, source_id = scope["page"], scope["source_id"]
    _context_source(window["window_id"] == scope["window_id"] and
                    predecessor["window_id"] == scope["predecessor_window_id"] and
                    all(item["source_id"] == source_id and item["source_sha256"] == scope["source_sha256"]
                        and item["source_format"] == "pdf" for item in (window, predecessor)))
    units = [unit for unit in document["units"] if unit["page"] in {page - 1, page}]
    _context_source([unit["page"] for unit in units] == [page - 1, page])
    for unit, origin in zip(units, ("native", "mixed"), strict=True):
        coverage = unit["coverage"]
        _context_source(coverage.get("status") == "read" and coverage.get("origin") == origin and
                        not any(coverage.get(key) for key in ("unread_objects", "unmapped_glyphs", "error_code", "ignored_objects")))
    prior, current = ([blocks[b["block_id"]] for b in unit["blocks"]] for unit in units)
    split = next((i for i, block in enumerate(current) if block["kind"] == "image"), -1)
    _context_source(split > 0)
    preface = [block for block in prior if block["block_id"] not in footer_skips] + current[:split]
    native_ids = [block["block_id"] for block in preface]
    prior_targets = {block["block_id"] for block in predecessor["blocks"]}
    _context_source(native_ids == scope["native_requirement_block_ids"] and set(native_ids) <= prior_targets and
                    all(b["kind"] == "paragraph" and b["origin"] == "native" and not b.get("illustration_source_use") and
                        re.fullmatch(r"pdf/page\[\d+\]/line\[\d+\]", b["locator"]) for b in preface))
    illustrated = [block for block in current[split:] if block["block_id"] not in footer_skips]
    annotated = {block["block_id"]: block for block in illustrated}
    image_locators = [block["locator"] for block in illustrated if block["kind"] == "image"]
    screenshot_rows = _reviewed_screenshot_rows(units[1], document, annotated, image_locators)
    targets = {block["block_id"]: block for block in window["blocks"]}
    adjacent = window["adjacent_context"]
    _context_source(all(block["block_id"] in annotated for block in [*targets.values(), *adjacent]))
    _context_source(set(scope["candidate_block_ids"]) <= targets.keys() and
                    set(scope["instruction_block_ids"]) <= annotated.keys())
    selected = [annotated[key] for key in scope["candidate_block_ids"]]
    selected_roles = [b["illustration_source_use"]["role"] for b in selected]
    _context_source(selected_roles == ["navigation", "instruction", "instruction"] and
                    all(b["locator"].startswith(image_locators[0] + "/source-review/") for b in selected))
    instruction_ids = scope["instruction_block_ids"]
    _context_source(set(instruction_ids) == {key for key, b in annotated.items()
                                            if b["illustration_source_use"]["role"] == "instruction"} and
                    set(instruction_ids) <= prior_targets | targets.keys())
    instructions = [{**_proof_block(annotated[key]), "text": annotated[key]["text"], "bbox": annotated[key]["bbox"],
                     "owner_window_id": predecessor["window_id"] if key in prior_targets else window["window_id"]}
                    for key in instruction_ids]
    selection = [{**_proof_block(b), "text": b["text"], "bbox": b["bbox"]} for b in selected]
    prior_proof = {"window_id": predecessor["window_id"], "requirement_sha256": scope["predecessor_requirement_sha256"],
                   "block_ids": native_ids, "start_locator": preface[0]["locator"], "end_locator": preface[-1]["locator"],
                   "quote_sha256": hashlib.sha256("\n".join(b["text"] for b in preface).encode()).hexdigest()}
    proof = {"rule": _CONTEXT_RULE, "window_id": window["window_id"], "source_id": source_id, "page": page,
             "source_sha256": scope["source_sha256"], "predecessor": prior_proof, "screenshots": screenshot_rows,
             "raw_candidate_sha256": scope["raw_candidate_sha256"], "selection": selection, "instructions": instructions,
             "target_blocks": [_proof_block(b) for b in targets.values()], "adjacent_blocks": [_proof_block(b) for b in adjacent]}
    return {**proof, "proof_sha256": digest(proof)}


def verify_context_proof(scope, proof, window):
    _context_source(proof.get("proof_sha256") == scope["source_proof_sha256"] and
                    digest({key: value for key, value in proof.items() if key != "proof_sha256"}) == proof["proof_sha256"] and
                    all(proof[key] == scope[key] for key in ("window_id", "source_id", "source_sha256", "page", "raw_candidate_sha256")) and
                    proof["rule"] == _CONTEXT_RULE and
                    [_proof_block(b) for b in window["blocks"]] == proof["target_blocks"] and
                    [_proof_block(b) for b in window["adjacent_context"]] == proof["adjacent_blocks"])
