"""Exact manual native exemptions with frozen synthetic visual evidence."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

from PIL import Image
import pytest

from company_bid.extraction import _index, _windows, _window_processing_ledger, validate_live_window
from company_bid.reading import ReadingFailure, read_documents
from company_bid.runtime import Executor, NodeSpec
from test_reading import Context, echo_ocr, pdf, source, text


class EvidenceContext(Context):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.stored = []

    def store_bytes(self, name, content, media_type):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{len(self.stored)}.png"
        path.write_bytes(content)
        reference = {"artifact_id": f"synthetic-{len(self.stored)}", "path": str(path),
                     "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content), "media_type": media_type}
        self.stored.append(reference)
        return deepcopy(reference)

    def read_artifact(self, reference):
        from pathlib import Path
        content = Path(reference["path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError("synthetic artifact corruption")
        return content


def fixture(tmp_path, *, second_page=False):
    path = pdf(tmp_path / "synthetic.pdf", [text("q", y=170) + text("Required inspection records", y=140)] +
               (["IMAGE"] if second_page else []))
    document = source(path)
    # Establish an exact native paragraph identity independently of the rule.
    baseline = read_documents(Context(), [document], {}) if not second_page else None
    if baseline is None:
        import pdfplumber
        from company_bid.reading import ReadingConfig, SourceDocument, _read_pdf_page
        with pdfplumber.open(path) as opened:
            unit = _read_pdf_page(SourceDocument.model_validate(document), opened.pages[0], 1, ReadingConfig(), None)
        target = unit.model_dump(mode="json")["blocks"][0]
    else:
        target = baseline["documents"][0]["units"][0]["blocks"][0]
    visual = tmp_path / "synthetic-visual.png"
    Image.new("RGB", (24, 24), "red").save(visual)
    rule = {"rule_id": "NATIVE-SYNTHETIC-1", "source_id": document["source_id"], "source_sha256": document["sha256"],
            "role": document["role"], "locator": target["locator"],
            "text_sha256": hashlib.sha256(target["text"].encode()).hexdigest(), "bbox": target["bbox"],
            "reason": "occluded_by_verified_decorative_image", "explanation": "Synthetic manual source review only.",
            "visual_evidence_path": str(visual), "visual_evidence_sha256": hashlib.sha256(visual.read_bytes()).hexdigest()}
    return document, rule, target, baseline


def test_exact_manual_rule_keeps_original_blocks_and_denominator(tmp_path):
    document, rule, target, baseline = fixture(tmp_path)
    ctx = EvidenceContext(tmp_path / "artifacts")
    output = read_documents(ctx, [document], {"native_ignored_objects": [rule]})
    original = baseline["documents"][0]["units"][0]
    unit = output["documents"][0]["units"][0]
    assert unit["blocks"] == original["blocks"]
    assert unit["coverage"]["native_text_chars"] == original["coverage"]["native_text_chars"]
    assert output["coverage"]["unit_count"] == baseline["coverage"]["unit_count"] == 1
    entry = unit["coverage"]["ignored_objects"][0]
    assert entry["text_sha256"] == rule["text_sha256"] and entry["locator"] == target["locator"]
    assert entry["basis"] == "explicit_source_bound_manual_ignore"
    ledger = output["coverage"]["native_ignore_rules"][0]
    assert ledger["match_count"] == 1 and ledger["status"] == "matched"
    assert ledger["automatic_visual_verification"] is False and len(ctx.stored) == 1
    assert hashlib.sha256(ctx.read_artifact(ledger["visual_evidence"])).hexdigest() == rule["visual_evidence_sha256"]
    assert "visual_evidence_path" not in json.dumps(ctx.captures)

    manifest = {"sources": [document], "source_roles": {document["source_id"]: document["role"]}}
    blocks, _, documents = _index(output, manifest)
    windows, denominator = _windows(documents, blocks, {})
    assert len(blocks) == len(original["blocks"]) == 2 and len(denominator) == 1
    assert blocks[target["block_id"]]["read_disposition"]["status"] == "ignored_by_frozen_reader_rule"
    window = windows[0]
    processing = _window_processing_ledger(window, [target["block_id"]])
    assert processing["counts"]["ignored_by_frozen_reader"] == 1 and len(processing["blocks"]) == 2
    validate_live_window({"window_id": window["window_id"], "processing_declaration": "processed_not_semantically_accepted",
                          "unresolved_block_ids": [target["block_id"]], "requirements": [], "format_chapters": [],
                          "format_status": "none_verified"}, window)


def test_single_character_is_never_automatically_ignored(tmp_path):
    _, _, target, baseline = fixture(tmp_path)
    unit = baseline["documents"][0]["units"][0]
    assert target["text"] == "q" and unit["coverage"]["ignored_objects"] == []
    assert "native_ignore_rules" not in baseline["coverage"]


@pytest.mark.parametrize("field,value", [("source_id", "OTHER"), ("source_sha256", "0" * 64), ("role", "reference_bid")])
def test_source_role_and_hash_do_not_fall_through_to_another_rule(tmp_path, field, value):
    document, rule, _, _ = fixture(tmp_path)
    rule[field] = value
    ctx = EvidenceContext(tmp_path / "artifacts")
    with pytest.raises(ReadingFailure, match="READING_NATIVE_IGNORE_SOURCE_MISMATCH"):
        read_documents(ctx, [document], {"native_ignored_objects": [rule]})
    assert not ctx.stored and not ctx.calls
    assert any(capture[1].get("status") == "source_mismatch" for capture in ctx.captures)


@pytest.mark.parametrize("change", ["text", "bbox", "missing_line", "missing_page"])
def test_wrong_object_identity_is_an_explicit_failure_with_a_complete_ledger(tmp_path, change):
    document, rule, _, _ = fixture(tmp_path)
    if change == "text": rule["text_sha256"] = "0" * 64
    elif change == "bbox": rule["bbox"] = [value + .01 for value in rule["bbox"]]
    elif change == "missing_line": rule["locator"] = "pdf/page[1]/line[99]"
    else: rule["locator"] = "pdf/page[9]/line[0]"
    code = "READING_NATIVE_IGNORE_OBJECT_MISMATCH" if change in {"text", "bbox"} else "READING_NATIVE_IGNORE_UNMATCHED"
    ctx = EvidenceContext(tmp_path / "artifacts")
    with pytest.raises(ReadingFailure, match=code) as failed:
        read_documents(ctx, [document], {"native_ignored_objects": [rule]})
    assert failed.value.result["coverage"]["required_complete"] is (change == "missing_page")
    assert failed.value.result["coverage"]["native_ignore_validation"]["status"] == "failed"
    assert failed.value.result["coverage"]["unit_count"] == 1
    assert failed.value.result["coverage"]["native_ignore_rules"][0]["match_count"] == 0
    assert len(failed.value.result["documents"][0]["units"][0]["blocks"]) == 2
    assert any(capture[0] == "reading-coverage" for capture in ctx.captures)


def test_bbox_allows_only_fixed_float_serialization_noise(tmp_path):
    document, rule, target, _ = fixture(tmp_path)
    rule["bbox"] = [value + .0000005 for value in rule["bbox"]]
    output = read_documents(EvidenceContext(tmp_path / "artifacts"), [document], {"native_ignored_objects": [rule]})
    assert output["documents"][0]["units"][0]["blocks"][0]["bbox"] == target["bbox"]


@pytest.mark.parametrize("corruption", ["missing", "hash", "not_png"])
def test_visual_evidence_is_verified_before_pages_or_exemptions(tmp_path, corruption):
    document, rule, _, _ = fixture(tmp_path)
    from pathlib import Path
    path = Path(rule["visual_evidence_path"])
    if corruption == "missing": path.unlink()
    elif corruption == "hash": rule["visual_evidence_sha256"] = "0" * 64
    else:
        path.write_bytes(b"synthetic nonimage")
        rule["visual_evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    ctx = EvidenceContext(tmp_path / "artifacts")
    with pytest.raises(ReadingFailure, match="READING_NATIVE_IGNORE_EVIDENCE_INVALID"):
        read_documents(ctx, [document], {"native_ignored_objects": [rule]})
    assert not ctx.stored and not any(key.endswith("pdf-p00001") for key in ctx.calls)


@pytest.mark.parametrize("change", ["reason", "explanation", "wildcard", "bbox_nan", "bbox_bool", "duplicate_id", "duplicate_anchor"])
def test_rules_are_explicit_strict_and_nonoverlapping(tmp_path, change):
    document, rule, _, _ = fixture(tmp_path)
    rules = [rule]
    if change == "reason": rule["reason"] = "ignore_all_short_text"
    elif change == "explanation": rule["explanation"] = " "
    elif change == "wildcard": rule["locator"] = "pdf/page[*]/line[*]"
    elif change == "bbox_nan": rule["bbox"] = [0, 0, float("nan"), 20]
    elif change == "bbox_bool": rule["bbox"] = [False, 0, 10, 20]
    else:
        other = deepcopy(rule)
        other["locator" if change == "duplicate_id" else "rule_id"] = "pdf/page[1]/line[1]" if change == "duplicate_id" else "OTHER-RULE"
        rules.append(other)
    with pytest.raises(ReadingFailure, match="READING_INPUT_INVALID"):
        read_documents(EvidenceContext(tmp_path / "artifacts"), [document], {"native_ignored_objects": rules})


def test_cached_evidence_and_page_reuse_do_not_need_external_png(tmp_path):
    document, rule, _, _ = fixture(tmp_path)
    ctx = EvidenceContext(tmp_path / "artifacts")
    config = {"native_ignored_objects": [rule]}
    first = read_documents(ctx, [document], config)
    from pathlib import Path
    Path(rule["visual_evidence_path"]).unlink()
    calls = ctx.calls.copy()
    second = read_documents(ctx, [document], config)
    assert second == first and ctx.calls == calls and len(ctx.stored) == 1
    assert second["coverage"]["native_ignore_rules"][0]["match_count"] == 1


def test_corrupt_frozen_evidence_is_not_replaced_from_original_png(tmp_path):
    document, rule, _, _ = fixture(tmp_path)
    ctx = EvidenceContext(tmp_path / "artifacts")
    config = {"native_ignored_objects": [rule]}
    read_documents(ctx, [document], config)
    from pathlib import Path
    Path(ctx.stored[0]["path"]).write_bytes(b"corrupt frozen evidence")
    with pytest.raises(ReadingFailure, match="READING_NATIVE_IGNORE_EVIDENCE_INVALID"):
        read_documents(ctx, [document], config)
    assert len(ctx.stored) == 1


@pytest.mark.parametrize("field", ["artifact_id", "size_bytes"])
def test_cached_visual_reference_must_keep_its_artifact_identity(tmp_path, field):
    document, rule, _, _ = fixture(tmp_path)
    ctx = EvidenceContext(tmp_path / "artifacts")
    config = {"native_ignored_objects": [rule]}
    read_documents(ctx, [document], config)
    proof = next(key for key in ctx.outputs if key.startswith("N1-native-ignore-proof-"))
    del ctx.outputs[proof][1]["visual_evidence"][field]
    with pytest.raises(ReadingFailure, match="READING_NATIVE_IGNORE_EVIDENCE_INVALID"):
        read_documents(ctx, [document], config)


@pytest.mark.parametrize("corruption", ["text", "bbox", "origin", "table", "image", "locator", "duplicate_block", "missing_decision", "duplicate_decision", "forged_decision"])
def test_cached_pages_recheck_native_identity_and_exact_decision(tmp_path, corruption):
    document, rule, target, _ = fixture(tmp_path)
    ctx = EvidenceContext(tmp_path / "artifacts")
    config = {"native_ignored_objects": [rule]}
    read_documents(ctx, [document], config)
    key = next(key for key in ctx.outputs if key.endswith("pdf-p00001"))
    unit = ctx.outputs[key][1]
    block = next(b for b in unit["blocks"] if b["block_id"] == target["block_id"])
    if corruption == "text": block["text"] = "Different native content"
    elif corruption == "bbox": block["bbox"][0] += .1
    elif corruption == "origin": block["origin"] = "ocr"
    elif corruption == "table": block.update(kind="table", rows=[[block["text"]]], column_widths=[10])
    elif corruption == "image": block["kind"] = "image"
    elif corruption == "locator": block["locator"] = "pdf/page[1]/line[99]"
    elif corruption == "duplicate_block": unit["blocks"].append(deepcopy(block))
    elif corruption == "missing_decision": unit["coverage"]["ignored_objects"] = []
    elif corruption == "duplicate_decision": unit["coverage"]["ignored_objects"].append(deepcopy(unit["coverage"]["ignored_objects"][0]))
    else: unit["coverage"]["ignored_objects"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ReadingFailure, match="READING_NATIVE_IGNORE_") as failed:
        read_documents(ctx, [document], config)
    assert not failed.value.result["coverage"]["required_complete"]
    assert failed.value.result["coverage"]["native_ignore_rules"][0]["match_count"] == 0


def test_real_executor_restart_reuses_evidence_and_good_page_only(tmp_path):
    document, rule, _, _ = fixture(tmp_path, second_page=True)
    calls = Counter()
    def ocr(request):
        calls[request["page"]] += 1
        if calls[request["page"]] == 1:
            raise ValueError("Synthetic OCR failure")
        return echo_ocr(request)
    config = {"native_ignored_objects": [rule], "ocr_version": "synthetic-v1"}
    nodes = [NodeSpec("N1", "native-ignore-test", lambda ctx, data: read_documents(ctx, [document], config, ocr))]
    execution_config = {"reading": config, "debug_capture": True}
    executor = Executor(tmp_path / "runtime", nodes, execution_config)
    run = executor.create_run({"synthetic": True})
    first = executor.execute(run)
    assert first["status"] == "failed" and calls[2] == 1
    from pathlib import Path
    Path(rule["visual_evidence_path"]).unlink()
    restarted = Executor(tmp_path / "runtime", nodes, execution_config)
    final = restarted.resume(run)
    assert final["status"] == "succeeded" and calls[2] == 2
    units = final["nodes"][0]["units"]
    proof = next(u for key, u in units.items() if key.startswith("N1-native-ignore-proof-"))
    first_page = next(u for key, u in units.items() if key.endswith("pdf-p00001"))
    second_page = next(u for key, u in units.items() if key.endswith("pdf-p00002"))
    assert len(proof["attempts"]) == len(first_page["attempts"]) == 1 and len(second_page["attempts"]) == 2
    output = restarted.output(run)
    assert output["coverage"]["native_ignore_rules"][0]["match_count"] == 1
    ordinary = "\n".join(p.read_text(encoding="utf8") for p in restarted.store.run_dir(run).glob("*.jsonl"))
    assert rule["visual_evidence_path"] not in ordinary and rule["explanation"] not in ordinary


def test_unmatched_global_rule_is_not_a_fabricated_source_reading_gap(tmp_path):
    from company_bid.diagnostics import inspect_diagnostics
    document, rule, _, _ = fixture(tmp_path)
    rule["locator"] = "pdf/page[999]/line[0]"
    config = {"native_ignored_objects": [rule]}
    nodes = [NodeSpec("N1", "native-ignore-test", lambda ctx, value: read_documents(ctx, [document], config))]
    executor = Executor(tmp_path / "runtime", nodes, {"reading": config, "debug_capture": True})
    run = executor.create_run({"synthetic": True})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "READING_NATIVE_IGNORE_UNMATCHED"
    capture = next(item for item in state["nodes"][0]["attempts"][-1]["captures"] if item["name"] == "reading-coverage")
    ledger = executor.store.read_json(capture["ref"])["coverage"]
    assert ledger["status"] == "complete" and ledger["complete"] and ledger["required_complete"]
    assert ledger["counts"]["read"] == 1 and not ledger["unresolved_units"]
    assert ledger["native_ignore_validation"]["status"] == "failed"
    report = inspect_diagnostics(tmp_path / "runtime", run)
    assert report["evidence_status"] == "available"
    assert "READING_LEDGER_INVALID" not in {gap["code"] for gap in report["gaps"]}
    assert report["nodes"][0]["attempts"][-1]["error_code"] == "READING_NATIVE_IGNORE_UNMATCHED"
