"""Source-bound partial reading crops; original proof attachments stay intact.

Only an explicit single-object unrotated full-image selection is supported.
The caller authenticates the original source and persists these returned bytes
as an immutable artifact. This module neither performs OCR nor establishes
whole-page reading coverage, facts or qualification.
"""
from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
import re

from PIL import Image
from pydantic import ValidationError

from .evidence import EvidenceSelection, _selected_png
from .models import file_sha256
from .storage import digest


MATERIAL_OCR_REGIONS_VERSION = "material-ocr-regions.v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_KEYS = {"selection", "image_sha256", "review_method", "review_id", "parent_selection_sha256"}


class MaterialOCRRegionFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(ok, code):
    if not ok:
        raise MaterialOCRRegionFailure(code)


def _sha(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _selection(raw):
    try:
        _require(isinstance(raw, dict) and set(raw) == set(EvidenceSelection.model_fields),
                 "MATERIAL_OCR_REGION_SELECTION_INVALID")
        selected = EvidenceSelection.model_validate(raw)
        # Reject implicit coercion/defaults, keeping exact frozen JSON identities.
        _require(digest(raw) == digest(selected.model_dump(mode="json")),
                 "MATERIAL_OCR_REGION_SELECTION_INVALID")
        return selected
    except (ValidationError, TypeError, ValueError):
        raise MaterialOCRRegionFailure("MATERIAL_OCR_REGION_SELECTION_INVALID") from None


def validate_reading_crops(config: dict) -> dict:
    """Validate a parent-image-hash map before any OCR is attempted.

    Scope/use/ambiguity across the selected index remains the caller's duty.
    Passing null is invalid; callers may omit the option or pass an empty map.
    """
    _require(isinstance(config, dict) and len(config) <= 2000,
             "MATERIAL_OCR_REGION_CONFIG_INVALID")
    for parent_hash, spec in config.items():
        _require(_sha(parent_hash) and isinstance(spec, dict) and set(spec) == _KEYS,
                 "MATERIAL_OCR_REGION_CONFIG_INVALID")
        _require(_sha(spec["image_sha256"]) and _sha(spec["parent_selection_sha256"])
                 and spec["image_sha256"] != parent_hash, "MATERIAL_OCR_REGION_CONFIG_INVALID")
        _require(isinstance(spec["review_method"], str)
                 and spec["review_method"] in {"operator_source_image_review", "agent_source_image_review"}
                 and isinstance(spec["review_id"], str)
                 and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", spec["review_id"]),
                 "MATERIAL_OCR_REGION_REVIEW_REQUIRED")
        selection = _selection(spec["selection"])
        targets = selection.pages if selection.format == "pdf_pages" else selection.package_parts
        _require(len(targets) == 1 and selection.rotate_clockwise == 0
                 and selection.crop_box_normalized is not None
                 and selection.crop_box_normalized != (0., 0., 1., 1.),
                 "MATERIAL_OCR_REGION_SCOPE_INVALID")
    return copy.deepcopy(config)


def derive_reading_region(parent_identity: dict, parent_attachment: dict,
                          parent_selection: EvidenceSelection | dict, spec: dict) -> dict:
    """Return authenticated cropped PNG bytes, child identity and projection.

    ``child_identity`` follows the single-region probe identity exactly: it
    copies the parent, replaces crop/selection/image hashes, and clears group
    bindings. Parent linkage lives in ``projection`` instead, so a child result
    cannot masquerade as successful full-image transcription.
    """
    _require(isinstance(parent_identity, dict) and _sha(parent_identity.get("image_sha256")),
             "MATERIAL_OCR_REGION_PARENT_INVALID")
    parent_hash = parent_identity["image_sha256"]
    validate_reading_crops({parent_hash: spec})
    parent = _selection(parent_selection.model_dump(mode="json")
                        if isinstance(parent_selection, EvidenceSelection) else parent_selection)
    child = _selection(spec["selection"])
    parent_targets = parent.pages if parent.format == "pdf_pages" else parent.package_parts
    child_targets = child.pages if child.format == "pdf_pages" else child.package_parts
    _require(len(parent_targets) == 1 and parent.crop_box_normalized is None and parent.rotate_clockwise == 0,
             "MATERIAL_OCR_REGION_PARENT_TRANSFORM_UNSUPPORTED")
    parent_selection_sha = digest(parent.model_dump(mode="json"))
    _require(parent_identity.get("selection_sha256") == parent_selection_sha == spec["parent_selection_sha256"],
             "MATERIAL_OCR_REGION_PARENT_SELECTION_CHANGED")
    _require(parent_identity.get("crop_box_normalized") is None and parent_identity.get("rotate_clockwise") == 0
             and parent_identity.get("format") == parent.format
             and parent_identity.get("render_dpi") == parent.render_dpi
             and parent_identity.get("source_id") == parent.source_id
             and parent_identity.get("source_sha256") == parent.source_sha256
             and parent_identity.get("target") == parent_targets[0]
             and isinstance(parent_identity.get("group_bindings"), list),
             "MATERIAL_OCR_REGION_PARENT_INVALID")
    _require(child.source_id == parent.source_id and child.source_sha256 == parent.source_sha256
             and child.format == parent.format and child_targets == parent_targets
             and child.render_dpi == parent.render_dpi and child.selection_id != parent.selection_id,
             "MATERIAL_OCR_REGION_OUTSIDE_PARENT_SCOPE")
    _require(isinstance(parent_attachment, dict) and parent_attachment.get("sha256") == parent_hash
             and isinstance(parent_attachment.get("path"), (str, Path)),
             "MATERIAL_OCR_REGION_PARENT_ATTACHMENT_INVALID")
    path = Path(parent_attachment["path"])
    try:
        _require(path.is_file() and 0 < path.stat().st_size <= 20_000_000,
                 "MATERIAL_OCR_REGION_PARENT_FILE_INVALID")
        original = path.read_bytes()
        _require(hashlib.sha256(original).hexdigest() == parent_hash,
                 "MATERIAL_OCR_REGION_PARENT_HASH_INVALID")
        with Image.open(io.BytesIO(original)) as image:
            _require(image.format == "PNG", "MATERIAL_OCR_REGION_PARENT_FORMAT_INVALID")
            data, dimensions = _selected_png(image, child)
        _require(file_sha256(path) == parent_hash, "MATERIAL_OCR_REGION_PARENT_CHANGED_DURING_READ")
    except MaterialOCRRegionFailure:
        raise
    except Exception:
        raise MaterialOCRRegionFailure("MATERIAL_OCR_REGION_RENDER_FAILED") from None
    _require(hashlib.sha256(data).hexdigest() == spec["image_sha256"],
             "MATERIAL_OCR_REGION_FROZEN_IMAGE_HASH_MISMATCH")
    identity = copy.deepcopy(parent_identity)
    identity.update(crop_box_normalized=list(child.crop_box_normalized),
                    selection_sha256=digest(child.model_dump(mode="json")),
                    image_sha256=spec["image_sha256"], group_bindings=[])
    projection = {"schema_version": MATERIAL_OCR_REGIONS_VERSION,
                  "parent_identity": copy.deepcopy(parent_identity), "child_identity": copy.deepcopy(identity),
                  "review_method": spec["review_method"], "review_id": spec["review_id"],
                  "scope": "partial_selected_region", "coordinate_space": "child_selected_region_pixels",
                  "whole_image_read": False, "reading_completeness": "not_established",
                  "original_appendix_replaced": False, "qualification_result": "not_determined",
                  "automatic_fact_adoption": False}
    projection["projection_sha256"] = digest(projection)
    return {"image_bytes": data, "dimensions": dimensions, "child_identity": identity,
            "projection": projection, "selection": child.model_dump(mode="json")}
