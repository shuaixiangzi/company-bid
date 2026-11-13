"""Real executor/correction lineage across fresh N0 paths; zero network."""
from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.extraction import extract_requirements
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest
from company_bid.structured import JsonTaskGateway
from test_citation_gap_repair import arrange
from test_diagnostic_replay import response, rows, snapshot
from test_targeted_validation_repair import assert_two_distinct_settled_calls


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Citation-gap replay tests must never use a network connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def source_run(tmp_path, monkeypatch):
    window, _, source, first, second, _ = arrange()
    original = tmp_path / "synthetic-contract.txt"
    raw = "\n".join(block["text"] for unit in source["parsed"]["documents"][0]["units"]
                    for block in unit["blocks"]).encode("utf-8")
    original.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    assert sha == source["manifest"]["sources"][0]["sha256"]
    source["manifest"]["sources"][0].update(path=str(original), title="Synthetic contract", kind="tender")
    source["manifest"]["project"] = {"project_id": "P", "name": "Synthetic project"}
    source["manifest"]["company"] = {"company_id": "C", "name": "Synthetic company"}
    source.update(source_artifacts=[], input_kind="automatic")
    options = {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "synthetic",
        "auth_env": "TEST_CITATION_GAP_REPLAY_KEY", "max_attempts": 1, "validation_repair_attempts": 1,
        "max_output_tokens": 512, "max_input_chars": 100000, "max_run_calls": 3, "max_run_usd": 1,
        "input_usd_per_million": .30, "output_usd_per_million": 1.20}
    monkeypatch.setenv(options["auth_env"], "synthetic-test-credential")
    sent = []

    def transport(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        assert len(sent) <= 2
        task = json.loads(wire["messages"][-1]["content"])
        assert task["window"]["window_id"] == window["window_id"]
        assert ("validation_feedback" in task) == (len(sent) == 2)
        return response(copy.deepcopy(first if len(sent) == 1 else second))

    executor = make_executor(tmp_path / "history", options, transport)
    run = executor.create_run(source)
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    assert len(sent) == 2
    node = state["nodes"][2]
    relations = [executor.store.read_json(capture["ref"]) for attempt in node["attempts"]
                 for capture in attempt["captures"] if capture["name"].startswith("validation-repair-result-")]
    relation, = relations
    assert relation["schema_version"] == "validation-repair-v11" and relation["status"] == "completed"
    assert_two_distinct_settled_calls(state, relation)
    value = executor.output(run, "N1")
    gap = relation["required_coverage"]["citation_gap_restore"]
    assert gap["source_proof"]["manifest_sha256"] == digest(value["manifest"])
    assert gap["source_proof_sha256"] == digest(gap["source_proof"])
    assert gap["original_block_ids"] == ["G1", "G3"] and gap["restored_block_ids"] == ["G1", "G2", "G3"]
    report = inspect_diagnostics(executor.store.root, run)
    assert report["actual_call_attempts"] == 2 and report["gaps"] == []
    return {"executor": executor, "run": run, "initial": source, "value": value,
            "window_id": window["window_id"], "options": options, "sent": sent, "relation": relation}


def make_executor(root, options, transport, *, spec=None, read_change=None, before_preflight=None):
    config = {"structured_provider": options, "debug_capture": True,
              "extraction": {"window_max_chars": 14000, "window_max_blocks": 40}}
    if spec is not None:
        config.update(run_purpose="diagnostic_replay", diagnostic_replay=spec)

    def freeze(ctx, value):
        value = copy.deepcopy(value)
        source = value["manifest"]["sources"][0]
        raw = Path(source["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        reference = ctx.store_bytes("synthetic-contract.txt", raw)
        source["path"] = str(ctx.executor.store.root / reference["path"])
        value["source_artifacts"] = [reference]
        return value

    def read(ctx, value):
        value = copy.deepcopy(value)
        if read_change:
            read_change(value)
        return value

    def extract(ctx, value):
        gateway = JsonTaskGateway(options, transport=transport, diagnostic_replay=spec)
        if spec is not None:
            if before_preflight:
                before_preflight(value)
            gateway.preflight(ctx, value)
        result = {**value, "extracted": extract_requirements(
            ctx, value["parsed"], value["manifest"], gateway, config["extraction"])}
        if spec is not None:
            result["diagnostic_replay"] = gateway.summary()
        return result

    return Executor(root, [NodeSpec("N0", "synthetic-freeze", freeze, ()),
        NodeSpec("N1", "synthetic-native-read", read, ()),
        NodeSpec("N2", "citation-gap-executor-validator", extract,
                 ("structured_provider", "extraction", "run_purpose", "diagnostic_replay"))], config)


def replay(source, tmp_path, monkeypatch, *, initial_change=None, read_change=None, before_preflight=None):
    spec = {"schema_version": "diagnostic-replay-v1", "sources": {"history": str(source["executor"].store.root)},
        "bindings": {"extract_requirements:" + source["window_id"]: {"alias": "history", "run_id": source["run"],
            "node_key": "N2", "unit_id": source["window_id"]}}}
    options = {**source["options"], "mode": "diagnostic_mixed"}
    sent = []

    def forbidden(endpoint, wire, secret, timeout):
        sent.append(copy.deepcopy(wire))
        pytest.fail("A declared citation-gap correction must not fall back to HTTP")

    monkeypatch.delenv(options["auth_env"])
    initial = copy.deepcopy(source["initial"])
    if initial_change:
        initial_change(initial)
    target = make_executor(tmp_path / "target", options, forbidden, spec=spec,
                           read_change=read_change, before_preflight=before_preflight)
    run = target.create_run(initial)
    state = target.execute(run)
    assert sent == [] and "call_budget" not in state
    assert inspect_diagnostics(target.store.root, run)["actual_call_attempts"] == 0
    return target, run, state


def test_fresh_n0_path_revalidates_the_exact_correction_without_http_or_history_mutation(tmp_path, monkeypatch):
    source = source_run(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)
    historical = copy.deepcopy(source["relation"])
    target, run, state = replay(source, tmp_path, monkeypatch)
    assert state["status"] == "succeeded", state.get("error_code")
    new_value = target.output(run, "N1")
    old_path = Path(source["value"]["manifest"]["sources"][0]["path"])
    new_path = Path(new_value["manifest"]["sources"][0]["path"])
    assert old_path != new_path and old_path.read_bytes() == new_path.read_bytes()
    assert digest(new_value["manifest"]) != digest(source["value"]["manifest"])
    output = target.output(run)
    expected = source["executor"].output(source["run"])["extracted"]
    assert output["extracted"] == expected
    assert "未经双方书面同意不得实施变更" in output["extracted"]["requirements"][6]["text"]
    summary = output["diagnostic_replay"]
    assert summary["diagnostic_only"] is True and summary["live_miss_count"] == 0
    assert summary["verified_binding_count"] == summary["revalidated_count"] == 1
    assert summary["historical_calls_charged_again"] == 0
    assert len(summary["origins"][0]["calls"]) == 2
    events = rows(target.store.run_dir(run) / "events.jsonl")
    assert not any(event["event"].startswith("call.") for event in events)
    spans = rows(target.store.run_dir(run) / "spans.jsonl")
    revalidation, = [span for span in spans if span["name"] == "candidate.revalidation"]
    assert len(revalidation["links"]) == 2
    assert inspect_diagnostics(target.store.root, run)["gaps"] == []
    assert source["relation"] == historical
    assert snapshot(source["executor"].store.root) == before


@pytest.mark.parametrize("field", ["source_title", "project_name"])
def test_non_path_manifest_metadata_drift_still_stops_before_any_http(tmp_path, monkeypatch, field):
    source = source_run(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)

    def change(value):
        if field == "source_title":
            value["manifest"]["sources"][0]["title"] = "Different source metadata"
        else:
            value["manifest"]["project"]["name"] = "Different project metadata"

    _, _, state = replay(source, tmp_path, monkeypatch, initial_change=change)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_REPLAY_SCOPE_MISMATCH"
    assert snapshot(source["executor"].store.root) == before


def test_corrupt_new_path_bytes_stop_before_any_binding_can_release_http(tmp_path, monkeypatch):
    source = source_run(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)

    def corrupt(value):
        current = Path(value["manifest"]["sources"][0]["path"])
        assert current != Path(source["value"]["manifest"]["sources"][0]["path"])
        current.write_bytes(b"synthetic corrupt new frozen source")

    _, _, state = replay(source, tmp_path, monkeypatch, before_preflight=corrupt)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_REPLAY_SOURCE_INVALID"
    assert snapshot(source["executor"].store.root) == before


@pytest.mark.parametrize("field,value", [("image_count", 1), ("unmapped_glyphs", ["unread glyph"]),
                                         ("error_code", "SYNTHETIC_READ_FAILURE")])
def test_current_reading_fault_is_not_hidden_by_manifest_path_normalization(tmp_path, monkeypatch, field, value):
    source = source_run(tmp_path, monkeypatch)
    before = snapshot(source["executor"].store.root)

    def change(data):
        data["parsed"]["documents"][0]["units"][0]["coverage"][field] = value

    _, _, state = replay(source, tmp_path, monkeypatch, read_change=change)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_REPLAY_SOURCE_CONTEXT_MISMATCH"
    assert snapshot(source["executor"].store.root) == before
