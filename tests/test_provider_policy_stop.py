"""Durable provider policy latches preserve known and unknown charges."""
from copy import deepcopy
from decimal import Decimal

import pytest

from company_bid.runtime import (
    Executor, NodeSpec, ProviderPolicyStopError, ProviderBudgetLedgerError,
    call_budget_summary,
)


STOP = "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"


def setup_run(tmp_path, *, known=True, mutation=None):
    attempts = []
    def handler(ctx, value):
        def execute():
            identity = f"actual-{len(attempts) + 1}"
            ctx.reserve_call(identity, 1, ".1", .9, 10, input_usd_per_million=1,
                             output_usd_per_million=1, unit_id="W1", input_hash="a" * 64)
            attempts.append(identity)
            if known:
                ctx.settle_call_usage(identity, 1, 8, 6, 14, response_sha256="b" * 64, unit_id="W1")
            args = {"code": STOP, "call_id": identity, "call_attempt": 1, "response_sha256": "b" * 64}
            if mutation:
                args.update(mutation)
            ctx.stop_provider_calls(**args)
            ctx.stop_provider_calls(**args)  # Exact duplicate is idempotent.
            raise ProviderPolicyStopError(STOP)
        return ctx.run_unit("W1", {"synthetic": True}, execute)
    nodes = [NodeSpec("N2", "synthetic-policy-v1", handler)]
    executor = Executor(tmp_path, nodes, {})
    return executor, executor.create_run({}), nodes, attempts


@pytest.mark.parametrize("known", [True, False])
def test_restart_cannot_reissue_after_billing_stop_or_release_charge(tmp_path, known):
    executor, run, nodes, sent = setup_run(tmp_path, known=known)
    first = executor.execute(run)
    assert first["status"] == "failed" and first["error_code"] == STOP
    before = deepcopy(first["call_budget"])
    summary = call_budget_summary(before)
    assert summary["budget_stopped"] and summary["stop_reason"] == "provider_policy_stop"
    assert Decimal(summary["effective_budget_charge_usd"]) == (Decimal(".000014") if known else Decimal(".1"))
    second = Executor(tmp_path, nodes, {}).resume(run)
    assert second["status"] == "failed" and second["error_code"] == STOP
    assert second["call_budget"] == before and len(sent) == 1


@pytest.mark.parametrize("after_save", [False, True])
def test_policy_write_failure_still_stops_and_is_visible_after_restart(tmp_path, monkeypatch, after_save):
    executor, run, nodes, sent = setup_run(tmp_path)
    original, failed = executor._save, []
    def fail_once(state):
        if state.get("call_budget", {}).get("stop_reason") == "provider_policy_stop" and not failed:
            failed.append(True)
            if after_save:
                original(state)
            raise OSError("synthetic policy persistence fault")
        return original(state)
    monkeypatch.setattr(executor, "_save", fail_once)
    first = executor.execute(run)
    assert first["status"] == "failed" and first["error_code"] == "PROVIDER_POLICY_PERSIST_FAILED"
    summary = call_budget_summary(first["call_budget"])
    assert summary["stop_reason"] == "provider_policy_persistence_failed"
    assert Decimal(summary["effective_budget_charge_usd"]) == Decimal(".000014")
    second = Executor(tmp_path, nodes, {}).resume(run)
    assert second["error_code"] == STOP and len(sent) == 1


@pytest.mark.parametrize("mutation", [{"code": "ARBITRARY"}, {"call_id": "unreserved"},
                                     {"call_attempt": True}, {"response_sha256": "c" * 64}])
def test_policy_stop_cannot_be_attached_to_another_call_or_response(tmp_path, mutation):
    executor, run, _, _ = setup_run(tmp_path, mutation=mutation)
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_BUDGET_LEDGER_INVALID"
    assert "provider_stop" not in state["call_budget"]


def test_tampered_latch_cannot_be_hidden_by_removing_stop_reason(tmp_path):
    executor, run, _, _ = setup_run(tmp_path)
    state = executor.execute(run)
    budget = deepcopy(state["call_budget"])
    budget.pop("stop_reason")
    assert call_budget_summary(budget)["budget_stopped"]
    budget["provider_stop"]["response_sha256"] = "c" * 64
    with pytest.raises(ProviderBudgetLedgerError):
        call_budget_summary(budget)
