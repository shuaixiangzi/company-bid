"""Explicit thinking effort uses the same frozen, budgeted actual request."""
from __future__ import annotations

import copy
from dataclasses import asdict
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure, digest
from company_bid.runtime import Executor, NodeSpec


def run_json(tmp_path, monkeypatch, *, thinking_mode=None, reasoning_effort=None, reasoning_only=False):
    monkeypatch.setenv("TEST_EFFORT_KEY", "synthetic-effort-credential")
    config = ChatConfig(endpoint="https://example.invalid/chat", model="synthetic", auth_env="TEST_EFFORT_KEY",
                        max_attempts=1, max_output_tokens=8000, max_run_usd=1, max_run_calls=1,
                        thinking_mode=thinking_mode, reasoning_effort=reasoning_effort)
    sent, validated = [], []
    def transport(endpoint, payload, secret, timeout):
        sent.append(copy.deepcopy(payload))
        return {"id": "synthetic-request", "choices": [{"finish_reason": "length" if reasoning_only else "stop",
                    "message": {"content": "" if reasoning_only else '{"ok":true}',
                                "reasoning_content": "synthetic reasoning" if reasoning_only else ""}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8000 if reasoning_only else 5,
                          "total_tokens": 8007 if reasoning_only else 12,
                          "completion_tokens_details": {"reasoning_tokens": 8000 if reasoning_only else 0}}}
    provider = ChatProposalProvider(config, transport=transport, sleep=lambda _: pytest.fail("No retry is permitted in this test"))
    def validate(value):
        validated.append(copy.deepcopy(value))
        return value
    def handler(ctx, value):
        return ctx.run_unit("W1", value, lambda: provider.generate_json(ctx, value, unit_id="W1", prompt_version="effort-v1",
            system_prompt="Return synthetic JSON only.", task_type="extract", validate=validate))
    executor = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {"provider": asdict(config)})
    run = executor.create_run({"synthetic": True})
    state = executor.execute(run)
    return executor, run, state, config, sent, validated


@pytest.mark.parametrize("mode", [None, "enabled", "disabled"])
def test_default_effort_is_omitted_without_changing_existing_thinking_payload(tmp_path, monkeypatch, mode):
    executor, run, state, _, sent, validated = run_json(tmp_path, monkeypatch, thinking_mode=mode)
    assert state["status"] == "succeeded" and len(sent) == len(validated) == 1
    assert "reasoning_effort" not in sent[0]
    if mode is None:
        assert "thinking" not in sent[0]
    else:
        assert sent[0]["thinking"] == {"type": mode}
    assert inspect_diagnostics(tmp_path, run)["actual_call_attempts"] == 1


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_explicit_effort_is_bound_to_payload_capture_hash_and_full_byte_reservation(tmp_path, monkeypatch, effort):
    executor, run, state, config, sent, _ = run_json(tmp_path, monkeypatch, thinking_mode="enabled", reasoning_effort=effort)
    assert state["status"] == "succeeded" and len(sent) == 1
    payload = sent[0]
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == effort
    assert payload["max_tokens"] == 8000 and config.max_attempts == 1
    captures = state["nodes"][0]["attempts"][0]["captures"]
    request = executor.store.read_json(next(c["ref"] for c in captures if c["name"].startswith("request-")))
    assert request == payload
    calls = inspect_diagnostics(tmp_path, run)["calls"]
    assert len(calls) == 1 and calls[0]["input_hash"] == digest(payload) == digest(request)
    without_effort = {key: value for key, value in payload.items() if key != "reasoning_effort"}
    assert digest(payload) != digest(without_effort)
    def upper_bound(body):
        request_tokens = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 2048
        return (request_tokens * config.input_usd_per_million + config.max_output_tokens * config.output_usd_per_million) / 1_000_000
    reserved = state["call_budget"]
    assert reserved["reservation_count"] == 1
    assert float(reserved["reservations"][0]["estimated_upper_usd"]) == pytest.approx(upper_bound(payload))
    assert upper_bound(payload) > upper_bound(without_effort)
    assert float(reserved["reserved_upper_usd"]) == pytest.approx(upper_bound(payload))


@pytest.mark.parametrize("mode,effort", [
    *[(mode, effort) for mode in (None, "disabled") for effort in ("low", "high", "max")],
    *[("enabled", effort) for effort in ("none", "medium", "LOW", "", True, 1, [], {})],
])
def test_conflicting_or_invalid_effort_is_rejected_before_transport(mode, effort):
    sent = []
    def transport(*args):
        sent.append(True)
        pytest.fail("Invalid configuration must not send HTTP")
    with pytest.raises(ProviderFailure, match="PROVIDER_CONFIG_INVALID"):
        ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="synthetic",
                            max_attempts=1, thinking_mode=mode, reasoning_effort=effort), transport=transport)
    assert sent == []


def test_reasoning_only_length_is_failed_without_validator_or_another_attempt(tmp_path, monkeypatch):
    executor, run, state, _, sent, validated = run_json(tmp_path, monkeypatch,
        thinking_mode="enabled", reasoning_effort="low", reasoning_only=True)
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_TRUNCATED_OR_INCOMPLETE"
    assert len(sent) == 1 and validated == []
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    assert state["call_budget"]["reservation_count"] == 1
    report = inspect_diagnostics(tmp_path, run)
    assert report["actual_call_attempts"] == 1 and report["unknown_usage_attempts"] == 0
    call = report["calls"][0]
    assert call["outcome"] == "failed" and call["usage_status"] == "known"
    assert call["output_tokens"] == 8000 and call["total_tokens"] == 8007
    assert call["validation_status"] == "failed" and call["input_hash"] == digest(sent[0])
    directory = executor.store.run_dir(run)
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(row["event"] == "call.started" for row in events) == 1
    assert sum(row["event"] == "call.failed" for row in events) == 1
    assert not any(row["event"] in ("call.completed", "call.retry_scheduled") for row in events)
    spans = [json.loads(line) for line in (directory / "spans.jsonl").read_text(encoding="utf-8").splitlines()]
    attempts = [span for span in spans if span["name"] == "provider.call"]
    assert len(attempts) == 1 and attempts[0]["status"] == "ERROR" and attempts[0]["span_id"] == call["span_id"]
    captures = state["nodes"][0]["attempts"][0]["captures"]
    response = executor.store.read_json(next(c["ref"] for c in captures if c["name"].startswith("response-")))
    assert response["choices"][0]["message"]["content"] == ""
    assert response["usage"]["completion_tokens_details"]["reasoning_tokens"] == 8000
