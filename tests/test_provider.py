import json
from urllib.error import HTTPError, URLError

import pytest

from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec


def response(usage=True):
    data = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"paragraphs": ["拟对各测点核对编号并记录异常，复测后移交记录。"], "steps": []}, ensure_ascii=False)}}]}
    if usage:
        data["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    return data


def run_provider(tmp_path, monkeypatch, transport, **kwargs):
    monkeypatch.setenv("TEST_BID_SECRET", "secret-canary-never-serialize")
    config = ChatConfig(endpoint="https://provider.invalid/chat/completions", model="test", auth_env="TEST_BID_SECRET", **kwargs)
    provider = ChatProposalProvider(config, transport=transport, sleep=lambda _: None)
    executor = Executor(tmp_path, [NodeSpec("N4", "v1", lambda ctx, data: provider.generate(ctx, {"section_id": "S1"}, [{"text": "SOURCE-CANARY"}]).model_dump())], {})
    run_id = executor.create_run({})
    return executor, run_id, executor.execute(run_id)


def test_actual_retries_and_usage_are_individually_recorded(tmp_path, monkeypatch):
    calls = []
    def transport(*args):
        calls.append(1)
        if len(calls) == 1:
            raise URLError("secret-canary-never-serialize SOURCE-CANARY")
        return response()
    executor, run_id, state = run_provider(tmp_path, monkeypatch, transport)
    assert state["status"] == "succeeded" and len(calls) == 2
    events = [json.loads(line) for line in (tmp_path / "runs" / run_id / "events.jsonl").read_text().splitlines()]
    assert sum(item["event"] == "call.started" for item in events) == 2
    assert sum(item["event"] == "call.completed" for item in events) == 1
    captures = state["nodes"][0]["attempts"][0]["captures"]
    assert sum(c["name"].startswith(("request-", "response-")) for c in captures) == 4
    ordinary = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs" / run_id).glob("*.jsonl"))
    assert "SOURCE-CANARY" not in ordinary and "secret-canary" not in ordinary
    assert "secret-canary" not in "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs" / run_id / "artifacts").glob("*.json"))


def test_auth_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []
    def transport(*args):
        calls.append(1)
        raise HTTPError("https://provider.invalid", 401, "SENSITIVE", {}, None)
    _, _, state = run_provider(tmp_path, monkeypatch, transport)
    assert state["status"] == "failed" and len(calls) == 1


def test_missing_usage_is_unknown_and_success_not_recalled(tmp_path, monkeypatch):
    calls = []
    def transport(*args):
        calls.append(1)
        return response(False)
    executor, run_id, state = run_provider(tmp_path, monkeypatch, transport)
    executor.resume(run_id)
    assert len(calls) == 1 and state["status"] == "succeeded"
    events = (tmp_path / "runs" / run_id / "events.jsonl").read_text()
    assert '"usage_status":"unknown"' in events


def test_truncation_is_failed_not_published(tmp_path, monkeypatch):
    data = response()
    data["choices"][0]["finish_reason"] = "length"
    _, _, state = run_provider(tmp_path, monkeypatch, lambda *args: data)
    assert state["status"] == "failed"


def test_credentials_cannot_appear_in_provider_url():
    with pytest.raises(ProviderFailure):
        ChatConfig(endpoint="https://user:secret@example.com/chat", model="test")
