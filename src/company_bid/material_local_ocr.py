"""Bounded, captured Windows OCR transcription; never a fact/qualification judge.

The installed OS recognizer is the only engine. There are no network calls,
model reservations, implicit resizing, fallback engines or automatic retries.
The caller owns selected-original/crop provenance and durable unit checkpoints.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from PIL import Image

from .storage import digest, json_bytes


ADAPTER_VERSION = "material-local-ocr.v1"
FINGERPRINT_SCHEMA = "material.windows-ocr.engine.v1"
RESPONSE_SCHEMA = "material.windows-ocr.response.v1"
MAX_IMAGE_BYTES = 20_000_000
MAX_IMAGE_DIMENSION = 10_000
MAX_IMAGE_PIXELS = 50_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_TEXT_CHARS = 1_000_000
MAX_LINES = 20_000
MAX_WORDS = 100_000
_SHA = re.compile(r"[0-9a-f]{64}")
_SCRIPT = Path(__file__).with_name("windows_material_ocr.ps1")


class MaterialLocalOCRFailure(RuntimeError):
    def __init__(self, code: str, *, details: dict | None = None):
        self.code = code
        self.details = details or {}
        # Runtime ordinary errors must never inherit subprocess/path/source text.
        super().__init__(code)


def _require(value, code):
    if not value:
        raise MaterialLocalOCRFailure(code)


def _options(options, *, require_expected=False):
    _require(isinstance(options, dict) and set(options) <= {
        "engine", "language", "timeout_seconds", "expected_engine_fingerprint"}, "MATERIAL_OCR_OPTIONS_INVALID")
    _require(options.get("engine", "windows_ocr") == "windows_ocr", "MATERIAL_OCR_ENGINE_UNSUPPORTED")
    _require(options.get("language", "zh-Hans-CN") == "zh-Hans-CN", "MATERIAL_OCR_LANGUAGE_UNSUPPORTED")
    timeout = options.get("timeout_seconds", 60)
    _require(type(timeout) in {int, float} and math.isfinite(timeout) and 1 <= timeout <= 180,
             "MATERIAL_OCR_TIMEOUT_INVALID")
    expected = options.get("expected_engine_fingerprint")
    _require(not require_expected or expected is not None, "MATERIAL_OCR_FINGERPRINT_REQUIRED")
    _require(expected is None or isinstance(expected, dict) or
             isinstance(expected, str) and _SHA.fullmatch(expected), "MATERIAL_OCR_FINGERPRINT_INVALID")
    return {"engine": "windows_ocr", "language": "zh-Hans-CN", "timeout_seconds": timeout,
            "expected_engine_fingerprint": copy.deepcopy(expected)}


def _powershell_path():
    _require(os.name == "nt", "MATERIAL_OCR_WINDOWS_REQUIRED")
    # Do not resolve an executable through PATH, SystemRoot or a caller option.
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    _require(0 < length < len(buffer), "MATERIAL_OCR_SYSTEM_DIRECTORY_UNAVAILABLE")
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    _require(executable.is_file(), "MATERIAL_OCR_EXECUTABLE_MISSING")
    return executable


def _run_powershell(mode: str, request: dict, timeout: float) -> dict:
    """Private stdin and bounded file-backed output; kill the actual child on timeout."""
    executable = _powershell_path()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(_SCRIPT), "-Mode", mode], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                shell=False, startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                process.communicate(json_bytes(request), timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                stdout.seek(0); stderr.seek(0)
                raise MaterialLocalOCRFailure("MATERIAL_OCR_TIMEOUT", details={
                    "stdout": stdout.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace"),
                    "stderr": stderr.read(65537).decode("utf-8", errors="replace"), "child_killed": True}) from None
            stdout.seek(0); stderr.seek(0)
            return {"returncode": process.returncode, "stdout": stdout.read(MAX_RESPONSE_BYTES + 1),
                    "stderr": stderr.read(65537)}
        except MaterialLocalOCRFailure:
            raise
        except Exception as exc:
            raise MaterialLocalOCRFailure("MATERIAL_OCR_PROCESS_FAILED", details={
                "exception_type": type(exc).__name__, "exception": str(exc)}) from None


def _process_json(result):
    _require(isinstance(result, dict) and set(result) == {"returncode", "stdout", "stderr"},
             "MATERIAL_OCR_PROCESS_CONTRACT_INVALID")
    _require(type(result["returncode"]) is int and isinstance(result["stdout"], bytes) and
             isinstance(result["stderr"], bytes), "MATERIAL_OCR_PROCESS_CONTRACT_INVALID")
    _require(len(result["stdout"]) <= MAX_RESPONSE_BYTES and len(result["stderr"]) <= 65536,
             "MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED")
    try:
        value = json.loads(result["stdout"].decode("utf-8-sig"),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise MaterialLocalOCRFailure("MATERIAL_OCR_RESPONSE_JSON_INVALID") from None
    _require(isinstance(value, dict), "MATERIAL_OCR_RESPONSE_INVALID")
    if result["returncode"] != 0 or value.get("status") == "failed":
        # Only allow adapter-owned stable error codes onto ordinary traces.
        code = value.get("error_code")
        if not isinstance(code, str) or not re.fullmatch(r"MATERIAL_OCR_[A-Z0-9_]{1,60}", code):
            code = "MATERIAL_OCR_ENGINE_FAILED"
        raise MaterialLocalOCRFailure(code, details={"engine_response": value})
    _require(not result["stderr"], "MATERIAL_OCR_UNEXPECTED_STDERR")
    return value


def _process_capture(result):
    """Represent malformed runner contracts without letting them escape capture."""
    if not isinstance(result, dict):
        return {"transport_contract_status": "invalid", "received_type": type(result).__name__}
    value = {"returncode": result.get("returncode") if type(result.get("returncode")) is int else None}
    for name, limit in (("stdout", MAX_RESPONSE_BYTES), ("stderr", 65536)):
        raw = result.get(name)
        value[name] = raw[:limit + 1].decode("utf-8", errors="replace") if isinstance(raw, bytes) else {
            "invalid_type": type(raw).__name__}
        value[name + "_status"] = "truncated" if isinstance(raw, bytes) and len(raw) > limit else "captured" if isinstance(raw, bytes) else "invalid"
    if isinstance(result.get("stdout"), bytes):
        value["output_sha256"] = hashlib.sha256(result["stdout"]).hexdigest()
    return value


def _fingerprint(value):
    _require(isinstance(value, dict) and set(value) == {
        "schema_version", "adapter_version", "engine", "language", "available_languages", "max_image_dimension",
        "os", "powershell", "ocr_dll", "script_sha256"}, "MATERIAL_OCR_FINGERPRINT_INVALID")
    _require(value["schema_version"] == FINGERPRINT_SCHEMA and value["adapter_version"] == ADAPTER_VERSION and
             value["engine"] == "windows_ocr" and value["language"] == "zh-Hans-CN",
             "MATERIAL_OCR_FINGERPRINT_INVALID")
    langs = value["available_languages"]
    _require(isinstance(langs, list) and 1 <= len(langs) <= 200 and
             all(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9-]{2,40}", tag) for tag in langs) and
             langs == sorted(set(langs)) and "zh-Hans-CN" in langs, "MATERIAL_OCR_LANGUAGE_UNAVAILABLE")
    _require(type(value["max_image_dimension"]) is int and
             1 <= value["max_image_dimension"] <= MAX_IMAGE_DIMENSION, "MATERIAL_OCR_FINGERPRINT_INVALID")
    _require(isinstance(value["os"], dict) and set(value["os"]) == {"version", "architecture"} and
             isinstance(value["os"]["architecture"], str) and
             value["os"]["architecture"] in {"x64", "x86", "arm64"}, "MATERIAL_OCR_FINGERPRINT_INVALID")
    for component, version_key in (("os", "version"), ("powershell", "version"), ("ocr_dll", "file_version")):
        obj = value[component]
        _require(isinstance(obj, dict) and set(obj) == ({"version", "architecture"} if component == "os" else
                 {version_key, "exe_sha256" if component == "powershell" else "sha256"}) and
                 isinstance(obj[version_key], str) and re.fullmatch(
                     r"[A-Za-z0-9. ()_-]{3,120}" if component == "ocr_dll" else r"[0-9.]{3,80}", obj[version_key]),
                 "MATERIAL_OCR_FINGERPRINT_INVALID")
    for sha in (value["script_sha256"], value["powershell"]["exe_sha256"], value["ocr_dll"]["sha256"]):
        _require(isinstance(sha, str) and _SHA.fullmatch(sha), "MATERIAL_OCR_FINGERPRINT_INVALID")
    _require(value["script_sha256"] == hashlib.sha256(_SCRIPT.read_bytes()).hexdigest(),
             "MATERIAL_OCR_SCRIPT_HASH_MISMATCH")
    return copy.deepcopy(value)


def _match_fingerprint(actual, expected):
    if expected is not None:
        _require(digest(actual) == (digest(_fingerprint(expected)) if isinstance(expected, dict) else expected),
                 "MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH")


def engine_fingerprint(options: dict) -> dict:
    """Inventory only: no RecognizeAsync, call event, fee reservation or network."""
    config = _options(options)
    raw = _run_powershell("fingerprint", {"language": config["language"]}, min(config["timeout_seconds"], 30))
    try:
        value = _process_json(raw)
        _require(set(value) == {"status", "engine_fingerprint"} and value["status"] == "succeeded",
                 "MATERIAL_OCR_FINGERPRINT_INVALID")
        actual = _fingerprint(value["engine_fingerprint"])
        _match_fingerprint(actual, config["expected_engine_fingerprint"])
        return actual
    except MaterialLocalOCRFailure as exc:
        exc.details["process"] = _process_capture(raw)
        raise


def _image(image_path, expected_sha):
    _require(isinstance(image_path, str) and 0 < len(image_path) <= 32767 and "\x00" not in image_path,
             "MATERIAL_OCR_IMAGE_PATH_INVALID")
    _require(isinstance(expected_sha, str) and _SHA.fullmatch(expected_sha), "MATERIAL_OCR_IMAGE_HASH_INVALID")
    path = Path(image_path)
    _require(path.is_absolute() and path.is_file(), "MATERIAL_OCR_IMAGE_MISSING")
    _require(0 < path.stat().st_size <= MAX_IMAGE_BYTES, "MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED")
    # A bounded read rather than trusting a racy stat or a compressed-image header.
    with path.open("rb") as handle:
        content = handle.read(MAX_IMAGE_BYTES + 1)
    _require(0 < len(content) <= MAX_IMAGE_BYTES, "MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED")
    _require(hashlib.sha256(content).hexdigest() == expected_sha, "MATERIAL_OCR_IMAGE_HASH_MISMATCH")
    try:
        with Image.open(BytesIO(content)) as image:
            _require(image.format in {"PNG", "JPEG"} and getattr(image, "n_frames", 1) == 1,
                     "MATERIAL_OCR_IMAGE_FORMAT_UNSUPPORTED")
            width, height = image.size
            _require(0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION and
                     width * height <= MAX_IMAGE_PIXELS, "MATERIAL_OCR_IMAGE_DIMENSION_LIMIT_EXCEEDED")
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
            _require(image.mode in {"1", "L", "LA", "P", "RGB", "RGBA"}, "MATERIAL_OCR_IMAGE_FORMAT_UNSUPPORTED")
    except MaterialLocalOCRFailure:
        raise
    except Exception:
        raise MaterialLocalOCRFailure("MATERIAL_OCR_IMAGE_INVALID") from None
    return path.resolve(), content, width, height


def _capture(ctx, name, payload):
    try:
        captured = ctx.capture(name, payload)
    except Exception:
        raise MaterialLocalOCRFailure("MATERIAL_OCR_CAPTURE_FAILED") from None
    _require(isinstance(captured, dict) and captured.get("status") == "captured",
             "MATERIAL_OCR_CAPTURE_REQUIRED")


def _text(value):
    _require(isinstance(value, str) and len(value) <= MAX_TEXT_CHARS and "\x00" not in value,
             "MATERIAL_OCR_TEXT_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise MaterialLocalOCRFailure("MATERIAL_OCR_TEXT_INVALID") from None
    return value


def _validate_response(value, *, width, height, image_sha256, fingerprint, call_id):
    _require(set(value) == {"schema_version", "status", "text", "lines", "width", "height", "image_sha256",
                           "engine_fingerprint"} and value["schema_version"] == RESPONSE_SCHEMA and
             value["status"] == "succeeded", "MATERIAL_OCR_RESPONSE_INVALID")
    _require(type(value["width"]) is int and type(value["height"]) is int and
             (value["width"], value["height"]) == (width, height), "MATERIAL_OCR_IMAGE_DIMENSION_MISMATCH")
    _require(value["image_sha256"] == image_sha256, "MATERIAL_OCR_IMAGE_HASH_MISMATCH")
    _require(_fingerprint(value["engine_fingerprint"]) == fingerprint, "MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH")
    text = _text(value["text"])
    _require(isinstance(value["lines"], list) and len(value["lines"]) <= MAX_LINES, "MATERIAL_OCR_LINES_INVALID")
    words_count = 0
    for line in value["lines"]:
        _require(isinstance(line, dict) and set(line) == {"text", "words"} and isinstance(line["words"], list),
                 "MATERIAL_OCR_LINES_INVALID")
        _text(line["text"])
        words_count += len(line["words"])
        _require(words_count <= MAX_WORDS, "MATERIAL_OCR_WORD_LIMIT_EXCEEDED")
        for word in line["words"]:
            _require(isinstance(word, dict) and set(word) == {"text", "box"}, "MATERIAL_OCR_WORD_INVALID")
            _require(bool(_text(word["text"]).strip()), "MATERIAL_OCR_WORD_INVALID")
            box = word["box"]
            _require(isinstance(box, list) and len(box) == 4 and
                     all(type(n) in {int, float} and math.isfinite(n) for n in box), "MATERIAL_OCR_COORDINATE_INVALID")
            x, y, w, h = box
            _require(x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= width and y + h <= height,
                     "MATERIAL_OCR_COORDINATE_OUT_OF_BOUNDS")
    _require(bool(text.strip()) == any(line["text"].strip() for line in value["lines"]),
             "MATERIAL_OCR_TEXT_LINES_MISMATCH")
    return {"schema_version": RESPONSE_SCHEMA, "text": text, "lines": copy.deepcopy(value["lines"]),
            "width": width, "height": height, "image_sha256": image_sha256,
            "engine_fingerprint": copy.deepcopy(fingerprint), "call_id": call_id,
            "quality_status": "machine_transcription_unverified",
            "reading_status": "machine_readable" if text.strip() else "unresolved",
            "unresolved_reasons": [] if text.strip() else ["OCR_NO_TEXT_RECOGNIZED_NOT_PROOF_OF_BLANK"],
            "qualification_result": "not_determined", "project_assignment": "not_made"}


def recognize(ctx, *, image_path: str, image_sha256: str, identity: dict, options: dict) -> dict:
    """Exactly one bounded local recognition attempt after captured preflight.

    Empty output is unresolved. Nonempty output remains an unverified machine
    transcription: completeness, table relationships and facts are not asserted.
    """
    preflight_id = uuid.uuid4().hex
    base = {"provider": "windows_ocr", "operation": "material_ocr", "usage_status": "local_no_api_charge", "cost": 0}
    with ctx._observer.span("ocr.local.preflight", {**base, "phase": "preflight", **ctx._metadata()}):
        try:
            config = _options(options, require_expected=True)
            _require(isinstance(identity, dict) and len(json_bytes(identity)) <= 65536, "MATERIAL_OCR_IDENTITY_INVALID")
            _capture(ctx, "ocr-preflight-request-" + preflight_id, {
                "identity": identity, "image_path": image_path, "image_sha256": image_sha256, "options": config})
            path, content, width, height = _image(image_path, image_sha256)
            fingerprint = engine_fingerprint(config)
            _require(max(width, height) <= fingerprint["max_image_dimension"],
                     "MATERIAL_OCR_IMAGE_DIMENSION_LIMIT_EXCEEDED")
            _capture(ctx, "ocr-preflight-" + preflight_id, {"status": "passed", "engine_fingerprint": fingerprint,
                     "image_sha256": image_sha256, "width": width, "height": height, "size_bytes": len(content)})
            call_id = uuid.uuid4().hex
            request = {"image_path": str(path), "image_sha256": image_sha256, "language": config["language"],
                       "expected_engine_fingerprint": fingerprint, "width": width, "height": height}
            _capture(ctx, "ocr-request-" + call_id, {"call_id": call_id, "identity": identity, "request": request})
        except Exception as exc:
            failure = exc if isinstance(exc, MaterialLocalOCRFailure) else MaterialLocalOCRFailure("MATERIAL_OCR_PREFLIGHT_FAILED")
            # Best effort here: a disabled/broken capture must not mask the reason
            # or admit an actual OCR call. The runtime also records capture gaps.
            try:
                _capture(ctx, "ocr-preflight-error-" + preflight_id, {"error_code": failure.code,
                         "details": getattr(exc, "details", {}), "exception_type": type(exc).__name__, "exception": str(exc)})
            except MaterialLocalOCRFailure:
                pass
            ctx.call_event("material_ocr.preflight.failed", {**base, "error_code": failure.code, "phase": "preflight"})
            raise failure from None

    metadata = {**base, "call_id": call_id, "call_attempt": 1, "input_hash": digest(request), "size_bytes": len(content)}
    started = time.monotonic()
    with ctx._observer.span("ocr.local.call", {**metadata, **ctx._metadata()}) as span:
        context = span.get_span_context()
        metadata.update(trace_id=format(context.trace_id, "032x"), span_id=format(context.span_id, "016x"))
        ctx.call_event("ocr.local.started", metadata)
        try:
            raw = _run_powershell("recognize", request, config["timeout_seconds"])
            if isinstance(raw, dict) and isinstance(raw.get("stdout"), bytes):
                metadata["output_hash"] = hashlib.sha256(raw["stdout"]).hexdigest()
            _capture(ctx, "ocr-response-" + call_id, {"call_id": call_id, **_process_capture(raw)})
            value = _process_json(raw)
            validated = _validate_response(value, width=width, height=height, image_sha256=image_sha256,
                                           fingerprint=fingerprint, call_id=call_id)
            final_path, final_content, _, _ = _image(image_path, image_sha256)
            _require(final_path == path and final_content == content, "MATERIAL_OCR_IMAGE_CHANGED")
            _capture(ctx, "ocr-validation-" + call_id, {"status": "passed", "call_id": call_id,
                     "validated_output_hash": digest(validated), "validated_output": validated})
            metadata.update(elapsed_ms=round((time.monotonic() - started) * 1000),
                            count=len(validated["text"]), validation_status="passed")
            span.set_attributes(metadata)
            ctx.call_event("ocr.local.completed", metadata)
            return validated
        except Exception as exc:
            failure = exc if isinstance(exc, MaterialLocalOCRFailure) else MaterialLocalOCRFailure("MATERIAL_OCR_ADAPTER_FAILED")
            try:
                _capture(ctx, "ocr-error-" + call_id, {"status": "failed", "call_id": call_id,
                         "error_code": failure.code, "details": getattr(exc, "details", {}),
                         "exception_type": type(exc).__name__, "exception": str(exc)})
            except MaterialLocalOCRFailure as capture_error:
                failure = capture_error
            metadata.update(elapsed_ms=round((time.monotonic() - started) * 1000), error_code=failure.code,
                            validation_status="failed")
            span.set_attributes(metadata)
            ctx.call_event("ocr.local.failed", metadata)
            raise failure from None
