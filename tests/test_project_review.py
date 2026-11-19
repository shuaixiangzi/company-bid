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


def test_summary_names_the_source_of_a_deadline_and_preserves_unverified_status():
    row = {"key": "deadline", "label": "正式递交截止", "status": "unresolved", "value": None,
           "evidence_refs": [{"kind": "tender", "source_ref": "condition", "locator": "pdf/page[2]/line[4]"},
                             {"kind": "tender", "source_ref": "notice", "locator": "pdf/page[8]/line[7]"}]}
    deadline = {"submitted_before": "2026-11-20T09:00+08:00", "source_ref": "notice"}
    form = {"questions": [row], "answers": {"deadline": deadline}}
    summary = review.review_summary(form)
    assert "经办人录入的截止时间为 2026-11-20T09:00+08:00" in summary
    assert "仍待正式招标原件核对" in summary
    row.update(status="known", value=deadline)
    summary = review.review_summary(form)
    assert "招标原件载明的递交截止时间" in summary and "第 8 页第 7 行" in summary
    assert "第 2 页" not in summary


def test_summary_reports_payment_conditions_and_original_reading_outcome():
    proof = {"kind": "reviewed_registration", "locator": "docx/word/media/example.jpeg",
             "transcription_status": "ocr_failed_pending_review"}
    row = {"key": "payment_evidence", "label": "实缴凭证", "status": "unresolved", "value": None,
           "conditions": {"window_days": 120, "minimum_payments": 2},
           "evidence_refs": [{"kind": "tender", "locator": "pdf/page[5]"}, proof, dict(proof)]}
    form = {"questions": [row], "answers": {"payment_evidence": {
        "person_name": "合成人员", "employer": "合成单位", "paid_on": "2026-11-01", "source_ref": "test-proof"}}}
    summary = review.review_summary(form)
    assert "正式递交截止前 120 日内，至少实缴 2 次" in summary
    assert summary.count("example.jpeg") == 1 and "该页机器读取或校验失败" in summary
    assert "经办人录入的缴费信息为 合成人员，合成单位，2026-11-01" in summary
    row["evidence_refs"][1:] = [{**proof, "transcription_status": "ocr_no_text_unresolved"}]
    assert "机器读取未取得文字" in review.review_summary(form)


def test_summary_explains_when_a_common_selection_is_unavailable():
    form = {"questions": [
        {"key": "manager_name", "label": "负责人", "status": "unresolved", "value": None,
         "conditions": {"ambiguous_assignment_scope": True}},
        {"key": "signer_role", "label": "签署", "status": "unresolved", "value": None,
         "conditions": {"allowed_roles": []}},
        {"key": "deadline", "label": "截止", "status": "unresolved", "value": None,
         "conditions": {"source_conflict": True}}], "answers": {}}
    summary = review.review_summary(form)
    assert "分别核对各自的人选和证明" in summary and "填写本项目实际拟派人员" not in summary
    assert "未能确定一致的签署身份" in summary and "选择本次响应文件由谁签署" not in summary
    assert "对截止时间存在不同表述" in summary
