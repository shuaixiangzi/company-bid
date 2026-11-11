"""Render explicitly selected proof pages/objects, with immutable provenance."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image, ImageOps
from pydantic import Field, model_validator

from .models import Contract, Sha256, SourceRef, Text, file_sha256
from .storage import digest


EVIDENCE_VERSION = "proof-pages-v2"


class EvidenceFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class EvidenceSelection(Contract):
    selection_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    source_id: Text
    source_sha256: Sha256
    title: Text
    format: Literal["pdf_pages", "docx_images"]
    pages: list[int] = Field(default_factory=list, max_length=100)
    package_parts: list[str] = Field(default_factory=list, max_length=100)
    selection_reason: Text
    render_dpi: int = Field(default=160, ge=120, le=240)
    crop_box_normalized: tuple[float, float, float, float] | None = None
    rotate_clockwise: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.crop_box_normalized:
            x0, y0, x1, y1 = self.crop_box_normalized
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("proof crop must stay inside the original image")
        if self.format == "pdf_pages":
            if not self.pages or self.package_parts or self.pages != sorted(set(self.pages)) or min(self.pages) < 1:
                raise ValueError("proof PDF selection requires an ordered, unique physical page scope")
        elif (not self.package_parts or self.pages or len(set(self.package_parts)) != len(self.package_parts)
              or any(not part.startswith("word/media/") or ".." in PurePosixPath(part).parts
                     or "\\" in part or not re.fullmatch(r"word/media/[^/]+\.(?:png|jpg|jpeg)", part, re.I)
                     for part in self.package_parts)):
            raise ValueError("proof DOCX selection requires exact embedded image package parts")
        return self


def _check_single_image(image):
    # Check the decoded source before transpose/crop copies can discard frame
    # metadata, or a small crop can hide an oversized original allocation.
    if min(image.size) < 1 or max(image.size) > 6000 or image.width * image.height > 18_000_000:
        raise EvidenceFailure("EVIDENCE_IMAGE_LIMIT_EXCEEDED")
    if getattr(image, "n_frames", 1) != 1:
        raise EvidenceFailure("EVIDENCE_ANIMATED_IMAGE_UNSUPPORTED")


def _png(image):
    _check_single_image(image)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA"):
        rgba = image.convert("RGBA")
        converted = Image.new("RGB", rgba.size, "white")
        converted.paste(rgba, mask=rgba.getchannel("A"))
    else:
        converted = image.convert("RGB")
    output = io.BytesIO(); converted.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > 20_000_000:
        raise EvidenceFailure("EVIDENCE_IMAGE_LIMIT_EXCEEDED")
    return data, list(converted.size)


def _selected_png(image, selection):
    _check_single_image(image)
    image = ImageOps.exif_transpose(image)
    if selection.crop_box_normalized:
        x0, y0, x1, y1 = selection.crop_box_normalized
        image = image.crop((round(x0 * image.width), round(y0 * image.height),
                            round(x1 * image.width), round(y1 * image.height)))
    if selection.rotate_clockwise:
        image = image.rotate(-selection.rotate_clockwise, expand=True)
    return _png(image)


def prepare_evidence(ctx, sources: list[dict], selections: list[dict], root: Path) -> dict:
    source_map = {source["source_id"]: source for source in sources}
    output = {"sources": [], "attachments": [], "artifacts": [], "pages": [], "producer_version": EVIDENCE_VERSION}
    for raw in selections:
        selection = EvidenceSelection.model_validate(raw)
        source = source_map.get(selection.source_id)
        if (not source or source["sha256"] != selection.source_sha256 or source["kind"] == "tender"
                or not Path(source["path"]).is_file() or file_sha256(Path(source["path"])) != selection.source_sha256):
            raise EvidenceFailure("EVIDENCE_SOURCE_IDENTITY_INVALID")
        if Path(source["path"]).stat().st_size > 200_000_000:
            raise EvidenceFailure("EVIDENCE_SOURCE_LIMIT_EXCEEDED")
        targets = selection.pages if selection.format == "pdf_pages" else selection.package_parts
        for index, target in enumerate(targets, 1):
            locator = f"pdf/page[{target}]" if selection.format == "pdf_pages" else "docx/" + target
            input_data = {"selection": selection.model_dump(mode="json"), "source": source,
                          "page_index": index, "target": target, "producer_version": EVIDENCE_VERSION}

            def convert(target=target, locator=locator):
                path = Path(source["path"])
                if file_sha256(path) != selection.source_sha256:
                    raise EvidenceFailure("EVIDENCE_SOURCE_CHANGED")
                if selection.format == "pdf_pages":
                    import pypdfium2 as pdfium
                    try:
                        with pdfium.PdfDocument(str(path)) as document:
                            if target > len(document):
                                raise EvidenceFailure("EVIDENCE_PAGE_OUT_OF_RANGE")
                            page = document[target - 1]
                            try:
                                width, height = page.get_size()
                                scale = selection.render_dpi / 72
                                if max(width, height) * scale > 6000 or width * height * scale * scale > 18_000_000:
                                    raise EvidenceFailure("EVIDENCE_IMAGE_LIMIT_EXCEEDED")
                                bitmap = page.render(scale=scale)
                                try:
                                    data, size = _selected_png(bitmap.to_pil(), selection)
                                finally:
                                    bitmap.close()
                            finally:
                                page.close()
                    except EvidenceFailure:
                        raise
                    except Exception as exc:
                        raise EvidenceFailure("EVIDENCE_PDF_RENDER_FAILED") from exc
                else:
                    try:
                        with zipfile.ZipFile(path) as archive:
                            exact_parts = [info for info in archive.infolist() if info.filename == target]
                            if len(exact_parts) > 1:
                                raise EvidenceFailure("EVIDENCE_DOCX_PART_AMBIGUOUS")
                            if not exact_parts:
                                raise EvidenceFailure("EVIDENCE_DOCX_IMAGE_INVALID")
                            info = exact_parts[0]
                            if info.file_size > 20_000_000:
                                raise EvidenceFailure("EVIDENCE_IMAGE_LIMIT_EXCEEDED")
                            raw_image = archive.read(info)
                        with Image.open(io.BytesIO(raw_image)) as decoded:
                            data, size = _selected_png(decoded, selection)
                    except EvidenceFailure:
                        raise
                    except Exception as exc:
                        raise EvidenceFailure("EVIDENCE_DOCX_IMAGE_INVALID") from exc
                reference = ctx.store_bytes("proof-page.png", data, "image/png")
                return {"image": reference, "dimensions": size, "source_id": source["source_id"],
                        "source_sha256": source["sha256"], "locator": locator, "selection_id": selection.selection_id,
                        "selection_hash": digest(selection.model_dump(mode="json")), "producer_version": EVIDENCE_VERSION,
                        "transform": {"crop_box_normalized": selection.crop_box_normalized,
                                      "rotate_clockwise": selection.rotate_clockwise}}

            rendered = ctx.run_unit("proof-" + digest(input_data)[:32], input_data, convert)
            scoped_id = "PROOF-SOURCE-" + digest([source["source_id"], locator, rendered["transform"]])[:24]
            scoped = {**source, "source_id": scoped_id, "locator": locator + (
                "; selected_image_transform=" + str(rendered["transform"]) if selection.crop_box_normalized or selection.rotate_clockwise else "")}
            SourceRef.model_validate(scoped)
            if scoped_id not in {item["source_id"] for item in output["sources"]}:
                output["sources"].append(scoped)
            output["artifacts"].append(rendered["image"])
            output["pages"].append(rendered)
            output["attachments"].append({"attachment_id": "PROOF-" + digest([selection.selection_id, locator])[:24],
                "section_id": "unassigned", "title": selection.title,
                "description": f"{selection.title}；选定证明第 {index}/{len(targets)} 页（对象），来源 {locator}。",
                "path": str(root / rendered["image"]["path"]), "sha256": rendered["image"]["sha256"],
                "source_refs": [scoped_id], "requirement_refs": ["unassigned"], "synthetic": source.get("synthetic", False),
                "bundle_id": selection.selection_id, "page_index": index, "page_count": len(targets)})
    ctx.capture("evidence-preparation", output)
    return output
