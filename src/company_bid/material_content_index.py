"""N3 selected-original text index, separate from frozen N1 and semantic input.

Only selected PDF pages are read natively. Selected DOCX image bytes may be
hashed, but parent paragraphs and unselected images are never parsed here.
This module has no OCR/provider adapter and makes no qualification decisions.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import zipfile

import pdfplumber

from .evidence import EvidenceSelection
from .material_matching import _proof_groups
from .models import file_sha256
from .reading import (READER_VERSION, ReadingConfig, ReadingUnit, SourceDocument,
                      _read_pdf_page, _UnitFailure)
from .storage import digest


MATERIAL_CONTENT_INDEX_VERSION = "material-content-index.v1"
MATERIAL_CONTENT_PARSED_SCHEMA = "material.content.parsed.v1"


class MaterialContentFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise MaterialContentFailure(code)


def group_content_binding(group: dict) -> dict:
    """The exact attachment group must own each projected source scope."""
    return {key: copy.deepcopy(group[key]) for key in
            ("group_id", "origin", "attachment_ids", "source_refs")}


def _verify_file(source):
    path = Path(source["path"])
    _require(path.is_file(), "MATERIAL_CONTENT_SOURCE_MISSING")
    _require(path.stat().st_size <= 200_000_000, "MATERIAL_CONTENT_SOURCE_LIMIT_EXCEEDED")
    _require(file_sha256(path) == source["sha256"], "MATERIAL_CONTENT_SOURCE_HASH_MISMATCH")


def _text(unit):
    return "\n".join(value for block in unit["blocks"] for value in
                     [block.get("text", ""), *[" | ".join(row) for row in block.get("rows", [])]]
                     if value.strip())


def _unit(source_id, target, status="needs_ocr", *, error=None):
    fmt = "pdf" if isinstance(target, int) else "docx"
    locator = f"pdf/page[{target}]" if fmt == "pdf" else "docx/" + target
    return {"unit_id": "material-content-" + digest([source_id, target])[:24],
            "source_id": source_id, "locator": locator,
            "page": target if fmt == "pdf" else None, "blocks": [],
            "coverage": {"status": status, "origin": "native", "native_text_chars": 0,
                         "unread_objects": [locator] if status not in {"read", "empty"} else [],
                         "error_code": error}}


def _existing_pdf(parsed, source, page):
    matches = [unit for doc in parsed.get("documents", [])
               if doc.get("source_id") == source["source_id"] and doc.get("sha256") == source["sha256"]
               and doc.get("role") != "tender" for unit in doc.get("units", [])
               if unit.get("source_id") == source["source_id"] and unit.get("page") == page]
    _require(len(matches) <= 1, "MATERIAL_CONTENT_EXISTING_PAGE_AMBIGUOUS")
    if matches and matches[0].get("coverage", {}).get("status") in {"read", "empty"}:
        unit = ReadingUnit.model_validate(matches[0]).model_dump(mode="json")
        _require(all(b["source_id"] == source["source_id"] and
                     b["locator"].startswith(f"pdf/page[{page}]/") for b in unit["blocks"]),
                 "MATERIAL_CONTENT_EXISTING_PAGE_SCOPE_INVALID")
        return unit
    return None


def _docx_projection(parsed, source, part, image_hash):
    """Copy only a reader-owned selected image and its own OCR descendants."""
    matches = []
    for doc in parsed.get("documents", []):
        if (doc.get("source_id") != source["source_id"] or doc.get("sha256") != source["sha256"]
                or doc.get("role") == "tender"):
            continue
        for raw in doc.get("units", []):
            images = [b for b in raw.get("blocks", []) if b.get("kind") == "image"
                      and b.get("source_id") == source["source_id"]
                      and b.get("image_ref", {}).get("package_part") == part]
            for image in images:
                _require(image.get("image_ref", {}).get("sha256") == image_hash,
                         "MATERIAL_CONTENT_IMAGE_HASH_MISMATCH")
                prefix = image["locator"] + "/ocr/"
                ocr = [b for b in raw.get("blocks", []) if b.get("source_id") == source["source_id"]
                       and b.get("origin") == "ocr" and b.get("locator", "").startswith(prefix)]
                complete = raw.get("coverage", {}).get("status") in {"read", "empty"}
                unit = _unit(source["source_id"], part,
                             "read" if complete and ocr else "needs_ocr")
                unit["blocks"] = copy.deepcopy([image, *ocr] if complete else [image])
                # The selected image is its own unit; no parent text/coverage is borrowed.
                unit["coverage"]["origin"] = "ocr" if complete and ocr else "native"
                matches.append(unit)
    if len(matches) == 1:
        return matches[0]
    unit = _unit(source["source_id"], part)
    if len(matches) > 1:
        # A selected package part is an immutable image asset even when the
        # bid places it at several XML positions. Do not pick an arbitrary
        # OCR position or borrow surrounding text as a resolution.
        unit["coverage"]["warnings"] = ["REPEATED_SELECTED_IMAGE_REFERENCE_OCR_NOT_ADOPTED"]
    unit["blocks"] = [{"block_id": unit["unit_id"] + "-image", "kind": "image", "text": "", "rows": [],
                       "locator": unit["locator"] + "/image[0]", "source_id": source["source_id"],
                       "origin": "native", "image_ref": {"package_part": part, "sha256": image_hash}}]
    return unit


def build_material_content_index(ctx, manifest: dict, parsed: dict) -> dict:
    """Build a read-only selected view with durable per-page/image checkpoints.

    Consume ``index['parsed']`` only for a *new* deterministic report after any
    authenticated historical semantic queries. Never substitute it for frozen
    N1 or the historical semantic catalog. ``summary``, ``groups`` and
    ``coverage`` are controlled-report metadata suitable for N3 diagnostics.
    """
    _require(isinstance(manifest, dict) and isinstance(parsed, dict), "MATERIAL_CONTENT_INPUT_INVALID")
    sources = {s["source_id"]: s for s in manifest.get("sources", [])}
    _require(len(sources) == len(manifest.get("sources", [])), "MATERIAL_CONTENT_SOURCE_ID_DUPLICATE")
    selections = {s["selection_id"]: EvidenceSelection.model_validate(s).model_dump(mode="json")
                  for s in manifest.get("evidence_selections", [])}
    _require(len(selections) == len(manifest.get("evidence_selections", [])),
             "MATERIAL_CONTENT_SELECTION_ID_DUPLICATE")
    groups = _proof_groups(manifest.get("attachments", []), manifest.get("material_records", []),
                           manifest.get("sources"), manifest.get("evidence_selections"),
                           manifest.get("proof_derivations"))
    bindings = [group_content_binding(group) for group in groups]
    tasks, group_rows = {}, []
    for group, binding in zip(groups, bindings, strict=True):
        origin = group.get("origin") or {}
        selection = selections.get(origin.get("selection_id"), {})
        targets = origin.get("pages", []) if origin.get("format") == "pdf_pages" else origin.get("package_parts", [])
        row = {**binding, "source_bound": group["source_bound"], "units": []}
        group_rows.append(row)
        if not group["source_bound"] or not targets:
            row.update(status="unbound_or_unsupported_scope", unread_count=max(1, group["page_count"]))
            continue
        for target in targets:
            key = (origin["source_id"], origin["source_sha256"], origin["format"], target,
                   digest(selection.get("crop_box_normalized")))
            task = tasks.setdefault(key, {"source_id": key[0], "source_sha256": key[1], "format": key[2],
                                         "target": target, "crop_box_normalized": selection.get("crop_box_normalized"),
                                         "group_bindings": [], "rows": []})
            task["group_bindings"].append(binding)
            task["rows"].append(row)
    documents, unit_reports, verified = {}, [], set()
    config = ReadingConfig(required_roles=[], ocr_roles=[])
    # Page handles stay inside the unit closure. Successful checkpoints avoid
    # reopening/re-extracting their pages on recovery.
    for task in tasks.values():
        source = sources.get(task["source_id"], {})
        _require(source.get("sha256") == task["source_sha256"] and source.get("kind") != "tender",
                 "MATERIAL_CONTENT_SOURCE_BINDING_INVALID")
        if source["source_id"] not in verified:
            _verify_file(source)
            verified.add(source["source_id"])
        target, fmt = task["target"], task["format"]
        existing = (_existing_pdf(parsed, source, target) if fmt == "pdf_pages"
                    and not task["crop_box_normalized"] else None)
        image_hash = None
        if fmt == "docx_images":
            try:
                with zipfile.ZipFile(source["path"]) as archive:
                    _require(archive.getinfo(target).file_size <= 20_000_000,
                             "MATERIAL_CONTENT_IMAGE_LIMIT_EXCEEDED")
                    image_hash = hashlib.sha256(archive.read(target)).hexdigest()
            except MaterialContentFailure:
                raise
            except Exception:
                raise MaterialContentFailure("MATERIAL_CONTENT_SELECTED_IMAGE_UNREADABLE") from None
            if not task["crop_box_normalized"]:
                existing = _docx_projection(parsed, source, target, image_hash)
        input_data = {"version": MATERIAL_CONTENT_INDEX_VERSION, "reader_version": READER_VERSION,
                      "source": source, "target": target, "format": fmt,
                      "group_bindings": task["group_bindings"], "crop_box_normalized": task["crop_box_normalized"],
                      "existing_selected_unit_sha256": digest(existing) if existing else None,
                      "image_sha256": image_hash, "reading_config": config.model_dump(mode="json"),
                      "ocr_allowed": False}

        def read_one(source=source, target=target, fmt=fmt, existing=existing, input_data=input_data):
            if existing is not None:
                value, method = copy.deepcopy(existing), "frozen_selected_unit_reused" if fmt == "pdf_pages" else "selected_image_projection"
            elif input_data["crop_box_normalized"]:
                value, method = _unit(source["source_id"], target, "unsupported",
                                      error="MATERIAL_CONTENT_CROP_NATIVE_UNSUPPORTED"), "unsupported_selected_crop"
            elif fmt == "pdf_pages":
                native_source = SourceDocument(**{**source, "role": "company_material"})
                try:
                    with pdfplumber.open(source["path"]) as pdf:
                        _require(type(target) is int and 1 <= target <= len(pdf.pages),
                                 "MATERIAL_CONTENT_PAGE_OUT_OF_RANGE")
                        page = pdf.pages[target - 1]
                        try:
                            value = _read_pdf_page(native_source, page, target, config, None).model_dump(mode="json")
                        finally:
                            page.close()
                except _UnitFailure as exc:
                    value = exc.unit.model_dump(mode="json")
                except MaterialContentFailure:
                    raise
                except Exception:
                    raise MaterialContentFailure("MATERIAL_CONTENT_PDF_UNREADABLE") from None
                # Parse failures are retryable, never successful empty checkpoints.
                _require(value["coverage"]["status"] != "failed", "MATERIAL_CONTENT_PAGE_PARSE_FAILED")
                method = "selected_pdf_native_only"
            else:
                raise MaterialContentFailure("MATERIAL_CONTENT_FORMAT_UNSUPPORTED")
            _verify_file(source)
            text = _text(value)
            result = {"input_sha256": digest(input_data), "source_id": source["source_id"],
                      "source_sha256": source["sha256"], "target": target, "format": fmt,
                      "group_bindings": input_data["group_bindings"], "unit": value, "method": method,
                      "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_char_count": len(text),
                      "qualification_result": "not_determined", "project_assignment": "not_made"}
            result["output_sha256"] = digest(result)
            return result

        unit_id = "material-content-" + digest([task["source_id"], task["source_sha256"], fmt, target,
                                               task["crop_box_normalized"]])[:24]
        result = ctx.run_unit(unit_id, input_data, read_one)
        _require(result.get("input_sha256") == digest(input_data) and
                 result.get("group_bindings") == task["group_bindings"] and
                 result.get("output_sha256") == digest({k: v for k, v in result.items() if k != "output_sha256"}),
                 "MATERIAL_CONTENT_CHECKPOINT_BINDING_INVALID")
        value = copy.deepcopy(result["unit"])
        value = ReadingUnit.model_validate(value).model_dump(mode="json")
        _require(value["source_id"] == source["source_id"] and
                 (fmt != "pdf_pages" or value.get("page") == target), "MATERIAL_CONTENT_UNIT_IDENTITY_INVALID")
        # Explicit partial native text is a candidate channel, not whole-page coverage.
        value["material_content_native_readable"] = (result["method"] == "selected_pdf_native_only"
                                                      and all(b.get("origin", "native") == "native" for b in value["blocks"]))
        value["material_content_group_ids"] = [b["group_id"] for b in task["group_bindings"]]
        doc = documents.setdefault(source["source_id"], {"source_id": source["source_id"], "sha256": source["sha256"],
            "title": source.get("title", source["source_id"]), "role": "company_material", "format": fmt, "units": []})
        doc["units"].append(value)
        meta = {k: copy.deepcopy(v) for k, v in result.items() if k not in {"unit", "group_bindings"}}
        meta.update(unit_id=value["unit_id"], status=value["coverage"]["status"],
                    unread_objects=copy.deepcopy(value["coverage"].get("unread_objects", [])),
                    group_ids=[b["group_id"] for b in task["group_bindings"]])
        unit_reports.append(meta)
        for row in task["rows"]:
            row["units"].append(copy.deepcopy(meta))
    for sid in verified:
        _verify_file(sources[sid])
    for row in group_rows:
        if row["units"]:
            row["unread_count"] = sum(u["status"] not in {"read", "empty"} for u in row["units"])
            row["status"] = "unread_or_incomplete" if row["unread_count"] else "read"
    counts = {status: sum(u["status"] == status for u in unit_reports)
              for status in ("read", "empty", "needs_ocr", "unsupported")}
    coverage = {"selected_unit_count": len(unit_reports), "selected_pdf_pages": sum(u["format"] == "pdf_pages" for u in unit_reports),
                "selected_docx_images": sum(u["format"] == "docx_images" for u in unit_reports),
                "status_counts": counts, "units_with_text": sum(u["text_char_count"] > 0 for u in unit_reports),
                "native_text_char_count": sum(u["text_char_count"] for u in unit_reports if u["format"] == "pdf_pages"),
                "unread_unit_count": sum(u["status"] not in {"read", "empty"} for u in unit_reports),
                "unbound_group_count": sum(r["status"] == "unbound_or_unsupported_scope" for r in group_rows),
                "whole_company_search_completed": False, "ocr_calls": 0, "model_calls": 0}
    projection = {"schema_version": MATERIAL_CONTENT_PARSED_SCHEMA, "producer_version": MATERIAL_CONTENT_INDEX_VERSION,
                  "group_bindings": bindings, "documents": list(documents.values()), "coverage": coverage}
    output = {"schema_version": "material.content.index.v1", "producer_version": MATERIAL_CONTENT_INDEX_VERSION,
              "input_manifest_sha256": digest(manifest), "input_parsed_sha256": digest(parsed),
              "source_scope_sha256": digest(bindings), "parsed": projection, "groups": group_rows,
              "coverage": coverage, "units": unit_reports,
              "summary": {"source_document_count": len(documents), "group_count": len(group_rows),
                          "scope": "selected_original_pages_and_images_only", "parent_docx_text_read": False,
                          "qualification_result": "not_determined", "project_assignment": "not_made"}}
    output["output_sha256"] = digest(output)
    ctx.capture("material-content-index", output)
    return output
