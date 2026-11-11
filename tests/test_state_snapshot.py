import json
import os

import pytest

from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import LocalStore


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing behavior")
def test_brief_status_reader_does_not_lose_checkpoint_and_retry_is_recorded(tmp_path, monkeypatch):
    executor = Executor(tmp_path, [NodeSpec("N0", "1", lambda ctx, data: data)], {})
    rid = executor.create_run({})
    path = tmp_path / "runs" / rid / "state.json"
    old = executor.inspect(rid)
    reader = path.open("rb")
    assert json.loads(reader.read())["revision"] == old["revision"]
    monkeypatch.setattr("company_bid.storage.time.sleep", lambda _: reader.close())
    newer = {**old, "revision": old["revision"] + 1}
    try:
        LocalStore(tmp_path).write_state(newer)
    finally:
        reader.close()
    state = executor.inspect(rid)
    assert state["revision"] == newer["revision"]
    assert state["storage_write_retries"][0]["retry_number"] == 1
    assert state["storage_write_retries"][0]["winerror"] in (5, 32, 33)
    assert executor.resume(rid)["status"] == "succeeded"
    from company_bid.diagnostics import inspect_diagnostics
    assert inspect_diagnostics(tmp_path, rid)["storage_write_retries"] == state["storage_write_retries"]


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing behavior")
def test_permanent_state_lock_fails_bounded_and_preserves_previous_state(tmp_path, monkeypatch):
    executor = Executor(tmp_path, [NodeSpec("N0", "1", lambda ctx, data: data)], {})
    rid = executor.create_run({})
    path = tmp_path / "runs" / rid / "state.json"
    original = path.read_bytes()
    state = executor.inspect(rid)
    monkeypatch.setattr("company_bid.storage.time.sleep", lambda _: None)
    with path.open("rb"):
        with pytest.raises(PermissionError):
            executor.store.write_state({**state, "revision": 99})
    assert path.read_bytes() == original
