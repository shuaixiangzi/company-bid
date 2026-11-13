"""Actual executor/provider paths with bounded offline transports only."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import _index, _windows, extract_requirements
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.structured import JsonTaskGateway
from test_diagnostic_replay import candidate, response, rows
from test_source_selected_citations import fixture, block


def setup(tmp_path, monkeypatch, transport, *, concurrency=2, count=6):
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest, parsed = fixture(tmp_path)
    parsed["documents"][0]["format"] = "pdf"
    for page in range(1, count + 1):
        item = block(parsed, f"B{page}", f"合成第{page}页必须保存巡检记录。", page=page, origin="native")
        item["locator"] = f"pdf/page[{page}]/line[0]"
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
               "auth_env": "TEST_PARALLEL_KEY", "max_attempts": 1, "max_output_tokens": 512,
               "max_input_chars": 100000, "max_run_calls": 30, "max_run_usd": 1,
               "input_usd_per_million": .3, "output_usd_per_million": 1.2}
    monkeypatch.setenv(options["auth_env"], "synthetic-parallel-key")
    config = {"structured_provider": options, "extraction": {"window_max_blocks": 1,
              "window_concurrency": concurrency}, "debug_capture": True}

    def extract(ctx, value):
        gateway = JsonTaskGateway(options, transport=transport)
        return {"extracted": extract_requirements(ctx, value["parsed"], value["manifest"], gateway, config["extraction"])}

    ex = Executor(tmp_path / "runtime", [NodeSpec("N1", "reader", lambda ctx, value: value),
                                        NodeSpec("N2", "parallel", extract)], config)
    initial = {"manifest": manifest, "parsed": parsed}
    run = ex.create_run(initial)
    blocks, _, docs = _index(parsed, manifest)
    expected = [w["window_id"] for w in _windows(docs, blocks, {"window_max_blocks": 1})[0]]
    return ex, run, expected


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_bounded_overlap_preserves_order_usage_trace_and_diagnostics(tmp_path, monkeypatch, concurrency):
    guard = threading.Lock()
    active = peak = 0
    barrier = threading.Barrier(concurrency)
    sent = []

    def transport(endpoint, wire, secret, timeout):
        nonlocal active, peak
        task = json.loads(wire["messages"][-1]["content"])
        with guard:
            active += 1
            peak = max(peak, active)
            sent.append(task["window"]["window_id"])
            position = len(sent)
        if position <= concurrency:
            barrier.wait(timeout=5)
        time.sleep(.01 if position % 2 == 0 else .035)
        with guard:
            active -= 1
        return response(candidate(task))

    ex, run, expected = setup(tmp_path, monkeypatch, transport, concurrency=concurrency, count=max(6, concurrency + 2))
    state = ex.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert peak == concurrency and len(sent) == len(expected)
    output = ex.output(run)["extracted"]
    assert [w["window_id"] for w in output["window_results"]] == expected
    totals = call_budget_summary(state["call_budget"])
    assert totals["known_usage_estimate_count"] == len(expected) and totals["unknown_reservation_count"] == 0
    d = inspect_diagnostics(ex.store.root, run)
    assert d["actual_call_attempts"] == len(expected) and d["gaps"] == []
    events = rows(ex.store.run_dir(run) / "events.jsonl")
    spans = rows(ex.store.run_dir(run) / "spans.jsonl")
    node_span = next(s for s in spans if s["name"] == "node.N2")
    units = {s["attributes"]["unit_id"]: s for s in spans if s["name"] == "unit.N2"}
    calls = {s["attributes"]["call_id"]: s for s in spans if s["name"] == "provider.call"}
    for e in (e for e in events if e["event"] == "call.completed"):
        assert e["unit_id"] in expected
        assert e["trace_id"] == node_span["trace_id"] == units[e["unit_id"]]["trace_id"]
        assert units[e["unit_id"]]["parent_span_id"] == node_span["span_id"]
        assert calls[e["call_id"]]["parent_span_id"] == units[e["unit_id"]]["span_id"]


@pytest.mark.parametrize("concurrency", [2, 8])
def test_failure_drains_inflight_success_and_resume_only_calls_unfinished(tmp_path, monkeypatch, concurrency):
    barrier = threading.Barrier(concurrency)
    sent = []
    failed_phase = [True]
    guard = threading.Lock()

    def transport(endpoint, wire, secret, timeout):
        task = json.loads(wire["messages"][-1]["content"])
        uid = task["window"]["window_id"]
        with guard:
            sent.append(uid)
        raw = candidate(task)
        if failed_phase[0]:
            barrier.wait(timeout=5)
            if task["window"]["blocks"][0]["block_id"] == "B1":
                raw["unresolved_block_ids"] = ["B1"]
            else:
                time.sleep(.15)  # Must settle/checkpoint before node finalizes.
        return response(raw)

    ex, run, expected = setup(tmp_path, monkeypatch, transport, concurrency=concurrency, count=concurrency + 2)
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    assert set(sent) == set(expected[:concurrency])
    good = {uid: copy.deepcopy(state["nodes"][1]["units"][uid]) for uid in expected[1:concurrency]}
    assert all(unit["status"] == "succeeded" for unit in good.values())
    assert call_budget_summary(state["call_budget"])["known_usage_estimate_count"] == concurrency
    failed_phase[0] = False
    restarted = Executor(ex.store.root, ex.nodes, ex.execution_config)
    completed = restarted.resume(run)
    assert completed["status"] == "succeeded", completed.get("error_code")
    assert all(sent.count(uid) == 1 for uid in good) and sent.count(expected[0]) == 2
    assert all(completed["nodes"][1]["units"][uid] == unit for uid, unit in good.items())
    assert inspect_diagnostics(ex.store.root, run)["gaps"] == []


@pytest.mark.parametrize("concurrency", [2, 8])
def test_concurrent_budget_admission_cannot_double_spend_or_drop_reservation(tmp_path, concurrency):
    barrier = threading.Barrier(concurrency)
    admitted = []

    def handler(ctx, value):
        def job(child, uid):
            def work():
                barrier.wait(timeout=5)
                child.reserve_call(uid, 1, .6, 1, 1, unit_id=uid, input_hash="a" * 64)
                admitted.append(uid)
                return {"uid": uid}
            return child.run_unit(uid, {}, work)
        return {"units": ctx.run_parallel_units([(uid, lambda child, uid=uid: job(child, uid))
                                                 for uid in (f"u{i}" for i in range(concurrency))], concurrency)}

    ex = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    state = ex.execute(ex.create_run({}))
    assert state["status"] == "failed" and state["error_code"] == "PROVIDER_RUN_BUDGET_EXCEEDED"
    assert len(admitted) == 1
    summary = call_budget_summary(state["call_budget"])
    assert summary["reservation_count"] == 1 and summary["effective_budget_charge_usd"] == "0.6"


def test_repair_witnesses_wait_for_prior_checkpoint_and_exclude_later_success(tmp_path):
    all_started = threading.Barrier(3)
    later_finished = threading.Event()

    def handler(ctx, value):
        def job(child, uid):
            def work():
                all_started.wait(timeout=5)
                if uid == "a":
                    assert later_finished.wait(timeout=5)
                    time.sleep(.03)
                elif uid == "b":
                    prior = child.prior_unit_record()
                    assert list(prior["units"]) == ["a"]
                    assert prior["units"]["a"]["status"] == "succeeded"
                    return {"prior": list(prior["units"])}
                else:
                    later_finished.set()
                return {"uid": uid}
            return child.run_unit(uid, {}, work)
        return {"units": ctx.run_parallel_units([(uid, lambda child, uid=uid: job(child, uid))
                                                 for uid in ("a", "b", "c")], 3)}

    ex = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    state = ex.execute(ex.create_run({}))
    assert state["status"] == "succeeded", state.get("error_code")


def test_process_model_ceiling_is_shared_across_two_runs(tmp_path, monkeypatch):
    barrier = threading.Barrier(8)
    guard = threading.Lock()
    count = active = peak = 0

    def transport(endpoint, wire, secret, timeout):
        nonlocal count, active, peak
        with guard:
            count += 1
            position = count
            active += 1
            peak = max(peak, active)
        if position <= 8:
            barrier.wait(timeout=5)
        time.sleep(.02)
        with guard:
            active -= 1
        return response(candidate(json.loads(wire["messages"][-1]["content"])))

    runs = [setup(tmp_path / str(i), monkeypatch, transport, concurrency=8, count=8) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda item: item[0].execute(item[1]), runs))
    assert all(s["status"] == "succeeded" for s in states)
    assert count == 16 and peak == 8


@pytest.mark.parametrize("limit", [0, 9, True, 2.0, "2"])
def test_invalid_concurrency_stops_before_provider(tmp_path, monkeypatch, limit):
    def never(*args):
        pytest.fail("Invalid concurrency must not reach a provider")
    ex, run, _ = setup(tmp_path, monkeypatch, never, concurrency=limit)
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "WINDOW_CONCURRENCY_INVALID"
    assert not state.get("call_budget")


@pytest.mark.parametrize("prior_fails", [False, True])
def test_actual_repair_waits_for_source_witness_without_buying_extra_attempts(tmp_path, monkeypatch, prior_fails):
    from test_prior_span_repair import source_case, CONFIG
    from company_bid.extraction import run_extraction_window
    prior, current, prior_raw, first, second = source_case(tmp_path)
    if prior_fails:
        prior_raw["unresolved_block_ids"] = ["CONDITION"]
    barrier = threading.Barrier(2)
    sent = []
    guard = threading.Lock()
    monkeypatch.setenv("TEST_PARALLEL_REPAIR_KEY", "synthetic-only")
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
        "auth_env": "TEST_PARALLEL_REPAIR_KEY", "max_attempts": 1, "validation_repair_attempts": 1,
        "max_output_tokens": 1000, "max_input_chars": 65000, "max_run_calls": 3, "max_run_usd": 1,
        "input_usd_per_million": .3, "output_usd_per_million": 1.2}

    def transport(endpoint, wire, secret, timeout):
        task = json.loads(wire["messages"][-1]["content"])
        with guard:
            sent.append(task)
        if "validation_feedback" in task:
            assert not prior_fails
            witness = task["validation_feedback"]["required_coverage"]["resolution_groups"][0]["source_witnesses"][0]
            assert witness["unit_id"] == prior["window_id"]
            return response(second)
        barrier.wait(timeout=5)
        if task["window"]["window_id"] == prior["window_id"]:
            time.sleep(.06)
            return response(prior_raw)
        return response(first)

    def handler(ctx, value):
        gateway = JsonTaskGateway(options, transport=transport)
        return {"windows": ctx.run_parallel_units([(w["window_id"],
            lambda child, w=w: run_extraction_window(child, w, CONFIG, gateway)) for w in (prior, current)], 2)}

    ex = Executor(tmp_path / "runtime", [NodeSpec("N2", "parallel-repair", handler)],
                  {"structured_provider": options, "debug_capture": True})
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == ("failed" if prior_fails else "succeeded"), state.get("error_code")
    assert len(sent) == (2 if prior_fails else 3)
    assert call_budget_summary(state["call_budget"])["known_usage_estimate_count"] == len(sent)
    assert inspect_diagnostics(ex.store.root, run)["gaps"] == []


def test_parallel_historical_revalidation_has_no_live_fallback(tmp_path, monkeypatch):
    from test_diagnostic_replay import make_source_run, replay_config, target_run
    source = make_source_run(tmp_path, monkeypatch, two_windows=True)
    spec = replay_config(source)
    bind = next(iter(spec["bindings"].values()))
    spec["bindings"] = {"extract_requirements:" + uid: {**bind, "unit_id": uid} for uid in source["unit_ids"]}
    source["extraction"]["window_concurrency"] = 2
    ex, rid, state, http, gateways = target_run(source, spec=spec)
    assert state["status"] == "succeeded", state.get("error_code")
    assert not http and not state.get("call_budget")
    summary = gateways[0].summary()
    assert summary["revalidated_count"] == summary["revalidated_this_attempt_count"] == 2
    assert summary["live_miss_count"] == 0
    assert inspect_diagnostics(ex.store.root, rid)["gaps"] == []


def test_scheduler_interruption_stops_new_requests_but_settles_inflight(tmp_path, monkeypatch):
    from company_bid import unit_concurrency
    from company_bid.provider import ChatConfig, ChatProposalProvider
    both_started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = []
    monkeypatch.setenv("TEST_INTERRUPTION_KEY", "synthetic-only")
    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="synthetic",
        auth_env="TEST_INTERRUPTION_KEY", max_attempts=1, max_output_tokens=512, max_run_calls=8,
        max_run_usd=1, input_usd_per_million=.3, output_usd_per_million=1.2))

    def transport(endpoint, wire, secret, timeout):
        task = json.loads(wire["messages"][-1]["content"])
        with guard:
            calls.append(task)
            if len(calls) == 2:
                both_started.set()
        assert task["phase"] == 1
        assert release.wait(timeout=5)
        return response({"value": "synthetic result"})

    provider.transport = transport
    original_stop = unit_concurrency.UnitCoordinator.stop

    def stopped(self, error):
        original_stop(self, error)
        release.set()

    def interrupt(*args, **kwargs):
        assert both_started.wait(timeout=5)
        raise RuntimeError("synthetic coordinator interruption")

    monkeypatch.setattr(unit_concurrency.UnitCoordinator, "stop", stopped)
    monkeypatch.setattr(unit_concurrency, "wait", interrupt)

    def handler(ctx, value):
        def job(child, uid):
            def work():
                for phase in (1, 2):
                    provider.generate_json(child, {"phase": phase}, unit_id=uid, prompt_version="synthetic",
                                           system_prompt="synthetic", task_type="extract_requirements", validate=lambda v: v)
                return {}
            return child.run_unit(uid, {}, work)
        return {"units": ctx.run_parallel_units([(uid, lambda child, uid=uid: job(child, uid))
                                                 for uid in ("a", "b")], 2)}

    ex = Executor(tmp_path, [NodeSpec("N2", "1", handler)], {})
    run = ex.create_run({})
    state = ex.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "NODE_EXECUTION_FAILED"
    assert len(calls) == 2
    assert call_budget_summary(state["call_budget"])["known_usage_estimate_count"] == 2
    assert inspect_diagnostics(ex.store.root, run)["gaps"] == []


def test_parallel_process_exit_keeps_checkpoint_and_unknown_cost(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = r'''
import os,sys
from pathlib import Path
from company_bid.runtime import Executor,NodeSpec
root=Path(sys.argv[1])
def handler(ctx,value):
 def job(child,uid):
  def work():
   if uid=='b':
    child.prior_unit_record()
    child.reserve_call('unknown-at-crash',1,.2,1,5,unit_id=uid,input_hash='a'*64)
    os._exit(23)
   return {'value':uid}
  return child.run_unit(uid,{},work)
 return {'units':ctx.run_parallel_units([(uid,lambda child,uid=uid:job(child,uid)) for uid in ('a','b')],2)}
ex=Executor(root,[NodeSpec('N2','1',handler)],{})
run=ex.create_run({});(root/'id.txt').write_text(run);ex.execute(run)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env, capture_output=True, timeout=15)
    assert process.returncode == 23, process.stderr.decode()
    run = (tmp_path / "id.txt").read_text()
    calls = []

    def resumed(ctx, value):
        def job(child, uid):
            return child.run_unit(uid, {}, lambda: calls.append(uid) or {"value": uid})
        return {"units": ctx.run_parallel_units([(uid, lambda child, uid=uid: job(child, uid))
                                                 for uid in ("a", "b")], 2)}

    ex = Executor(tmp_path, [NodeSpec("N2", "1", resumed)], {})
    before = ex.inspect(run)["nodes"][0]["units"]["a"]["output_ref"]
    state = ex.resume(run)
    assert state["status"] == "succeeded" and calls == ["b"]
    assert state["nodes"][0]["units"]["a"]["output_ref"] == before
    assert state["observability"]["trace_status"] == "partial"
    summary = call_budget_summary(state["call_budget"])
    assert summary["unknown_reserved_upper_usd"] == "0.2" and summary["unknown_reservation_count"] == 1
