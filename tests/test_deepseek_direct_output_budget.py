"""Direct DeepSeek capacity is explicitly bounded; all HTTP is simulated."""
from dataclasses import asdict
from decimal import Decimal
import json

import pytest

import company_bid.provider as module
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec, call_budget_summary


DIRECT_ENDPOINT = "https://api.deepseek.com/chat/completions"
DIRECT_MODEL = "deepseek-flash"


@pytest.mark.parametrize("tokens", [16000, 24000, 32000])
def test_exact_direct_flash_configuration_accepts_bounded_output(tokens):
    config = ChatConfig(endpoint=DIRECT_ENDPOINT, model=DIRECT_MODEL, max_output_tokens=tokens)
    assert config.max_output_tokens == tokens and config.service == "generic"


@pytest.mark.parametrize("tokens", [32001, 65536, True, 24000.0])
def test_direct_flash_capacity_does_not_remove_output_validation(tokens):
    with pytest.raises(ProviderFailure, match="PROVIDER_BUDGET_INVALID"):
        ChatConfig(endpoint=DIRECT_ENDPOINT, model=DIRECT_MODEL, max_output_tokens=tokens)


@pytest.mark.parametrize("endpoint,model", [
    ("https://provider.invalid/chat/completions", DIRECT_MODEL),
    ("https://api.deepseek.com.evil.invalid/chat/completions", DIRECT_MODEL),
    ("https://sub.api.deepseek.com/chat/completions", DIRECT_MODEL),
    ("https://api.deepseek.com:443/chat/completions", DIRECT_MODEL),
    ("https://API.deepseek.com/chat/completions", DIRECT_MODEL),
    ("https://api.deepseek.com/v1/chat/completions", DIRECT_MODEL),
    ("https://api.deepseek.com/chat/completions/", DIRECT_MODEL),
    ("https://api.deepseek.com/chat/completions/extra", DIRECT_MODEL),
    (DIRECT_ENDPOINT, "deepseek-chat"),
    (DIRECT_ENDPOINT, "deepseek-flash-other"),
    (DIRECT_ENDPOINT, "DeepSeek-Flash"),
])
def test_similar_endpoints_and_other_models_keep_generic_limit(endpoint, model):
    assert ChatConfig(endpoint=endpoint, model=model, max_output_tokens=16000).max_output_tokens == 16000
    with pytest.raises(ProviderFailure, match="PROVIDER_BUDGET_INVALID"):
        ChatConfig(endpoint=endpoint, model=model, max_output_tokens=16001)


def test_other_capacity_defaults_and_historical_go_limit_are_unchanged():
    direct = ChatConfig(endpoint=DIRECT_ENDPOINT, model=DIRECT_MODEL)
    assert direct.max_output_tokens == 2500
    assert direct.timeout_seconds == 90 and direct.max_attempts == 2
    assert direct.input_usd_per_million == .30 and direct.output_usd_per_million == 1.20
    with pytest.raises(ProviderFailure, match="PROVIDER_CONFIG_INVALID"):
        ChatConfig(endpoint=DIRECT_ENDPOINT, model=DIRECT_MODEL, max_output_tokens=32000, timeout_seconds=181)
    go = ChatConfig(endpoint=module.OPENCODE_GO_ENDPOINT, model=module.OPENCODE_GO_MODEL,
                    service="opencode_go", max_output_tokens=65536, timeout_seconds=600)
    assert go.max_output_tokens == 65536 and go.timeout_seconds == 600


@pytest.mark.parametrize("output_tokens", [16000, 24000])
def test_direct_output_limit_reaches_http_wire_and_full_cost_reservation(tmp_path, monkeypatch, output_tokens):
    monkeypatch.setenv("TEST_DIRECT_KEY", "synthetic-direct-credential")
    sent = []

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit):
            # Unknown actual usage must retain the entire original reservation.
            return b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"ok\\":true}"}}]}'

    class Opener:
        def open(self, request, timeout):
            sent.append((request, timeout))
            return Reply()

    def build_opener(handler):
        assert isinstance(handler, module._NoRedirect)
        return Opener()

    monkeypatch.setattr(module, "build_opener", build_opener)
    config = ChatConfig(endpoint=DIRECT_ENDPOINT, model=DIRECT_MODEL, auth_env="TEST_DIRECT_KEY",
                        max_output_tokens=output_tokens, max_attempts=1, max_run_usd=3,
                        max_run_calls=1, thinking_mode="enabled", reasoning_effort="low")
    provider = ChatProposalProvider(config, sleep=lambda _: pytest.fail("No retry expected"))

    def handler(ctx, value):
        return provider.generate_json(ctx, value, unit_id="W1", prompt_version="direct-budget-v1",
                                      system_prompt="Return synthetic JSON.", task_type="extract", validate=lambda x: x)

    executor = Executor(tmp_path, [NodeSpec("N2", "direct-budget-v1", handler)], {"provider": asdict(config)})
    run_id = executor.create_run({"synthetic": "test input"})
    state = executor.execute(run_id)
    assert state["status"] == "succeeded" and len(sent) == 1
    request, timeout = sent[0]
    payload = json.loads(request.data)
    assert request.full_url == DIRECT_ENDPOINT and request.get_method() == "POST"
    assert timeout == 90 and request.get_header("Authorization") == "Bearer synthetic-direct-credential"
    assert request.get_header("X-opencode-session") is None
    assert payload["model"] == DIRECT_MODEL and payload["max_tokens"] == output_tokens
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "low"
    request_token_bound = len(request.data) + 2048
    expected = (Decimal(request_token_bound) * Decimal("0.30") +
                Decimal(output_tokens) * Decimal("1.20")) / Decimal(1_000_000)
    budget = state["call_budget"]
    assert budget["reservation_count"] == 1
    assert Decimal(budget["reservations"][0]["estimated_upper_usd"]) == expected
    assert Decimal(budget["reserved_upper_usd"]) == expected
    summary = call_budget_summary(budget)
    assert summary["unknown_reservation_count"] == 1
    assert Decimal(summary["effective_budget_charge_usd"]) == expected
