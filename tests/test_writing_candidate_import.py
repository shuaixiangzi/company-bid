"""Synthetic pinned packets only: this suite never reads private bid artifacts."""
from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest

from company_bid import writing_candidate_import as imports
from company_bid.models import DocumentPackage, PreparedBid, Section
from company_bid.provider import ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest
from test_writing_replay import synthetic_prepared, provider_options, candidate


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def packet(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Offline candidate import tried to use a network/provider")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(ChatProposalProvider, "generate_json", no_network)
    options = {**provider_options(), "mode": imports.MODE}
    old = synthetic_prepared(tmp_path)
    prepared = old.model_dump(mode="json")
    prepared["preparation_note"] = "New diagnostic source-format container; same writing inputs."
    prepared["sections"][0]["start_on_new_page"] = True
    for source in prepared["sources"]:
        path = tmp_path / "new-frozen-source" / Path(source["path"]).name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(Path(source["path"]).read_bytes())
        source["path"] = str(path)
    new = PreparedBid.model_validate(prepared)
    entries = {}
    for section in old.sections:
        for task in section.writing_tasks:
            base = imports.build_import_unit_input(old, section, task, options, None, {})
            correction = None
            if int(task.unit_id.split("-")[-1]) >= 4:
                extra = old.requirements[-1].model_dump(mode="json")
                correction = {"instruction": "Synthetic correction; retain the source scope.",
                    "review_feedback": "Use only the synthetic scope; do not invent facts.",
                    "prior_raw_candidate": candidate(), "semantic_status": "requires_source_review",
                    "source_scope": {"role": "readonly_frozen_tender_requirements", "requirements": [extra],
                        "sources": [{"source_id": "ET", "source_sha256": old.sources[-1].sha256,
                                     "locator": old.sources[-1].locator}]}}
                base["writing_correction"] = correction
            entries[task.unit_id] = {"candidate": candidate(), "candidate_sha256": digest(candidate()),
                "authenticated_input": base, "writing_task": task.model_dump(mode="json"), "correction_context": correction,
                "origin_run_id": "synthetic-origin", "unit_input_hash": digest(base), "unit_output_hash": "2" * 64,
                "request_sha256": "3" * 64, "response_sha256": "4" * 64,
                "call_id": "5" * 32, "trace_id": "6" * 32, "span_id": "7" * 16,
                "historical_usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                "new_external_calls": 0}
    folder = tmp_path / "export" / "writing-origin-snapshots"
    evidence = []
    for index in range(87):
        raw = json.dumps({"synthetic_snapshot": index}).encode()
        sha = hashlib.sha256(raw).hexdigest()
        target = folder / (sha + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        evidence.append({"run_id": "synthetic-origin", "source_relative_path": f"synthetic/{index}.json",
                         "sha256": sha, "snapshot": "synthetic/writing-origin-snapshots/" + target.name})
    payload = {"schema_version": imports.PACKET_SCHEMA, "diagnostic_only": True,
        "frozen_authentication_code": imports.AUTHENTICATION_CODE, "current_validation_required": True,
        "actual_new_model_calls": 0, "entries": entries, "evidence": evidence,
        "original_prepared": old.model_dump(mode="json"), "original_prepared_sha256": digest(old.model_dump(mode="json")),
        "source_review": "synthetic-review.json", "source_review_sha256": "8" * 64}
    path = tmp_path / "export" / "packet.json"
    config = {"schema_version": imports.SCHEMA_VERSION, "packet": {"path": str(path), "sha256": save(path, payload)}}
    return {"path": path, "config": config, "payload": payload, "prepared": new.model_dump(mode="json"),
            "options": options, "tmp": tmp_path}


def repin(packet):
    packet["config"]["packet"]["sha256"] = save(packet["path"], packet["payload"])


def run(packet, *, purpose="diagnostic_replay", capture=True, interrupt=False, mutate_base=None,
        proposals=None, n5_damage=None, after_preflight=None):
    config = {"pipeline": "prepared", "provider": packet["options"], "writing_candidate_import": packet["config"],
              "run_purpose": purpose, "debug_capture": capture}
    holder = {"provider": imports.DiagnosticWritingCandidateImport(packet["options"], packet["config"]),
              "interrupt": interrupt, "generated": 0}

    def n4(ctx, value):
        prepared = PreparedBid.model_validate(value["prepared"])
        provider = holder["provider"]
        bases = {task.unit_id: imports.build_import_unit_input(prepared, section, task, packet["options"], None, value["proposals"])
                 for section in prepared.sections for task in section.writing_tasks}
        if mutate_base is not None:
            mutate_base(bases)
        provider.preflight(ctx, value["prepared"], bases)
        if after_preflight is not None:
            after_preflight(provider, ctx)
        outputs, reviews, sections = {}, [], []
        for section in prepared.sections:
            blocks = []
            for task in section.writing_tasks:
                base = bases[task.unit_id]
                unit_input = provider.unit_input(task.unit_id, base)
                def write(base=base, task=task, section=section, unit_input=unit_input):
                    proposal = provider.generate(ctx, base["context"], base["requirements"])
                    holder["generated"] += 1
                    return {"blocks": imports.expected_import_output(prepared, section, task, unit_input, proposal),
                            "review": imports.expected_import_review(section, task, unit_input, proposal, None)}
                output = ctx.run_unit(task.unit_id, unit_input, write)
                outputs[task.unit_id] = output
                blocks.extend(output["blocks"])
                reviews.append(output["review"])
            sections.append(Section(section_id=section.section_id, title=section.title, level=section.level,
                document_role=section.document_role, render_body=section.render_body,
                requirement_refs=section.requirement_refs, blocks=blocks))
        if holder["interrupt"]:
            holder["interrupt"] = False
            raise RuntimeError("Synthetic interruption after all durable candidate units")
        package = DocumentPackage(**prepared.model_dump(exclude={"sections", "tables", "input_kind"}),
                                  title="Synthetic diagnostic", sections=sections)
        return {"package": package.model_dump(mode="json"), "writing_review": reviews,
                "summary": provider.summary(), "outputs": outputs}

    def n5(ctx, value):
        value = copy.deepcopy(value)
        if n5_damage is not None:
            n5_damage(value)
        package = DocumentPackage.model_validate(value["package"])
        links = imports.validate_candidate_import_links(ctx, value, package)
        return {**value, "links": links}

    executor = Executor(packet["tmp"] / "runtime", [NodeSpec("N3", "synthetic-n3", lambda ctx, value: value, ()),
        NodeSpec("N4", "synthetic-import", n4, ("provider", "writing_candidate_import", "run_purpose", "debug_capture")),
        NodeSpec("N5", "synthetic-import-check", n5, ("provider", "writing_candidate_import"))], config)
    rid = executor.create_run({"prepared": packet["prepared"], "proposals": proposals or {}})
    state = executor.execute(rid)
    return executor, rid, state, holder


def test_changed_container_and_frozen_paths_import_nine_raw_candidates_with_five_extra_scopes(packet):
    executor, rid, state, holder = run(packet)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(rid, "N5")
    assert holder["generated"] == 9 and len(output["links"]) == 5
    assert output["summary"]["imported_count"] == 9
    assert output["summary"]["new_external_calls"] == 0
    assert not state.get("call_budget", {}).get("reservations")
    assert len(output["summary"]["historical_evidence"]) == 87
    corrected = output["outputs"]["WRITE-4"]
    assert all("ET" in block["source_refs"] for block in corrected["blocks"])
    assert corrected["review"]["correction_context_requirement_refs"] == ["R-CONTEXT"]
    assert corrected["review"]["semantic_status"] == "requires_source_review"
    spans = [json.loads(line) for line in (packet["tmp"] / "runtime" / "runs" / rid / "spans.jsonl").read_text().splitlines()]
    imported = [item for item in spans if item.get("name") == "candidate.offline_import"]
    assert len(imported) == 9 and all(item["links"] for item in imported)


@pytest.mark.parametrize("damage", ["packet", "snapshot", "last_snapshot", "missing_snapshot", "candidate", "coverage", "missing_unit",
    "extra_unit", "missing_context", "changed_context", "manifest", "escape", "source_file", "extra_source", "task", "authentication_code"])
def test_preflight_rejects_tampering_or_changed_source_before_any_unit_is_generated(packet, damage):
    payload = packet["payload"]
    if damage == "packet":
        packet["path"].write_bytes(packet["path"].read_bytes() + b" ")
    elif damage in {"snapshot", "last_snapshot", "missing_snapshot"}:
        index = -1 if damage == "last_snapshot" else 0
        path = packet["path"].parent / "writing-origin-snapshots" / Path(payload["evidence"][index]["snapshot"]).name
        path.write_bytes(b"tampered") if damage != "missing_snapshot" else path.unlink()
    elif damage == "source_file":
        Path(packet["prepared"]["sources"][0]["path"]).write_text("changed source", encoding="utf-8")
    elif damage == "extra_source":
        source = copy.deepcopy(packet["prepared"]["sources"][0]); source["source_id"] = "UNDECLARED"
        packet["prepared"]["sources"].append(source)
        packet["prepared"]["requirements"][0]["source_refs"].append("UNDECLARED")
    elif damage == "task":
        packet["prepared"]["sections"][0]["writing_tasks"][0]["objective"] += " Changed scope."
    else:
        entry = payload["entries"]["WRITE-4"]
        if damage == "candidate":
            entry["candidate"]["paragraphs"][0] += " Invented content."
        elif damage == "coverage":
            entry["candidate"]["coverage"] = []
            entry["candidate_sha256"] = digest(entry["candidate"])
        elif damage == "missing_unit":
            del payload["entries"]["WRITE-0"]
        elif damage == "extra_unit":
            payload["entries"]["UNDECLARED"] = copy.deepcopy(entry)
        elif damage == "missing_context":
            entry["correction_context"] = None
        elif damage == "changed_context":
            entry["correction_context"]["source_scope"]["requirements"][0]["text"] += " Forged scope."
            entry["authenticated_input"]["writing_correction"] = copy.deepcopy(entry["correction_context"])
            entry["unit_input_hash"] = digest(entry["authenticated_input"])
        elif damage == "manifest":
            payload["evidence"].pop()
        elif damage == "escape":
            payload["evidence"][0]["snapshot"] = "../../escape.json"
        elif damage == "authentication_code":
            payload["frozen_authentication_code"] = "unsupported-other-export"
        repin(packet)
    executor, rid, state, holder = run(packet)
    assert state["status"] == "failed"
    assert holder["generated"] == 0
    assert not state.get("call_budget", {}).get("reservations")
    n4 = next(node for node in state["nodes"] if node["key"] == "N4")
    assert not n4["units"]


def test_current_base_and_prepared_proposals_cannot_change_historical_inputs(packet):
    _, _, state, holder = run(packet, proposals={"WRITE-0": candidate()})
    assert state["status"] == "failed" and holder["generated"] == 0


def test_provider_model_change_cannot_be_hidden_as_an_offline_rebinding(packet):
    packet["options"]["model"] = "changed-model"
    _, _, state, holder = run(packet)
    assert state["status"] == "failed" and holder["generated"] == 0


def test_unrelated_format_source_is_not_promoted_into_the_writer_scope(packet):
    source = copy.deepcopy(packet["prepared"]["sources"][0]); source["source_id"] = "NEW-FORMAT-ONLY"
    packet["prepared"]["sources"].append(source)
    executor, rid, state, _ = run(packet)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(rid, "N5")
    assert all("NEW-FORMAT-ONLY" not in block["source_refs"] for item in output["outputs"].values() for block in item["blocks"])


def test_base_input_cannot_be_self_reported_instead_of_rebuilt_from_n3(packet):
    def change(bases):
        bases["WRITE-0"]["context"]["title"] = "Forged current task"
    _, _, state, holder = run(packet, mutate_base=change)
    assert state["status"] == "failed" and holder["generated"] == 0


@pytest.mark.parametrize("purpose,capture", [("production", True), ("diagnostic_replay", False)])
def test_import_is_diagnostic_and_requires_private_capture(packet, purpose, capture):
    _, _, state, holder = run(packet, purpose=purpose, capture=capture)
    assert state["status"] == "failed" and holder["generated"] == 0


def test_source_mutation_after_preflight_is_caught_without_http(packet):
    def change(provider, ctx):
        Path(packet["prepared"]["sources"][0]["path"]).write_text("Changed after seal", encoding="utf-8")
    _, _, state, holder = run(packet, after_preflight=change)
    assert state["status"] == "failed" and holder["generated"] == 0


def test_resume_reuses_current_successful_checkpoints_and_does_not_regenerate_or_charge(packet):
    executor, rid, state, holder = run(packet, interrupt=True)
    assert state["status"] == "failed" and holder["generated"] == 9
    holder["provider"] = imports.DiagnosticWritingCandidateImport(packet["options"], packet["config"])
    state = executor.execute(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    output = executor.output(rid, "N5")
    assert holder["generated"] == 9
    assert output["summary"]["imported_count"] == output["summary"]["checkpoint_reuse_count"] == 9
    assert output["summary"]["imported_this_attempt_count"] == 0
    assert not state.get("call_budget", {}).get("reservations")


@pytest.mark.parametrize("damage", ["review", "extra_source", "paragraph", "coverage"])
def test_n5_rebuilds_candidate_and_extra_context_links_instead_of_trusting_n4_report(packet, damage):
    def change(value):
        if damage == "review":
            value["writing_review"][4]["correction_source_scope_sha256"] = "0" * 64
        elif damage == "coverage":
            value["writing_review"][0]["coverage"] = []
        else:
            section = next(s for s in value["package"]["sections"] if s["section_id"] == "TECH")
            if damage == "extra_source":
                block = next(b for b in section["blocks"] if "ET" in b["source_refs"])
                block["source_refs"].remove("ET")
            else:
                block = next(b for b in section["blocks"] if b["kind"] == "paragraph")
                block["text"] += " Changed after candidate validation."
    _, _, state, holder = run(packet, n5_damage=change)
    assert state["status"] == "failed" and holder["generated"] == 9
    assert next(node for node in state["nodes"] if node["key"] == "N4")["status"] == "succeeded"


def test_provenance_projection_is_finite_and_keeps_semantic_and_source_hashes():
    from test_project_constraints import task_fixture
    _, _, _, model, _ = task_fixture()
    old = model.model_dump(mode="json")
    new = copy.deepcopy(old)
    new["source_reading_sha256"] = "b" * 64
    source_set = {"schema_version": "project-constraints-v2", **{key: new[key] for key in
        ("source_id", "source_sha256", "source_reading_sha256", "constraints", "unresolved")}}
    new["source_set_sha256"] = digest(source_set)
    new["binding_sha256"] = digest({key: item for key, item in new.items() if key not in {"binding_id", "binding_sha256"}})
    new["binding_id"] = "PJCT-" + new["binding_sha256"][:24]
    base = {"provider": {"mode": imports.MODE}, "context": {"project_constraints": [old]}, "source_sha256": "a" * 64}
    other = copy.deepcopy(base); other["context"]["project_constraints"] = [new]
    assert imports.semantic_footprint(base) == imports.semantic_footprint(other)
    other["source_sha256"] = "c" * 64
    assert imports.semantic_footprint(base) != imports.semantic_footprint(other)
    other = copy.deepcopy(base); other["context"]["project_constraints"][0]["constraints"][0]["instruction"] = "Allow subcontracting"
    with pytest.raises(ValueError):
        imports.semantic_footprint(other)
