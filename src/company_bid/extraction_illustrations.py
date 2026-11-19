"""Bind N2 screenshot use to reviewed N1 regions and the frozen PDF."""
from __future__ import annotations

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
