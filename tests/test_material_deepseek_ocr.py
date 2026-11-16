from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image
import pytest

from company_bid import material_deepseek_ocr as ocr
from company_bid.diagnostics import inspect_diagnostics
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import json_bytes


SECRET = "test-credential-private-2718281828459045"
CANARY = "SOURCE_CANARY_PRIVATE_COMPANY"


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "PRIVATE_PATH_CANARY.png"
    Image.new("RGB", (128, 64), "white").save(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def options(**extra):
    value = {"engine": "deepseek_ocr", "timeout_seconds": 5, "max_run_usd": ".012", "max_run_calls": 8}
    value.update(extra)
    value["expected_engine_fingerprint"] = ocr.engine_fingerprint(value)
    return value


def payload(text=CANARY):
    return {"id": "ocr-test-1", "object": "ocr.result", "created": 1794816000,
            "purpose": "rich_layout_recognition", "page_count": 1,
            "pages": [{"index": 0, "text": text, "finish_reason": "stop"}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}


def executor(tmp_path, image, config=None, *, two=False, numeric_reference=None, capture=True):
    config = options() if config is None else config
    def handler(ctx, initial):
        result = []
        for name in ("first", "second") if two else ("first",):
            identity = {"image_sha256": image[1], "source_id": name}
            result.append(ctx.run_unit(name, {"identity": identity, "options": config},
                lambda identity=identity: ocr.recognize(ctx, image_path=str(image[0]), image_sha256=image[1],
                    identity=identity, options=config, numeric_reference=numeric_reference)))
        return {"responses": result}
    return Executor(tmp_path / "runtime", [NodeSpec("N3", "deepseek-ocr-test.v1", handler)], {"ocr": config, "debug_capture": capture})


def stub(monkeypatch, response=None, callback=None):
    calls = []
    monkeypatch.setenv("COMPANY_BID_DEEPSEEK_API_KEY", SECRET)
    def transport(body, media, secret, timeout):
        calls.append({"body": body, "media": media, "secret": secret, "timeout": timeout})
        if callback:
            return callback(body, media, secret, timeout)
        return 200, {"x-request-id": "req-1"}, json_bytes(response if response is not None else payload())
    monkeypatch.setattr(ocr, "_transport", transport)
    return calls


def records(ex, rid):
    folder = ex.store.run_dir(rid)
    return {name: [json.loads(line) for line in (folder / name).read_text("utf-8").splitlines()]
            for name in ("events.jsonl", "spans.jsonl")}


def test_live_request_one_attempt_shared_ledger_trace_private_capture_and_quality(monkeypatch, tmp_path, image):
    calls = stub(monkeypatch)
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "succeeded" and len(calls) == 1
    result = ex.output(run)["responses"][0]
    assert result["text"] == CANARY and result["numeric_validation"]["automatic_numeric_adoption"] is False
    assert result["qualification_result"] == "not_determined" and result["local_fallback"] is False
    assert result["reading_completeness"] == "not_established" and result["provider_model_version"] == "not_reported"
    assert result["billing"]["official_page_fee_estimate_cny"] == "0.0015"
    budget = call_budget_summary(state["call_budget"])
    assert budget["reservation_count"] == 1 and budget["effective_budget_charge_usd"] == "0.0015"
    assert budget["known_usage_estimate_count"] == 0
    assert ex.resume(run)["status"] == "succeeded" and len(calls) == 1
    values = records(ex, run)
    ordinary = json.dumps(values) + json.dumps(state)
    assert CANARY not in ordinary and SECRET not in ordinary and image[0].name not in ordinary
    starts = [e for e in values["events.jsonl"] if e["event"] == "call.started"]
    ends = [e for e in values["events.jsonl"] if e["event"] == "call.completed"]
    assert len(starts) == len(ends) == 1 and starts[0]["call_id"] == ends[0]["call_id"]
    assert ends[0]["provider_request_id"] == "ocr-test-1"
    diagnosis = inspect_diagnostics(ex.store.root, run)
    assert diagnosis["actual_call_attempts"] == 1 and diagnosis["gaps"] == []


@pytest.mark.parametrize("response,code", [
    ({"error": {"message": CANARY}}, "MATERIAL_OCR_BODY_ERROR"),
    ({**payload(), "page_count": 2}, "MATERIAL_OCR_PAGE_COVERAGE_INVALID"),
    ({**payload(), "purpose": "text_recognition"}, "MATERIAL_OCR_RESPONSE_SCHEMA_INVALID"),
    ({**payload(), "pages": [{"index": 0, "text": CANARY, "finish_reason": "length"}]}, "MATERIAL_OCR_OUTPUT_INCOMPLETE"),
    ({**payload(), "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 999}}, "MATERIAL_OCR_USAGE_INVALID"),
    (payload(("<｜ref｜>text<｜/ref｜><｜box｜>[[10,20,30,40]]<｜/box｜>\n") * 5), "MATERIAL_OCR_REPETITIVE_OUTPUT"),
    (payload("<script>alert(1)</script>"), "MATERIAL_OCR_HTML_UNSAFE"),
])
def test_bad_response_fails_node_and_keeps_single_charge_without_fallback(monkeypatch, tmp_path, image, response, code):
    calls = stub(monkeypatch, response)
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == code and len(calls) == 1
    assert call_budget_summary(state["call_budget"])["effective_budget_charge_usd"] == "0.0015"
    events = records(ex, run)["events.jsonl"]
    assert len([e for e in events if e["event"] == "call.failed"]) == 1
    assert not any(e["event"] in {"call.completed", "call.retry_scheduled", "ocr.local.started"} for e in events)
    captures = state["nodes"][0]["attempts"][0]["captures"]
    response_capture = next(ex.store.read_json(c["ref"]) for c in captures if c["name"].startswith("response-"))
    assert ex.store.read_bytes(response_capture["raw_response_ref"]) == json_bytes(response)
    if code in {"MATERIAL_OCR_OUTPUT_INCOMPLETE", "MATERIAL_OCR_USAGE_INVALID"}:
        assert response_capture["billing"]["official_page_fee_estimate_cny"] == "0.0015"


def test_numeric_unit_source_conflict_fails_not_silently_corrected(monkeypatch, tmp_path, image):
    calls = stub(monkeypatch, payload("恢复 740m3"))
    reference = {"image_sha256": image[1], "checks": [{"field_id": "area", "region_index": 0,
                  "kind": "measure", "expected_text": "740m²"}]}
    ex = executor(tmp_path, image, numeric_reference=reference)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["error_code"] == "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH" and len(calls) == 1
    assert state["nodes"][0]["units"]["first"]["status"] == "failed"


def test_missing_reference_leaves_numbers_pending_never_verified(monkeypatch, tmp_path, image):
    stub(monkeypatch, payload("总计 740m3"))
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    assert ex.execute(run)["status"] == "succeeded"
    check = ex.output(run)["responses"][0]["numeric_validation"]
    assert check["status"] == "pending_source_check" and check["source_accuracy_established_for_complete_page"] is False


@pytest.mark.parametrize("kind,code", [("http", "MATERIAL_OCR_HTTP_ERROR"), ("json", "MATERIAL_OCR_RESPONSE_NOT_JSON"),
    ("large", "MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED"), ("unknown", "MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN")])
def test_transport_failures_are_recorded_without_retries(monkeypatch, tmp_path, image, kind, code):
    def response(*_):
        if kind == "unknown":
            raise ocr.MaterialDeepSeekOCRFailure(code, details={"child_killed": True})
        return (503 if kind == "http" else 200), {}, (b"x" * (ocr.MAX_RESPONSE_BYTES + 1) if kind == "large"
                 else b"not-json" if kind == "json" else json_bytes(payload()))
    calls = stub(monkeypatch, callback=response)
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    assert ex.execute(run)["error_code"] == code and len(calls) == 1
    assert call_budget_summary(ex.inspect(run)["call_budget"])["effective_budget_charge_usd"] == "0.0015"


def test_echoed_credential_never_enters_any_persistent_artifact(monkeypatch, tmp_path, image):
    calls = stub(monkeypatch, payload(SECRET))
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    assert ex.execute(run)["error_code"] == "MATERIAL_OCR_CREDENTIAL_ECHO_REJECTED" and len(calls) == 1
    assert all(SECRET.encode() not in p.read_bytes() for p in ex.store.run_dir(run).rglob("*") if p.is_file())


def test_json_escaped_credential_echo_is_also_rejected_and_not_returned(monkeypatch, tmp_path, image):
    encoded = json_bytes(payload(SECRET)).replace(SECRET.encode(), b"".join(f"\\u{ord(c):04x}".encode() for c in SECRET))
    calls = stub(monkeypatch, callback=lambda *_: (200, {}, encoded))
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    assert ex.execute(run)["error_code"] == "MATERIAL_OCR_CREDENTIAL_ECHO_REJECTED" and len(calls) == 1
    for path in ex.store.run_dir(run).rglob("*"):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        assert SECRET.encode() not in raw and encoded not in raw
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        assert SECRET not in json.dumps(decoded, ensure_ascii=False)


@pytest.mark.parametrize("kind", ["disabled_capture", "missing_key", "hash", "fingerprint", "local_engine", "retry"])
def test_preflight_blocks_bad_inputs_before_any_network(monkeypatch, tmp_path, image, kind):
    calls = stub(monkeypatch)
    config = options()
    if kind == "missing_key":
        monkeypatch.delenv("COMPANY_BID_DEEPSEEK_API_KEY")
    elif kind == "hash":
        image = (image[0], "0" * 64)
    elif kind == "fingerprint":
        config["expected_engine_fingerprint"] = "0" * 64
    elif kind == "local_engine":
        config["engine"] = "windows_ocr"
    elif kind == "retry":
        config["max_attempts"] = 2
    ex = executor(tmp_path, image, config, capture=kind != "disabled_capture")
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and calls == [] and not state.get("call_budget")


def test_only_failed_unit_is_retried_after_explicit_resume(monkeypatch, tmp_path, image):
    def response(*_):
        if len(calls) == 2:
            return 503, {}, b"{}"
        return 200, {}, json_bytes(payload())
    calls = stub(monkeypatch, callback=response)
    ex = executor(tmp_path, image, two=True)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and len(calls) == 2
    original = state["nodes"][0]["units"]["first"]["output_ref"]
    resumed = executor(tmp_path, image, two=True).resume(run)
    assert resumed["status"] == "succeeded" and len(calls) == 3
    assert resumed["nodes"][0]["units"]["first"]["output_ref"] == original
    assert call_budget_summary(resumed["call_budget"])["reservation_count"] == 3


def test_global_call_budget_stops_before_second_request(monkeypatch, tmp_path, image):
    calls = stub(monkeypatch)
    ex = executor(tmp_path, image, options(max_run_usd=".0015"), two=True)
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED" and len(calls) == 1


def test_changed_image_during_request_fails_with_original_response_kept(monkeypatch, tmp_path, image):
    def changed(*_):
        Image.new("RGB", (128, 64), "black").save(image[0])
        return 200, {}, json_bytes(payload())
    calls = stub(monkeypatch, callback=changed)
    ex = executor(tmp_path, image)
    run = ex.create_run({})
    assert ex.execute(run)["error_code"] == "MATERIAL_OCR_IMAGE_HASH_MISMATCH" and len(calls) == 1


def test_hard_timeout_kills_and_reaps_single_hidden_http_child(monkeypatch):
    children = []
    class Child:
        returncode = 0
        def __init__(self, args, **kwargs):
            self.args, self.kwargs, self.killed, self.inputs = args, kwargs, False, []
            children.append(self)
        def communicate(self, content=None, timeout=None):
            self.inputs.append((content, timeout))
            if len(self.inputs) == 1:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return b"", b""
        def kill(self):
            self.killed = True
    monkeypatch.setattr(ocr.subprocess, "Popen", Child)
    with pytest.raises(ocr.MaterialDeepSeekOCRFailure, match="MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN"):
        ocr._transport(b"image-bytes", "multipart/form-data", SECRET, 1)
    child = children[0]
    assert len(children) == 1 and child.killed and len(child.inputs) == 2
    assert SECRET not in " ".join(child.args) and child.kwargs["shell"] is False
    assert json.loads(child.inputs[0][0])["credential"] == SECRET
    if ocr.os.name == "nt":
        assert child.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert child.kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_deadline_terminates_actual_stalled_child_without_network(monkeypatch):
    real_popen = subprocess.Popen
    children = []
    def sleeping_child(args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(ocr.subprocess, "Popen", sleeping_child)
    start = time.monotonic()
    with pytest.raises(ocr.MaterialDeepSeekOCRFailure, match="MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN"):
        ocr._transport(b"image-bytes", "multipart/form-data", SECRET, 1)
    assert 0.9 <= time.monotonic() - start < 5
    assert len(children) == 1 and children[0].poll() is not None
