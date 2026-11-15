"""Explicit offline import of a pinned, externally authenticated N4 packet.

This is changed-input diagnostic revalidation, never exact wire replay. The
operator pins the frozen authentication export; no live provider is constructed.
Only raw candidates are imported. Today's N4 assembler builds every block.
This bounded v57 adapter supports only the c131ded 9/5/87 packet contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .models import (HeadingBlock, ListBlock, ParagraphBlock, PreparedBid,
                     ProposalContent, TableBlock, file_sha256)
from .project_constraint_runtime import load_project_constraints, validate_project_constraint_context
from .project_constraints import TaskProjectConstraints
from .provider import ProviderFailure, validate_writing_context, validate_writing_output
from .storage import digest
from .writing_replay import build_writing_unit_input


MODE = "diagnostic_writing_candidate_import"
SCHEMA_VERSION = "diagnostic-writing-candidate-import-v1"
PACKET_SCHEMA = "private-authenticated-writing-packet-v1"
AUTHENTICATION_CODE = "c131ded"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_BINDING_PROVENANCE = {"binding_id", "binding_sha256", "source_reading_sha256", "source_set_sha256"}


def _require(condition, code):
    if not condition:
        raise ProviderFailure("WRITING_IMPORT_" + code)


def _capture(ctx, name, data):
    result = ctx.capture(name, data)
    _require(isinstance(result, dict) and result.get("status") == "captured", "CAPTURE_FAILED")


def import_provider_options(options):
    """Normalize only the explicit import mode for the historical comparison."""
    result = copy.deepcopy(options)
    _require(result.get("mode") in {MODE, "chat_completions", "diagnostic_writing_commitment_partition"}, "PROVIDER_INVALID")
    result["mode"] = "chat_completions"
    return result


def build_import_unit_input(prepared, section, task, options, project_constraints, proposals):
    return build_writing_unit_input(prepared, section, task, import_provider_options(options),
                                   project_constraints, proposals)


def semantic_footprint(base):
    """Remove just independently rebuilt provenance, never text or source hashes.

    Callers must first authenticate both inputs and rebuild the current project
    plan from N1/N2/N3. Source-context proofs remain exact: their proof_ref values
    are source proof IDs, not filesystem locations, in this bounded v57 import.
    """
    value = copy.deepcopy(base)
    value["provider"] = import_provider_options(value["provider"])
    value.pop("writing_correction", None)
    value.pop("project_constraint_artifact_refs", None)
    value.pop("project_constraint_plan_sha256", None)
    bindings = value["context"].get("project_constraints", [])
    for index, binding in enumerate(bindings):
        checked = TaskProjectConstraints.model_validate(binding).model_dump(mode="json")
        bindings[index] = {key: item for key, item in checked.items() if key not in _BINDING_PROVENANCE}
    return value


def _scope(entry, prepared):
    old = entry["correction_context"]
    if old is None:
        return None
    _require(set(old) == {"instruction", "prior_raw_candidate", "review_feedback", "source_scope", "semantic_status"}
             and old["semantic_status"] == "requires_source_review", "CORRECTION_INVALID")
    _require(isinstance(old["instruction"], str) and bool(old["instruction"].strip())
             and isinstance(old["review_feedback"], str) and 0 < len(old["review_feedback"].strip()) <= 12000
             and ProposalContent.model_validate(old["prior_raw_candidate"]).model_dump(mode="json") == old["prior_raw_candidate"],
             "CORRECTION_INVALID")
    old_scope = old["source_scope"]
    _require(set(old_scope) == {"role", "requirements", "sources"}
             and old_scope["role"] == "readonly_frozen_tender_requirements", "CORRECTION_INVALID")
    refs = [item["requirement_id"] for item in old_scope["requirements"]]
    requirements = {item.requirement_id: item for item in prepared.requirements}
    sources = {item.source_id: item for item in prepared.sources}
    _require(bool(refs) and len(refs) == len(set(refs)) and set(refs) <= set(requirements), "CORRECTION_CONTEXT_MISSING")
    selected = [requirements[ref].model_dump(mode="json") for ref in refs]
    source_ids = list(dict.fromkeys(ref for item in selected for ref in item["source_refs"]))
    _require(all(ref in sources and sources[ref].kind == "tender" for ref in source_ids), "CORRECTION_SOURCE_INVALID")
    current = {"role": "readonly_frozen_tender_requirements", "requirements": selected,
               "sources": [{"source_id": ref, "source_sha256": sources[ref].sha256,
                            "locator": sources[ref].locator} for ref in source_ids]}
    _require(current == old_scope, "CORRECTION_CONTEXT_CHANGED")
    result = copy.deepcopy(old)
    result["source_scope"] = current
    return result


def expected_import_output(prepared, section, task, base, candidate):
    """Independently reconstruct N5's candidate/trace links from current N3."""
    context, requirements = base["context"], base["requirements"]
    references = context["reference_excerpts"]
    source_contexts = context.get("source_contexts", [])
    scope = base.get("writing_correction", {}).get("source_scope")
    extra = [item["source_id"] for item in scope["sources"]] if scope else []
    if "commitment_drafting" in base:
        from .commitment_drafting import commitment_content_source_refs
        extra.extend(commitment_content_source_refs(base))
    project_refs = list(dict.fromkeys(item["source_id"] for item in context.get("project_constraints", [])))
    refs = list(dict.fromkeys([*(item["source_id"] for item in references),
        *(ref for item in requirements for ref in item["source_refs"]),
        *(ref for item in source_contexts for ref in item["source_refs"]), *project_refs, *extra]))
    blocks = [HeadingBlock(text=task.title, level=section.level + 1,
                           requirement_refs=task.requirement_refs, source_refs=refs)]
    blocks.extend(ParagraphBlock(text=text, content_role="proposal", requirement_refs=task.requirement_refs,
                                 source_refs=refs) for text in candidate.paragraphs)
    if candidate.steps:
        blocks.append(ListBlock(items=candidate.steps, content_role="proposal", requirement_refs=task.requirement_refs,
                                source_refs=refs))
    tables = {item.table_id: item for item in prepared.tables}
    blocks.extend(TableBlock(table=tables[ref], requirement_refs=task.requirement_refs, source_refs=extra)
                  for ref in task.table_refs)
    return [item.model_dump(mode="json") for item in blocks]


def expected_import_review(section, task, base, candidate, provenance):
    context = base["context"]
    contexts = context.get("source_contexts", [])
    review = {"unit_id": task.unit_id, "section_id": section.section_id, "document_role": section.document_role,
        "title": task.title, "operation": task.operation, "requirement_refs": task.requirement_refs,
        "reference_refs": task.reference_refs, "must_cover": task.must_cover, "completion_criteria": task.completion_criteria,
        "coverage": [item.model_dump(mode="json") for item in candidate.coverage],
        "source_context_refs": task.source_context_refs, "source_context_proof_hashes": base["source_context_proof_hashes"],
        "project_constraint_bindings": validate_project_constraint_context(task.model_dump(mode="json"), context, provenance),
        "project_constraint_source_refs": list(dict.fromkeys(item["source_id"] for item in context.get("project_constraints", []))),
        "context_review_status": "not_checked" if contexts else "not_applicable", "semantic_status": "requires_source_review"}
    scope = base.get("writing_correction", {}).get("source_scope")
    if scope is not None:
        review.update(correction_context_requirement_refs=[item["requirement_id"] for item in scope["requirements"]],
                      correction_source_scope_sha256=digest(scope),
                      correction_source_refs=[item["source_id"] for item in scope["sources"]])
    return review


class DiagnosticWritingCandidateImport:
    def __init__(self, provider_options, config, *, commitment_partition=None):
        self.options, self.config = copy.deepcopy(provider_options), copy.deepcopy(config)
        self.commitment_partition = copy.deepcopy(commitment_partition)
        expected_mode = MODE if commitment_partition is None else "diagnostic_writing_commitment_partition"
        _require(self.options.get("mode") == expected_mode, "PROVIDER_INVALID")
        _require(isinstance(config, dict) and set(config) == {"schema_version", "packet"}
                 and config["schema_version"] == SCHEMA_VERSION and isinstance(config["packet"], dict)
                 and set(config["packet"]) == {"path", "sha256"}
                 and isinstance(config["packet"]["path"], str)
                 and Path(config["packet"]["path"]).is_absolute()
                 and isinstance(config["packet"]["sha256"], str)
                 and bool(_HEX.fullmatch(config["packet"]["sha256"])), "CONFIG_INVALID")
        self._ready, self._entries, self._inputs, self._files = None, {}, {}, {}
        self._summary = {"mode": MODE, "diagnostic_only": True, "semantic_status": "requires_source_review",
                         "new_external_calls": 0, "historical_calls_charged_again": 0,
                         "packet_support": "v57_c131ded_9_units_5_corrections_87_snapshots_only"}

    def summary(self):
        return copy.deepcopy(self._summary)

    def _read(self, path, expected):
        path = Path(path).absolute()
        resolved = path.resolve(strict=True)
        raw = resolved.read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == expected, "FILE_HASH_MISMATCH")
        _require(path not in self._files or self._files[path] == (resolved, expected), "FILE_IDENTITY_CHANGED")
        self._files[path] = (resolved, expected)
        return raw

    def _seal(self):
        for path, (resolved, expected) in self._files.items():
            _require(path.resolve(strict=True) == resolved and file_sha256(path) == expected, "FILE_CHANGED")

    def _packet(self):
        path = Path(self.config["packet"]["path"]).absolute()
        raw = self._read(path, self.config["packet"]["sha256"])
        packet = json.loads(raw)
        _require(packet.get("schema_version") == PACKET_SCHEMA and packet.get("diagnostic_only") is True
                 and packet.get("frozen_authentication_code") == AUTHENTICATION_CODE
                 and packet.get("current_validation_required") is True and packet.get("actual_new_model_calls") == 0,
                 "PACKET_INVALID")
        _require(digest(packet["original_prepared"]) == packet["original_prepared_sha256"], "PREPARED_HASH_MISMATCH")
        evidence = packet["evidence"]
        _require(isinstance(evidence, list) and len(evidence) == 87
                 and len({(item["run_id"], item["source_relative_path"]) for item in evidence}) == 87,
                 "SNAPSHOT_MANIFEST_INVALID")
        folder = (path.parent / "writing-origin-snapshots").resolve(strict=True)
        snapshots = []
        for item in evidence:
            _require(isinstance(item["sha256"], str) and bool(_HEX.fullmatch(item["sha256"])), "SNAPSHOT_MANIFEST_INVALID")
            name = Path(item["snapshot"]).name
            suffix = Path(name).suffix
            _require(suffix in {".json", ".jsonl"} and name == item["sha256"] + suffix
                     and item["snapshot"].replace("\\", "/").endswith("/writing-origin-snapshots/" + name),
                     "SNAPSHOT_PATH_INVALID")
            target = (folder / name).resolve(strict=True)
            _require(target.parent == folder, "SNAPSHOT_PATH_INVALID")
            snapshots.append((item, self._read(target, item["sha256"])))
        return packet, raw, snapshots

    def _prepare(self, ctx, prepared_dict, base_inputs):
        _require(ctx.execution_config.get("run_purpose") == "diagnostic_replay" and ctx.artifact_scope == "diagnostic"
                 and ctx.execution_config.get("debug_capture") is True, "PURPOSE_REQUIRED")
        _require(ctx.execution_config.get("writing_candidate_import") == self.config
                 and ctx.execution_config.get("provider") == self.options, "CONFIG_CHANGED")
        nodes = {item["key"]: item for item in ctx._state["nodes"]}
        _require(nodes.get("N3", {}).get("status") == "succeeded", "N3_REQUIRED")
        frozen = ctx.executor._checkpoint_data(nodes["N3"])
        _require(frozen["prepared"] == prepared_dict, "CURRENT_PREPARED_MISMATCH")
        prepared = PreparedBid.model_validate(prepared_dict)
        _require(not any(section.kind == "technical" and not section.writing_tasks
                         and not section.fixed_content and section.render_body for section in prepared.sections),
                 "FALLBACK_UNSUPPORTED")
        provenance = load_project_constraints(ctx, prepared_dict)
        task_map = {task.unit_id: (section, task) for section in prepared.sections for task in section.writing_tasks}
        if self.commitment_partition is not None:
            from .commitment_drafting import checked_commitment_partition
            _require(ctx.execution_config.get("commitment_drafting") == self.commitment_partition, "CONFIG_CHANGED")
            extra = checked_commitment_partition(prepared, self.commitment_partition, provenance)
            # Explicit partition of the actual frozen N3, never a prepared subset.
            task_map = {uid: item for uid, item in task_map.items() if uid not in extra}
        expected = {uid: build_import_unit_input(prepared, section, task, self.options, provenance, frozen["proposals"])
                    for uid, (section, task) in task_map.items()}
        _require(expected == base_inputs, "CURRENT_INPUT_MISMATCH")
        packet, raw, snapshots = self._packet()
        old_prepared = PreparedBid.model_validate(packet["original_prepared"])
        old_tasks = {task.unit_id: (section, task) for section in old_prepared.sections for task in section.writing_tasks}
        entries = packet["entries"]
        _require(len(entries) == len(task_map) == 9 and set(entries) == set(task_map) == set(old_tasks)
                 and sum(item["correction_context"] is not None for item in entries.values()) == 5,
                 "UNIT_PARTITION_INVALID")
        old_sources = {item.source_id: item.model_dump(mode="json", exclude={"path"}) for item in old_prepared.sources}
        current_sources = {item.source_id: item.model_dump(mode="json", exclude={"path"}) for item in prepared.sources}
        used_sources = {item["source_id"] for base in base_inputs.values() for item in base["sources"]}
        for entry in entries.values():
            if entry["correction_context"] is not None:
                used_sources.update(item["source_id"] for item in entry["correction_context"]["source_scope"]["sources"])
        _require(used_sources <= set(old_sources) & set(current_sources)
                 and all(current_sources[ref] == old_sources[ref] for ref in used_sources), "SOURCES_CHANGED")
        for source in prepared.sources:
            if source.source_id in used_sources:
                self._read(source.path, source.sha256)
        proof_map = {item.proof_id: item.model_dump(mode="json") for item in prepared.source_context_proofs}
        old_proofs = {item.proof_id: item.model_dump(mode="json") for item in old_prepared.source_context_proofs}
        used_proofs = {item["proof_ref"] for base in base_inputs.values() for item in base["context"].get("source_contexts", [])}
        for entry in entries.values():
            if entry["correction_context"] is not None:
                used_proofs.update(context["proof_ref"] for item in entry["correction_context"]["source_scope"]["requirements"]
                                   for context in item.get("source_contexts", []))
        _require(used_proofs <= set(proof_map) & set(old_proofs)
                 and all(proof_map[ref] == old_proofs[ref] for ref in used_proofs), "SOURCE_CONTEXT_PROOF_CHANGED")
        manifest_hash = digest(packet["evidence"])
        self._entries, self._inputs = {}, {}
        for uid, entry in entries.items():
            section, task = task_map[uid]
            old_section, old_task = old_tasks[uid]
            _require(task.model_dump(mode="json") == entry["writing_task"] == old_task.model_dump(mode="json")
                     and all(getattr(section, key) == getattr(old_section, key)
                             for key in ("section_id", "level", "document_role", "render_body")), "TASK_CHANGED")
            base, old_base = base_inputs[uid], entry["authenticated_input"]
            _require(old_base["context"]["writing_task"] == entry["writing_task"]
                     and digest(old_base) == entry["unit_input_hash"]
                     and old_base.get("writing_correction") == entry["correction_context"], "ORIGIN_INPUT_INVALID")
            _require(semantic_footprint(base) == semantic_footprint(old_base), "SEMANTIC_INPUT_CHANGED")
            validate_project_constraint_context(task.model_dump(mode="json"), base["context"], provenance)
            candidate = ProposalContent.model_validate(entry["candidate"])
            _require(candidate.model_dump(mode="json") == entry["candidate"]
                     and digest(entry["candidate"]) == entry["candidate_sha256"], "CANDIDATE_CHANGED")
            validate_writing_context(base["context"], base["requirements"])
            validate_writing_output(base["context"], candidate, base["requirements"])
            _require(entry["new_external_calls"] == 0
                     and entry["origin_run_id"] in {item["run_id"] for item in packet["evidence"]}
                     and all(isinstance(entry[key], str) and re.fullmatch(pattern, entry[key])
                             for key, pattern in (("call_id", r"[a-f0-9]{32}"), ("trace_id", r"[a-f0-9]{32}"),
                                                  ("span_id", r"[a-f0-9]{16}")))
                     and all(bool(_HEX.fullmatch(entry[key])) for key in
                             ("unit_input_hash", "unit_output_hash", "request_sha256", "response_sha256")), "ORIGIN_INVALID")
            current = copy.deepcopy(base)
            correction = _scope(entry, prepared)
            if correction is not None:
                current["writing_correction"] = correction
            current["candidate_import"] = {"schema_version": SCHEMA_VERSION, "packet_sha256": self.config["packet"]["sha256"],
                "entry_sha256": digest(entry), "snapshot_manifest_sha256": manifest_hash,
                "historical_unit_input_hash": entry["unit_input_hash"], "current_base_input_hash": digest(base),
                "historical_request_sha256": entry["request_sha256"], "candidate_sha256": entry["candidate_sha256"],
                "source_review_sha256": packet["source_review_sha256"], "semantic_status": "requires_source_review"}
            self._inputs[uid] = current
            self._entries[uid] = {"candidate": candidate, "origin": {key: entry[key] for key in
                ("origin_run_id", "call_id", "trace_id", "span_id", "request_sha256", "response_sha256",
                 "unit_input_hash", "unit_output_hash", "candidate_sha256", "historical_usage")},
                "base": copy.deepcopy(base), "corrected": correction is not None}
        self._seal()
        return prepared, task_map, packet, raw, snapshots

    def preflight(self, ctx, prepared_dict, base_inputs_by_unit_id):
        try:
            _require(ctx.node_key == "N4", "N4_REQUIRED")
            self._ready = None
            _, _, packet, raw, snapshots = self._prepare(ctx, prepared_dict, base_inputs_by_unit_id)
            reused = 0
            for uid, record in ctx._record.get("units", {}).items():
                if uid not in self._inputs or record.get("status") != "succeeded":
                    continue
                _require(ctx.executor.store.read_json(record["input_ref"]) == self._inputs[uid]
                         and record["input_hash"] == digest(self._inputs[uid]), "CHECKPOINT_INPUT_CHANGED")
                ctx.executor._checkpoint_data(record)
                reused += 1
            packet_ref = ctx.store_bytes("writing-import-packet.json", raw, "application/json")
            evidence_refs = [{"origin": item, "snapshot": ctx.store_bytes("writing-import-origin" + Path(item["snapshot"]).suffix,
                              content, "application/json" if item["snapshot"].endswith(".json") else "application/x-ndjson")}
                             for item, content in snapshots]
            self._summary.update(packet_sha256=self.config["packet"]["sha256"], packet_snapshot=packet_ref,
                historical_evidence=evidence_refs, binding_count=9, historical_corrected_count=5,
                imported_count=reused, imported_this_attempt_count=0, checkpoint_reuse_count=reused,
                statistics_scope="completed units; this_attempt excludes checkpoint reuse",
                authentication_boundary="externally_authenticated_c131ded_export_pinned_by_operator_sha256",
                source_review_sha256=packet["source_review_sha256"])
            _capture(ctx, "writing-candidate-import-preflight", self._summary)
            ctx._observer.check()
            self._ready = (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config))
            return self.summary()
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure("WRITING_IMPORT_PREFLIGHT_FAILED") from error

    def unit_input(self, unit_id, base_input):
        _require(self._ready is not None and unit_id in self._inputs, "PREFLIGHT_REQUIRED")
        current = copy.deepcopy(base_input)
        current["provider"] = import_provider_options(current["provider"])
        _require(current == self._entries[unit_id]["base"], "CURRENT_INPUT_MISMATCH")
        return copy.deepcopy(self._inputs[unit_id])

    def generate(self, ctx, context, requirements):
        _require(self._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
        uid = context.get("section_id")
        _require(uid in self._entries, "UNIT_NOT_DECLARED")
        entry = self._entries[uid]
        _require(context == entry["base"]["context"] and requirements == entry["base"]["requirements"], "CURRENT_INPUT_MISMATCH")
        record = ctx._record["units"][uid]
        _require(ctx._active_unit.get("unit_id") == uid
                 and ctx.executor.store.read_json(record["input_ref"]) == self._inputs[uid], "CURRENT_UNIT_INPUT_CHANGED")
        self._seal()
        origin = entry["origin"]
        with ctx._observer.span("candidate.offline_import", {**ctx._metadata(), "operation": MODE,
                "input_hash": digest(self._inputs[uid]), "output_hash": origin["candidate_sha256"]},
                links=[{"trace_id": origin["trace_id"], "span_id": origin["span_id"]}]):
            proposal = ProposalContent.model_validate(entry["candidate"].model_dump(mode="json"))
            validate_writing_context(context, requirements)
            validate_writing_output(context, proposal, requirements)
            _capture(ctx, "writing-candidate-import-" + uid, {"origin": origin,
                "candidate": proposal.model_dump(mode="json"), "binding": self._inputs[uid]["candidate_import"],
                "external_call": False, "diagnostic_only": True})
            ctx.call_event("candidate.revalidated", {"operation": MODE, "input_hash": digest(self._inputs[uid]),
                "output_hash": origin["candidate_sha256"], "status": "passed"})
        ctx._observer.check()
        self._summary["imported_count"] += 1
        self._summary["imported_this_attempt_count"] += 1
        return proposal


def validate_candidate_import_links(ctx, value, package, *, partition_provider=None):
    """N5 rebuilds current inputs and corrected source links independently."""
    mode = ctx.execution_config.get("provider", {}).get("mode")
    if mode != MODE and partition_provider is None:
        return []
    try:
        provider = partition_provider or DiagnosticWritingCandidateImport(ctx.execution_config["provider"], ctx.execution_config["writing_candidate_import"])
        if partition_provider is not None:
            _require(mode == "diagnostic_writing_commitment_partition" and provider.commitment_partition is not None,
                     "PROVIDER_INVALID")
        nodes = {item["key"]: item for item in ctx._state["nodes"]}
        _require(nodes.get("N4", {}).get("status") == "succeeded", "N4_REQUIRED")
        frozen = ctx.executor._checkpoint_data(nodes["N3"])
        prepared = PreparedBid.model_validate(frozen["prepared"])
        provenance = load_project_constraints(ctx, frozen["prepared"])
        bases = {task.unit_id: build_import_unit_input(prepared, section, task, provider.options, provenance, frozen["proposals"])
                 for section in prepared.sections for task in section.writing_tasks}
        if provider.commitment_partition is not None:
            from .commitment_drafting import checked_commitment_partition
            extra = checked_commitment_partition(prepared, provider.commitment_partition, provenance)
            bases = {uid: base for uid, base in bases.items() if uid not in extra}
        _, tasks, _, _, _ = provider._prepare(ctx, frozen["prepared"], bases)
        _require(package.requirements == prepared.requirements and package.sources == prepared.sources, "PACKAGE_SOURCES_CHANGED")
        reviews = value.get("writing_review", [])
        if provider.commitment_partition is not None:
            reviews = [item for item in reviews if item["unit_id"] in tasks]
        by_uid = {item["unit_id"]: item for item in reviews}
        _require(len(by_uid) == len(reviews) == 9 and set(by_uid) == set(tasks), "REVIEW_CHANGED")
        sections = {item.section_id: item for item in package.sections}
        result = []
        for uid, (section, task) in tasks.items():
            record = nodes["N4"]["units"][uid]
            expected_input = provider._inputs[uid]
            _require(record.get("status") == "succeeded" and record["input_hash"] == digest(expected_input)
                     and ctx.executor.store.read_json(record["input_ref"]) == expected_input, "CHECKPOINT_INPUT_CHANGED")
            output = ctx.executor._checkpoint_data(record)
            review = by_uid[uid]
            candidate = provider._entries[uid]["candidate"]
            blocks = expected_import_output(prepared, section, task, expected_input, candidate)
            _require(output["blocks"] == blocks and review == output["review"]
                     and review == expected_import_review(section, task, expected_input, candidate, provenance), "OUTPUT_CHANGED")
            actual = [item.model_dump(mode="json") for item in sections[section.section_id].blocks]
            _require(sum(actual[index:index + len(blocks)] == blocks for index in range(len(actual) - len(blocks) + 1)) == 1,
                     "ASSEMBLY_CHANGED")
            correction = expected_input.get("writing_correction")
            if correction is not None:
                scope = correction["source_scope"]
                refs = [item["source_id"] for item in scope["sources"]]
                rids = [item["requirement_id"] for item in scope["requirements"]]
                _require(review.get("correction_context_requirement_refs") == rids
                         and review.get("correction_source_scope_sha256") == digest(scope)
                         and review.get("correction_source_refs") == refs, "CORRECTION_LINK_CHANGED")
                result.append({"unit_id": uid, "context_requirement_refs": rids, "source_refs": refs,
                    "source_scope_sha256": digest(scope), "provenance_status": "checked", "satisfaction_status": "not_determined"})
        provider._seal()
        return result
    except ProviderFailure:
        raise
    except Exception as error:
        raise ProviderFailure("WRITING_IMPORT_N5_INVALID") from error
