"""Explicit proof disposition preserves failed validation; no real network."""
import copy
import hashlib
import socket

import pytest

from company_bid import material_content_index, material_deepseek_ocr, material_ocr_index, material_ocr_replay
from company_bid.evidence import EvidenceSelection, prepare_evidence
from company_bid.material_ocr_disposition import recorded_quality_disposition
from company_bid.material_recall import annotate_material_issues
from company_bid.material_source_review import build_material_source_review
from company_bid.models import file_sha256
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes
from test_material_ocr_n3_replay import original, snapshot
from test_material_ocr_index import _manifest, _pdf
from test_material_ocr_checklist import _case


EVIDENCE = b"Synthetic independent image review: original area 740 m2; OCR 740 m3."
MISMATCH = "MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disposition tests must not reach external services")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _review_spec(identity, binding, response_sha256, evidence_ref):
    review = build_material_source_review({
        "identity": {k: identity[k] for k in ("source_id", "source_sha256", "selection_sha256", "image_sha256")},
        "ocr": {**binding, "response_sha256": response_sha256, "status": "succeeded", "error_code": None},
        "reviewer": {"review_id": "synthetic-source-area", "review_method": "agent_source_image_review",
                     "evidence_sha256": hashlib.sha256(EVIDENCE).hexdigest()},
        "field": {"field_id": "area", "scope": {"image_region_normalized": [.1, .1, .9, .9]},
                  "disposition": "machine_error", "source_observation": "clear_value",
                  "recognized_text": "740 m3", "source_text": "740 m²"}})
    return {"expected_error_code": MISMATCH, "source_review": review, "source_review_evidence": evidence_ref}


def _reference(identity, value="740 m²"):
    return {"image_sha256": identity["image_sha256"], "checks": [
        {"field_id": "area", "kind": "measure", "region_index": 0, "expected_text": value}]}


def _modified_review(spec, section, field, value):
    raw = {key: copy.deepcopy(spec["source_review"][key]) for key in ("identity", "ocr", "reviewer", "field")}
    raw[section][field] = value
    spec["source_review"] = build_material_source_review(raw)


def _execute_disposition(original, *, damage=None, fail_after=False):
    binding = {"run_id": original["rid"], "node_key": "N3", "unit_id": original["uid"]}
    identity = copy.deepcopy(original["identity"])
    later_attempts = []
    def evidence(ctx, value):
        return {"review_ref": ctx.store_bytes("independent-review.txt", EVIDENCE)}
    def handler(ctx, value):
        spec = _review_spec(identity, binding, hashlib.sha256(json_bytes(original["payload"])).hexdigest(), value["review_ref"])
        reference = _reference(identity)
        if damage == "identity": _modified_review(spec, "identity", "image_sha256", "f"*64)
        elif damage == "response_hash": _modified_review(spec, "ocr", "response_sha256", "f"*64)
        elif damage == "review_origin": _modified_review(spec, "ocr", "run_id", "f"*32)
        elif damage == "evidence_hash": _modified_review(spec, "reviewer", "evidence_sha256", "f"*64)
        elif damage == "evidence_ref": spec["source_review_evidence"]["sha256"] = "f"*64
        elif damage == "expected_code": spec["expected_error_code"] = "MATERIAL_OCR_REPETITIVE_OUTPUT"
        elif damage == "not_quality_error": spec["expected_error_code"] = "MATERIAL_OCR_REPLAY_SOURCE_INVALID"
        elif damage == "no_failure": reference = _reference(identity, "740 m3")
        elif damage == "review_value": _modified_review(spec, "field", "source_text", "741 m²")
        elif damage == "review_status":
            raw = {key: copy.deepcopy(spec["source_review"][key]) for key in ("identity", "ocr", "reviewer", "field")}
            raw["ocr"].update(status="failed", error_code="MATERIAL_OCR_OUTPUT_INCOMPLETE")
            spec["source_review"] = build_material_source_review(raw)
        elif damage == "review_ambiguous":
            raw = {key: copy.deepcopy(spec["source_review"][key]) for key in ("identity", "ocr", "reviewer", "field")}
            raw["field"].update(disposition="source_ambiguous", source_observation="ambiguous_or_unreadable", source_text=None)
            spec["source_review"] = build_material_source_review(raw)
        frozen = {"binding": binding, "identity": identity, "dimensions": [80, 60],
                  "provider": {"purpose": original["payload"]["purpose"]}, "reference": reference,
                  "specification": spec}
        def plan():
            result = ctx.run_unit("material-ocr-disposition-test", frozen,
                                  lambda: recorded_quality_disposition(ctx, **frozen))
            if fail_after:
                def later():
                    later_attempts.append(1)
                    if len(later_attempts)==1: raise RuntimeError("synthetic later interruption")
                    return {"status":"completed"}
                ctx.run_unit("later-unit", {}, later)
            return result
        return ctx.run_unit("plan-assemble", value, plan)
    ex = Executor(original["ex"].store.root,
        [NodeSpec("N0", "review-evidence-v1", evidence), NodeSpec("N3", "quality-disposition-v1", handler)],
        {"debug_capture": True, "run_purpose": "diagnostic_replay"})
    rid = ex.create_run({"identity_sha256": digest(identity), "origin_run_id": original["rid"]})
    state = ex.execute(rid)
    assert original["calls"] == [1]
    assert not state.get("call_budget", {}).get("reservations")
    assert snapshot(original["ex"].store.run_dir(original["rid"])) == original["before"]
    return ex, rid, state


def test_authenticated_quality_conflict_retains_nested_failure_and_resumes_without_revalidating(original):
    ex, rid, state = _execute_disposition(original, fail_after=True)
    assert state["status"] == "failed"
    node = next(n for n in state["nodes"] if n["key"] == "N3")
    validation_id = "material-ocr-validation-" + digest(original["identity"])[:24]
    failed = copy.deepcopy(node["units"][validation_id])
    outer = copy.deepcopy(node["units"]["material-ocr-disposition-test"])
    assert failed["status"] == "failed" and failed["attempts"][0]["error_code"] == MISMATCH
    assert outer["status"] == "succeeded"
    disposition = ex.store.read_json(outer["output_ref"])["data"]
    assert disposition["status"] == "reading_failed_pending_review"
    assert disposition["source_review_authenticated"] is True
    assert disposition["machine_text_adopted"] is False
    assert disposition["material_presence"] == "present_but_unread_or_conflicting"
    assert disposition["qualification_result"] == "not_determined"
    state = ex.resume(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    after = next(n for n in state["nodes"] if n["key"] == "N3")["units"]
    assert after[validation_id] == failed
    assert after["material-ocr-disposition-test"] == outer
    assert ex.output(rid) == disposition
    assert original["calls"] == [1] and not state.get("call_budget", {}).get("reservations")
    assert snapshot(original["ex"].store.run_dir(original["rid"])) == original["before"]
    logs = (ex.store.run_dir(rid)/"events.jsonl").read_text("utf-8")
    assert "PRIVATE_SOURCE_CANARY" not in logs and '"call.started"' not in logs


@pytest.mark.parametrize("damage,code", [
    ("identity", "MATERIAL_OCR_DISPOSITION_REVIEW_SOURCE_CHANGED"),
    ("response_hash", "MATERIAL_OCR_DISPOSITION_REVIEW_RESPONSE_CHANGED"),
    ("review_origin", "MATERIAL_OCR_DISPOSITION_REVIEW_RESPONSE_CHANGED"),
    ("evidence_hash", "MATERIAL_OCR_DISPOSITION_REVIEW_EVIDENCE_CHANGED"),
    ("evidence_ref", "CHECKPOINT_INVALID"),
    ("expected_code", "MATERIAL_OCR_DISPOSITION_ERROR_CHANGED"),
    ("not_quality_error", "MATERIAL_OCR_DISPOSITION_ERROR_NOT_ALLOWED"),
    ("no_failure", "MATERIAL_OCR_DISPOSITION_EXPECTED_FAILURE_MISSING"),
    ("review_value", "MATERIAL_OCR_DISPOSITION_REVIEW_REFERENCE_CHANGED"),
    ("review_status", "MATERIAL_OCR_DISPOSITION_REVIEW_RESPONSE_CHANGED"),
    ("review_ambiguous", "MATERIAL_OCR_DISPOSITION_REVIEW_REFERENCE_CHANGED"),
])
def test_changed_review_response_evidence_or_quality_expectation_cannot_authorize_disposition(original, damage, code):
    _, _, state = _execute_disposition(original, damage=damage)
    assert state["status"] == "failed" and state["error_code"] == code


def _index_pipeline(tmp_path, monkeypatch, *, source_kind="company_material", configuration=None):
    root = tmp_path / "runtime"
    source_path = _pdf(tmp_path)
    source_before = source_path.read_bytes()
    manifest = _manifest(source_path)
    initial = copy.deepcopy(manifest)
    payload = {"page_count":1,"pages":[{"index":0,"finish_reason":"stop","text":"PRIVATE_FAILED_TRANSCRIPT 740 m3"}]}
    binding = {"run_id":"a"*32,"node_key":"N3","unit_id":"material-ocr-"+"b"*24}
    replay_calls = []
    def recorded(ctx, **args):
        replay_calls.append(copy.deepcopy(args))
        return copy.deepcopy(payload), {"origin_run_id":binding["run_id"],"origin_node_key":"N3",
            "origin_unit_id":binding["unit_id"],"raw_response_sha256":hashlib.sha256(json_bytes(payload)).hexdigest()}
    def forbidden(*args,**kwargs): pytest.fail("Failure disposition cannot buy or invoke OCR")
    monkeypatch.setattr(material_ocr_replay,"replay_material_ocr",recorded)
    monkeypatch.setattr(material_deepseek_ocr,"recognize",forbidden)
    monkeypatch.setattr(material_deepseek_ocr,"_transport",forbidden)
    def n0(ctx,value):
        prepared=copy.deepcopy(value["manifest"])
        proof=prepare_evidence(ctx,prepared["sources"],prepared["evidence_selections"],root)
        prepared["sources"].extend(proof["sources"]);prepared["attachments"].extend(proof["attachments"])
        return {"manifest":prepared,"review_ref":ctx.store_bytes("independent-review.txt",EVIDENCE)}
    def n3(ctx,value):
        prepared=copy.deepcopy(value["manifest"])
        native=material_content_index.build_material_content_index(ctx,prepared,{"documents":[]})
        parent_before=copy.deepcopy((prepared,native))
        image=prepared["attachments"][0]
        selected=EvidenceSelection.model_validate(prepared["evidence_selections"][0])
        source=next(s for s in prepared["sources"] if s["source_id"]==selected.source_id)
        identity=material_ocr_index._image_identity(source,native["units"][0],selected,native["groups"],image)
        spec=_review_spec(identity,binding,hashlib.sha256(json_bytes(payload)).hexdigest(),value["review_ref"])
        reference=_reference(identity)
        reviewed={**{k:identity[k] for k in ("source_id","source_sha256","selection_sha256","image_sha256")},
            "review_method":"agent_source_image_review","review_id":"synthetic-current-reference",
            "reference":reference,"reference_sha256":digest(reference)}
        options={"engine":"deepseek_ocr","recorded_only":True,"recorded_sources":{image["sha256"]:binding},
            "failure_dispositions":{image["sha256"]:spec},"source_checks":{image["sha256"]:reviewed}}
        options["expected_engine_fingerprint"]=material_deepseek_ocr.engine_fingerprint({"engine":"deepseek_ocr"})
        # Native source reading succeeded before injecting a forbidden proof role;
        # this isolates the failure-disposition boundary in production index code.
        source["kind"]=source_kind
        if configuration: configuration(options,image["sha256"])
        result=material_ocr_index.transcribe_selected_index(ctx,prepared,native,options)
        assert parent_before==(prepared,native)
        group_id=result["groups"][0]["group_id"]
        report,issue=_case(units=[],candidate_ids=[group_id])
        report["proof_groups"]=[{"group_id":group_id,"title":"Synthetic selected proof"}]
        report["original_content_index"]={k:v for k,v in result.items() if k!="parsed"}
        original_issue=copy.deepcopy(issue)
        report_before=copy.deepcopy(report)
        changes=annotate_material_issues([issue],report)
        assert report==report_before
        assert all(issue[k]==v for k,v in original_issue.items() if k!="reason")
        assert file_sha256(source_path)==source["sha256"]
        return {"index":result,"manifest":prepared,"checklist_issue":issue,"checklist_changes":changes}
    ex=Executor(root,[NodeSpec("N0","proof-disposition-v1",n0),NodeSpec("N3","index-disposition-v1",n3)],
                {"debug_capture":True,"run_purpose":"diagnostic_replay"})
    rid=ex.create_run({"manifest":manifest});state=ex.execute(rid)
    assert source_path.read_bytes()==source_before and manifest==initial
    assert not state.get("call_budget",{}).get("reservations")
    return ex,rid,state,replay_calls


def test_production_index_keeps_failed_page_unread_and_scoped_checklist_with_original_values(tmp_path,monkeypatch):
    ex,rid,state,calls=_index_pipeline(tmp_path,monkeypatch)
    assert state["status"]=="succeeded",state.get("error_code")
    assert len(calls)==1
    output=ex.output(rid);index=output["index"];meta=index["units"][0]
    assert meta["status"]=="needs_ocr" and meta["transcription_status"]=="ocr_failed_pending_review"
    assert meta["ocr_char_count"]==0 and meta["reading_completeness"]=="not_established"
    assert index["coverage"]["ocr_failed_pending_review_count"]==1
    assert index["coverage"]["ocr_failed_count"]==1
    assert index["coverage"]["reading_result"]=="ok_with_issues"
    assert index["coverage"]["pending_ocr_unit_count"]==1
    assert index["coverage"]["ocr_transcribed_unverified_count"]==0
    unit=index["parsed"]["documents"][0]["units"][0]
    assert unit["material_content_ocr_candidate_readable"] is False
    assert unit["coverage"]["error_code"]==MISMATCH
    assert not any(b.get("origin")=="ocr" or "PRIVATE_FAILED_TRANSCRIPT" in b.get("text","") for b in unit["blocks"])
    reason=output["checklist_issue"]["reason"]
    assert "机器识别错误" in reason and "740 m²" in reason and "740 m3" in reason
    assert "读取失败状态保留" in reason and "公司缺资料" in reason
    units=next(n for n in state["nodes"] if n["key"]=="N3")["units"]
    assert sum(u["status"]=="failed" for key,u in units.items() if key.startswith("material-ocr-validation-"))==1
    assert sum(u["status"]=="succeeded" for key,u in units.items() if key.startswith("material-ocr-disposition-"))==1
    assert ex.resume(rid)["status"]=="succeeded" and len(calls)==1


@pytest.mark.parametrize("kind",["tender","manual_input","reference_bid"])
def test_production_index_cannot_dispose_tender_or_manual_source_read_failure(tmp_path,monkeypatch,kind):
    _,_,state,calls=_index_pipeline(tmp_path,monkeypatch,source_kind=kind)
    assert state["status"]=="failed" and state["error_code"]=="MATERIAL_OCR_FAILURE_COMPANY_PROOF_ONLY"
    assert calls==[]


@pytest.mark.parametrize("damage,code",[("live","MATERIAL_OCR_DISPOSITION_RECORDED_ONLY_REQUIRED"),
    ("unbound","MATERIAL_OCR_DISPOSITION_BINDING_INVALID")])
def test_disposition_requires_explicit_recorded_only_and_original_response_binding(tmp_path,monkeypatch,damage,code):
    def configure(options,image_hash):
        if damage=="live": options["recorded_only"]=False
        else: options["recorded_sources"]={}
    _,_,state,calls=_index_pipeline(tmp_path,monkeypatch,configuration=configure)
    assert state["status"]=="failed" and state["error_code"]==code
    assert calls==[]
