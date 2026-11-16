"""Add explicitly selected, source-bound DOCX proof images during N3.

This is a narrow material inventory repair, not another N1 reader. It preserves
the frozen parsed document and never creates company facts or qualifications.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence import EvidenceFailure, EvidenceSelection, prepare_evidence
from .intake import IntakeManifest
from .models import file_sha256
from .reading import SourceDocument, _docx_paragraph
from .storage import digest


MATERIAL_SOURCE_SELECTION_VERSION = "material-source-selection.v1"


class MaterialImageSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    selection_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_block_id: str = Field(min_length=1)
    heading_locator: str = Field(pattern=r"^word/document\.xml/body/block\[[0-9]+\]$")
    heading_text: str = Field(min_length=1, max_length=200)
    image_block_id: str = Field(min_length=1)
    image_locator: str = Field(pattern=r"^word/document\.xml/body/block\[[0-9]+\]/image\[0\]$")
    package_part: str = Field(pattern=r"^word/media/[^/\\]+\.(?:png|jpg|jpeg)$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _require(condition, code):
    if not condition:
        raise EvidenceFailure(code)


def _verify_file(source):
    path = Path(source["path"])
    _require(path.is_file() and file_sha256(path) == source["sha256"], "MATERIAL_SELECTION_SOURCE_CHANGED")
    _require(path.stat().st_size <= 200_000_000, "MATERIAL_SELECTION_SOURCE_LIMIT_EXCEEDED")


def _bind(selection, manifest, parsed, blocks, units):
    sources = [s for s in manifest["sources"] if s["source_id"] == selection.source_id]
    _require(len(sources) == 1, "MATERIAL_SELECTION_SOURCE_INVALID")
    source = sources[0]
    role = manifest.get("source_roles", {}).get(selection.source_id)
    _require(source["kind"] != "tender" and role in {"company_material", "reference_bid"}
             and source["sha256"] == selection.source_sha256, "MATERIAL_SELECTION_SOURCE_INVALID")
    docs = [d for d in parsed.get("documents", []) if d["source_id"] == selection.source_id]
    _require(len(docs) == 1 and docs[0].get("format") == "docx" and docs[0].get("role") == role
             and docs[0]["sha256"] == selection.source_sha256, "MATERIAL_SELECTION_PARSED_SCOPE_INVALID")
    heading, image = blocks.get(selection.heading_block_id), blocks.get(selection.image_block_id)
    _require(bool(heading and image), "MATERIAL_SELECTION_BLOCK_INVALID")
    _require(all(b["source_id"] == selection.source_id and b.get("origin") == "native"
                 and not b.get("read_disposition") for b in (heading, image)), "MATERIAL_SELECTION_BLOCK_INVALID")
    _require(heading["kind"] == "paragraph" and heading["locator"] == selection.heading_locator
             and heading["text"] == selection.heading_text and selection.heading_text.strip()
             and selection.heading_text == selection.heading_text.strip()
             and units[heading["unit_id"]].get("coverage", {}).get("status") == "read",
             "MATERIAL_SELECTION_HEADING_INVALID")
    _require(image["kind"] == "image" and image["locator"] == selection.image_locator
             and (image.get("image_ref") or {}).get("package_part") == selection.package_part
             and image["image_ref"].get("sha256") == selection.image_sha256
             and units[image["unit_id"]].get("coverage", {}).get("status") in {"read", "needs_ocr"},
             "MATERIAL_SELECTION_IMAGE_INVALID")
    hi = int(re.search(r"block\[([0-9]+)\]", selection.heading_locator).group(1))
    ii = int(re.search(r"block\[([0-9]+)\]", selection.image_locator).group(1))
    _require(ii == hi + 1 and units[heading["unit_id"]]["locator"] == selection.heading_locator
             and units[image["unit_id"]]["locator"] + "/image[0]" == selection.image_locator,
             "MATERIAL_SELECTION_ADJACENCY_INVALID")
    _verify_file(source)
    return source, role, hi, ii


def _verify_native(selection, source, role, hi, ii):
    """Recheck the exact XML positions; supplied N1 text cannot invent a title."""
    _verify_file(source)
    try:
        document = Document(source["path"])
        body = list(document.element.body)
        _require(ii < len(body) and body[hi].tag == qn("w:p") and body[ii].tag == qn("w:p"),
                 "MATERIAL_SELECTION_NATIVE_SCOPE_INVALID")
        source_doc = SourceDocument.model_validate({**source, "role": role})
        heading, heading_requests, heading_unsupported = _docx_paragraph(
            source_doc, body[hi], document.part, selection.heading_locator)
        images, image_requests, image_unsupported = _docx_paragraph(
            source_doc, body[ii], document.part, selection.image_locator.rsplit("/image[", 1)[0])
        _require(not heading_requests and not heading_unsupported and len(heading) == 1
                 and heading[0].kind == "paragraph" and heading[0].block_id == selection.heading_block_id
                 and heading[0].text == selection.heading_text, "MATERIAL_SELECTION_NATIVE_HEADING_MISMATCH")
        _require(not image_unsupported and len(images) == 1 and len(image_requests) == 1
                 and images[0].kind == "image" and images[0].block_id == selection.image_block_id
                 and images[0].locator == selection.image_locator
                 and images[0].image_ref["package_part"] == selection.package_part
                 and images[0].image_ref["sha256"] == selection.image_sha256,
                 "MATERIAL_SELECTION_NATIVE_IMAGE_MISMATCH")
    except EvidenceFailure:
        raise
    except Exception:
        raise EvidenceFailure("MATERIAL_SELECTION_NATIVE_READ_FAILED") from None
    _verify_file(source)
    return {"selection": selection.model_dump(mode="json"), "producer_version": MATERIAL_SOURCE_SELECTION_VERSION,
            "binding_rule": "explicit_adjacent_native_docx_heading_and_image",
            "image_text_read": False, "qualification_result": "not_determined", "project_assignment": "not_made"}


def extend_material_evidence(ctx, manifest: dict, parsed: dict, selections: list) -> dict:
    """Return an extended copy; original sources and N1/N2 remain unchanged.

    Each selection is an exact source/image/heading binding, without a path,
    generated title, OCR text or fact field. Scope verification and conversion
    have durable independent checkpoints on the existing executor.
    """
    from .extraction import _index

    try:
        _require(isinstance(selections, list) and len(selections) <= 20, "MATERIAL_SELECTION_LIMIT_INVALID")
        requested = [MaterialImageSelection.model_validate(item) for item in selections]
    except ValidationError:
        raise EvidenceFailure("MATERIAL_SELECTION_INPUT_INVALID") from None
    if not requested:
        return copy.deepcopy(manifest)
    blocks, units, _ = _index(parsed, manifest)
    existing_ids = {s["selection_id"] for s in manifest.get("evidence_selections", [])}
    existing_images = {(s["source_id"], p) for s in manifest.get("evidence_selections", [])
                       for p in s.get("package_parts", [])}
    bound = []
    for selection in requested:
        _require(selection.selection_id not in existing_ids and
                 (selection.source_id, selection.package_part) not in existing_images,
                 "MATERIAL_SELECTION_DUPLICATE")
        existing_ids.add(selection.selection_id)
        existing_images.add((selection.source_id, selection.package_part))
        bound.append((selection, *_bind(selection, manifest, parsed, blocks, units)))
    proofs, additions = [], []
    for selection, source, role, hi, ii in bound:
        input_data = {"selection": selection.model_dump(mode="json"), "source": source,
                      "parsed_sha256": digest(parsed), "producer_version": MATERIAL_SOURCE_SELECTION_VERSION}
        proof = ctx.run_unit("material-source-" + digest(input_data)[:24], input_data,
            lambda selection=selection, source=source, role=role, hi=hi, ii=ii:
                _verify_native(selection, source, role, hi, ii))
        proofs.append(proof)
        additions.append(EvidenceSelection(selection_id=selection.selection_id, source_id=selection.source_id,
            source_sha256=selection.source_sha256, title=selection.heading_text, format="docx_images",
            package_parts=[selection.package_part],
            selection_reason="Explicit frozen native heading/image binding: " + digest(proof)).model_dump(mode="json"))
    output = copy.deepcopy(manifest)
    evidence = prepare_evidence(ctx, output["sources"], additions, ctx.executor.store.root)
    source_ids = {s["source_id"] for s in output["sources"]}
    attachment_ids = {a["attachment_id"] for a in output.get("attachments", [])}
    for source in evidence["sources"]:
        _require(source["source_id"] not in source_ids, "MATERIAL_SELECTION_SOURCE_ID_COLLISION")
        source_ids.add(source["source_id"])
        output["sources"].append(source)
        output["source_roles"][source["source_id"]] = "provenance_only"
    for attachment in evidence["attachments"]:
        _require(attachment["attachment_id"] not in attachment_ids, "MATERIAL_SELECTION_ATTACHMENT_ID_COLLISION")
        attachment_ids.add(attachment["attachment_id"])
        output.setdefault("attachments", []).append(attachment)
    output.setdefault("evidence_selections", []).extend(additions)
    IntakeManifest.model_validate(output)
    _index(parsed, output)
    for _, source, *_ in bound:
        _verify_file(source)
    ctx.capture("material-source-selection", {"producer_version": MATERIAL_SOURCE_SELECTION_VERSION,
        "input_manifest_sha256": digest(manifest), "output_manifest_sha256": digest(output),
        "parsed_sha256": digest(parsed), "bindings": proofs, "artifacts": evidence["artifacts"],
        "added_source_ids": [s["source_id"] for s in evidence["sources"]],
        "added_attachment_ids": [a["attachment_id"] for a in evidence["attachments"]],
        "facts_added": 0, "qualification_result": "not_determined", "project_assignment": "not_made"})
    return output
