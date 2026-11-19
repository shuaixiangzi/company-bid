"""Freeze an optional project decision against the exact pre-writing checkpoint."""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path

from PIL import Image

from .models import PreparedBid, SourceRef
from .observability import redact_payload, utc_now
from .provider import ProviderFailure
from .runtime import Executor
from .storage import digest


FORM_VERSION = "bid.project-questions.v1"
SNAPSHOT_VERSION = "bid.project-answers.v1"
SNAPSHOT_KEY = "project_answer_snapshot"
UPLOAD_ALIAS = "supplied_payment_proof"


class ProjectReviewExecutor(Executor):
    """Pause once at the completed plan when this project has decisions to make."""

    def execute(self, run_id, stop_after=None):
        initial = self.inspect(run_id)
        if (stop_after is None and initial["status"] == "paused_debug" and initial["cursor"] == 4
                and not initial["execution_config"].get(SNAPSHOT_KEY)):
            return initial
        if stop_after is not None or initial["cursor"] > 3:
            return super().execute(run_id, stop_after)
        state = super().execute(run_id, stop_after="N3")
        if state["status"] != "paused_debug":
            return state
        form = review_form(self, run_id)
        known = {row["key"]: row["value"] for row in form["questions"] if row["status"] == "known"}
        for row in form["questions"]:
            condition = row.get("depends_on")
            if row["status"] != "known" and (not condition or known.get(condition["key"]) == condition["value"]):
                return state
        return super().execute(run_id)

    def resume(self, run_id, stop_after=None):
        if self.inspect(run_id)["cursor"] == 4:
            return super().execute(run_id, stop_after)
        return self.execute(run_id, stop_after)


def _require(condition, code):
    if not condition:
        raise ProviderFailure(code)


def require_offline_writing(config):
    # A new fork has its own run ledger. This release validates supplement
    # application with deterministic/fully replayed writing only.
    mode = config.get("provider", {"mode": "prepared"}).get("mode")
    _require(mode in {"prepared", "diagnostic_writing_candidate_import"} or
             mode == "diagnostic_writing_commitment_partition" and
             bool(config.get("commitment_drafting", {}).get("reviewed_candidates")),
             "PROJECT_REVIEW_REQUIRES_OFFLINE_WRITING")


def _rows(executor, state, prepared):
    from .project_questions import build_project_questions
    frozen = executor._checkpoint_data(next(node for node in state["nodes"] if node["key"] == "N0"))
    roles = frozen["manifest"]["source_roles"]
    return build_project_questions(prepared, project_source_ids={
        source_id for source_id, role in roles.items() if role == "project_supplement"})


def _checkpoint(state):
    node = next((node for node in state["nodes"] if node["key"] == "N3"), None)
    _require(node and node["status"] == "succeeded", "PROJECT_REVIEW_REQUIRES_COMPLETED_N3")
    return node


def _form(run_id, node, prepared, rows):
    return {"schema_version": FORM_VERSION, "run_id": run_id,
            "project_id": prepared["project"]["project_id"], "plan_sha256": node["output_hash"],
            "n3_artifact_id": node["output_ref"]["artifact_id"], "questions_sha256": digest(rows),
            "questions": rows, "reviewed_by": None, "answers": {}}


def _read_snapshot(store, state, node, prepared, rows):
    ref = state["execution_config"].get(SNAPSHOT_KEY)
    if ref is None:
        return None
    snapshot = store.read_json(ref)
    origin = snapshot["run_id"]
    parent = state.get("parent_run_id")
    _require(parent is not None, "PROJECT_REVIEW_ORIGIN_MISMATCH")
    immediate = store.read_state(parent)["execution_config"].get(SNAPSHOT_KEY)
    _require(parent == origin or ref == immediate, "PROJECT_REVIEW_STALE_DECISION")
    visited = set()
    while parent and parent != origin and parent not in visited:
        visited.add(parent)
        parent = store.read_state(parent).get("parent_run_id")
    _require(parent == origin and ref["path"].startswith(f"runs/{origin}/artifacts/"),
             "PROJECT_REVIEW_ORIGIN_MISMATCH")
    prior_ref = store.read_state(origin)["execution_config"].get(SNAPSHOT_KEY)
    _require(snapshot.get("previous_snapshot") == (prior_ref["sha256"] if prior_ref else None),
             "PROJECT_REVIEW_ORIGIN_MISMATCH")
    proof = snapshot["payment_proof"]
    if proof:
        if not proof["path"].startswith(f"runs/{origin}/artifacts/"):
            _require(prior_ref and store.read_json(prior_ref)["payment_proof"] == proof,
                     "PROJECT_REVIEW_PROOF_ORIGIN_MISMATCH")
        _require(snapshot["answers"].get("payment_evidence", {}).get("source_ref")
                 == "F-PAYMENT-" + proof["sha256"], "PROJECT_PAYMENT_UPLOAD_UNBOUND")
    _require(snapshot["schema_version"] == SNAPSHOT_VERSION and
             snapshot["project_id"] == prepared["project"]["project_id"] and
             snapshot["plan_sha256"] == node["output_hash"] and
             snapshot["n3_artifact_id"] == node["output_ref"]["artifact_id"] and
             snapshot["questions_sha256"] == digest(rows), "PROJECT_REVIEW_PLAN_CHANGED")
    store.validate_embedded_artifacts(snapshot)
    return snapshot


def review_form(executor, run_id):
    state = executor.inspect(run_id)
    node = _checkpoint(state)
    prepared = executor.output(run_id, "N3")["prepared"]
    rows = _rows(executor, state, prepared)
    form = _form(run_id, node, prepared, rows)
    snapshot = _read_snapshot(executor.store, state, node, prepared, rows)
    if snapshot:
        form["answers"] = snapshot["answers"]
    return form


def review_summary(form):
    labels = {"legal_representative": "法定代表人直接签署", "agent": "委托代理人签署"}
    lines = ["# 写标前项目信息核对", "", "按现有资料即可继续生成。补录会生成新版本。", ""]
    choices = {row["key"]: row["value"] for row in form["questions"] if row["status"] == "known"}
    choices.update(form["answers"])
    for row in form["questions"]:
        condition = row.get("depends_on")
        if condition and choices.get(condition["key"]) != condition["value"]:
            continue
        lines.append("## " + row["label"])
        lines.append("")
        value = form["answers"].get(row["key"], row["value"])
        if isinstance(value, dict):
            value = " ".join(str(v) for k, v in value.items() if k != "source_ref")
        prompts = {"manager_name": "填写本项目实际拟派人员的姓名。", "employer": "填写该人员的实际用工主体。",
                   "deadline": "尚未定位正式截止时点，请提供载明时间的正式公告或澄清。",
                   "payment_evidence": "可选用已有实缴凭证或补充本人的缴费凭证。",
                   "signer_role": "选择本次响应文件由谁签署。", "agent_name": "填写本次拟任代理人姓名。"}
        lines.append("已记录：" + labels.get(value, str(value)) if value else prompts[row["key"]])
        conditions = row.get("conditions", {})
        if row["key"] == "signer_role" and conditions.get("allowed_roles"):
            lines.append("可采用：" + " / ".join(labels[r] for r in conditions["allowed_roles"]))
            if "agent" in conditions["allowed_roles"] and value is None:
                lines.append("选择代理人签署时可一并填写代理人姓名。")
        if row["key"] == "payment_evidence" and "window_days" in conditions:
            lines.append(f"核对窗口：正式递交截止前 {conditions['window_days']} 日。")
        evidence = row.get("evidence_refs", [])
        proof = next((item for item in evidence if item["kind"] == "reviewed_registration"), None)
        if proof and row["key"] == "payment_evidence":
            lines.append("已有参保登记页（" + proof["locator"].split("/")[-1] + "），实缴仍待核。")
        if evidence:
            item = next((item for item in evidence if item["kind"] == "tender"), evidence[0])
            locator = str(item.get("locator", "")).split(";", 1)[0]
            pdf = re.fullmatch(r"pdf/page\[(\d+)\](?:/line\[(\d+)\])?", locator)
            if pdf:
                locator = f"招标原件第 {pdf[1]} 页" + (f"第 {pdf[2]} 行" if pdf[2] else "")
            lines.append("相关条款：" + locator)
        lines.append("")
    if not form["questions"]:
        lines.append("本次核对范围内没有需要补充的项目。")
    return "\n".join(lines) + "\n"


def export_review(executor, run_id):
    form = review_form(executor, run_id)
    ref = executor.store.put_json(run_id, "project-questions", form)
    summary = executor.store.put_bytes(run_id, "project-questions.md", review_summary(form).encode(), "text/markdown")
    return {"questions_file": str(executor.store.resolve(ref["path"])),
            "summary_file": str(executor.store.resolve(summary["path"])),
            "question_count": len(form["questions"])}


def _freeze_image(store, run_id, path):
    # The CLI accepts an explicitly supplied original image. Never discover
    # evidence by traversing filesystem paths embedded in the answer JSON.
    limit = 32 * 1024 * 1024
    with Path(path).open("rb") as stream:
        content = stream.read(limit + 1)
    _require(len(content) <= limit, "PROJECT_PAYMENT_IMAGE_TOO_LARGE")
    with Image.open(io.BytesIO(content)) as img:
        fmt = img.format
        _require(fmt in {"PNG", "JPEG"} and getattr(img, "n_frames", 1) == 1,
                 "PROJECT_PAYMENT_IMAGE_INVALID")
        img.verify()
    with Image.open(io.BytesIO(content)) as img:
        img.load()
    ext, mime = ("png", "image/png") if fmt == "PNG" else ("jpg", "image/jpeg")
    return store.put_bytes(run_id, "payment-proof." + ext, content, mime)


def _source(store, ref, source_id, title, kind, synthetic):
    return SourceRef(source_id=source_id, title=title, path=store.resolve(ref["path"]),
                     sha256=ref["sha256"], locator="本项目写标前补录", kind=kind,
                     provenance="manual_input" if kind == "manual_input" else "original",
                     synthetic=synthetic).model_dump(mode="json")


def _apply(store, prepared, rows, snapshot, ref):
    from .project_answers import apply_project_answers
    plan = copy.deepcopy(prepared)
    source_id = "F-REVIEW-" + ref["sha256"]
    extra = [_source(store, ref, source_id, "本次项目信息确认记录", "manual_input", plan["synthetic"])]
    proof = snapshot["payment_proof"]
    if proof:
        extra.append(_source(store, proof, "F-PAYMENT-" + proof["sha256"], "本次补充的社保凭证（待核）",
                             "attachment", plan["synthetic"]))
    existing = {s["source_id"] for s in plan["sources"]}
    _require(all(s["source_id"] not in existing for s in extra), "PROJECT_REVIEW_SOURCE_COLLISION")
    plan["sources"].extend(extra)
    updated = apply_project_answers(plan, rows, snapshot["answers"], source_id)
    return PreparedBid.model_validate(updated).model_dump(mode="json")


def submit_answers(executor, run_id, submission, payment_proof=None):
    state = executor.inspect(run_id)
    require_offline_writing(state["execution_config"])
    node = _checkpoint(state)
    # Authentication covers both the N3 prefix and the question generator.
    prepared = executor.output(run_id, "N3")["prepared"]
    rows = _rows(executor, state, prepared)
    expected = _form(run_id, node, prepared, rows)
    _require(isinstance(submission, dict) and submission.keys() == expected.keys() and
             all(submission[key] == value for key, value in expected.items()
                 if key not in {"answers", "reviewed_by"}), "PROJECT_REVIEW_FORM_CHANGED")
    answers = copy.deepcopy(submission["answers"])
    _require(isinstance(answers, dict) and not set(answers) - {row["key"] for row in rows},
             "PROJECT_REVIEW_ANSWER_UNEXPECTED")
    actor = submission["reviewed_by"]
    _require(isinstance(actor, str) and 0 < len(actor.strip()) <= 120 and actor.isprintable(),
             "PROJECT_REVIEW_OPERATOR_REQUIRED")
    entered = {"reviewed_by": actor, "answers": answers}
    _require(redact_payload(entered) == entered, "PROJECT_REVIEW_SENSITIVE_INPUT")
    from .project_answers import validate_project_answer_values
    validate_project_answer_values(rows, answers)
    previous = _read_snapshot(executor.store, state, node, prepared, rows)
    proof = None
    payment = answers.get("payment_evidence")
    evidence_id = payment.get("source_ref") if isinstance(payment, dict) else None
    if payment_proof is not None:
        _require(evidence_id == UPLOAD_ALIAS, "PROJECT_PAYMENT_UPLOAD_UNBOUND")
        proof = _freeze_image(executor.store, run_id, payment_proof)
        payment["source_ref"] = "F-PAYMENT-" + proof["sha256"]
    elif previous and previous["payment_proof"] and evidence_id == "F-PAYMENT-" + previous["payment_proof"]["sha256"]:
        proof = previous["payment_proof"]
    else:
        _require(evidence_id != UPLOAD_ALIAS, "PROJECT_PAYMENT_UPLOAD_MISSING")
    snapshot = {key: expected[key] for key in ("run_id", "project_id", "plan_sha256",
                                                "n3_artifact_id", "questions_sha256")}
    snapshot.update(schema_version=SNAPSHOT_VERSION, answers=answers, payment_proof=proof,
                    previous_snapshot=state["execution_config"].get(SNAPSHOT_KEY, {}).get("sha256"),
                    reviewed_by=actor.strip(), recorded_at=utc_now())
    ref = executor.store.put_json(run_id, "project-answers", snapshot)
    _apply(executor.store, prepared, rows, snapshot, ref)
    config = {**state["execution_config"], SNAPSHOT_KEY: ref}
    child_id = executor.fork(run_id, "N4", execution_config=config)
    return child_id, config


def project_review_input(ctx, value):
    """Derive exactly the same protected source bindings during N4 and N5."""
    ref = ctx.execution_config.get(SNAPSHOT_KEY)
    if ref is None:
        if ctx._state.get("fork_from_node") in {"N4", "N5", "N6"}:
            parent = ctx.executor.store.read_state(ctx._state["parent_run_id"])
            _require(not parent["execution_config"].get(SNAPSHOT_KEY), "PROJECT_REVIEW_STALE_DECISION")
        return value
    require_offline_writing(ctx.execution_config)
    prepared = value["prepared"]
    rows = _rows(ctx.executor, ctx._state, prepared)
    snapshot = _read_snapshot(ctx.executor.store, ctx._state, _checkpoint(ctx._state), prepared, rows)
    updated = _apply(ctx.executor.store, prepared, rows, snapshot, ref)
    artifacts = [ref] + ([snapshot["payment_proof"]] if snapshot["payment_proof"] else [])
    return {**value, "prepared": updated, "source_artifacts": [*value["source_artifacts"], *artifacts],
            "chain_diagnostics": {**value.get("chain_diagnostics", {}),
                                  "project_review": {"snapshot": ref, "answers_count": len(snapshot["answers"]),
                                                     "questions_sha256": snapshot["questions_sha256"]}}}
