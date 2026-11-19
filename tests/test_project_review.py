from copy import deepcopy

import pytest

from company_bid import project_review as review
from company_bid.provider import ProviderFailure
from company_bid.runtime import NodeSpec


def runner(tmp_path, monkeypatch, *, known=False):
    row = {"key": "manager_name", "label": "本次拟派哪位项目负责人？", "status": "known" if known else "unresolved",
           "value": "样例人员" if known else None, "requirement_ids": ["R1"], "depends_on": None}
    monkeypatch.setattr(review, "_rows", lambda executor, state, prepared: [row])
    monkeypatch.setattr(review, "_apply", lambda store, prepared, rows, snapshot, ref: prepared)
    nodes = [NodeSpec("N" + str(i), "v1", lambda ctx, value: value, config_keys=()) for i in range(4)]
    nodes.append(NodeSpec("N4", "v1", lambda ctx, value: review.project_review_input(ctx, value),
                          config_keys=(review.SNAPSHOT_KEY,)))
    return review.ProjectReviewExecutor(tmp_path, nodes, {"review_before_writing": True})


def start(executor):
    return executor.create_run({"prepared": {"project": {"project_id": "P-1"}}, "source_artifacts": []})


def test_pause_and_skip_preserve_plan(tmp_path, monkeypatch):
    executor = runner(tmp_path, monkeypatch)
    parent = start(executor)
    assert executor.execute(parent)["cursor"] == 4
    assert executor.execute(parent)["status"] == "paused_debug"
    plan = executor.output(parent, "N3")
    assert executor.resume(parent)["status"] == "succeeded"
    assert executor.output(parent) == plan


def test_no_unresolved_decision_continues_without_pause(tmp_path, monkeypatch):
    executor = runner(tmp_path, monkeypatch, known=True)
    assert executor.execute(start(executor))["status"] == "succeeded"


def test_stale_submission_and_transplant_fail_closed(tmp_path, monkeypatch):
    executor = runner(tmp_path, monkeypatch)
    first, second = start(executor), start(executor)
    for rid in (first, second):
        executor.execute(rid)
    form = review.review_form(executor, first)
    form.update(answers={"manager_name": "样例人员"}, reviewed_by="合成经办人")
    with pytest.raises(ProviderFailure, match="PROJECT_REVIEW_FORM_CHANGED"):
        review.submit_answers(executor, second, form)
    altered = deepcopy(form)
    altered["questions"][0]["requirement_ids"] = []
    with pytest.raises(ProviderFailure, match="PROJECT_REVIEW_FORM_CHANGED"):
        review.submit_answers(executor, first, altered)
    child, config = review.submit_answers(executor, first, form)
    current = review.ProjectReviewExecutor(tmp_path, executor.nodes, config)
    assert current.resume(child)["status"] == "succeeded"
    assert review.review_form(current, child)["answers"] == form["answers"]
    second_form = review.review_form(current, child)
    second_form.update(answers={"manager_name": "第二人"}, reviewed_by="合成经办人")
    latest, latest_config = review.submit_answers(current, child, second_form)
    latest_ex = review.ProjectReviewExecutor(tmp_path, executor.nodes, latest_config)
    assert latest_ex.resume(latest)["status"] == "succeeded"
    stale = latest_ex.fork(latest, "N4", execution_config=config)
    assert current.resume(stale)["error_code"] == "PROJECT_REVIEW_STALE_DECISION"
    dropped = latest_ex.fork(latest, "N4", execution_config=executor.execution_config)
    assert executor.resume(dropped)["error_code"] == "PROJECT_REVIEW_STALE_DECISION"
    foreign_child = executor.fork(second, "N4", execution_config=config)
    assert current.resume(foreign_child)["error_code"] in ("PROJECT_REVIEW_ORIGIN_MISMATCH", "PROJECT_REVIEW_STALE_DECISION")
    assert review.review_form(executor, first)["answers"] == {}


def test_extra_upload_cannot_attach_to_unrelated_decision(tmp_path, monkeypatch):
    executor = runner(tmp_path, monkeypatch)
    parent = start(executor)
    executor.execute(parent)
    form = review.review_form(executor, parent)
    form.update(answers={"manager_name": "样例人员"}, reviewed_by="合成经办人")
    with pytest.raises(ProviderFailure, match="PROJECT_PAYMENT_UPLOAD_UNBOUND"):
        review.submit_answers(executor, parent, form, tmp_path / "unread.png")


@pytest.mark.parametrize("config", [
    {"provider": {"mode": "chat_completions"}},
    {"provider": {"mode": "diagnostic_writing_commitment_partition"}, "commitment_drafting": {}},
    {"provider": {"mode": "diagnostic_writing_mixed"}},
])
def test_answer_fork_cannot_open_a_fresh_paid_writing_budget(config):
    with pytest.raises(ProviderFailure, match="PROJECT_REVIEW_REQUIRES_OFFLINE_WRITING"):
        review.require_offline_writing(config)
