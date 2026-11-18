"""Check the explicit v2 commitment prefix against unchanged window evidence."""
import copy
from types import SimpleNamespace

import pytest

from company_bid import commitment_drafting as commitments
from company_bid.extraction import _index, _merge_results, _windows, extraction_window_payload
from company_bid.provider import ProviderFailure
from company_bid.storage import digest
from test_numbered_procedure_spans import extract


@pytest.fixture
def prefix():
    new_extracted, parsed, manifest, _ = extract()
    old_extracted = copy.deepcopy(new_extracted)
    old_extracted.pop("source_clause_repairs")
    old_extracted["requirements"], old_extracted["format_chapters"] = _merge_results(old_extracted["window_results"])
    inherited = {"parsed": parsed, "manifest": manifest, "proposals": {}, "recorded_candidates": {},
                 "input_kind": "automatic", "source_artifacts": []}
    old_n2, new_n2 = ({**inherited, "extracted": value} for value in (old_extracted, new_extracted))
    artifacts, original, current = {}, {}, {}

    def ref(producer, name, content):
        path = f"runs/{producer * 32}/artifacts/{name}.json"
        artifacts[path] = content
        return {"path": path}

    def node(producer, key, input_hash, data):
        out_hash = digest(data)
        output_ref = ref(producer, key, {"data": data, "output_hash": out_hash, "input_hash": input_hash, "node_key": key})
        return {"key": key, "status": "succeeded", "input_hash": input_hash, "output_ref": output_ref,
                "output_hash": out_hash, "units": {}}

    original["N1"] = current["N1"] = node("1", "N1", "1" * 64, inherited)
    original["N2"] = node("1", "N2", original["N1"]["output_hash"], old_n2)
    current["N2"] = node("2", "N2", current["N1"]["output_hash"], new_n2)
    original["N3"] = node("3", "N3", original["N2"]["output_hash"], {"prepared": "original"})
    current["N3"] = node("4", "N3", current["N2"]["output_hash"], {"prepared": "current"})
    blocks, _, documents = _index(parsed, manifest)
    windows, _ = _windows(documents, blocks, old_extracted["window_config"])
    for position, (window, result) in enumerate(zip(windows, old_extracted["window_results"])):
        uid = window["window_id"]
        window_input = extraction_window_payload(window, old_extracted["window_config"])
        for producer, n2 in (("1", original["N2"]), ("2", current["N2"])):
            row = {"unit_id": uid, "status": "succeeded", "version": "unchanged-window-contract",
                   "input_hash": digest(window_input), "output_hash": digest(result)}
            row["input_ref"] = ref(producer, f"window-input-{position}", copy.deepcopy(window_input))
            envelope = {"schema_version": "m1.checkpoint.v1", "node_key": "N2", "run_id": producer * 32,
                        "unit_id": uid, "producer_version": row["version"], "input_hash": row["input_hash"],
                        "output_hash": row["output_hash"], "data": copy.deepcopy(result)}
            row["output_ref"] = ref(producer, f"window-output-{position}", envelope)
            n2["units"][uid] = row

    def artifact(reference):
        return artifacts[reference["path"]]

    def checkpoint(record, key):
        value = artifact(record["output_ref"])
        assert value["node_key"] == key and value["input_hash"] == record["input_hash"]
        assert digest(value["data"]) == value["output_hash"] == record["output_hash"]
        return value

    origin = SimpleNamespace(node=lambda key: original[key], artifact=artifact, checkpoint=checkpoint)
    spec = {"version": commitments.REVIEW_REBIND_V2,
            "origin_n2_output_sha256": original["N2"]["output_hash"], "current_n2_output_sha256": current["N2"]["output_hash"],
            "origin_n3_output_sha256": original["N3"]["output_hash"], "current_n3_output_sha256": current["N3"]["output_hash"]}
    return SimpleNamespace(origin=origin, original=original, current=current, spec=spec, checkpoint=checkpoint,
                           old_n2=old_n2, new_n2=new_n2)


def verify(prefix):
    return commitments._review_rebind_n2(prefix.origin, prefix.spec, prefix.current,
                                         prefix.original["N3"], prefix.current["N3"])


def bind_current_modified_n2(prefix):
    record = prefix.current["N2"]
    new_hash = digest(prefix.new_n2)
    prefix.origin.artifact(record["output_ref"])["output_hash"] = new_hash
    prefix.spec["current_n2_output_sha256"] = record["output_hash"] = prefix.current["N3"]["input_hash"] = new_hash


def test_v2_proves_identical_frozen_windows_and_the_repaired_merge(prefix):
    untouched = copy.deepcopy((prefix.old_n2, prefix.new_n2))
    proof = verify(prefix)
    assert proof["origin_n2_output_ref"] == prefix.original["N2"]["output_ref"]
    assert proof["current_n2_output_ref"] == prefix.current["N2"]["output_ref"]
    assert proof["n1_output_ref"] == prefix.current["N1"]["output_ref"]
    assert proof["n2_source_clause_repair_sha256"] == prefix.new_n2["extracted"]["source_clause_repairs"][0]["proof_sha256"]
    assert proof["n2_window_count"] == 1
    assert proof["n2_window_results_sha256"] == digest(prefix.old_n2["extracted"]["window_results"])
    assert (prefix.old_n2, prefix.new_n2) == untouched


@pytest.mark.parametrize("damage", ["origin_hash", "current_hash", "n2_to_n3", "n1", "window_set", "unit_input", "unit_producer"])
def test_v2_rejects_changed_source_or_checkpoint_bindings(prefix, damage):
    if damage in ("origin_hash", "current_hash"):
        prefix.spec[damage.split("_")[0] + "_n2_output_sha256"] = "f" * 64
    elif damage == "n2_to_n3":
        prefix.current["N3"]["input_hash"] = "f" * 64
    elif damage == "n1":
        prefix.current["N1"] = {**prefix.current["N1"], "output_ref": {"path": "other-physical-source"}}
    else:
        uid, = prefix.current["N2"]["units"]
        unit = prefix.current["N2"]["units"][uid]
        if damage == "window_set":
            del prefix.current["N2"]["units"][uid]
        elif damage == "unit_input":
            prefix.origin.artifact(unit["input_ref"])["task_instructions"] += " altered"
        else:
            prefix.origin.artifact(unit["output_ref"])["run_id"] = "f" * 32
    with pytest.raises(ProviderFailure, match="REVIEW_REBIND_(SOURCE_CHANGED|N2_)"):
        verify(prefix)


@pytest.mark.parametrize("damage", ["other_requirement", "proof", "coverage", "candidate", "extra_payload"])
def test_v2_rejects_other_changes_even_when_the_current_hash_is_explicitly_pinned(prefix, damage):
    extracted = prefix.new_n2["extracted"]
    if damage == "other_requirement":
        extracted["requirements"][1]["text"] += " changed"
    elif damage == "proof":
        extracted["source_clause_repairs"][0]["proof_sha256"] = "f" * 64
    elif damage == "coverage":
        extracted["coverage"]["tender_unit_count"] += 1
    elif damage == "candidate":
        extracted["window_results"][0]["raw_model_candidate"]["requirements"][0]["citation"]["block_ids"] = ["T2"]
    else:
        prefix.new_n2["recorded_candidates"] = {"other": "now changed"}
    bind_current_modified_n2(prefix)
    with pytest.raises(ProviderFailure, match="REVIEW_REBIND_N2_PROJECTION_CHANGED"):
        verify(prefix)


def test_v1_still_rejects_even_the_supported_change_to_n2(prefix):
    spec = {key: value for key, value in prefix.spec.items() if "n2" not in key}
    spec["version"] = commitments.REVIEW_REBIND_V1
    ctx = SimpleNamespace(execution_config={"pipeline": "automatic"}, _state={"nodes": list(prefix.current.values())})
    with pytest.raises(ProviderFailure, match="REVIEW_REBIND_SOURCE_CHANGED"):
        commitments._review_rebind_origin(ctx, prefix.origin, {"source_rebind": spec}, prefix.current["N3"],
                                          prefix.original["N3"], {})


def test_v2_checks_the_changed_plan_metadata_without_removing_any_writing_input():
    plan = {"requirements_sha256": "a" * 64, "tasks": {"one": ["existing bindings"]}, "sources": ["existing source"]}
    next_plan = {**plan, "requirements_sha256": "b" * 64}
    old = {"plan": plan, "plan_sha256": digest(plan)}
    new = {"plan": next_plan, "plan_sha256": digest(next_plan)}
    proof = commitments._review_constraint_rebind(old, new)
    original = {"project_constraint_plan_sha256": old["plan_sha256"], "context": {"text": "unchanged prompt"},
                "project_constraint_artifact_refs": {"N3": "origin"}}
    current = {**original, "project_constraint_plan_sha256": new["plan_sha256"],
               "project_constraint_artifact_refs": {"N3": "new"}}
    assert commitments._same_writing_input_after_clause_repair(original, current, proof)
    assert not commitments._same_writing_input(original, current)
    assert not commitments._same_writing_input_after_clause_repair(original, {**current, "context": {"text": "changed"}}, proof)
    with pytest.raises(ProviderFailure, match="REVIEW_REBIND_CONSTRAINT_PLAN_CHANGED"):
        commitments._same_writing_input_after_clause_repair(
            original, {**current, "project_constraint_plan_sha256": "e" * 64}, proof)
    next_plan["tasks"] = {"one": ["different binding"]}
    new["plan_sha256"] = digest(next_plan)
    with pytest.raises(ProviderFailure, match="REVIEW_REBIND_CONSTRAINT_PLAN_CHANGED"):
        commitments._review_constraint_rebind(old, new)


def test_v2_does_not_extend_allowed_execution_config_changes():
    binding = {"source_rebind": {"version": commitments.REVIEW_REBIND_V2}}
    old = {"pipeline": "automatic", "provider": {"model": "frozen"}}
    new = {**old, "planning": {"semantic_material_recall": {"enabled": True}}}
    assert commitments._review_config_rebind(old, new, binding)["allowed_config_path"] == "planning.semantic_material_recall"
    with pytest.raises(ProviderFailure, match="REVIEW_ORIGIN_CONFIG_CHANGED"):
        commitments._review_config_rebind(old, {**new, "provider": {"model": "changed"}}, binding)
