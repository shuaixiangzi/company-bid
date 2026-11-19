import json

from docx import Document
from PIL import Image
import pytest

from company_bid import material_deepseek_ocr as official
from company_bid.ocr import DEEPSEEK_OCR_VERSION, create_ocr
from company_bid.reading import read_documents
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import json_bytes
from test_ocr import pdf, source
from test_tender_deepseek_ocr import KEY, options, payload, region, stub, target


def chain(root, path, config, *, diagnostic=True):
    def freeze(ctx, _):
        ref = ctx.store_bytes("original-source" + path.suffix, path.read_bytes())
        doc = {**source(path), "path": str(ctx.executor.store.resolve(ref["path"]))}
        return {"source_artifacts": [ref], "manifest": {"sources": [doc]}}

    def read(ctx, input_data):
        return read_documents(ctx, input_data["manifest"]["sources"], {"ocr_version": DEEPSEEK_OCR_VERSION},
                              create_ocr(ctx, config))

    execution = {"pipeline": "automatic", "ocr": config, "debug_capture": True}
    if diagnostic:
        execution["run_purpose"] = "diagnostic_replay"
    return Executor(root, [NodeSpec("N0", "freeze", freeze), NodeSpec("N1", "read", read)], execution)


def recorded_targets(ex, run_id):
    state = ex.store.read_state(run_id)
    node = state["nodes"][1]
    rows = map(json.loads, (ex.store.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines())
    bindings = []
    for event in rows:
        if event["event"] not in {"call.completed", "call.failed"} or event.get("operation") != "tender_ocr":
            continue
        unit = node["units"][event["unit_id"]]
        prep = ex.store.read_json(unit["input_ref"])
        identity = prep["identity"]
        origin = {key: event[key] for key in ("run_id", "unit_id", "call_id", "trace_id", "span_id")}
        bindings.append({**{key: identity[key] for key in ("source_sha256", "locator", "page", "object_kind", "bbox",
                         "package_part")}, "image_sha256": identity["embedded_image_sha256"],
                         "rendered_sha256": identity["image_sha256"], "origin": {**origin,
                                                                               "raw_response_sha256": event["output_hash"]}})
    return bindings


def execute(ex):
    run_id = ex.create_run({})
    return run_id, ex.execute(run_id)


def no_http(monkeypatch):
    monkeypatch.delenv("COMPANY_BID_DEEPSEEK_API_KEY", raising=False)
    calls = []

    def transport(*args):
        calls.append(args)
        raise AssertionError("Recorded targets must never attempt HTTP.")

    monkeypatch.setattr(official, "_transport", transport)
    return calls


def test_successful_recorded_source_revalidates_with_no_http_or_reservation(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    sent = stub(monkeypatch, json_bytes(payload(region("审核要求：期限 4 天"))))
    live = chain(tmp_path / "runtime", path, options())
    old, state = execute(live)
    assert state["status"] == "succeeded" and len(sent) == 1
    old_state = (live.store.run_dir(old) / "state.json").read_bytes()
    pending_http = no_http(monkeypatch)
    binding = recorded_targets(live, old)[0]
    binding["required_text"] = ["期限4天"]
    replay = chain(live.store.root, path, options(recorded_targets=[binding]))
    new, state = execute(replay)
    assert state["status"] == "succeeded", state.get("error_code")
    assert not state.get("call_budget") and not pending_http
    assert state["artifact_scope"] == "diagnostic"
    assert replay.output(new)["documents"][0]["units"][0]["blocks"][0]["text"] == "审核要求：期限 4 天"
    captures = state["nodes"][1]["attempts"][0]["captures"]
    evidence = next(replay.store.read_json(c["ref"]) for c in captures if c["name"] == "tender-ocr-replay-source")
    assert evidence["origin"] == binding["origin"] and evidence["new_fee_reservations"] == 0
    assert evidence["raw_response_ref"]["sha256"] == binding["origin"]["raw_response_sha256"]
    assert (live.store.run_dir(old) / "state.json").read_bytes() == old_state
    assert replay.resume(new)["status"] == "succeeded" and not pending_http


@pytest.mark.parametrize(("patch", "code"), [("raw_hash", "OCR_REPLAY_RESPONSE_CAPTURE_INVALID"),
    ("trace", "OCR_REPLAY_CALL_TRACE_INVALID"), ("missing", "OCR_REPLAY_TARGET_NOT_BOUND"),
    ("formal", "OCR_REPLAY_DIAGNOSTIC_SCOPE_REQUIRED")])
def test_unapproved_or_changed_recording_never_falls_back(monkeypatch, tmp_path, patch, code):
    path = pdf(tmp_path / "scan.pdf")
    calls = stub(monkeypatch, json_bytes(payload(region("冻结文字"))))
    live = chain(tmp_path / "runtime", path, options())
    old, baseline = execute(live)
    assert baseline["status"] == "succeeded" and len(calls) == 1
    bindings = recorded_targets(live, old)
    if patch == "raw_hash":
        bindings[0]["origin"]["raw_response_sha256"] = "0" * 64
    elif patch == "trace":
        bindings[0]["origin"]["span_id"] = "1" * 16
    elif patch == "missing":
        bindings.clear()
    calls = no_http(monkeypatch)
    replay = chain(live.store.root, path, options(recorded_targets=bindings), diagnostic=patch != "formal")
    _, state = execute(replay)
    assert state["status"] == "failed" and state["error_code"] == code
    assert not state.get("call_budget") and not calls


@pytest.mark.parametrize("review", [[], [{"region_index": 1, "reason": "browser_chrome_icon",
                                         "normalized_boxes": [[200, 10, 250, 20]]}]])
def test_config_cannot_waive_captured_image_regions(monkeypatch, tmp_path, review):
    path = pdf(tmp_path / "scan.pdf")
    image_box = (200, 10, 250, 20)
    calls = stub(monkeypatch, json_bytes(payload(region("关键步骤 4", box=(10, 10, 150, 20)) + region("", "image", image_box))))
    live = chain(tmp_path / "runtime", path, options(live_targets=[target(path, required_text=["关键步骤4"])]))
    old, baseline = execute(live)
    assert baseline["error_code"] == "OCR_IMAGE_REGION_REVIEW_REQUIRED" and len(calls) == 1
    binding = recorded_targets(live, old)[0]
    binding["reviewed_image_regions"] = review
    calls = no_http(monkeypatch)
    replay = chain(live.store.root, path, options(recorded_targets=[binding]))
    _, state = execute(replay)
    assert state["status"] == "failed" and state["error_code"] == "OCR_CONFIG_INVALID"
    assert not state.get("call_budget") and not calls


def test_replayed_success_must_satisfy_current_witness(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    calls = stub(monkeypatch, json_bytes(payload(region("要求 4 天"))))
    live = chain(tmp_path / "runtime", path, options(live_targets=[target(path, required_text=["4 天"])]))
    old, baseline = execute(live)
    assert baseline["status"] == "succeeded" and len(calls) == 1
    bindings = recorded_targets(live, old)
    bindings[0]["required_text"] = ["要求5天"]
    calls = no_http(monkeypatch)
    replay = chain(live.store.root, path, options(recorded_targets=bindings))
    _, state = execute(replay)
    assert state["status"] == "failed" and state["error_code"] == "OCR_REVIEWED_TEXT_MISSING"
    assert not state.get("call_budget") and not calls


def test_unresolved_image_does_not_prevent_reuse_of_independent_sibling(monkeypatch, tmp_path):
    path = tmp_path / "photos.docx"
    document = Document()
    paragraph = document.add_paragraph()
    for i in range(2):
        image = tmp_path / f"image-{i}.png"
        Image.new("RGB", (80, 30), (50, 100 * i, 75)).save(image)
        paragraph.add_run().add_picture(str(image))
    document.save(path)
    calls = []
    monkeypatch.setenv("COMPANY_BID_DEEPSEEK_API_KEY", KEY)

    def transport(body, media, secret, timeout):
        calls.append(body)
        text = region("partial") + region("", "image", (10, 10, 20, 20)) if len(calls) == 1 else region("independent")
        return 200, {}, json_bytes(payload(text))

    monkeypatch.setattr(official, "_transport", transport)
    live = chain(tmp_path / "runtime", path, options())
    old, baseline = execute(live)
    assert baseline["error_code"] == "OCR_IMAGE_REGION_REVIEW_REQUIRED" and len(calls) == 2
    bindings = recorded_targets(live, old)
    calls = no_http(monkeypatch)
    replay = chain(live.store.root, path, options(recorded_targets=bindings))
    _, state = execute(replay)
    assert state["status"] == "failed" and state["error_code"] == "OCR_IMAGE_REGION_REVIEW_REQUIRED"
    assert not state.get("call_budget") and not calls
    succeeded = [unit for unit in state["nodes"][1]["units"].values()
                 if unit["unit_id"].startswith("tender-ocr-") and unit["status"] == "succeeded"]
    assert len(succeeded) == 1 and succeeded[0]["unit_id"] == bindings[1]["origin"]["unit_id"]


def test_truncated_origin_cannot_receive_review_approval(monkeypatch, tmp_path):
    path = pdf(tmp_path / "scan.pdf")
    calls = stub(monkeypatch, json_bytes(payload(region("possible fragment"), "length")))
    live = chain(tmp_path / "runtime", path, options())
    old, baseline = execute(live)
    assert baseline["error_code"] == "OCR_OUTPUT_INCOMPLETE" and len(calls) == 1
    calls = no_http(monkeypatch)
    replay = chain(live.store.root, path, options(recorded_targets=recorded_targets(live, old)))
    _, state = execute(replay)
    assert state["status"] == "failed" and state["error_code"] == "OCR_REPLAY_SOURCE_UNIT_INVALID"
    assert not state.get("call_budget") and not calls
