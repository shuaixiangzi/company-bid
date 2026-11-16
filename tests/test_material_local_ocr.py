from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from PIL import Image
import pytest

from company_bid import material_local_ocr as ocr
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes


CANARY = "SOURCE_CANARY_PRIVATE_COMPANY_证书"


@pytest.fixture
def image(tmp_path):
    path = tmp_path / ("PRIVATE_PATH_CANARY.png")
    Image.new("RGB", (128, 64), "white").save(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fingerprint():
    return {"schema_version": ocr.FINGERPRINT_SCHEMA, "adapter_version": ocr.ADAPTER_VERSION,
            "engine": "windows_ocr", "language": "zh-Hans-CN", "available_languages": ["en-US", "zh-Hans-CN"],
            "max_image_dimension": 10000, "os": {"version": "10.0.26200.0", "architecture": "x64"},
            "powershell": {"version": "5.1.26100.9818", "exe_sha256": "a" * 64},
            "ocr_dll": {"file_version": "10.0.26100.9278 (WinBuild.160101.0800)", "sha256": "b" * 64},
            "script_sha256": hashlib.sha256(ocr._SCRIPT.read_bytes()).hexdigest()}


def response(fingerprint, image_sha, *, text=CANARY):
    return {"schema_version": ocr.RESPONSE_SCHEMA, "status": "succeeded", "text": text,
            "lines": [{"text": text, "words": [{"text": text, "box": [1, 2, 100, 30]}]}] if text else [],
            "width": 128, "height": 64, "image_sha256": image_sha, "engine_fingerprint": copy.deepcopy(fingerprint)}


def process(value, *, returncode=0, stderr=b""):
    return {"returncode": returncode, "stdout": value if isinstance(value, bytes) else json_bytes(value), "stderr": stderr}


class Context:
    def __init__(self, capture_status="captured"):
        self.events, self.captures, self.spans = [], [], []
        self.capture_status, self._observer = capture_status, self

    def _metadata(self):
        return {"node_key": "N3"}

    @contextmanager
    def span(self, name, attributes):
        self.spans.append({"name": name, "attributes": copy.deepcopy(attributes)})
        yield self

    def set_attributes(self, value):
        self.spans[-1]["attributes"].update(copy.deepcopy(value))

    def get_span_context(self):
        return SimpleNamespace(trace_id=1234, span_id=5678)

    def capture(self, name, payload):
        self.captures.append((name, copy.deepcopy(payload)))
        return {"status": self.capture_status}

    def call_event(self, event, metadata):
        self.events.append({"event": event, **copy.deepcopy(metadata)})


def install_runner(monkeypatch, fingerprint, image_sha, value=None):
    calls = []
    def runner(mode, request, timeout):
        calls.append((mode, copy.deepcopy(request), timeout))
        if mode == "fingerprint":
            return process({"status": "succeeded", "engine_fingerprint": fingerprint})
        return process(response(fingerprint, image_sha)) if value is None else value(request)
    monkeypatch.setattr(ocr, "_run_powershell", runner)
    return calls


def recognize(ctx, image, fingerprint):
    return ocr.recognize(ctx, image_path=str(image[0]), image_sha256=image[1], identity={"source_id": "source-1"},
                         options={"engine": "windows_ocr", "language": "zh-Hans-CN", "timeout_seconds": 12,
                                  "expected_engine_fingerprint": fingerprint})


def test_one_local_attempt_has_safe_metadata_and_full_controlled_captures(monkeypatch, image, fingerprint):
    calls = install_runner(monkeypatch, fingerprint, image[1])
    ctx = Context()
    result = recognize(ctx, image, fingerprint)
    assert [call[0] for call in calls] == ["fingerprint", "recognize"]
    assert result["quality_status"] == "machine_transcription_unverified"
    assert result["qualification_result"] == "not_determined" and result["project_assignment"] == "not_made"
    assert result["reading_status"] == "machine_readable"
    assert [row["event"] for row in ctx.events] == ["ocr.local.started", "ocr.local.completed"]
    assert len({row["call_id"] for row in ctx.events}) == 1
    assert all(row["cost"] == 0 and row["usage_status"] == "local_no_api_charge" for row in ctx.events)
    assert "output_hash" in ctx.events[-1] and "input_hash" in ctx.events[-1]
    ordinary = json.dumps([ctx.events, ctx.spans], ensure_ascii=False)
    assert CANARY not in ordinary and image[0].name not in ordinary
    assert CANARY in json.dumps(ctx.captures, ensure_ascii=False)
    assert [span["name"] for span in ctx.spans] == ["ocr.local.preflight", "ocr.local.call"]


def test_empty_text_is_unresolved_never_proof_of_blank(monkeypatch, image, fingerprint):
    install_runner(monkeypatch, fingerprint, image[1], lambda _: process(response(fingerprint, image[1], text="")))
    result = recognize(Context(), image, fingerprint)
    assert result["reading_status"] == "unresolved"
    assert result["unresolved_reasons"] == ["OCR_NO_TEXT_RECOGNIZED_NOT_PROOF_OF_BLANK"]
    assert result["quality_status"] == "machine_transcription_unverified"


@pytest.mark.parametrize("change,code", [
    (lambda v: v.update(width=True), "MATERIAL_OCR_IMAGE_DIMENSION_MISMATCH"),
    (lambda v: v.update(height=65), "MATERIAL_OCR_IMAGE_DIMENSION_MISMATCH"),
    (lambda v: v.update(image_sha256="f" * 64), "MATERIAL_OCR_IMAGE_HASH_MISMATCH"),
    (lambda v: v["lines"][0]["words"][0].update(box=[0, 0, 129, 20]), "MATERIAL_OCR_COORDINATE_OUT_OF_BOUNDS"),
    (lambda v: v["lines"][0]["words"][0].update(box=[-1, 0, 20, 20]), "MATERIAL_OCR_COORDINATE_OUT_OF_BOUNDS"),
    (lambda v: v["lines"][0]["words"][0].update(box=[True, 0, 20, 20]), "MATERIAL_OCR_COORDINATE_INVALID"),
    (lambda v: v.update(lines=[]), "MATERIAL_OCR_TEXT_LINES_MISMATCH"),
    (lambda v: v["engine_fingerprint"].update(language="en-US"), "MATERIAL_OCR_FINGERPRINT_INVALID"),
    (lambda v: v["engine_fingerprint"]["ocr_dll"].update(sha256="c" * 64), "MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH"),
])
def test_response_validation_has_one_failed_attempt_no_retry(monkeypatch, image, fingerprint, change, code):
    value = response(fingerprint, image[1]); change(value)
    calls = install_runner(monkeypatch, fingerprint, image[1], lambda _: process(value))
    ctx = Context()
    with pytest.raises(ocr.MaterialLocalOCRFailure, match=code):
        recognize(ctx, image, fingerprint)
    assert [row["event"] for row in ctx.events] == ["ocr.local.started", "ocr.local.failed"]
    assert len(calls) == 2 and ctx.events[-1]["error_code"] == code
    assert any(name.startswith("ocr-response-") for name, _ in ctx.captures)
    assert any(name.startswith("ocr-error-") for name, _ in ctx.captures)


@pytest.mark.parametrize("raw,code", [
    (b"invalid-json PRIVATE_ERROR_CANARY", "MATERIAL_OCR_RESPONSE_JSON_INVALID"),
    (b"\xff", "MATERIAL_OCR_RESPONSE_JSON_INVALID"),
    (b'{"value":NaN}', "MATERIAL_OCR_RESPONSE_JSON_INVALID"),
    (b"[]", "MATERIAL_OCR_RESPONSE_INVALID"),
])
def test_malformed_wire_output_is_private_and_fails_closed(monkeypatch, image, fingerprint, raw, code):
    install_runner(monkeypatch, fingerprint, image[1], lambda _: process(raw))
    ctx = Context()
    with pytest.raises(ocr.MaterialLocalOCRFailure, match=code):
        recognize(ctx, image, fingerprint)
    assert "PRIVATE_ERROR_CANARY" not in json.dumps(ctx.events + ctx.spans)
    assert [row["event"] for row in ctx.events] == ["ocr.local.started", "ocr.local.failed"]


def test_preflight_hash_language_and_fingerprint_errors_make_no_ocr_call(monkeypatch, image, fingerprint):
    calls = install_runner(monkeypatch, fingerprint, image[1])
    for changed in ({**fingerprint, "script_sha256": "f" * 64}, {**fingerprint, "language": "en-US"},
                    {**fingerprint, "os": {"version": "10.0.1.0", "architecture": "x64"}}):
        ctx = Context()
        with pytest.raises(ocr.MaterialLocalOCRFailure):
            recognize(ctx, image, changed)
        assert not any(row["event"] == "ocr.local.started" for row in ctx.events)
        assert ctx.events[-1]["event"] == "material_ocr.preflight.failed"
    assert all(call[0] == "fingerprint" for call in calls)
    before = len(calls)
    ctx = Context()
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_IMAGE_HASH_MISMATCH"):
        recognize(ctx, (image[0], "0" * 64), fingerprint)
    assert len(calls) == before
    for options, code in [({"language": "en-US"}, "MATERIAL_OCR_LANGUAGE_UNSUPPORTED"),
                          ({"timeout_seconds": float("nan")}, "MATERIAL_OCR_TIMEOUT_INVALID"),
                          ({"timeout_seconds": True}, "MATERIAL_OCR_TIMEOUT_INVALID"),
                          ({"engine": "network"}, "MATERIAL_OCR_ENGINE_UNSUPPORTED"),
                          ({"shell": "something"}, "MATERIAL_OCR_OPTIONS_INVALID"),
                          ({}, "MATERIAL_OCR_FINGERPRINT_REQUIRED")]:
        with pytest.raises(ocr.MaterialLocalOCRFailure, match=code):
            ocr.recognize(Context(), image_path=str(image[0]), image_sha256=image[1], identity={}, options=options)
    assert len(calls) == before


def test_fingerprint_digest_is_supported_and_inventory_does_not_recognize(monkeypatch, image, fingerprint):
    calls = install_runner(monkeypatch, fingerprint, image[1])
    assert ocr.engine_fingerprint({"expected_engine_fingerprint": digest(fingerprint)}) == fingerprint
    result = recognize(Context(), image, digest(fingerprint))
    assert result["engine_fingerprint"] == fingerprint
    assert [row[0] for row in calls] == ["fingerprint", "fingerprint", "recognize"]


@pytest.mark.parametrize("status", ["disabled_by_policy", "write_failed", "partial"])
def test_capture_gap_blocks_before_recognition(monkeypatch, image, fingerprint, status):
    calls = install_runner(monkeypatch, fingerprint, image[1])
    ctx = Context(status)
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_CAPTURE_REQUIRED"):
        recognize(ctx, image, fingerprint)
    assert calls == [] and not any(row["event"] == "ocr.local.started" for row in ctx.events)


@pytest.mark.parametrize("boundary", ["ocr-request-", "ocr-response-", "ocr-validation-"])
def test_late_capture_failure_preserves_failed_boundary(monkeypatch, image, fingerprint, boundary):
    calls = install_runner(monkeypatch, fingerprint, image[1])
    ctx = Context()
    original = ctx.capture
    def capture(name, payload):
        original(name, payload)
        return {"status": "write_failed" if name.startswith(boundary) else "captured"}
    ctx.capture = capture
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_CAPTURE_REQUIRED"):
        recognize(ctx, image, fingerprint)
    actual = [row for row in ctx.events if row["event"].startswith("ocr.local.")]
    if boundary == "ocr-request-":
        assert len(calls) == 1 and actual == []
        assert ctx.events[-1]["event"] == "material_ocr.preflight.failed"
    else:
        assert len(calls) == 2 and [row["event"] for row in actual] == ["ocr.local.started", "ocr.local.failed"]


def test_failed_process_body_stays_private_and_unrecognized_error_code_is_sanitized(monkeypatch, image, fingerprint):
    install_runner(monkeypatch, fingerprint, image[1], lambda _: process({"status": "failed",
        "error_code": "PRIVATE_ERROR_CANARY", "diagnostic": {"exception": CANARY}}, returncode=1,
        stderr=b"PRIVATE_STDERR_CANARY"))
    ctx = Context()
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_ENGINE_FAILED"):
        recognize(ctx, image, fingerprint)
    ordinary = json.dumps([ctx.events, ctx.spans], ensure_ascii=False)
    captured = json.dumps(ctx.captures, ensure_ascii=False)
    assert CANARY not in ordinary and "PRIVATE_STDERR_CANARY" not in ordinary and "PRIVATE_ERROR_CANARY" not in ordinary
    assert CANARY in captured and "PRIVATE_STDERR_CANARY" in captured and "PRIVATE_ERROR_CANARY" in captured
    assert "output_hash" in ctx.events[-1]


def test_response_and_image_byte_limits_are_enforced(monkeypatch, image, fingerprint):
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED"):
        ocr._process_json(process(b"x" * (ocr.MAX_RESPONSE_BYTES + 1)))
    calls = install_runner(monkeypatch, fingerprint, image[1])
    monkeypatch.setattr(ocr, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED"):
        recognize(Context(), image, fingerprint)
    assert calls == []


def test_original_changed_during_ocr_is_not_adopted(monkeypatch, image, fingerprint):
    def changed(_):
        Image.new("RGB", (128, 64), "black").save(image[0])
        return process(response(fingerprint, image[1]))
    calls = install_runner(monkeypatch, fingerprint, image[1], changed)
    ctx = Context()
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_IMAGE_HASH_MISMATCH"):
        recognize(ctx, image, fingerprint)
    assert len(calls) == 2 and ctx.events[-1]["event"] == "ocr.local.failed"


@pytest.mark.parametrize("kind", ["too_wide", "gif", "corrupt", "animated"])
def test_input_images_are_bounded_and_format_checked(monkeypatch, tmp_path, fingerprint, kind):
    path = tmp_path / "image.bin"
    if kind == "too_wide":
        Image.new("RGB", (10001, 1)).save(path, format="PNG")
    elif kind == "gif":
        Image.new("RGB", (128, 64)).save(path, format="GIF")
    elif kind == "animated":
        Image.new("RGB", (128, 64)).save(path, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (128, 64), "white")], duration=50, loop=0)
    else:
        path.write_bytes(b"not an image")
    calls = install_runner(monkeypatch, fingerprint, "0" * 64)
    with pytest.raises(ocr.MaterialLocalOCRFailure):
        recognize(Context(), (path, hashlib.sha256(path.read_bytes()).hexdigest()), fingerprint)
    assert calls == []


def test_real_executor_restores_only_failed_unit_and_keeps_local_calls_out_of_model_ledger(monkeypatch, tmp_path, image, fingerprint):
    calls = []
    failed_once = False
    def runner(mode, request, timeout):
        nonlocal failed_once
        if mode == "fingerprint":
            return process({"status": "succeeded", "engine_fingerprint": fingerprint})
        calls.append(request["image_sha256"])
        if len(calls) == 2 and not failed_once:
            failed_once = True
            raise ocr.MaterialLocalOCRFailure("MATERIAL_OCR_TIMEOUT", details={"error": "PRIVATE_ERROR_CANARY"})
        return process(response(fingerprint, image[1]))
    monkeypatch.setattr(ocr, "_run_powershell", runner)
    def handler(ctx, data):
        return {name: ctx.run_unit(name, {"image_sha256": image[1], "identity": name},
                     lambda: recognize(ctx, image, fingerprint)) for name in ("one", "two")}
    executor = Executor(tmp_path / "runtime", [NodeSpec("N3", "test-ocr-v1", handler)], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "MATERIAL_OCR_TIMEOUT"
    first = state["nodes"][0]["units"]["one"]["output_ref"]
    restarted = Executor(tmp_path / "runtime", [NodeSpec("N3", "test-ocr-v1", handler)], {})
    completed = restarted.resume(run)
    assert completed["status"] == "succeeded"
    assert completed["nodes"][0]["units"]["one"]["output_ref"] == first and len(calls) == 3
    directory = executor.store.run_dir(run)
    events_text = (directory / "events.jsonl").read_text(encoding="utf-8")
    spans_text = (directory / "spans.jsonl").read_text(encoding="utf-8")
    ordinary = events_text + spans_text + json.dumps(completed, ensure_ascii=False)
    assert CANARY not in ordinary and "PRIVATE_ERROR_CANARY" not in ordinary and image[0].name not in ordinary
    events = [json.loads(line) for line in events_text.splitlines()]
    starts = [row for row in events if row["event"] == "ocr.local.started"]
    assert len(starts) == 3 and len({row["call_id"] for row in starts}) == 3
    assert not any(row["event"].startswith("call.") for row in events)
    assert not completed.get("call_budget", {}).get("reservations")
    spans = [json.loads(line) for line in spans_text.splitlines()]
    actual = [row for row in spans if row["name"] == "ocr.local.call"]
    assert len(actual) == 3 and all(len(row["trace_id"]) == 32 and row["parent_span_id"] for row in actual)
    assert sum(row["status"] == "ERROR" for row in actual) == 1
    assert not any(row["name"] == "provider.call" for row in spans)
    for start in starts:
        terminal = [row for row in events if row.get("call_id") == start["call_id"] and
                    row["event"] in {"ocr.local.completed", "ocr.local.failed"}]
        assert len(terminal) == 1 and terminal[0]["cost"] == 0


def test_runner_uses_hidden_fixed_executable_private_stdin_and_kills_child(monkeypatch):
    if not hasattr(subprocess, "STARTUPINFO"):
        pytest.skip("Windows process flags are only defined on Windows")
    monkeypatch.setattr(ocr, "_powershell_path", lambda: Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
    processes = []
    class Child:
        def __init__(self, args, **kwargs):
            self.args, self.kwargs, self.returncode, self.killed = args, kwargs, 0, False
            self.communications = []
            processes.append(self)
        def communicate(self, content=None, timeout=None):
            self.communications.append((content, timeout))
            if len(self.communications) == 1:
                raise subprocess.TimeoutExpired(self.args, timeout)
        def kill(self):
            self.killed = True
    monkeypatch.setattr(subprocess, "Popen", Child)
    with pytest.raises(ocr.MaterialLocalOCRFailure, match="MATERIAL_OCR_TIMEOUT") as error:
        ocr._run_powershell("recognize", {"image_path": "PRIVATE_PATH_CANARY", "language": "zh-Hans-CN"}, 1)
    child = processes[0]
    assert child.killed and len(child.communications) == 2 and error.value.details["child_killed"] is True
    assert child.kwargs["shell"] is False and child.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert child.kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
    assert "PRIVATE_PATH_CANARY" not in " ".join(child.args)
    assert json.loads(child.communications[0][0])["image_path"] == "PRIVATE_PATH_CANARY"
