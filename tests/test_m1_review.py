"""Independent M1 integration review; every provider transport is offline."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from company_bid import cli, pipeline
from company_bid.models import PreparedBid
from company_bid.provider import ChatConfig, ChatProposalProvider
from company_bid.runtime import CheckpointInvalidError, Executor, NodeSpec
from company_bid.sample import create_synthetic_sample
from company_bid.storage import atomic_json


def _config(model="review-v1"):
    return {"debug_capture": True, "provider": {
        "mode": "chat_completions", "endpoint": "https://offline.invalid/chat/completions",
        "model": model, "auth_env": "M1_REVIEW_SECRET", "max_attempts": 1,
        "max_run_calls": 10, "max_run_usd": 1.0,
    }}


def _response(text):
    return {"id": "offline-response", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"paragraphs": [text], "steps": []}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}


def _inject_transport(monkeypatch, transport):
    monkeypatch.setenv("M1_REVIEW_SECRET", "M1_REVIEW_CREDENTIAL_CANARY")
    monkeypatch.setattr(pipeline, "ChatProposalProvider", lambda config: ChatProposalProvider(
        config, transport=transport, sleep=lambda _: None))


def _initial(tmp_path, two_technical_sections=False):
    prepared = create_synthetic_sample(tmp_path / "input").model_dump(mode="json")
    if two_technical_sections:
        prepared["sections"].append({"section_id": "S5", "title": "五 拟实施成果交付复核", "kind": "technical",
                                      "requirement_refs": ["R-002"]})
    return {"prepared": PreparedBid.model_validate(prepared).model_dump(mode="json"), "proposals": {}}


def test_pipeline_failed_later_chapter_reuses_successful_paid_chapter(tmp_path, monkeypatch):
    calls = Counter()

    def transport(endpoint, payload, secret, timeout):
        section = json.loads(payload["messages"][-1]["content"])["section"]["section_id"]
        calls[section] += 1
        if section == "S5" and calls[section] == 1:
            raise TimeoutError("provider outcome unknown")
        return _response("拟按编号复核测点和异常复测记录，逐项确认交付清单。")

    _inject_transport(monkeypatch, transport)
    root, config = tmp_path / "runtime", _config()
    executor = pipeline.build_executor(root, config)
    run = executor.create_run(_initial(tmp_path, two_technical_sections=True))
    failed = executor.execute(run)
    n4 = failed["nodes"][1]
    first_ref = n4["units"]["S2"]["output_ref"]
    assert failed["status"] == "failed" and n4["units"]["S2"]["status"] == "succeeded"
    restarted = pipeline.build_executor(root, config)
    finished = restarted.resume(run)
    assert finished["status"] == "succeeded"
    assert calls == {"S2": 1, "S5": 2}
    assert finished["nodes"][1]["units"]["S2"]["output_ref"] == first_ref
    assert finished["call_budget"]["reservation_count"] == 3


def test_render_failure_resumes_only_renderer_and_publishes_exact_bytes(tmp_path, monkeypatch, capsys):
    calls, render_attempts = [], []
    _inject_transport(monkeypatch, lambda *args: calls.append(True) or _response(
        "拟按作业许可核对隔离条件，逐点登记实测值，异常复测后移交编号记录。"))
    actual_render = pipeline.render_package

    def render(package, target):
        render_attempts.append(True)
        if len(render_attempts) == 1:
            raise OSError("offline renderer interruption")
        return actual_render(package, target)

    monkeypatch.setattr(pipeline, "render_package", render)
    root, config = tmp_path / "runtime", _config()
    executor = pipeline.build_executor(root, config)
    run = executor.create_run(_initial(tmp_path))
    failed = executor.execute(run)
    assert failed["status"] == "failed" and failed["cursor"] == 3
    succeeded = pipeline.build_executor(root, config).resume(run, stop_after="N6")
    assert succeeded["status"] == "succeeded" and succeeded["cursor"] == 4
    assert len(calls) == 1 and len(succeeded["nodes"][1]["attempts"]) == 1
    manifest = executor.output(run)
    original = {}
    for key, reference in manifest["files"].items():
        content = (root / reference["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == reference["sha256"]
        assert len(content) == reference["size_bytes"]
        original[key] = content
    config_file = tmp_path / "config.json"
    atomic_json(config_file, config)
    args = ["--root", str(root), "--config", str(config_file), "result", run]
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert len(calls) == 1 and len(render_attempts) == 2
    assert all((root / manifest["files"][key]["path"]).read_bytes() == content for key, content in original.items())


def test_changed_model_forks_n4_reuses_n0_and_preserves_parent_result(tmp_path, monkeypatch):
    calls = []

    def transport(endpoint, payload, secret, timeout):
        calls.append(payload["model"])
        return _response("拟核对巡检记录并复测异常，形成项目交付记录。" + payload["model"])

    _inject_transport(monkeypatch, transport)
    root = tmp_path / "runtime"
    old = pipeline.build_executor(root, _config("review-v1"))
    parent = old.create_run(_initial(tmp_path))
    old.execute(parent)
    parent_state = old.inspect(parent)
    parent_manifest = old.output(parent)
    parent_bytes = (root / parent_manifest["files"]["bid_docx"]["path"]).read_bytes()
    new = pipeline.build_executor(root, _config("review-v2"))
    child = new.fork(parent, "N4")
    assert new.execute(child)["status"] == "succeeded"
    child_state = new.inspect(child)
    assert calls == ["review-v1", "review-v2"]
    assert child_state["nodes"][0]["output_ref"] == parent_state["nodes"][0]["output_ref"]
    assert child_state["nodes"][0]["attempts"] == []
    assert new.diff(parent, child)["configuration_changes"] == ["provider"]
    assert (root / parent_manifest["files"]["bid_docx"]["path"]).read_bytes() == parent_bytes
    assert new.output(child)["files"]["bid_docx"]["path"] != parent_manifest["files"]["bid_docx"]["path"]


def test_source_changes_between_check_and_copy_stop_before_model(tmp_path, monkeypatch):
    calls = []
    _inject_transport(monkeypatch, lambda *args: calls.append(True) or _response("拟实施。"))
    initial = _initial(tmp_path)
    source = Path(initial["prepared"]["sources"][0]["path"])
    original_validate = pipeline.validate_sources

    def mutate_after_check(prepared):
        original_validate(prepared)
        source.write_bytes(b"changed between validation and freeze")

    monkeypatch.setattr(pipeline, "validate_sources", mutate_after_check)
    executor = pipeline.build_executor(tmp_path / "runtime", _config())
    state = executor.execute(executor.create_run(initial))
    assert state["status"] == "failed" and state["error_code"] == "SOURCE_CHANGED_DURING_FREEZE"
    assert state["cursor"] == 0 and not calls and "call_budget" not in state


def test_final_result_corruption_is_rejected_by_cli(tmp_path, monkeypatch, capsys):
    _inject_transport(monkeypatch, lambda *args: _response("拟按测点编号核对记录并复测异常。"))
    root, config = tmp_path / "runtime", _config()
    executor = pipeline.build_executor(root, config)
    run = executor.create_run(_initial(tmp_path))
    executor.execute(run)
    reference = executor.output(run)["files"]["bid_docx"]
    (root / reference["path"]).write_bytes(b"tampered result")
    config_file = tmp_path / "config.json"
    atomic_json(config_file, config)
    assert cli.main(["--root", str(root), "--config", str(config_file), "result", run]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == "CHECKPOINT_INVALID"
    with pytest.raises(CheckpointInvalidError):
        executor.output(run)


def test_request_capture_failure_prevents_budget_reservation_and_http(tmp_path, monkeypatch):
    calls = []
    _inject_transport(monkeypatch, lambda *args: calls.append(True) or _response("拟实施。"))
    executor = pipeline.build_executor(tmp_path / "runtime", _config())
    run = executor.create_run(_initial(tmp_path))
    original = executor.store.put_json

    def fail_request(run_id, name, payload):
        if name == "capture" and isinstance(payload, dict) and "messages" in payload:
            raise OSError("capture unavailable")
        return original(run_id, name, payload)

    monkeypatch.setattr(executor.store, "put_json", fail_request)
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED"
    assert not calls and "call_budget" not in state


def test_http_error_body_is_controlled_redacted_and_not_retried(tmp_path, monkeypatch):
    secret, body_canary = "M1_REVIEW_CREDENTIAL_CANARY", "PRIVATE_HTTP_ERROR_BODY_CANARY"
    monkeypatch.setenv("M1_REVIEW_SECRET", secret)
    calls = []

    def transport(*args):
        calls.append(True)
        raise HTTPError("https://offline.invalid", 401, "ignored", {}, BytesIO((body_canary + secret).encode()))

    provider = ChatProposalProvider(ChatConfig("https://offline.invalid", "offline", auth_env="M1_REVIEW_SECRET"), transport=transport)
    executor = Executor(tmp_path, [NodeSpec("N4", "review-v1", lambda ctx, data: provider.generate(ctx, {}, []).model_dump())], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["error_code"] == "PROVIDER_AUTH_FAILED" and len(calls) == 1
    captures = state["nodes"][0]["attempts"][0]["captures"]
    response = next(item for item in captures if item["name"].startswith("response-error"))
    controlled = executor.store.read_bytes(response["ref"]).decode()
    ordinary = "".join(path.read_text() for path in executor.store.run_dir(run).glob("*.jsonl"))
    assert body_canary in controlled and secret not in controlled
    assert body_canary not in ordinary and secret not in ordinary


def test_malformed_http_json_keeps_invalid_body_in_controlled_capture(tmp_path, monkeypatch):
    """A bad HTTP-200 body must remain diagnosable after transport parsing fails."""
    secret, body_canary = "M1_REVIEW_CREDENTIAL_CANARY", "PRIVATE_MALFORMED_RESPONSE_CANARY"
    monkeypatch.setenv("M1_REVIEW_SECRET", secret)

    def transport(*args):
        return (body_canary + secret).encode()

    provider = ChatProposalProvider(ChatConfig("https://offline.invalid", "offline", auth_env="M1_REVIEW_SECRET", max_attempts=1), transport=transport)
    executor = Executor(tmp_path, [NodeSpec("N4", "review-v1", lambda ctx, data: provider.generate(ctx, {}, []).model_dump())], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["error_code"] == "PROVIDER_SCHEMA_INVALID"
    captures = state["nodes"][0]["attempts"][0]["captures"]
    controlled = "".join(executor.store.read_bytes(item["ref"]).decode() for item in captures if item["name"].startswith("response") and item["status"] == "captured")
    assert body_canary in controlled and secret not in controlled
    ordinary = "".join(path.read_text() for path in executor.store.run_dir(run).glob("*.jsonl"))
    assert body_canary not in ordinary and secret not in ordinary


def test_oversized_response_retains_bounded_capture_and_explicit_partial_status(tmp_path, monkeypatch):
    monkeypatch.setenv("M1_REVIEW_SECRET", "M1_REVIEW_CREDENTIAL_CANARY")
    provider = ChatProposalProvider(ChatConfig("https://offline.invalid", "offline", auth_env="M1_REVIEW_SECRET", max_attempts=1),
                                    transport=lambda *args: b"large-response-canary" + b"x" * (4 * 1024 * 1024))
    executor = Executor(tmp_path, [NodeSpec("N4", "review-v1", lambda ctx, data: provider.generate(ctx, {}, []).model_dump())], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_RESPONSE_TOO_LARGE"
    assert state["observability"]["capture_status"] == "partial"
    capture = next(item for item in state["nodes"][0]["attempts"][0]["captures"] if item["name"].startswith("response-error"))
    assert capture["status"] == "partial"
    assert capture["ref"]["size_bytes"] < 66000
