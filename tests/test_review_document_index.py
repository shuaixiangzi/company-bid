import hashlib
import json

import pytest

from company_bid.pipeline import build_executor
from test_review_document_role import prepared_data, PROPOSAL


@pytest.mark.parametrize("debug", [False, True])
def test_business_checklist_index_survives_disabled_diagnostic_capture(tmp_path, debug):
    config = {"provider": {"mode": "prepared"}, "debug_capture": debug, "review_document_mode": "task_list"}
    executor = build_executor(tmp_path / "runtime", config)
    run = executor.create_run({"prepared": prepared_data(tmp_path), "proposals": {"S2": PROPOSAL}})
    state = executor.execute(run)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(run, "N6")
    evidence = output["review_document_index"]
    assert evidence["capture_status"] == ("captured" if debug else "disabled_by_policy")
    raw = (executor.store.root / evidence["ref"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == evidence["ref"]["sha256"]
    index = json.loads(raw)
    assert index["mode"] == "task_list"
    assert {row["issue_id"] for row in index["issues"]} == set(output["issue_ids"])
