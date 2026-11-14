"""Derived historical evidence retains actual physical scope, not presumed eligibility."""
import copy
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from company_bid.evidence import EvidenceFailure
from company_bid.diagnostics import inspect_diagnostics
from company_bid.intake import IntakeManifest, ProofDerivation, verify_derived_proof_scopes
from company_bid.models import file_sha256
from company_bid.pipeline import build_executor

from test_intake_material_records import manifest_with_history


def derivation():
    return {"derivation_id": "D1", "relation": "user_authorized_bid_split", "selection_id": "E1",
        "child_source_id": "C", "child_source_sha256": "a" * 64,
        "parent_source_id": "P", "parent_source_sha256": "b" * 64, "record_id": "R",
        "physical_pages": [1, 2, 3],
        "parent_locator": {"text": "人工资料登记范围", "status": "declared_unverified_parent_page_range"},
        "visual_review": {"method": "agent_visual_review_of_local_render", "status": "candidate_identity_and_boundary_reviewed",
            "project_title": "合成历史项目", "physical_pages": [1, 3], "observations": ["标题和尾页类型可见"]},
        "qualification_result": "not_determined", "project_assignment": "not_made"}


@pytest.mark.parametrize("damage", ["missing_middle", "duplicate", "outside_visual", "qualified", "unrecognized"])
def test_derivation_cannot_hide_gaps_or_upgrade_visual_review(damage):
    value = derivation()
    if damage == "missing_middle":value["physical_pages"] = [1, 3]
    elif damage == "duplicate":value["physical_pages"] = [1, 2, 2]
    elif damage == "outside_visual":value["visual_review"]["physical_pages"] = [4]
    elif damage == "qualified":value["qualification_result"] = "verified"
    else:value["user_verified"] = True
    with pytest.raises(ValidationError):ProofDerivation.model_validate(value)


class Context:
    def __init__(self):self.inputs=[];self.outputs=[]
    def run_unit(self, name, value, fn):
        self.inputs.append(copy.deepcopy(value));result=fn();self.outputs.append(result);return result


def test_actual_pdf_page_count_rejects_omitted_final_page_even_when_declared_list_is_consecutive(tmp_path):
    path=tmp_path/'three.pdf'
    img=Image.new('RGB',(80,100),'white');img.save(path,save_all=True,append_images=[img,img])
    value=derivation();value['child_source_sha256']=file_sha256(path)
    data={'sources':[{'source_id':'C','path':str(path),'sha256':value['child_source_sha256']}],
          'proof_derivations':[value]}
    context=Context();verify_derived_proof_scopes(context,data)
    assert context.outputs[0]['physical_page_count']==3
    assert context.outputs[0]['complete_contract_text']=='not_determined'
    assert context.outputs[0]['qualification_result']=='not_determined'
    value['physical_pages']=[1,2]
    with pytest.raises(EvidenceFailure,match='DERIVED_PROOF_PHYSICAL_SCOPE_INCOMPLETE'):
        verify_derived_proof_scopes(Context(),data)


def proof_manifest(tmp_path):
    value = manifest_with_history(tmp_path)
    path = tmp_path / "synthetic-three-page-proof.pdf"
    with Image.new("RGB", (80, 100), "white") as image:
        image.save(path, save_all=True, append_images=[image, image])
    source = {"source_id": "C", "title": "Synthetic historical proof", "path": str(path),
              "sha256": file_sha256(path), "locator": "document", "kind": "company_material",
              "provenance": "derived_from_bid", "synthetic": True}
    value["sources"].append(source)
    value["source_roles"]["C"] = "provenance_only"
    parent = value["sources"][0]
    binding = derivation()
    binding.update(child_source_sha256=source["sha256"], parent_source_id=parent["source_id"],
                   parent_source_sha256=parent["sha256"], record_id=value["material_records"][0]["record_id"])
    binding["visual_review"]["project_title"] = value["material_records"][0]["fields"]["project_name"]["value"]
    value["proof_derivations"] = [binding]
    value["evidence_selections"] = [{"selection_id": "E1", "source_id": "C", "source_sha256": source["sha256"],
        "title": "Synthetic historical proof", "selection_reason": "Synthetic complete-source conversion test",
        "format": "pdf_pages", "pages": [1, 2, 3]}]
    return value


def test_typed_manifest_keeps_derivation_separate_from_facts_and_qualification(tmp_path):
    value = IntakeManifest.model_validate(proof_manifest(tmp_path))
    assert value.facts == []
    binding, = value.proof_derivations
    assert binding.qualification_result == "not_determined"
    assert binding.project_assignment == "not_made"
    assert binding.parent_locator.status == "declared_unverified_parent_page_range"
    assert value.material_records[0].independently_verified is False


@pytest.mark.parametrize("damage", [
    "unknown_child", "unknown_parent", "child_hash", "parent_hash", "selection_hash", "selection_source",
    "selection_scope", "selection_crop", "selection_rotation", "unknown_record", "child_provenance",
    "parent_provenance", "duplicate_derivation", "duplicate_selection", "parent_promoted", "review_promoted",
    "assignment_promoted", "duplicate_visual_page",
])
def test_typed_manifest_rejects_unbound_or_promoted_proof(tmp_path, damage):
    value = proof_manifest(tmp_path)
    binding = value["proof_derivations"][0]
    selection = value["evidence_selections"][0]
    if damage == "unknown_child":
        binding["child_source_id"] = "MISSING"
    elif damage == "unknown_parent":
        binding["parent_source_id"] = "MISSING"
    elif damage == "child_hash":
        binding["child_source_sha256"] = "0" * 64
    elif damage == "parent_hash":
        binding["parent_source_sha256"] = "0" * 64
    elif damage == "selection_hash":
        selection["source_sha256"] = "0" * 64
    elif damage == "selection_source":
        selection.update(source_id=binding["parent_source_id"], source_sha256=binding["parent_source_sha256"])
    elif damage == "selection_scope":
        selection["pages"] = [1, 2]
    elif damage == "selection_crop":
        selection["crop_box_normalized"] = [0, 0, 0.5, 1]
    elif damage == "selection_rotation":
        selection["rotate_clockwise"] = 90
    elif damage == "unknown_record":
        binding["record_id"] = "MISSING"
    elif damage == "child_provenance":
        value["sources"][-1]["provenance"] = "original"
    elif damage == "parent_provenance":
        value["sources"][0]["provenance"] = "original"
    elif damage == "duplicate_derivation":
        value["proof_derivations"].append(copy.deepcopy(binding))
    elif damage == "duplicate_selection":
        value["evidence_selections"].append(copy.deepcopy(selection))
    elif damage == "parent_promoted":
        binding["parent_locator"]["status"] = "verified"
    elif damage == "review_promoted":
        binding["visual_review"]["status"] = "user_verified"
    elif damage == "assignment_promoted":
        binding["project_assignment"] = "made"
    else:
        binding["visual_review"]["physical_pages"] = [1, 1, 3]
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate(value)


def test_actual_child_hash_is_checked_before_its_pdf_is_opened(tmp_path):
    value = proof_manifest(tmp_path)
    Path(value["sources"][-1]["path"]).write_bytes(b"changed synthetic proof")
    with pytest.raises(EvidenceFailure, match="DERIVED_PROOF_SOURCE_CHANGED"):
        verify_derived_proof_scopes(Context(), value)


def test_n0_checks_actual_complete_scope_before_any_proof_page_conversion(tmp_path, monkeypatch):
    import company_bid.evidence as evidence

    value = proof_manifest(tmp_path)
    value["proof_derivations"][0]["physical_pages"] = [1, 2]
    value["proof_derivations"][0]["visual_review"]["physical_pages"] = [1, 2]
    value["evidence_selections"][0]["pages"] = [1, 2]
    IntakeManifest.model_validate(value)
    def must_not_render(*args, **kwargs):
        pytest.fail("Complete physical scope must be checked before rendering")
    monkeypatch.setattr(evidence, "_png", must_not_render)
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value})
    state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "failed" and state["error_code"] == "DERIVED_PROOF_PHYSICAL_SCOPE_INCOMPLETE"
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    units = state["nodes"][0]["units"]
    assert not any(key.startswith("proof-") for key in units)
    scope, = [unit for key, unit in units.items() if key.startswith("verify-proof-scope-")]
    assert scope["status"] == "failed" and "output_ref" not in scope


@pytest.mark.parametrize("source_index", [0, -1], ids=["parent", "child"])
def test_n0_freezes_both_parent_and_child_hashes_before_scope_verification(tmp_path, source_index):
    value = proof_manifest(tmp_path)
    Path(value["sources"][source_index]["path"]).write_bytes(b"changed synthetic original")
    executor = build_executor(tmp_path / "runtime", {"pipeline": "automatic", "provider": {"mode": "prepared"}})
    rid = executor.create_run({"manifest": value})
    state = executor.execute(rid, stop_after="N0")
    assert state["status"] == "failed" and state["error_code"] == "SOURCE_HASH_MISMATCH"
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    assert not any(key.startswith(("proof-", "verify-proof-scope-")) for key in state["nodes"][0]["units"])


def test_n0_scope_checkpoint_trace_and_successful_pages_survive_conversion_resume(tmp_path, monkeypatch):
    import company_bid.evidence as evidence

    original = evidence._png
    calls = []
    def interrupt_second_page(image):
        calls.append(1)
        if len(calls) == 2:
            raise EvidenceFailure("SYNTHETIC_RENDER_INTERRUPTION")
        return original(image)
    monkeypatch.setattr(evidence, "_png", interrupt_second_page)
    root = tmp_path / "runtime"
    settings = {"pipeline": "automatic", "provider": {"mode": "prepared"}}
    executor = build_executor(root, settings)
    rid = executor.create_run({"manifest": proof_manifest(tmp_path)})
    failed = executor.execute(rid, stop_after="N0")
    assert failed["error_code"] == "SYNTHETIC_RENDER_INTERRUPTION"
    successful = {key: copy.deepcopy(unit) for key, unit in failed["nodes"][0]["units"].items()
                  if unit["status"] == "succeeded"}
    scope_id, = [key for key in successful if key.startswith("verify-proof-scope-")]
    scope = successful[scope_id]
    scope_output = executor.store.read_json(scope["output_ref"])["data"]
    assert scope_output["physical_page_count"] == 3
    assert scope_output["complete_physical_source"] is True
    assert scope_output["complete_contract_text"] == scope_output["qualification_result"] == "not_determined"
    assert len([key for key in successful if key.startswith("proof-")]) == 1

    executor = build_executor(root, settings)
    resumed = executor.resume(rid, stop_after="N0")
    assert resumed["status"] == "paused_debug", resumed.get("error_code")
    assert len(calls) == 4
    for key, prior in successful.items():
        current = resumed["nodes"][0]["units"][key]
        assert current["output_ref"] == prior["output_ref"]
        assert current["attempts"] == prior["attempts"]
    output = executor.store.read_json(resumed["nodes"][0]["output_ref"])["data"]
    assert [(item["page_index"], item["page_count"]) for item in output["manifest"]["attachments"]] == [(1, 3), (2, 3), (3, 3)]
    directory = executor.store.run_dir(rid)
    spans = [json.loads(line) for line in (directory / "spans.jsonl").read_text().splitlines()]
    attempt = scope["attempts"][0]
    assert any(span["span_id"] == attempt["span_id"] and span["trace_id"] == attempt["trace_id"]
               and span["name"] == "unit.N0" for span in spans)
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "unit.reused" and event.get("unit_id") == scope_id for event in events)
    diagnostics = inspect_diagnostics(root, rid)
    assert diagnostics["actual_call_attempts"] == 0
    assert diagnostics["gaps"] == []
