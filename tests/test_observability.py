from __future__ import annotations

import json

from company_bid.observability import redact_payload, safe_metadata
from company_bid.runtime import Executor, NodeSpec


def test_opencode_credential_prefix_is_redacted_even_without_a_named_secret_field():
    key = "oc_sk_SYNTHETIC_ONLY_never_a_real_credential_012345"
    value = redact_payload({"note": "echo=" + key, key: "synthetic field name"})
    assert key not in json.dumps(value)
    assert "[REDACTED]" in value["note"]
    assert safe_metadata({"provider_request_id": key, "model": "deepseek-v4.1-flash"}) == {"model": "deepseek-v4.1-flash"}


def test_secret_and_source_canaries_absent_from_events_spans_and_error(tmp_path):
    source = "SOURCE_CANARY_PRIVATE_BID"
    secret = "SECRET_CANARY_CREDENTIAL"

    def handler(ctx, data):
        with ctx.call_span({"provider": "synthetic", "call_id": "c1", "call_attempt": 1}):
            ctx.call_event("call.started", {"provider": "synthetic", "source_text": source, "api_key": secret,
                                           "url": "https://example.invalid/?token=" + secret})
            captured = ctx.capture("request", {"text": source, "api_key": secret, "nested": {"Authorization": "Bearer " + secret},
                                               "echo": secret, "url": "https://example.invalid/?token=" + secret})
            assert captured["status"] == "captured"
        raise ValueError(source + secret)

    executor = Executor(tmp_path, [NodeSpec("N4", "1", handler)], {})
    run = executor.create_run({"text": source})
    state = executor.execute(run)
    directory = executor.store.run_dir(run)
    ordinary = (directory / "events.jsonl").read_text() + (directory / "spans.jsonl").read_text() + json.dumps(state)
    assert source not in ordinary and secret not in ordinary
    request_capture = state["nodes"][0]["attempts"][0]["captures"][1]
    payload = executor.store.read_bytes(request_capture["ref"]).decode()
    assert source in payload and secret not in payload
    spans = [json.loads(line) for line in (directory / "spans.jsonl").read_text().splitlines()]
    assert any(span["name"] == "provider.call" and len(span["trace_id"]) == 32 for span in spans)
    assert state["error_code"] == "NODE_EXECUTION_FAILED"


def test_capture_disabled_is_explicit_and_does_not_block(tmp_path):
    def handler(ctx, data):
        assert ctx.capture("request", {"source": "private"})["status"] == "disabled_by_policy"
        return {"ok": True}
    executor = Executor(tmp_path, [NodeSpec("N4", "1", handler)], {"debug_capture": False})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "succeeded"
    assert state["observability"]["capture_status"] == "disabled_by_policy"


def test_capture_failure_blocks_node_and_records_gap(tmp_path, monkeypatch):
    executor = Executor(tmp_path, [NodeSpec("N4", "1", lambda ctx, data: {"ok": True})], {})
    run = executor.create_run({})
    original = executor.store.put_json

    def fail_capture(run_id, name, payload):
        if name == "capture":
            raise OSError("secret source")
        return original(run_id, name, payload)

    monkeypatch.setattr(executor.store, "put_json", fail_capture)
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED"
    assert state["observability"]["capture_status"] == "write_failed"
    assert state["cursor"] == 0
    assert "output_ref" not in state["nodes"][0]


def test_local_span_write_failure_is_visible(tmp_path, monkeypatch):
    import company_bid.observability as module

    original = module.append_json

    def fail_spans(path, value):
        if path.name == "spans.jsonl":
            raise OSError("secret source")
        return original(path, value)

    monkeypatch.setattr(module, "append_json", fail_spans)
    executor = Executor(tmp_path, [NodeSpec("N0", "1", lambda ctx, data: {"ok": True})], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed"
    assert state["observability"]["trace_status"] == "write_failed"
    assert state["cursor"] == 0


def test_redaction_handles_structured_headers_signed_urls_and_credentials():
    result = redact_payload({"Authorization": "Bearer secret", "text": "Bearer secret", "Cookie": "sid=abc",
                             "url": "https://user:pass@example.invalid/file?X-Amz-Signature=abc&safe=yes"})
    encoded = json.dumps(result)
    assert "secret" not in encoded and "sid=abc" not in encoded and "user:pass" not in encoded
    assert "safe=yes" in result["url"] and "abc" not in result["url"]


def test_redaction_cleans_credential_echoes_in_mapping_keys():
    secret = "opaque-active-canary-86"
    value = {"api_key": secret, secret: {"prefix-" + secret: "source text"}, "other": {"sk-testvalue0123456789": "unused"}}
    result = redact_payload(value)
    encoded = json.dumps(result)
    assert secret not in encoded and "sk-testvalue0123456789" not in encoded
    assert "source text" in encoded
    assert safe_metadata({"source_text": "private", "input_tokens": 3, "total_tokens": 4}) == {"input_tokens": 3, "total_tokens": 4}


def test_provider_environment_reference_is_frozen_without_credential_value(tmp_path):
    config = {"provider": {"api_key_env": "COMPANY_BID_API_KEY", "api_key": "secret", "model": "test"}}
    executor = Executor(tmp_path, [NodeSpec("N4", "1", lambda ctx, data: data)], config)
    run = executor.create_run({})
    frozen = executor.inspect(run)["execution_config"]["provider"]
    assert frozen["api_key_env"] == "COMPANY_BID_API_KEY"
    assert frozen["api_key"] == "[REDACTED]"
