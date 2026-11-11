"""OpenCode Go transport/receipts with synthetic credentials and zero network."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import json

import pytest

import company_bid.provider as module
from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeContext, NodeSpec, call_budget_summary


def response():
    return {"id": "synthetic-go-request", "model": module.OPENCODE_GO_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0,
                      "opencode": {"billing_source": "go", "allowance": .001, "list_cost": .002}}}


def setup_run(tmp_path, monkeypatch, *, reply=None, validate=None, units=("W1",), wrap_unit=True, **options):
    monkeypatch.setenv("TEST_GO_KEY", "synthetic-oc_sk-credential-canary")
    values = dict(endpoint=module.OPENCODE_GO_ENDPOINT, model=module.OPENCODE_GO_MODEL, service="opencode_go",
                  auth_env="TEST_GO_KEY", max_attempts=2, max_run_usd=.90, max_run_calls=10,
                  max_output_tokens=65536, timeout_seconds=600, thinking_mode="enabled", reasoning_effort="max")
    values.update(options)
    config = ChatConfig(**values)
    sent, validated = [], []
    def transport(endpoint, payload, secret, timeout, *, headers):
        sent.append({"endpoint": endpoint, "payload": deepcopy(payload), "timeout": timeout, "headers": deepcopy(headers)})
        return reply(len(sent)) if reply else response()
    provider = ChatProposalProvider(config, transport=transport, sleep=lambda _: None)
    def check(candidate):
        validated.append(deepcopy(candidate))
        return validate(candidate) if validate else candidate
    def handler(ctx, data):
        def call(unit):
            return provider.generate_json(ctx, {"window": unit}, unit_id=unit, prompt_version="synthetic-go-v1",
                task_type="extract", system_prompt="Return synthetic JSON.", validate=check)
        return {"results": [ctx.run_unit(unit, {"window": unit}, lambda unit=unit: call(unit)) if wrap_unit else call(unit)
                            for unit in units]}
    nodes, settings = [NodeSpec("N2", "synthetic-go-v1", handler)], {"provider": asdict(config), "debug_capture": True}
    executor = Executor(tmp_path, nodes, settings)
    rid = executor.create_run({"synthetic": True})
    return executor, rid, sent, validated, nodes, settings


def captures(executor, state, prefix):
    return [executor.store.read_json(item["ref"]) for attempt in state["nodes"][0]["attempts"]
            for item in attempt.get("captures", []) if item["name"].startswith(prefix)]


@pytest.mark.parametrize("source", ["go", "go-plus"])
def test_subscription_receipt_keeps_peak_quota_and_stable_run_headers(tmp_path, monkeypatch, source):
    def reply(count):
        item = response()
        item["usage"]["opencode"]["billing_source"] = source
        return item
    executor, rid, sent, validated, _, _ = setup_run(tmp_path, monkeypatch, reply=reply, units=("W1", "W2"))
    state = executor.execute(rid)
    assert state["status"] == "succeeded" and len(sent) == len(validated) == 2
    assert all(item["endpoint"] == module.OPENCODE_GO_ENDPOINT for item in sent)
    assert all(item["headers"] == {"User-Agent": "company-bid-test/0.1", "x-opencode-session": rid} for item in sent)
    assert all(item["payload"]["thinking"] == {"type": "enabled"} and item["payload"]["reasoning_effort"] == "max" for item in sent)
    assert all(item["payload"]["max_tokens"] == 65536 and item["timeout"] == 600 for item in sent)
    receipts = captures(executor, state, "billing-")
    assert len(receipts) == 2 and all(r["billing_status"] == "known" and r["billing_source"] == source for r in receipts)
    assert all(Decimal(r["reported_balance_charge"]["usd"]) == 0 and r["subscription_allowance"]["usd"] == "0.001" for r in receipts)
    assert all(r["quota_basis"] == "frozen_peak_rate_estimate_not_reported_charge" for r in receipts)
    assert call_budget_summary(state["call_budget"])["effective_budget_charge_usd"] == "0.000018"
    assert all(capture["usage"] == reply(1)["usage"] for capture in captures(executor, state, "response-"))
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 2 and all(c["provider"] == "opencode_go" for c in report["calls"])
    ordinary = "\n".join(path.read_text(encoding="utf8") for path in executor.store.run_dir(rid).glob("*.jsonl"))
    assert "synthetic-oc_sk-credential-canary" not in ordinary and "Authorization" not in ordinary
    assert "synthetic-oc_sk-credential-canary" not in "\n".join(path.read_text(encoding="utf8") for path in (executor.store.run_dir(rid) / "artifacts").glob("*.json"))


@pytest.mark.parametrize("kind,code", [
    ("balance", "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"),
    ("byok", "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"),
    ("free", "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"),
    ("go_charge", "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"),
    ("missing_opencode", "PROVIDER_BILLING_UNVERIFIED"),
    ("missing_cost", "PROVIDER_BILLING_UNVERIFIED"),
    ("missing_allowance", "PROVIDER_BILLING_UNVERIFIED"),
    ("missing_list", "PROVIDER_BILLING_UNVERIFIED"),
    ("bad_source", "PROVIDER_BILLING_INVALID"),
    ("negative", "PROVIDER_BILLING_INVALID"),
    ("bool", "PROVIDER_BILLING_INVALID"),
    ("string", "PROVIDER_BILLING_INVALID"),
    ("bad_opencode", "PROVIDER_BILLING_INVALID"),
    ("estimated_go", "PROVIDER_BILLING_INVALID"),
])
def test_rejected_billing_is_recorded_settled_and_blocks_resume_without_fallback(tmp_path, monkeypatch, kind, code):
    item = response()
    usage, billing = item["usage"], item["usage"]["opencode"]
    if kind in {"balance", "byok", "free"}:
        billing["billing_source"] = kind
        billing.pop("allowance")
        usage["cost"] = 0 if kind == "free" else .003
        if kind == "byok": billing["estimated"] = True
    elif kind == "go_charge": usage["cost"] = .003
    elif kind == "missing_opencode": usage.pop("opencode")
    elif kind == "missing_cost": usage.pop("cost")
    elif kind == "missing_allowance": billing.pop("allowance")
    elif kind == "missing_list": billing.pop("list_cost")
    elif kind == "bad_source": billing["billing_source"] = "unexpected"
    elif kind == "negative": billing["allowance"] = -1
    elif kind == "bool": usage["cost"] = False
    elif kind == "string": usage["cost"] = "0"
    elif kind == "bad_opencode": usage["opencode"] = []
    else: billing["estimated"] = True
    executor, rid, sent, checked, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: deepcopy(item))
    first = executor.execute(rid)
    assert first["status"] == "failed" and first["error_code"] == code and len(sent) == 1 and not checked
    summary = call_budget_summary(first["call_budget"])
    assert summary["known_usage_estimate_count"] == 1 and summary["effective_budget_charge_usd"] == "0.000009"
    assert first["call_budget"]["provider_stop"]["code"] == code
    receipt = captures(executor, first, "billing-")[0]
    assert receipt["policy_error_code"] == code and receipt["server_side_extra_usage_may_already_have_charged"]
    if kind == "byok":
        assert receipt["charge_is_estimate"] is True
        assert receipt["reported_balance_charge"]["status"] == "not_applicable"
        assert receipt["reported_usage_cost"]["usd"] == "0.003"
    assert captures(executor, first, "response-")[0]["usage"] == item["usage"]
    final = Executor(tmp_path, nodes, settings).resume(rid)
    assert final["error_code"] == code and len(sent) == 1 and not checked
    assert inspect_diagnostics(tmp_path, rid)["actual_call_attempts"] == 1
    assert all(call["endpoint"] == module.OPENCODE_GO_ENDPOINT for call in sent)


def test_missing_token_usage_never_converts_subscription_zero_charge_into_zero_quota(tmp_path, monkeypatch):
    item = response()
    item["usage"].pop("total_tokens")
    executor, rid, _, _, _, _ = setup_run(tmp_path, monkeypatch, reply=lambda _: item)
    state = executor.execute(rid)
    assert state["status"] == "succeeded"
    summary = call_budget_summary(state["call_budget"])
    assert summary["unknown_reservation_count"] == 1 and Decimal(summary["effective_budget_charge_usd"]) > 0
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]


def test_invalid_json_response_cannot_bypass_the_subscription_billing_guard(tmp_path, monkeypatch):
    executor, rid, sent, _, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: b"not-json-synthetic")
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_BILLING_INVALID" and len(sent) == 1
    assert captures(executor, state, "response-error-")[0]["body"] == "not-json-synthetic"
    assert call_budget_summary(state["call_budget"])["unknown_reservation_count"] == 1
    assert Executor(tmp_path, nodes, settings).resume(rid)["error_code"] == "PROVIDER_BILLING_INVALID" and len(sent) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_receipt_is_invalid_retains_raw_error_and_full_unknown_reserve(tmp_path, monkeypatch, value):
    item = response()
    item["usage"]["cost"] = value
    executor, rid, sent, _, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: item)
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_BILLING_INVALID" and len(sent) == 1
    assert captures(executor, state, "response-error-")[0]["body_status"] == "noncanonical_json"
    assert captures(executor, state, "billing-")[0]["billing_status"] == "invalid"
    assert call_budget_summary(state["call_budget"])["unknown_reservation_count"] == 1
    assert Executor(tmp_path, nodes, settings).resume(rid)["error_code"] == "PROVIDER_BILLING_INVALID" and len(sent) == 1


def test_rejected_billing_latches_when_usage_event_fails_after_durable_settlement(tmp_path, monkeypatch):
    item = response()
    item["usage"]["opencode"].update(billing_source="balance")
    original = NodeContext.call_event
    def event(ctx, name, metadata=None):
        if name == "call.usage_recorded":
            raise RuntimeError("synthetic usage event failure")
        return original(ctx, name, metadata)
    monkeypatch.setattr(NodeContext, "call_event", event)
    executor, rid, sent, _, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: item)
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_EXECUTION_ERROR" and len(sent) == 1
    assert call_budget_summary(state["call_budget"])["known_usage_estimate_count"] == 1
    assert state["call_budget"]["provider_stop"]["code"] == "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"
    assert Executor(tmp_path, nodes, settings).resume(rid)["error_code"] == "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED" and len(sent) == 1


def test_rejected_billing_latches_even_when_receipt_capture_fails(tmp_path, monkeypatch):
    original = NodeContext.capture
    def capture(ctx, name, value, **kwargs):
        if name.startswith("billing-"):
            return {"status": "write_failed"}
        return original(ctx, name, value, **kwargs)
    monkeypatch.setattr(NodeContext, "capture", capture)
    item = response()
    item["usage"].pop("opencode")
    executor, rid, sent, _, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: item)
    state = executor.execute(rid)
    assert state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED" and len(sent) == 1
    assert state["call_budget"]["provider_stop"]["code"] == "PROVIDER_BILLING_UNVERIFIED"
    assert Executor(tmp_path, nodes, settings).resume(rid)["error_code"] == "PROVIDER_BILLING_UNVERIFIED" and len(sent) == 1


@pytest.mark.parametrize("reply_kind", ["complete_balance", "invalid_json", "noncanonical_json"])
@pytest.mark.parametrize("failure_point", ["capture_write_failed", "capture_state_raises"])
def test_response_capture_failure_cannot_erase_a_known_billing_rejection(tmp_path, monkeypatch, reply_kind, failure_point):
    item = response()
    item["usage"]["cost"] = .003
    item["usage"]["opencode"]["billing_source"] = "balance"
    if reply_kind == "invalid_json":
        item = b"not-json-synthetic"
    elif reply_kind == "noncanonical_json":
        item["usage"]["cost"] = float("nan")
        item = json.dumps(item).encode()
    expected_policy = "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED" if reply_kind == "complete_balance" else "PROVIDER_BILLING_INVALID"
    executor, rid, sent, checked, nodes, settings = setup_run(tmp_path, monkeypatch, reply=lambda _: item)
    failed = []
    if failure_point == "capture_write_failed":
        original = executor.store.put_json
        def put_json(run_id, name, value):
            target = isinstance(value, dict) and (
                "choices" in value if reply_kind == "complete_balance" else
                value.get("body_status") == ("invalid_json" if reply_kind == "invalid_json" else "noncanonical_json"))
            if not failed and name == "capture" and target:
                failed.append(True)
                raise OSError("Synthetic one-time response artifact failure")
            return original(run_id, name, value)
        monkeypatch.setattr(executor.store, "put_json", put_json)
    else:
        original = executor.store.write_state
        def write_state(state):
            attempts = state["nodes"][0]["attempts"]
            captures = attempts[-1].get("captures", []) if attempts else []
            if not failed and captures and captures[-1]["name"].startswith("response-"):
                failed.append(True)
                raise OSError("Synthetic one-time response state failure")
            return original(state)
        monkeypatch.setattr(executor.store, "write_state", write_state)
    first = executor.execute(rid)
    assert failed and first["status"] == "failed" and len(sent) == 1 and not checked
    summary = call_budget_summary(first["call_budget"])
    assert summary["known_usage_estimate_count"] == 0 and summary["unknown_reservation_count"] == 1
    assert summary["effective_budget_charge_usd"] == summary["original_reserved_upper_usd"]
    assert first["call_budget"]["provider_stop"]["code"] == expected_policy
    assert captures(executor, first, "billing-")[0]["policy_error_code"] == expected_policy
    restored = Executor(tmp_path, nodes, settings).resume(rid)
    assert restored["error_code"] == expected_policy and len(sent) == 1 and not checked
    assert inspect_diagnostics(tmp_path, rid)["actual_call_attempts"] == 1


def test_node_only_call_retains_original_policy_code(tmp_path, monkeypatch):
    item = response()
    item["usage"]["opencode"]["billing_source"] = "balance"
    executor, rid, sent, _, _, _ = setup_run(tmp_path, monkeypatch, reply=lambda _: item, wrap_unit=False)
    state = executor.execute(rid)
    assert state["error_code"] == "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED" and len(sent) == 1


def test_generic_four_argument_transport_and_defaults_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GO_KEY", "synthetic-generic-credential")
    called = []
    def old_transport(endpoint, payload, secret, timeout):
        called.append(payload)
        item = response()
        item["usage"].pop("opencode")
        return item
    config = ChatConfig(endpoint="https://example.invalid/chat", model="synthetic", auth_env="TEST_GO_KEY", max_run_usd=.90)
    assert config.service == "generic" and config.max_output_tokens == 2500 and config.timeout_seconds == 90
    provider = ChatProposalProvider(config, transport=old_transport)
    def handler(ctx, data):
        return provider.generate_json(ctx, data, unit_id="W", prompt_version="1", task_type="extract", system_prompt="synthetic", validate=lambda x: x)
    executor = Executor(tmp_path, [NodeSpec("N2", "generic", handler)], {})
    rid = executor.create_run({})
    state = executor.execute(rid)
    assert state["status"] == "succeeded" and len(called) == 1
    assert not captures(executor, state, "billing-") and not captures(executor, state, "transport-")


@pytest.mark.parametrize("options,code", [
    ({"service": "unknown"}, "PROVIDER_SERVICE_INVALID"),
    ({"endpoint": "https://api.deepseek.com/chat/completions"}, "PROVIDER_SERVICE_ENDPOINT_MODEL_INVALID"),
    ({"endpoint": "https://opencode.ai/inference/openai/v1/chat/completions"}, "PROVIDER_SERVICE_ENDPOINT_MODEL_INVALID"),
    ({"model": "other-model"}, "PROVIDER_SERVICE_ENDPOINT_MODEL_INVALID"),
    ({"max_output_tokens": 65537}, "PROVIDER_BUDGET_INVALID"),
    ({"timeout_seconds": 601}, "PROVIDER_CONFIG_INVALID"),
    ({"input_usd_per_million": 0}, "PROVIDER_BUDGET_INVALID"),
])
def test_go_service_configuration_is_explicit_bounded_and_never_zero_priced(options, code):
    settings = {"endpoint": module.OPENCODE_GO_ENDPOINT, "model": module.OPENCODE_GO_MODEL, "service": "opencode_go"}
    settings.update(options)
    with pytest.raises(ProviderFailure, match=code):
        ChatConfig(**settings)


@pytest.mark.parametrize("options", [{"max_output_tokens": 16001}, {"timeout_seconds": 181}])
def test_generic_capacity_is_not_increased(options):
    with pytest.raises(ProviderFailure):
        ChatConfig(endpoint="https://example.invalid/chat", model="synthetic", **options)


def test_default_http_transport_sends_only_owned_extra_headers_and_no_redirects(monkeypatch):
    requests = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b'{}'
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Reply()
    def opener(handler):
        assert isinstance(handler, module._NoRedirect)
        return Opener()
    monkeypatch.setattr(module, "build_opener", opener)
    module.http_request(module.OPENCODE_GO_ENDPOINT, {"synthetic": True}, "synthetic-http-key", 600,
                        headers={"User-Agent": module.OPENCODE_GO_USER_AGENT, "x-opencode-session": "stable-synthetic-run"})
    request, timeout = requests[0]
    assert timeout == 600 and request.get_header("User-agent") == "company-bid-test/0.1"
    assert request.get_header("X-opencode-session") == "stable-synthetic-run"
    assert request.get_header("Authorization") == "Bearer synthetic-http-key"
    with pytest.raises(ProviderFailure, match="PROVIDER_HEADERS_INVALID"):
        module.http_request(module.OPENCODE_GO_ENDPOINT, {}, "synthetic-http-key", 600, headers={"Authorization": "override"})
    assert len(requests) == 1
