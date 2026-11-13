"""Diagnostic replay is a sticky artifact scope, never ordinary publication."""
import copy
import json
from pathlib import Path

import pytest
from docx import Document

from company_bid.cli import main
from company_bid.observability import Observer, ObservabilityWriteError
from company_bid.pipeline import build_executor
from company_bid.provider import ChatProposalProvider
from company_bid.rendering import DIAGNOSTIC_MARKER
from company_bid.runtime import Executor, NodeContext, NodeSpec
from test_prescribed_cover_rendering import COVER, prepared_data


def events(executor, run):
    return [json.loads(line) for line in (executor.store.run_dir(run) / "events.jsonl").read_text("utf-8").splitlines()]


def complete(tmp_path, monkeypatch, *, diagnostic=True, prescribed=True):
    data = prepared_data(tmp_path)
    if not prescribed:
        data.pop("prescribed_cover_section_id")
    def forbidden(*args, **kwargs):
        pytest.fail("Publication isolation must not call a model")
    monkeypatch.setattr(ChatProposalProvider, "generate", forbidden)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", forbidden)
    config = {"provider": {"mode": "prepared"}, "debug_capture": True}
    if diagnostic:
        config["run_purpose"] = "diagnostic_replay"
    executor = build_executor(tmp_path / "runtime", config)
    run = executor.create_run({"prepared": data, "proposals": {}})
    state = executor.execute(run, stop_after="N6")
    assert state["status"] == "succeeded", state.get("error_code")
    return executor, run, executor.output(run)


@pytest.mark.parametrize("prescribed", [False, True])
def test_diagnostic_files_mark_actual_first_page_and_headers_without_ordinary_files(tmp_path, monkeypatch, prescribed):
    executor, run, output = complete(tmp_path, monkeypatch, prescribed=prescribed)
    assert executor.inspect(run)["artifact_scope"] == "diagnostic"
    assert output["publication_status"] == "diagnostic_only" and "files" not in output
    assert set(output["diagnostic_files"]) == {"bid_docx", "issues_docx"}
    for key, ref in output["diagnostic_files"].items():
        assert "diagnostic-" in Path(ref["path"]).name
        document = Document(executor.store.root / ref["path"])
        assert document.paragraphs[0].text == DIAGNOSTIC_MARKER
        assert document.core_properties.title.startswith("【诊断回放】")
        for section in document.sections:
            assert [p.text for p in section.header.paragraphs].count(DIAGNOSTIC_MARKER) == 1
        if key == "bid_docx" and prescribed:
            assert [p.text for p in document.paragraphs][1:6] == COVER
    names = [event["event"] for event in events(executor, run)]
    assert names.count("artifact.diagnostic_created") == 1 and "artifact.published" not in names


def test_normal_publication_and_synthetic_fixture_are_unchanged(tmp_path, monkeypatch):
    executor, run, output = complete(tmp_path, monkeypatch, diagnostic=False)
    assert "artifact_scope" not in executor.inspect(run)
    assert "files" in output and "diagnostic_files" not in output and "publication_status" not in output
    assert "artifact.published" in [e["event"] for e in events(executor, run)]
    bid = Document(executor.store.root / output["files"]["bid_docx"]["path"])
    assert [p.text for p in bid.paragraphs][:5] == COVER
    assert DIAGNOSTIC_MARKER not in "\n".join(p.text for p in bid.paragraphs)
    assert output["synthetic"] is True


def test_cli_result_uses_frozen_scope_even_without_diagnostic_config(tmp_path, monkeypatch, capsys):
    executor, run, output = complete(tmp_path, monkeypatch)
    assert main(["--root", str(executor.store.root), "result", run]) == 2
    rejected = capsys.readouterr()
    assert "DIAGNOSTIC_RESULT_NOT_PUBLISHED" in rejected.err and not rejected.out
    assert main(["--root", str(executor.store.root), "diagnostic-result", run]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publication_status"] == "diagnostic_only" and "files" not in result
    assert set(result["diagnostic_files"]) == set(output["diagnostic_files"])


def test_normal_result_cannot_be_returned_as_diagnostic(tmp_path, monkeypatch, capsys):
    executor, run, _ = complete(tmp_path, monkeypatch, diagnostic=False)
    assert main(["--root", str(executor.store.root), "diagnostic-result", run]) == 2
    assert "RUN_NOT_DIAGNOSTIC_RESULT" in capsys.readouterr().err
    assert main(["--root", str(executor.store.root), "result", run]) == 0
    assert "files" in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("from_node", ["N0", "N6"])
def test_diagnostic_parent_cannot_be_laundered_by_removing_run_purpose(tmp_path, monkeypatch, from_node):
    executor, parent, _ = complete(tmp_path, monkeypatch)
    before = (executor.store.run_dir(parent) / "state.json").read_bytes()
    ordinary = build_executor(executor.store.root, {"provider": {"mode": "prepared"}, "debug_capture": True})
    child = ordinary.fork(parent, from_node)
    assert ordinary.inspect(child)["execution_config"].get("run_purpose") is None
    assert ordinary.inspect(child)["artifact_scope"] == "diagnostic"
    state = ordinary.execute(child, stop_after="N6")
    assert state["status"] == "succeeded"
    assert "files" not in ordinary.output(child)
    assert ordinary.output(child)["publication_status"] == "diagnostic_only"
    assert (executor.store.run_dir(parent) / "state.json").read_bytes() == before
    assert "artifact.published" not in [e["event"] for e in events(ordinary, child)]


@pytest.mark.parametrize("provenance", [
    {"diagnostic_replay": {"publication_status": "diagnostic_only"}},
    {"chain_diagnostics": {"diagnostic_replay": {"publication_status": "diagnostic_only"}}},
    {"publication_status": "diagnostic_only"},
])
def test_verified_inherited_provenance_is_sticky_without_purpose_flag(tmp_path, provenance):
    def origin(ctx, value):
        return ctx.run_unit("origin", {}, lambda: copy.deepcopy(provenance)) and {}
    def final(ctx, value):
        assert ctx.artifact_scope == "diagnostic"
        return {"diagnostic_files": {}, "publication_status": "diagnostic_only"}
    nodes = [NodeSpec("N2", "1", origin, ()), NodeSpec("N6", "1", final, ())]
    executor = Executor(tmp_path, nodes, {})
    run = executor.create_run({})
    assert executor.execute(run, stop_after="N2")["status"] == "paused_debug"
    # Simulate an older state lacking the new field. The immutable successful
    # unit still has explicit provenance and must protect a fork from N0 too.
    state = executor.store.read_state(run)
    state.pop("artifact_scope")
    executor.store.write_state(state)
    assert executor.artifact_scope(run) == "diagnostic"
    child = executor.fork(run, "N6", execution_config={})
    assert executor.execute(child)["status"] == "succeeded"
    assert executor.inspect(child)["artifact_scope"] == "diagnostic"


def test_source_prose_cannot_change_scope(tmp_path):
    executor = Executor(tmp_path, [NodeSpec("N6", "1", lambda ctx, value: {"source": value}, ())], {})
    run = executor.create_run({"source_text": "diagnostic_replay diagnostic_only", "source": {"text": "诊断回放"}})
    assert executor.execute(run)["status"] == "succeeded"
    assert executor.artifact_scope(run) == "standard"
    assert "artifact.published" in [e["event"] for e in events(executor, run)]


def test_legacy_diagnostic_output_refuses_ordinary_cli_result_when_state_flag_is_absent(tmp_path, capsys):
    executor = build_executor(tmp_path / "runtime", {"provider": {"mode": "prepared"}, "debug_capture": True})
    run = executor.create_run({"prepared": prepared_data(tmp_path), "proposals": {},
                              "chain_diagnostics": {"diagnostic_replay": {"publication_status": "diagnostic_only"}}})
    assert executor.execute(run)["status"] == "succeeded"
    state = executor.store.read_state(run)
    state.pop("artifact_scope")
    executor.store.write_state(state)
    assert state["execution_config"].get("run_purpose") is None
    assert main(["--root", str(executor.store.root), "result", run]) == 2
    assert "DIAGNOSTIC_RESULT_NOT_PUBLISHED" in capsys.readouterr().err


def test_unsafe_handler_cannot_publish_files_under_diagnostic_scope(tmp_path):
    executor = Executor(tmp_path, [NodeSpec("N6", "1", lambda ctx, value: {"files": {}}, ())],
                        {"run_purpose": "diagnostic_replay"})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_PUBLICATION_SCOPE_INVALID"
    assert "artifact.published" not in [e["event"] for e in events(executor, run)]


@pytest.mark.parametrize("failure_event", ["node.checkpointed", "artifact.diagnostic_created"])
def test_final_checkpoint_recovers_diagnostic_event_once_without_rerender(tmp_path, monkeypatch, failure_event):
    real_event = Observer.event
    calls = []
    def event(observer, name, metadata=None):
        if name == failure_event:
            raise ObservabilityWriteError("synthetic post-commit sink failure")
        return real_event(observer, name, metadata)
    monkeypatch.setattr(Observer, "event", event)
    nodes = [NodeSpec("N6", "1", lambda ctx, value: calls.append(True) or
                      {"diagnostic_files": {}, "publication_status": "diagnostic_only"}, ())]
    executor = Executor(tmp_path, nodes, {"run_purpose": "diagnostic_replay"})
    run = executor.create_run({})
    first = executor.execute(run, stop_after="N6")
    assert first["status"] == "failed" and first["cursor"] == 1
    monkeypatch.setattr(Observer, "event", real_event)
    restarted = Executor(tmp_path, nodes, {"run_purpose": "diagnostic_replay"})
    assert restarted.resume(run)["status"] == "succeeded"
    assert restarted.resume(run)["status"] == "succeeded"
    assert calls == [True]
    names = [e["event"] for e in events(restarted, run)]
    assert names.count("artifact.diagnostic_created") == 1 and "artifact.published" not in names
    assert restarted.inspect(run)["observability"]["event_status"] == "write_failed"


def test_diagnostic_capture_failure_does_not_publish(tmp_path, monkeypatch):
    real_capture = NodeContext.capture
    def capture(ctx, name, value):
        if name == "node-output":
            ctx._capture_failed = True
            return {"status": "write_failed"}
        return real_capture(ctx, name, value)
    monkeypatch.setattr(NodeContext, "capture", capture)
    executor = Executor(tmp_path, [NodeSpec("N6", "1", lambda ctx, value:
        {"diagnostic_files": {}, "publication_status": "diagnostic_only"}, ())], {"run_purpose": "diagnostic_replay"})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTICS_CAPTURE_FAILED"
    assert not any(e["event"].startswith("artifact.") for e in events(executor, run))


def test_diagnostic_trace_failure_does_not_publish(tmp_path, monkeypatch):
    def broken_trace(observer):
        raise ObservabilityWriteError("synthetic trace persistence failure")
    monkeypatch.setattr(Observer, "check", broken_trace)
    executor = Executor(tmp_path, [NodeSpec("N6", "1", lambda ctx, value:
        {"diagnostic_files": {}, "publication_status": "diagnostic_only"}, ())], {"run_purpose": "diagnostic_replay"})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "OBSERVABILITY_WRITE_FAILED"
    assert not any(e["event"].startswith("artifact.") for e in events(executor, run))
