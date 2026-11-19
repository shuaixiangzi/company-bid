"""One-pass source indexing must not weaken authentication or isolation."""
import copy

import pytest

from company_bid import extraction
from company_bid.diagnostic_replay import DiagnosticReplay, _OriginRun
from company_bid.provider import ChatConfig, ProviderFailure
from test_diagnostic_replay import make_source_run, replay_config, target_run


def prepared_source(tmp_path, monkeypatch):
    source = make_source_run(tmp_path, monkeypatch, two_windows=True)
    spec = replay_config(source)
    binding = next(iter(spec["bindings"].values()))
    spec["bindings"] = {"extract_requirements:" + uid: {**binding, "unit_id": uid}
                        for uid in source["unit_ids"]}
    return source, spec


def test_all_windows_share_one_authenticated_source_index(tmp_path, monkeypatch):
    source, spec = prepared_source(tmp_path, monkeypatch)
    windows = extraction._windows
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return windows(*args, **kwargs)

    monkeypatch.setattr(extraction, "_windows", counted)
    _, _, state, http, gateways = target_run(source, spec=spec)
    assert state["status"] == "succeeded"
    assert http == []
    # Current preflight + one historical N1 + current extraction. Not one
    # complete source reconstruction for every historical window.
    assert len(calls) == 3
    assert gateways[0].summary()["revalidated_count"] == 2


@pytest.mark.parametrize("changed", ["checkpoint", "original"])
def test_cached_index_still_rechecks_source_bytes_before_next_window(tmp_path, monkeypatch, changed):
    source, spec = prepared_source(tmp_path, monkeypatch)
    original = DiagnosticReplay._source_window
    calls = []

    def corrupt_after_first(self, run, payload, uid):
        result = original(self, run, payload, uid)
        calls.append(uid)
        if len(calls) == 1:
            path = (run.store.root / run.node("N1")["output_ref"]["path"] if changed == "checkpoint"
                    else source["value"]["manifest"]["sources"][0]["path"])
            from pathlib import Path
            Path(path).write_bytes(b"changed-after-indexing")
        return result

    monkeypatch.setattr(DiagnosticReplay, "_source_window", corrupt_after_first)
    _, _, state, http, _ = target_run(source, spec=spec)
    assert state["status"] == "failed"
    expected = "DIAGNOSTIC_REPLAY_PREFLIGHT_FAILED" if changed == "checkpoint" else "DIAGNOSTIC_REPLAY_SOURCE_CHANGED"
    assert state["error_code"] == expected
    assert http == []
    assert len(calls) == (1 if changed == "checkpoint" else 2)


def test_cached_window_is_isolated_and_configuration_is_part_of_identity(tmp_path, monkeypatch):
    source, spec = prepared_source(tmp_path, monkeypatch)
    replay = DiagnosticReplay(spec, ChatConfig(**{k: v for k, v in source["options"].items() if k != "mode"}))
    replay.roots = {"history": source["executor"].store.root}
    replay.current = copy.deepcopy(source["value"])
    origin = _OriginRun("history", source["executor"].store.root, source["run"])
    payload = origin.artifact(origin.node("N2")["units"][source["unit_id"]]["input_ref"])
    blocks, _, docs = extraction._index(replay.current["parsed"], replay.current["manifest"])
    replay.windows = {w["window_id"]: w for w in extraction._windows(docs, blocks, payload["window_config"])[0]}
    first, _ = replay._source_window(origin, payload, source["unit_id"])
    first["blocks"][0]["text"] = "caller modification must not enter cache"
    again, _ = replay._source_window(origin, payload, source["unit_id"])
    assert again == replay.windows[source["unit_id"]]
    modified = copy.deepcopy(payload)
    modified["window_config"]["window_max_blocks"] = 2
    with pytest.raises(ProviderFailure):
        replay._source_window(origin, modified, source["unit_id"])
    # The changed configuration was actually rebuilt, rather than reusing the
    # old window identity based only on its source file.
    assert len(replay._source_windows) == 2


def test_each_preflight_discards_old_candidate_and_source_caches(tmp_path, monkeypatch):
    source, spec = prepared_source(tmp_path, monkeypatch)
    captured = []
    original = DiagnosticReplay.preflight

    def repeat(self, ctx, value):
        original(self, ctx, value)
        previous_runs = dict(self._runs)
        # Stale candidates must be reauthenticated, not merely reused after a
        # second preflight on the same object.
        for entry in self._loaded_entries.values():
            entry["candidate"] = {"corrupt": True}
        result = original(self, ctx, value)
        assert all(self._runs[key] is not old for key, old in previous_runs.items())
        captured.append(result)
        return result

    monkeypatch.setattr(DiagnosticReplay, "preflight", repeat)
    _, _, state, http, _ = target_run(source, spec=spec)
    assert state["status"] == "succeeded"
    assert http == [] and len(captured) == 1


def test_original_files_get_two_full_reads_not_one_per_window(tmp_path, monkeypatch):
    from pathlib import Path
    source, spec = prepared_source(tmp_path, monkeypatch)
    original_path = Path(source["value"]["manifest"]["sources"][0]["path"]).resolve()
    read_bytes = Path.read_bytes
    reads = []
    inside_source_verifier = False

    def measure(method):
        def wrapped(*args):
            nonlocal inside_source_verifier
            inside_source_verifier = True
            try:
                return method(*args)
            finally:
                inside_source_verifier = False
        return wrapped

    def counted(path):
        if inside_source_verifier and path.resolve() == original_path:
            reads.append(1)
        return read_bytes(path)

    monkeypatch.setattr(DiagnosticReplay, "_verify_source_file", measure(DiagnosticReplay._verify_source_file))
    monkeypatch.setattr(DiagnosticReplay, "_seal_source_files", measure(DiagnosticReplay._seal_source_files))
    monkeypatch.setattr(Path, "read_bytes", counted)
    _, _, state, http, gateways = target_run(source, spec=spec)
    assert state["status"] == "succeeded" and http == []
    # Other independent consumers (diagnostic inspector and output integrity)
    # may also verify originals. Count only the replay's own source verifier.
    report = gateways[0].summary()["source_file_verification"]
    assert report == {"policy": "full_hash_before_and_after_preflight", "distinct_paths": 1}
    assert len(reads) == 2


def test_original_changes_block_even_an_earlier_undeclared_live_window(tmp_path, monkeypatch):
    from pathlib import Path
    source, spec = prepared_source(tmp_path, monkeypatch)
    first = source["unit_ids"][0]
    del spec["bindings"]["extract_requirements:" + first]
    build = DiagnosticReplay._source_window

    def corrupt(self, run, payload, uid):
        result = build(self, run, payload, uid)
        Path(source["value"]["manifest"]["sources"][0]["path"]).write_bytes(b"changed-before-live-miss")
        return result

    monkeypatch.setattr(DiagnosticReplay, "_source_window", corrupt)
    _, _, state, http, _ = target_run(source, spec=spec)
    assert state["status"] == "failed" and state["error_code"] == "DIAGNOSTIC_REPLAY_SOURCE_CHANGED"
    assert http == [] and not state.get("call_budget")


def test_retargeted_manifest_alias_is_rejected_even_if_old_file_is_intact(tmp_path, monkeypatch):
    from pathlib import Path
    import hashlib
    source, spec = prepared_source(tmp_path, monkeypatch)
    replay = DiagnosticReplay(spec, ChatConfig(**{k: v for k, v in source["options"].items() if k != "mode"}))
    real = Path(source["value"]["manifest"]["sources"][0]["path"]).resolve()
    alias = tmp_path / "manifest-source-alias"
    alternate = tmp_path / "different-target"
    alternate.write_bytes(real.read_bytes())  # Even equal bytes do not waive path scope.
    resolve = Path.resolve
    redirected = [real]

    def mapped(path, *args, **kwargs):
        return redirected[0] if path == alias else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", mapped)
    replay._verify_source_file(real, hashlib.sha256(real.read_bytes()).hexdigest(), str(alias))
    redirected[0] = alternate
    with pytest.raises(ProviderFailure, match="DIAGNOSTIC_REPLAY_SOURCE_CHANGED"):
        replay._seal_source_files()
