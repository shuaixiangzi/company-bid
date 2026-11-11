"""Actual-attempt diagnostics distinguish source limits from lost evidence."""
import json

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec


def test_business_validation_context_is_private_redacted_and_correlated(tmp_path, monkeypatch):
    secret = "test-active-secret-raw-value"
    monkeypatch.setenv("TEST_CHAIN_KEY", secret)

    class Rejected(ValueError):
        code = "EXTRACTION_LABEL_NOT_GROUNDED"
        diagnostic_context = {"candidate_type": "requirement", "candidate_index": 1,
                              "block_id": "B2", "locator": "PRIVATE-SOURCE-LOCATOR",
                              "echo": secret, secret: "private-key-name"}

    def reject(candidate):
        raise Rejected("PRIVATE-EXCEPTION-MESSAGE " + secret)

    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test",
        auth_env="TEST_CHAIN_KEY", max_attempts=1), transport=lambda *args: {
            "id": "remote-request-123", "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})
    def run(ctx, data):
        return ctx.run_unit("W", data, lambda: provider.generate_json(ctx, data, unit_id="W",
            prompt_version="v1", system_prompt="Extract JSON", task_type="extract", validate=reject))
    executor = Executor(tmp_path, [NodeSpec("N2", "v1", run)], {})
    rid = executor.create_run({})
    state = executor.execute(rid)
    assert state["error_code"] == Rejected.code
    report = inspect_diagnostics(tmp_path, rid)
    call = report["calls"][0]
    assert report["actual_call_attempts"] == 1 and call["provider_request_id"] == "remote-request-123"
    captures = report["nodes"][0]["attempts"][0]["captures"]
    evidence = next(item for item in captures if item["name"].startswith("validation-"))
    content = json.loads((tmp_path / evidence["artifact"]["path"]).read_text(encoding="utf-8"))
    assert content["call_id"] == call["call_id"] and content["unit_id"] == "W"
    assert content["diagnostic_context"]["candidate_index"] == 1
    assert content["diagnostic_context"]["locator"] == "PRIVATE-SOURCE-LOCATOR"
    ordinary = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs" / rid).glob("*.jsonl"))
    assert "PRIVATE-SOURCE-LOCATOR" not in ordinary and "PRIVATE-EXCEPTION-MESSAGE" not in ordinary
    for path in (tmp_path / "runs" / rid).rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def ledger():
    unit = {"unit_id": "R-U1", "source_id": "R", "locator": "body/block[1]", "page": None,
            "coverage": {"status": "needs_ocr"}}
    unresolved = {"source_id": "R", "unit_id": "R-U1", "locator": "body/block[1]", "page": None,
                  "status": "needs_ocr", "error_code": None}
    return {"schema_version": "1.0", "documents": [{"source_id": "R", "role": "reference_bid", "expected_units": 1, "units": [unit]}],
            "coverage": {"status": "partial", "complete": False, "required_complete": True,
                "required_roles": ["tender"], "document_count": 1, "unit_count": 1,
                "counts": {"read": 0, "empty": 0, "needs_ocr": 1, "failed": 0, "unsupported": 0},
                "unresolved_units": [unresolved], "required_unresolved_units": []}}


@pytest.mark.parametrize("legacy_partial", [False, True])
def test_complete_ledger_of_optional_unread_content_is_not_a_capture_gap(tmp_path, legacy_partial):
    def run(ctx, data):
        ctx.capture("reading-coverage", ledger(), partial=legacy_partial)
        return {"read": True}
    executor = Executor(tmp_path, [NodeSpec("N1", "v1", run)], {})
    rid = executor.create_run({})
    executor.execute(rid)
    before = (tmp_path / "runs" / rid / "state.json").read_bytes()
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "available_with_content_limitations"
    assert report["gaps"] == [] and report["content_limitations"][0]["unresolved_unit_count"] == 1
    assert report["content_limitations"][0]["required_complete"] is True
    assert (tmp_path / "runs" / rid / "state.json").read_bytes() == before


@pytest.mark.parametrize("case", ["missing_json", "count", "expected_count", "identity", "hidden_required", "wrong_name", "unavailable_response"])
def test_invalid_or_unrelated_partial_capture_remains_a_gap(tmp_path, case):
    value, name = ledger(), "reading-coverage"
    if case == "missing_json":
        value = {"schema_version": "1.0"}
    elif case == "count":
        value["coverage"]["unit_count"] = 2
    elif case == "expected_count":
        value["documents"][0]["expected_units"] = 2
    elif case == "identity":
        value["coverage"]["unresolved_units"][0]["unit_id"] = "WRONG"
    elif case == "hidden_required":
        value["documents"][0]["role"] = "tender"
    elif case == "wrong_name":
        name = "other-partial-content"
    else:
        name, value = "response-error-test", {"response_status": "unavailable"}
    def run(ctx, data):
        ctx.capture(name, value, partial=True)
        return {}
    executor = Executor(tmp_path, [NodeSpec("N1", "v1", run)], {})
    rid = executor.create_run({})
    executor.execute(rid)
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "incomplete" and report["content_limitations"] == []
    assert any(gap["code"] == "CAPTURE_INCOMPLETE" for gap in report["gaps"])


def test_corrupted_partial_ledger_cannot_be_reclassified_as_available(tmp_path):
    def run(ctx, data):
        ctx.capture("reading-coverage", ledger(), partial=True)
        return {}
    executor = Executor(tmp_path, [NodeSpec("N1", "v1", run)], {})
    rid = executor.create_run({})
    state = executor.execute(rid)
    capture = next(item for item in state["nodes"][0]["attempts"][0]["captures"] if item["name"] == "reading-coverage")
    (tmp_path / capture["ref"]["path"]).write_bytes(b"bad JSON")
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == "incomplete" and report["content_limitations"] == []
    assert {gap["code"] for gap in report["gaps"]} >= {"CAPTURE_INCOMPLETE", "ARTIFACT_INTEGRITY_FAILED"}


@pytest.mark.parametrize("valid", [False, True])
def test_captured_reading_ledger_must_be_complete_in_shape(tmp_path, valid):
    value = ledger()
    if valid:
        value["documents"][0]["units"][0]["coverage"]["status"] = "read"
        value["coverage"].update(status="complete", complete=True, unresolved_units=[])
        value["coverage"]["counts"].update(read=1, needs_ocr=0)
    else:
        value = {"schema_version": "1.0", "coverage": {"status": "partial"}, "documents": []}
    def run(ctx, data):
        ctx.capture("reading-coverage", value)
        return {}
    executor = Executor(tmp_path, [NodeSpec("N1", "v1", run)], {})
    rid = executor.create_run({})
    executor.execute(rid)
    report = inspect_diagnostics(tmp_path, rid)
    assert report["evidence_status"] == ("available" if valid else "incomplete")
    assert report["content_limitations"] == []
    assert valid or any(gap["code"] == "READING_LEDGER_INVALID" for gap in report["gaps"])
