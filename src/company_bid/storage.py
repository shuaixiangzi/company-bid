"""Local M1 storage. This is deliberately not a database/lease implementation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class StorageError(RuntimeError):
    code = "STORAGE_ERROR"


class RunLockedError(StorageError):
    code = "RUN_LOCKED"


_INTEGRITY_REASONS = frozenset({"missing", "unreadable", "invalid_ref", "size_mismatch", "hash_mismatch",
                                "invalid_json", "metadata_mismatch"})
# These are application schema keys, never document labels or source IDs.
# Unknown mapping keys suppress the entire location instead of echoing data.
_ARTIFACT_SCHEMA_KEYS = frozenset({"data", "prepared", "package", "sources", "attachments", "documents", "units",
                                   "files", "bid_docx", "issues_docx", "source_artifacts", "snapshot_artifact",
                                   "source", "artifact", "input_ref", "output_ref"})


def _artifact_schema_path(parts) -> str | None:
    if not isinstance(parts, tuple) or len(parts) > 24:
        return None
    location = "$"
    for part in parts:
        if isinstance(part, str) and part in _ARTIFACT_SCHEMA_KEYS:
            location += "." + part
        elif isinstance(part, int) and not isinstance(part, bool) and 0 <= part < 1_000_000:
            location += f"[{part}]"
        else:
            return None
    return location


class ArtifactIntegrityError(StorageError):
    code = "CHECKPOINT_INVALID"

    def __init__(self, message: str, *, reference=None, reason="metadata_mismatch"):
        super().__init__(message)
        identifier = reference.get("artifact_id") if isinstance(reference, dict) else None
        self.artifact_id = identifier if isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier) else None
        self.reason = reason if isinstance(reason, str) and reason in _INTEGRITY_REASONS else "metadata_mismatch"
        self._schema_parts = None

    def diagnostic_context(self) -> dict:
        """Only for controlled error capture; never serialize the reference."""
        return {"artifact_id": self.artifact_id if isinstance(self.artifact_id, str) and
                re.fullmatch(r"[0-9a-f]{32}", self.artifact_id) else None,
                "reason": self.reason if isinstance(self.reason, str) and self.reason in _INTEGRITY_REASONS else "metadata_mismatch",
                "schema_path": _artifact_schema_path(self._schema_parts)}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _sync_directory(directory: Path) -> None:
    # Windows fsync does not support directory handles. File fsync and atomic
    # replacement still protect against partial JSON; full power-loss guarantees
    # depend on the filesystem and are outside this local prototype.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path: Path, value: Any, *, on_replace_retry=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, 4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                # Windows readers/antivirus can briefly deny replacement. Only
                # state writes with a durable retry recorder opt into retries.
                if (on_replace_retry is None or os.name != "nt" or attempt == 3
                        or getattr(error, "winerror", None) not in (5, 32, 33)):
                    raise
                on_replace_retry(attempt, error)
                with temporary.open("wb") as handle:
                    handle.write(json_bytes(value))
                    handle.flush()
                    os.fsync(handle.fileno())
                time.sleep(0.025 * attempt)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    """One nonblocking OS advisory lock; held for the whole local operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RunLockedError("This local run is already locked.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RunLockedError("This local run is already locked.") from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ArtifactIntegrityError("Invalid storage reference.", reason="invalid_ref")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ArtifactIntegrityError("Storage reference leaves its root.", reason="invalid_ref")
        return path

    def run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise StorageError("Invalid run identifier.")
        return self.resolve("runs/" + run_id)

    def read_state(self, run_id: str) -> dict:
        try:
            state = json.loads((self.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
            if state.get("run_id") != run_id or state.get("schema_version") != "m1.local.v1":
                raise ValueError
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError("Run state is unavailable or invalid.") from exc

    def write_state(self, state: dict) -> None:
        def record_retry(attempt, error):
            state.setdefault("storage_write_retries", []).append({
                "operation": "state_replace", "retry_number": attempt,
                "state_revision": state.get("revision"), "winerror": getattr(error, "winerror", None),
                "error_code": "FILE_REPLACE_BUSY", "recorded_at": datetime.now(timezone.utc).isoformat()})
        atomic_json(self.run_dir(state["run_id"]) / "state.json", state, on_replace_retry=record_retry)

    def put_bytes(self, run_id: str, name: str, content: bytes, media_type: str = "application/octet-stream") -> dict:
        directory = self.run_dir(run_id) / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64] or "artifact"
        identifier = uuid.uuid4().hex
        target = directory / (identifier + "-" + label)
        temporary = directory / ("." + identifier + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link publication is atomic and never overwrites an existing
            # artifact; both paths are on the same local filesystem.
            os.link(temporary, target)
            _sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return {"artifact_id": identifier, "path": target.relative_to(self.root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content), "media_type": media_type}

    def put_json(self, run_id: str, name: str, value: Any) -> dict:
        return self.put_bytes(run_id, name + ".json", json_bytes(value), "application/json")

    def read_bytes(self, reference: dict) -> bytes:
        if (not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or not reference["path"]
                or not isinstance(reference.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
                or not isinstance(reference.get("size_bytes"), int) or isinstance(reference["size_bytes"], bool)
                or reference["size_bytes"] < 0):
            raise ArtifactIntegrityError("An immutable artifact reference is invalid.", reference=reference, reason="invalid_ref")
        try:
            target = self.resolve(reference["path"])
        except (ArtifactIntegrityError, TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("An immutable artifact reference is invalid.", reference=reference, reason="invalid_ref") from exc
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("An immutable artifact is missing.", reference=reference, reason="missing") from exc
        except OSError as exc:
            raise ArtifactIntegrityError("An immutable artifact is unreadable.", reference=reference, reason="unreadable") from exc
        try:
            content = target.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("An immutable artifact is missing.", reference=reference, reason="missing") from exc
        except OSError as exc:
            raise ArtifactIntegrityError("An immutable artifact is unreadable.", reference=reference, reason="unreadable") from exc
        if len(content) != reference["size_bytes"]:
            raise ArtifactIntegrityError("An immutable artifact size has changed.", reference=reference, reason="size_mismatch")
        if hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise ArtifactIntegrityError("An immutable artifact hash has changed.", reference=reference, reason="hash_mismatch")
        return content

    def read_json(self, reference: dict) -> Any:
        try:
            return json.loads(self.read_bytes(reference))
        except (ValueError, UnicodeError) as exc:
            raise ArtifactIntegrityError("An immutable JSON artifact is invalid.", reference=reference, reason="invalid_json") from exc

    def validate_embedded_artifacts(self, value: Any) -> None:
        """Validate binary references embedded in a node/unit output as well."""
        def validate(item, location):
            if isinstance(item, dict):
                if {"artifact_id", "path", "sha256", "size_bytes"}.issubset(item):
                    try:
                        self.read_bytes(item)
                    except ArtifactIntegrityError as exc:
                        exc._schema_parts = location
                        raise
                for key, child in item.items():
                    validate(child, location + (key,))
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    validate(child, location + (index,))
        validate(value, ())
