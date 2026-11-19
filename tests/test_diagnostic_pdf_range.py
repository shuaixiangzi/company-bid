import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.pipeline import PipelineFailure, build_executor
from company_bid.reading import ReadingFailure, read_documents
from company_bid.runtime import Executor, NodeSpec
from test_reading import Context, pdf, source, text


def sample(tmp_path):
    path = pdf(tmp_path / "original.pdf", ["IMAGE", text("Original page two"), text("Original page three")])
    original = {**source(path), "kind": "tender", "provenance": "original"}
    config = {"diagnostic_pdf_range": {"source_id": original["source_id"], "source_sha256": original["sha256"],
                                       "first_page": 2, "last_page": 3}}
    ctx = Context()
    ctx.execution_config = {"run_purpose": "diagnostic_replay"}
    ctx.artifact_scope = "diagnostic"
    return ctx, original, config


def test_range_reads_the_frozen_original_and_labels_unexamined_pages(tmp_path):
    ctx, original, config = sample(tmp_path)
    limited = read_documents(ctx, [original], config)
    doc, = limited["documents"]
    assert [(u["page"], u["locator"]) for u in doc["units"]] == [(2, "pdf/page[2]"), (3, "pdf/page[3]")]
    scope = limited["coverage"]["diagnostic_pdf_range"]
    assert scope == dict(config["diagnostic_pdf_range"], source_page_count=3, outside_scope_page_count=1)
    assert doc["expected_units"] == 2
    assert limited["coverage"]["publication_status"] == "diagnostic_only"
    with pytest.raises(ReadingFailure, match="OCR_REQUIRED") as full:
        read_documents(Context(), [original], {})
    assert full.value.result["coverage"]["unit_count"] == 3
    assert [u["page"] for u in full.value.result["coverage"]["required_unresolved_units"]] == [1]
    config["diagnostic_pdf_range"]["first_page"] = 1
    child = Context()
    child.execution_config = ctx.execution_config
    child.artifact_scope = ctx.artifact_scope
    with pytest.raises(ReadingFailure, match="OCR_REQUIRED"):
        read_documents(child, [original], config)


@pytest.mark.parametrize("tamper", [None, "source", "page", "outside"])
def test_diagnose_reports_and_authenticates_limited_page_coverage(tmp_path, tamper):
    cold, original, config = sample(tmp_path)
    parsed = read_documents(cold, [original], config)
    if tamper == "source":
        parsed["documents"][0]["sha256"] = "0" * 64
    elif tamper == "page":
        parsed["documents"][0]["units"][0]["page"] = 1
    elif tamper == "outside":
        parsed["coverage"]["diagnostic_pdf_range"]["outside_scope_page_count"] = 0

    def record(ctx, data):
        ctx.capture("reading-coverage", parsed)
        return {"parsed": parsed}

    runtime = tmp_path / "runtime"
    executor = Executor(runtime, [NodeSpec("N1", "v1", record), NodeSpec("N2", "v1", lambda ctx, data: {})],
                        {"run_purpose": "diagnostic_replay", "reading": config})
    run = executor.create_run({})
    executor.execute(run)
    report = inspect_diagnostics(runtime, run)
    if tamper:
        assert "READING_LEDGER_INVALID" in {gap["code"] for gap in report["gaps"]}
    else:
        assert report["gaps"] == [] and report["evidence_status"] == "available_with_content_limitations"
        assert [(row["code"], row["outside_scope_page_count"]) for row in report["content_limitations"]] == [
            ("DIAGNOSTIC_ORIGINAL_PDF_RANGE", 1)]
        child = executor.fork(run, "N2")
        executor.execute(child)
        inherited = inspect_diagnostics(runtime, child)
        assert inherited["gaps"] == [] and inherited["evidence_status"] == "available_with_content_limitations"
        assert inherited["content_limitations"][0]["reused_from_run_id"] == run


@pytest.mark.parametrize("change", ["normal", "wrong_source", "wrong_hash", "past_end", "manual", "second_source",
                                     "not_required", "zero", "bool", "reversed"])
def test_range_never_changes_normal_execution_or_accepts_unbound_sources(tmp_path, change):
    ctx, original, config = sample(tmp_path)
    documents = [original]
    scoped = config["diagnostic_pdf_range"]
    if change == "normal":
        with pytest.raises(PipelineFailure, match="DIAGNOSTIC_ORIGINAL_PDF_SCOPE_INVALID"):
            build_executor(tmp_path / "runtime", {"pipeline": "automatic", "reading": config})
        ctx.execution_config = {}
    elif change == "manual":
        original["provenance"] = "manual_input"
    elif change == "second_source":
        documents.append(dict(original, source_id="B"))
    elif change == "not_required":
        config["required_roles"] = ["company_material"]
    else:
        scoped.update({"wrong_source": {"source_id": "other"}, "wrong_hash": {"source_sha256": "0" * 64},
                       "past_end": {"last_page": 4}, "zero": {"first_page": 0}, "bool": {"first_page": True},
                       "reversed": {"last_page": 1}}[change])
    with pytest.raises(ReadingFailure):
        read_documents(ctx, documents, config)
