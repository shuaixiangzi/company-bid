"""Local M1 storage. This is deliberately not a database/lease implementation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class StorageError(RuntimeError):
    code = "STORAGE_ERROR"


class RunLockedError(StorageError):
    code = "RUN_LOCKED"


class ArtifactIntegrityError(StorageError):
    code = "CHECKPOINT_INVALID"


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


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
            raise ArtifactIntegrityError("Invalid storage reference.")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ArtifactIntegrityError("Storage reference leaves its root.")
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
        atomic_json(self.run_dir(state["run_id"]) / "state.json", state)

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
        try:
            content = self.resolve(reference["path"]).read_bytes()
            if len(content) != reference["size_bytes"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise ValueError
            return content
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("An immutable artifact is missing or has changed.") from exc

    def read_json(self, reference: dict) -> Any:
        try:
            return json.loads(self.read_bytes(reference))
        except (ValueError, UnicodeError) as exc:
            raise ArtifactIntegrityError("An immutable JSON artifact is invalid.") from exc

    def validate_embedded_artifacts(self, value: Any) -> None:
        """Validate binary references embedded in a node/unit output as well."""
        if isinstance(value, dict):
            if {"artifact_id", "path", "sha256", "size_bytes"}.issubset(value):
                self.read_bytes(value)
            for item in value.values():
                self.validate_embedded_artifacts(item)
        elif isinstance(value, list):
            for item in value:
                self.validate_embedded_artifacts(item)
