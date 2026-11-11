"""Import an authorized directory into a controlled, local M1 file catalog.

This is a content-addressed file catalog, not a database or a document parser.
The caller must authorize the selected sources and keep the destination outside
Git. Metadata and manifests may contain sensitive source paths. This module
never prints them and never extracts personal fields or document text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

IMPORT_VERSION = "m1-local-catalog-v1"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg"})
SOURCE_ROLES = frozenset({"tender_requirement", "user_authorized_fact", "derived_from_bid"})
DEFAULT_EXCLUDED_PATTERNS = ("半成品*", "~$*")
_PAGE_PATTERN = re.compile(r"(?:页\s*(\d+)(?:\s*[-－—~～至]\s*(\d+))?|第\s*(\d+)(?:\s*[-－—~～至]\s*(\d+))?\s*页)")


class SourceImportError(ValueError):
    """A source or stored object cannot safely be imported."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _validate_format(path: Path, suffix: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(8)
    valid = (
        (suffix == ".pdf" and header.startswith(b"%PDF-"))
        or (suffix == ".png" and header == b"\x89PNG\r\n\x1a\n")
        or (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
    )
    if suffix in {".docx", ".xlsx"}:
        required = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.namelist()
                valid = "[Content_Types].xml" in members and required in members
                valid = valid and not any(item.flag_bits & 1 for item in archive.infolist())
        except (OSError, zipfile.BadZipFile):
            valid = False
    if not valid:
        raise SourceImportError("unsupported_or_mismatched_file_signature")


def _filename_pages(name: str) -> dict[str, Any] | None:
    match = _PAGE_PATTERN.search(Path(name).stem)
    if match is None:
        return None
    start = int(match.group(1) or match.group(3))
    end = int(match.group(2) or match.group(4) or start)
    if start < 1 or end < start:
        return None
    return {
        "start": start,
        "end": end,
        "status": "filename_inferred",
        "page_number_space": "unverified_parent_document",
        "verification": "not_checked_against_parent",
    }


def _metadata_for(relative: str, metadata: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for prefix, values in sorted(metadata.items(), key=lambda item: len(item[0])):
        normalized = prefix.replace("\\", "/")
        if relative == normalized or (normalized.endswith("/") and relative.startswith(normalized)):
            result.update(values)
    return result


def import_directory(
    source_dir: str | Path,
    destination: str | Path,
    *,
    source_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    exclude_patterns: Sequence[str] = DEFAULT_EXCLUDED_PATTERNS,
    include_patterns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Freeze authorized originals, deduplicate bytes, and return a manifest.

    Per-source metadata keys are source-relative paths or directory prefixes
    ending in '/'. Their ``source_role`` can be ``tender_requirement``,
    ``user_authorized_fact`` (default), or ``derived_from_bid``. Derived material
    is always labelled as a copy from a bid, never an independent certificate.
    ``parent_source_sha256`` and ``locator`` retain supplied provenance without
    guessing that filename page numbers were visually verified.

    Objects and manifests use hashes as storage keys; original names are metadata
    only. Repeating the same import yields the same catalog id and manifest bytes.
    No OCR, network request, database, or provider is used.
    """
    requested_source = Path(source_dir)
    requested_destination = Path(destination)
    if _is_link(requested_source) or _is_link(requested_destination):
        raise SourceImportError("linked_catalog_root_not_allowed")
    source = requested_source.resolve(strict=True)
    target = requested_destination.resolve()
    if not source.is_dir():
        raise SourceImportError("source_directory_required")
    if source == target or source in target.parents or target in source.parents:
        raise SourceImportError("source_and_catalog_must_not_overlap")
    if any(_is_link(parent) for parent in requested_destination.absolute().parents):
        raise SourceImportError("linked_catalog_parent_not_allowed")
    objects = target / "objects"
    manifests = target / "manifests"
    if _is_link(objects) or _is_link(manifests):
        raise SourceImportError("linked_catalog_storage_not_allowed")
    objects.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    metadata = source_metadata or {}
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    paths: list[Path] = []
    for current, dirs, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for directory in list(dirs):
            item = current_path / directory
            if _is_link(item):
                skipped.append({"relative_path": item.relative_to(source).as_posix(), "reason": "linked_directory"})
                dirs.remove(directory)
        paths.extend(current_path / name for name in files)
    for path in sorted(paths, key=lambda item: item.relative_to(source).as_posix().casefold()):
        relative = path.relative_to(source).as_posix()
        relpath = Path(relative)
        if _is_link(path):
            skipped.append({"relative_path": relative, "reason": "linked_file"})
            continue
        if any(relpath.match(pattern) or path.name == pattern for pattern in exclude_patterns):
            skipped.append({"relative_path": relative, "reason": "excluded_by_policy"})
            continue
        if include_patterns is not None and not any(relpath.match(pattern) for pattern in include_patterns):
            skipped.append({"relative_path": relative, "reason": "not_selected"})
            continue
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            skipped.append({"relative_path": relative, "reason": "unsupported_file_type"})
            continue
        properties = _metadata_for(relative, metadata)
        role = properties.get("source_role", "user_authorized_fact")
        if role not in SOURCE_ROLES:
            raise SourceImportError("invalid_source_role")
        if "source_page_range" in properties:
            raise SourceImportError("use_locator_for_verified_pages_filename_pages_remain_inferred")
        before = path.stat()
        _validate_format(path, suffix)
        temporary: Path | None = None
        try:
            with path.open("rb") as original, tempfile.NamedTemporaryFile(dir=objects, prefix=".import-", delete=False) as copy:
                temporary = Path(copy.name)
                digest = hashlib.sha256()
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    digest.update(chunk)
                    copy.write(chunk)
                copy.flush()
                os.fsync(copy.fileno())
            after = path.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise SourceImportError("source_changed_during_import")
            content_hash = digest.hexdigest()
            stored = objects / content_hash
            if stored.exists():
                if _is_link(stored) or stored.stat().st_size != after.st_size or _sha256(stored) != content_hash:
                    raise SourceImportError("existing_object_failed_integrity_check")
            else:
                # A hard link publishes complete bytes without replacing an existing object.
                try:
                    os.link(temporary, stored)
                except FileExistsError:
                    if _is_link(stored) or _sha256(stored) != content_hash:
                        raise SourceImportError("concurrent_object_failed_integrity_check") from None
                # Windows read-only attributes apply to every hard link. Remove
                # our temporary link before setting the shared object's flag.
                temporary.unlink()
                temporary = None
                stored.chmod(0o444)
            entry: dict[str, Any] = {
                "source_id": "src-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20],
                "source_path": str(path),
                "relative_path": relative,
                "sha256": content_hash,
                "size_bytes": after.st_size,
                "format": suffix.lstrip("."),
                "stored_path": str(stored),
                "source_role": role,
                "identity": "derived_from_bid" if role == "derived_from_bid" else role,
                "independent_original": False if role == "derived_from_bid" else None,
                "fact_assurance": "user_authorized_not_automatically_reverified" if role != "tender_requirement" else "requirement_source_not_company_fact",
                "source_page_range": _filename_pages(path.name),
                "provenance": {key: value for key, value in properties.items() if key != "source_role"},
            }
            entries.append(entry)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "producer_version": IMPORT_VERSION,
        "catalog_kind": "controlled_local_m1_file_catalog",
        "database_imported": False,
        "source_root": str(source),
        "source_read_only": True,
        "content_extracted": False,
        "entries": entries,
        "skipped": sorted(skipped, key=lambda item: item["relative_path"]),
        "counts": {"source_files": len(entries), "unique_objects": len({entry["sha256"] for entry in entries}), "skipped": len(skipped)},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    catalog_id = hashlib.sha256(canonical).hexdigest()
    payload["catalog_id"] = catalog_id
    manifest_path = manifests / (catalog_id + ".json")
    serialized = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if manifest_path.exists():
        if _is_link(manifest_path) or manifest_path.read_bytes() != serialized:
            raise SourceImportError("existing_manifest_failed_integrity_check")
    else:
        try:
            with manifest_path.open("xb") as output:
                output.write(serialized)
                output.flush()
                os.fsync(output.fileno())
            manifest_path.chmod(0o444)
        except FileExistsError:
            if manifest_path.read_bytes() != serialized:
                raise SourceImportError("concurrent_manifest_failed_integrity_check") from None
    return {**payload, "manifest_path": str(manifest_path)}
