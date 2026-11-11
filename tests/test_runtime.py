from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from company_bid.runtime import CheckpointInvalidError, ConfigurationChangedError, Executor, NodeSpec, RuntimeContractError
from company_bid.storage import RunLockedError, process_lock


def test_partial_unit_retry_only_calls_failed_and_unfinished(tmp_path):
    counts = {"a": 0, "b": 0, "c": 0}

    def handler(ctx, data):
        outputs = {}
        for name in counts:
            def work(name=name):
                counts[name] += 1
                if name == "b" and counts[name] == 1:
                    raise TimeoutError("source content must not leak")
                return {"value": name}
            outputs[name] = ctx.run_unit(name, {"name": name}, work)
        return outputs

    executor = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    run = executor.create_run({})
    failed = executor.execute(run)
    good_ref = failed["nodes"][0]["units"]["a"]["output_ref"]
    assert failed["status"] == "failed"
    completed = executor.resume(run)
    assert completed["status"] == "succeeded"
    assert counts == {"a": 1, "b": 2, "c": 1}
    assert completed["nodes"][0]["units"]["a"]["output_ref"] == good_ref
    assert len(completed["nodes"][0]["attempts"]) == 2


def test_pause_restart_resume_and_last_step_succeeds(tmp_path):
    calls = []
    nodes = [NodeSpec(key, "1", lambda ctx, data: calls.append(ctx.node_key) or {"value": data["value"] + 1}) for key in ["N0", "N1", "N2"]]
    first = Executor(tmp_path, nodes, {})
    run = first.create_run({"value": 0})
    assert first.execute(run, stop_after="N1")["status"] == "paused_debug"
    restarted = Executor(tmp_path, nodes, {})
    assert restarted.resume(run, stop_after="N2")["status"] == "succeeded"
    assert restarted.output(run) == {"value": 3}
    restarted.resume(run)
    assert calls == ["N0", "N1", "N2"]


def test_actual_process_exit_keeps_unit_checkpoint_and_marks_interrupted(tmp_path):
    script = r'''
import os, sys
from pathlib import Path
from company_bid.runtime import Executor, NodeSpec
root = Path(sys.argv[1])
def handler(ctx, data):
    ctx.run_unit("one", {}, lambda: {"one": 1})
    ctx.run_unit("two", {}, lambda: os._exit(23))
    return {}
ex = Executor(root, [NodeSpec("N2", "1", handler)], {})
run = ex.create_run({})
(root / "id.txt").write_text(run)
ex.execute(run)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env, capture_output=True)
    assert process.returncode == 23, process.stderr.decode()
    run = (tmp_path / "id.txt").read_text()
    called = []

    def resumed(ctx, data):
        one = ctx.run_unit("one", {}, lambda: called.append("one") or {"one": 1})
        two = ctx.run_unit("two", {}, lambda: called.append("two") or {"two": 2})
        return {**one, **two}

    executor = Executor(tmp_path, [NodeSpec("N2", "1", resumed)], {})
    assert executor.inspect(run)["observability"]["trace_status"] == "partial"
    state = executor.resume(run)
    assert state["status"] == "succeeded"
    assert state["observability"]["trace_status"] == "partial"
    assert state["observability"]["current_trace_status"] == "captured"
    assert called == ["two"]
    assert state["nodes"][0]["attempts"][0]["status"] == "interrupted"
    spans = [json.loads(line) for line in (tmp_path / "runs" / run / "spans.jsonl").read_text().splitlines()]
    assert any(span["links"] for span in spans if span["name"] == "job.execute")


def test_fork_reuses_exact_prefix_and_recomputes_downstream(tmp_path):
    calls = []
    nodes = [NodeSpec(key, "1", lambda ctx, data: calls.append(ctx.node_key) or {"value": data["value"] + 1}) for key in ["N0", "N1", "N2"]]
    executor = Executor(tmp_path, nodes, {})
    old = executor.create_run({"value": 0})
    original = executor.execute(old)
    parent_state = (tmp_path / "runs" / old / "state.json").read_bytes()
    child = executor.fork(old, "N1")
    finished = executor.execute(child)
    assert calls == ["N0", "N1", "N2", "N1", "N2"]
    assert finished["nodes"][0]["output_ref"] == original["nodes"][0]["output_ref"]
    assert finished["nodes"][1]["output_ref"] != original["nodes"][1]["output_ref"]
    assert (tmp_path / "runs" / old / "state.json").read_bytes() == parent_state
    assert executor.diff(old, child)["nodes"][0]["reused"]


def test_configuration_change_requires_child_with_valid_dependency_prefix(tmp_path):
    def handler(ctx, data):
        return data
    nodes = [NodeSpec("N0", "1", handler, ()), NodeSpec("N1", "1", handler, ("prompt_version",))]
    old_executor = Executor(tmp_path, nodes, {"prompt_version": "v1"})
    run = old_executor.create_run({"value": 0})
    old_executor.execute(run)
    new_executor = Executor(tmp_path, nodes, {"prompt_version": "v2"})
    with pytest.raises(ConfigurationChangedError):
        new_executor.resume(run)
    child = new_executor.fork(run, "N1")
    assert new_executor.execute(child)["status"] == "succeeded"
    assert new_executor.diff(run, child)["configuration_changes"] == ["prompt_version"]


def test_changed_global_configuration_rejects_unsafe_prefix(tmp_path):
    nodes = [NodeSpec("N0", "1", lambda ctx, data: data), NodeSpec("N1", "1", lambda ctx, data: data)]
    executor = Executor(tmp_path, nodes, {"model": "v1"})
    run = executor.create_run({})
    executor.execute(run)
    with pytest.raises(ConfigurationChangedError):
        Executor(tmp_path, nodes, {"model": "v2"}).fork(run, "N1")


def test_corrupt_checkpoint_stops_parent_and_repair_child_preserves_prefix(tmp_path):
    nodes = [NodeSpec(key, "1", lambda ctx, data: {"v": data["v"] + 1}) for key in ["N0", "N1", "N2"]]
    executor = Executor(tmp_path, nodes, {})
    parent = executor.create_run({"v": 0})
    state = executor.execute(parent)
    damaged = executor.store.resolve(state["nodes"][1]["output_ref"]["path"])
    damaged.write_bytes(b"bad")
    with pytest.raises(CheckpointInvalidError):
        executor.resume(parent)
    failed = executor.inspect(parent)
    assert failed["checkpoint_invalid_at"] == "N1"
    child = executor.repair(parent)
    assert executor.execute(child)["status"] == "succeeded"
    assert executor.inspect(child)["nodes"][0]["output_ref"] == state["nodes"][0]["output_ref"]
    assert damaged.read_bytes() == b"bad"


def test_binary_artifact_corruption_invalidates_checkpoint(tmp_path):
    def handler(ctx, data):
        return {"document": ctx.store_bytes("result.docx", b"bytes")}
    executor = Executor(tmp_path, [NodeSpec("N6", "1", handler)], {})
    run = executor.create_run({})
    executor.execute(run)
    reference = executor.output(run)["document"]
    executor.store.resolve(reference["path"]).write_bytes(b"changed")
    with pytest.raises(CheckpointInvalidError):
        executor.output(run)


def test_os_lock_prevents_duplicate_local_claim(tmp_path):
    executor = Executor(tmp_path, [NodeSpec("N0", "1", lambda ctx, data: data)], {})
    run = executor.create_run({})
    with process_lock(executor.store.run_dir(run) / ".lock"):
        with pytest.raises(RunLockedError):
            executor.execute(run)


def test_changed_initial_input_must_fork_from_start(tmp_path):
    nodes = [NodeSpec(key, "1", lambda ctx, data: data) for key in ["N0", "N1"]]
    executor = Executor(tmp_path, nodes, {})
    run = executor.create_run({"a": 1})
    executor.execute(run)
    with pytest.raises(ConfigurationChangedError):
        executor.fork(run, "N1", initial_input={"a": 2})
    child = executor.fork(run, "N0", initial_input={"a": 2})
    executor.execute(child)
    assert executor.output(child) == {"a": 2}


def test_successful_node_step_requires_child_for_rerun(tmp_path):
    executor = Executor(tmp_path, [NodeSpec(key, "1", lambda ctx, data: data) for key in ["N0", "N1"]], {})
    run = executor.create_run({})
    executor.execute(run, stop_after="N0")
    with pytest.raises(RuntimeContractError):
        executor.execute(run, stop_after="N0")


def test_call_budget_reservations_survive_resume_and_block_excess_calls(tmp_path):
    attempts = []

    def handler(ctx, data):
        identity = "call-" + str(len(attempts) + 1)
        ctx.reserve_call(identity, 1, 0.009, 0.02, 2)
        attempts.append(identity)
        raise TimeoutError("unknown provider result")

    nodes = [NodeSpec("N4", "1", handler)]
    executor = Executor(tmp_path, nodes, {})
    run = executor.create_run({})
    assert executor.execute(run)["call_budget"]["reservation_count"] == 1
    restarted = Executor(tmp_path, nodes, {})
    assert restarted.resume(run)["call_budget"]["reservation_count"] == 2
    state = restarted.resume(run)
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED"
    assert state["call_budget"]["reserved_upper_usd"] == "0.018"
    assert attempts == ["call-1", "call-2"]


def test_call_budget_money_limit_is_checked_before_request(tmp_path):
    calls = []

    def handler(ctx, data):
        ctx.reserve_call("call-1", 1, 0.021, 0.02, 2)
        calls.append("would call provider")
        return {}

    executor = Executor(tmp_path, [NodeSpec("N4", "1", handler)], {})
    state = executor.execute(executor.create_run({}))
    assert state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED"
    assert not calls and state["call_budget"]["reservation_count"] == 0


def test_successful_unit_remains_reusable_after_checkpoint_event_failure(tmp_path, monkeypatch):
    from company_bid.observability import Observer, ObservabilityWriteError

    original = Observer.event
    failed = []
    calls = []

    def event(observer, name, metadata=None):
        if name == "unit.checkpointed" and not failed:
            failed.append(True)
            raise ObservabilityWriteError("failure after checkpoint")
        return original(observer, name, metadata)

    monkeypatch.setattr(Observer, "event", event)
    def handler(ctx, data):
        return ctx.run_unit("chapter", {}, lambda: calls.append(True) or {"value": 1})

    executor = Executor(tmp_path, [NodeSpec("N4", "1", handler)], {})
    run = executor.create_run({})
    first = executor.execute(run)
    assert first["status"] == "failed"
    assert first["nodes"][0]["units"]["chapter"]["status"] == "succeeded"
    assert executor.resume(run)["status"] == "succeeded"
    assert len(calls) == 1


def test_final_commit_reconciles_without_rerender_after_event_failure(tmp_path, monkeypatch):
    from company_bid.observability import Observer, ObservabilityWriteError

    original = Observer.event
    calls = []
    def event(observer, name, metadata=None):
        if name == "node.checkpointed":
            raise ObservabilityWriteError("failure after final commit")
        return original(observer, name, metadata)

    monkeypatch.setattr(Observer, "event", event)
    executor = Executor(tmp_path, [NodeSpec("N6", "1", lambda ctx, data: calls.append(True) or {})], {})
    run = executor.create_run({})
    first = executor.execute(run)
    assert first["status"] == "failed" and first["cursor"] == 1
    second = executor.resume(run)
    assert second["status"] == "succeeded"
    assert second["observability"]["event_status"] == "write_failed"
    assert len(calls) == 1
