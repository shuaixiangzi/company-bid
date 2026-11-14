"""Exact, privileged N4 candidate replay and explicitly bounded calibration.

This adapter never copies old document blocks into a new document. Historical
raw model candidates pass through today's writing validator and assembler.
Only explicitly declared corrections may enter the ordinary HTTP provider.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .diagnostic_replay import _OriginRun
from .diagnostics import inspect_diagnostics
from .models import (HeadingBlock, ListBlock, ParagraphBlock, PreparedBid,
                     ProposalContent, TableBlock, file_sha256)
from .project_constraints import project_constraint_trace_attributes
from .provider import (ChatConfig, ChatProposalProvider, ProviderFailure,
                       build_json_request, validate_writing_context,
                       validate_writing_output)
from .storage import LocalStore, digest
from .writing_prompt import PROMPT_VERSION, SYSTEM_PROMPT


SCHEMA_VERSION = "diagnostic-writing-replay-v1"
MODE = "diagnostic_writing_mixed"
_BINDING_KEYS = {"alias", "run_id", "node_key", "unit_id"}
_CORRECTION_KEYS = _BINDING_KEYS | {"feedback", "context_requirement_refs"}


def _require(condition, reason="SOURCE_INVALID"):
    if not condition:
        raise ProviderFailure("WRITING_REPLAY_" + reason)


def _one(items):
    values = list(items)
    _require(len(values) == 1, "SOURCE_AMBIGUOUS")
    return values[0]


def _capture(ctx, name, value):
    result = ctx.capture(name, value)
    _require(isinstance(result, dict) and result.get("status") == "captured", "CAPTURE_FAILED")


def effective_provider_options(options):
    """The sole permitted normalization of a historical base unit input."""
    result = copy.deepcopy(options)
    _require(result.get("mode") in {"chat_completions", MODE}, "PROVIDER_INVALID")
    result["mode"] = "chat_completions"
    _require(result.get("max_attempts") == 1, "SINGLE_ATTEMPT_REQUIRED")
    ChatConfig(**{key: value for key, value in result.items() if key != "mode"})
    return result


def build_writing_unit_input(prepared, plan, task, provider_options, project_constraints, proposals):
    """Rebuild the existing N4 base input, including non-model dependencies."""
    tables = {item.table_id: item for item in prepared.tables}
    excerpts = {item.reference_id: item for item in prepared.reference_excerpts}
    requirements = [item.model_dump(mode="json") for item in prepared.requirements
                    if item.requirement_id in plan.requirement_refs and item.requirement_id in task.requirement_refs]
    references = [excerpts[ref].model_dump(mode="json") for ref in task.reference_refs]
    contexts = [context for item in requirements for context in item.get("source_contexts", [])]
    proof_hashes = list(dict.fromkeys(item["proof_sha256"] for item in contexts))
    context = {"section_id": task.unit_id, "title": task.title,
               "writing_task": task.model_dump(mode="json"), "reference_excerpts": references,
               "source_contexts": contexts}
    if project_constraints is not None:
        context["project_constraints"] = copy.deepcopy(project_constraints["plan"]["tasks"][task.unit_id])
    project_refs = list(dict.fromkeys(item["source_id"] for item in context.get("project_constraints", [])))
    source_ids = {item["source_id"] for item in references} | set(project_refs)
    source_ids.update(ref for item in requirements for ref in item["source_refs"])
    source_ids.update(ref for item in contexts for ref in item["source_refs"])
    source_ids.update(ref for tid in task.table_refs for ref in tables[tid].source_refs)
    result = {"context": context, "requirements": requirements,
              "sources": [{"source_id": item.source_id, "sha256": item.sha256}
                          for item in prepared.sources if item.source_id in source_ids],
              "tables": [tables[ref].model_dump(mode="json") for ref in task.table_refs],
              "source_context_proof_hashes": proof_hashes,
              "provider": effective_provider_options(provider_options), "proposal": proposals.get(task.unit_id)}
    if project_constraints is not None:
        result["project_constraint_artifact_refs"] = copy.deepcopy(project_constraints["artifact_refs"])
        result["project_constraint_plan_sha256"] = project_constraints["plan_sha256"]
    return result


def correction_source_scope(correction_config, prepared_dict):
    """Select only complete, immutable requirement/source objects by identity."""
    _require(isinstance(correction_config, dict) and set(correction_config) == _CORRECTION_KEYS, "CORRECTION_INVALID")
    feedback = correction_config["feedback"]
    refs = correction_config["context_requirement_refs"]
    _require(isinstance(feedback, str) and bool(feedback.strip()) and len(feedback) <= 12000, "CORRECTION_INVALID")
    _require(isinstance(refs, list) and len(refs) <= 30 and all(isinstance(ref, str) for ref in refs)
             and len(refs) == len(set(refs)), "CORRECTION_INVALID")
    prepared = PreparedBid.model_validate(prepared_dict)
    original = {item.requirement_id: item for item in prepared.requirements}
    source_map = {item.source_id: item for item in prepared.sources}
    _require(set(refs) <= set(original), "CONTEXT_REQUIREMENT_UNKNOWN")
    extra = [original[ref].model_dump(mode="json") for ref in refs]
    source_refs = list(dict.fromkeys(ref for item in extra for ref in item["source_refs"]))
    _require(all(ref in source_map and source_map[ref].kind == "tender" for ref in source_refs), "CONTEXT_SOURCE_INVALID")
    return {"role": "readonly_frozen_tender_requirements", "requirements": extra,
            "sources": [{"source_id": ref, "source_sha256": source_map[ref].sha256,
                         "locator": source_map[ref].locator} for ref in source_refs]}


def build_correction_payload(base_input, prior_raw_candidate, correction_config, prepared_dict):
    """Pure exact payload builder, shared with no-I/O cost preflight callers."""
    scope = correction_source_scope(correction_config, prepared_dict)
    proposal = ProposalContent.model_validate(prior_raw_candidate)
    validate_writing_output(base_input["context"], proposal, base_input["requirements"])
    return {"section": copy.deepcopy(base_input["context"]), "requirements": copy.deepcopy(base_input["requirements"]),
            "writing_correction": {
                "instruction": "仅校准所列问题，保留原WritingTask的must_cover与coverage合同。旧响应不是事实来源；反馈不是新增公司事实。额外上下文仅为冻结招标原字，冲突或未获授权的承诺明确待补，不从标准名称扩展工作对象，不推断本次人员、日期或资格结论。",
                "prior_raw_candidate": copy.deepcopy(prior_raw_candidate), "review_feedback": correction_config["feedback"],
                "source_scope": scope,
                "semantic_status": "requires_source_review"}}


def build_correction_wire(provider_options, base_input, prior_raw_candidate, correction_config, prepared_dict):
    options = effective_provider_options(provider_options)
    config = ChatConfig(**{key: value for key, value in options.items() if key != "mode"})
    return build_json_request(config, build_correction_payload(base_input, prior_raw_candidate,
                                                              correction_config, prepared_dict), SYSTEM_PROMPT)


def _expected_output(prepared, plan, task, base, proposal):
    """Check the old successful WRITE checkpoint against its actual candidate."""
    context = base["context"]
    references, requirements = context["reference_excerpts"], base["requirements"]
    contexts = context.get("source_contexts", [])
    project_refs = list(dict.fromkeys(item["source_id"] for item in context.get("project_constraints", [])))
    source_refs = list(dict.fromkeys([*(item["source_id"] for item in references),
        *(ref for item in requirements for ref in item["source_refs"]),
        *(ref for item in contexts for ref in item["source_refs"]), *project_refs]))
    blocks = [HeadingBlock(text=task.title, level=plan.level + 1,
                           requirement_refs=task.requirement_refs, source_refs=source_refs)]
    blocks.extend(ParagraphBlock(text=text, content_role="proposal", requirement_refs=task.requirement_refs,
                                 source_refs=source_refs) for text in proposal.paragraphs)
    if proposal.steps:
        blocks.append(ListBlock(items=proposal.steps, content_role="proposal",
                                requirement_refs=task.requirement_refs, source_refs=source_refs))
    tables = {item.table_id: item for item in prepared.tables}
    blocks.extend(TableBlock(table=tables[ref], requirement_refs=task.requirement_refs) for ref in task.table_refs)
    return {"blocks": [block.model_dump(mode="json") for block in blocks],
            "review": {"unit_id": task.unit_id, "section_id": plan.section_id, "document_role": plan.document_role,
                       "title": task.title, "operation": task.operation, "requirement_refs": task.requirement_refs,
                       "reference_refs": task.reference_refs, "must_cover": task.must_cover,
                       "completion_criteria": task.completion_criteria,
                       "coverage": [item.model_dump(mode="json") for item in proposal.coverage],
                       "source_context_refs": task.source_context_refs,
                       "source_context_proof_hashes": base["source_context_proof_hashes"],
                       "project_constraint_bindings": [project_constraint_trace_attributes(item)
                                                       for item in context.get("project_constraints", [])],
                       "project_constraint_source_refs": project_refs,
                       "context_review_status": "not_checked" if contexts else "not_applicable",
                       "semantic_status": "requires_source_review"}}


class WritingOrigin(_OriginRun):
    """N4's sealed source contract; N2's provider and lineage guards stay intact."""

    def __init__(self, alias, root, run_id):
        _require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id), "CONFIG_INVALID")
        self.alias, self.root, self.run_id = alias, Path(root), run_id
        _require(self.root.is_dir(), "CONFIG_INVALID")
        self.store, self.evidence = LocalStore(self.root), {}
        self.state_raw = self._file("state.json")
        self.state = json.loads(self.state_raw)
        _require(self.state.get("run_id") == run_id and self.state.get("schema_version") == "m1.local.v1", "SOURCE_INVALID")
        _require(self.state.get("status") in {"succeeded", "paused_debug"}, "SOURCE_NOT_SEALED")
        settings = self.state["execution_config"]
        _require(self.state.get("config_hash") == digest(settings) and settings.get("debug_capture", True) is True
                 and self.state.get("capture_policy") is True, "SOURCE_DIAGNOSTICS_INCOMPLETE")
        attempts = self.state.get("execution_attempts", [])
        _require(attempts and all(item.get("ended_at") and item.get("status") in {"succeeded", "paused_debug"}
                                 and item.get("trace_status") == "captured" for item in attempts), "SOURCE_NOT_SEALED")
        n4 = self.node("N4")
        _require(n4.get("status") == "succeeded" and n4.get("attempts") and
                 all(item.get("ended_at") and item.get("status") == "succeeded" for item in n4["attempts"]), "SOURCE_NOT_SEALED")
        options = settings.get("provider", {})
        _require(options.get("mode") == "chat_completions", "SOURCE_ACTUAL_CALL_REQUIRED")
        effective = effective_provider_options(options)
        self.config = ChatConfig(**{key: value for key, value in effective.items() if key != "mode"})
        self.mixed_source = False
        self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")
        report = inspect_diagnostics(self.root, run_id)
        _require(not report["gaps"] and report["capture_policy"], "SOURCE_DIAGNOSTICS_INCOMPLETE")
        self.checkpoint(n4, "N4")


class DiagnosticWritingProvider:
    def __init__(self, provider_options, replay_config):
        self.options = copy.deepcopy(provider_options)
        _require(self.options.get("mode") == MODE, "PROVIDER_INVALID")
        effective = effective_provider_options(self.options)
        self.chat_config = ChatConfig(**{key: value for key, value in effective.items() if key != "mode"})
        self.config = copy.deepcopy(replay_config)
        self.live = ChatProposalProvider(self.chat_config)
        self._ready = None
        self._entries, self._inputs, self._snapshots, self._files, self._paths = {}, {}, [], {}, {}
        self._summary = {"schema_version": SCHEMA_VERSION, "diagnostic_only": True,
                         "publication_status": "diagnostic_only", "revalidated_count": 0,
                         "correction_completed_count": 0, "historical_calls_charged_again": 0, "origins": []}

    def summary(self):
        return copy.deepcopy(self._summary)

    def _hash_source(self, raw_path, expected):
        original, resolved = Path(raw_path), Path(raw_path).resolve()
        _require(resolved.is_file() and file_sha256(resolved) == expected, "SOURCE_FILE_CHANGED")
        _require(self._files.get(resolved, expected) == expected, "SOURCE_FILE_CHANGED")
        self._files[resolved], self._paths[original] = expected, resolved

    def _seal_files(self):
        for path, expected in self._files.items():
            _require(path.is_file() and path.resolve() == path and file_sha256(path) == expected, "SOURCE_FILE_CHANGED")
        _require(all(path.resolve() == resolved for path, resolved in self._paths.items()), "SOURCE_FILE_CHANGED")

    def preflight(self, ctx, prepared_dict, base_inputs_by_unit_id):
        self._ready = None
        self._entries, self._inputs, self._snapshots, self._files, self._paths = {}, {}, [], {}, {}
        self._summary.update(revalidated_count=0, correction_completed_count=0,
            revalidated_this_attempt_count=0, correction_completed_this_attempt_count=0,
            replay_checkpoint_reuse_count=0, correction_checkpoint_reuse_count=0)
        try:
            _require(ctx.node_key == "N4" and ctx.execution_config.get("run_purpose") == "diagnostic_replay"
                     and ctx.execution_config.get("debug_capture", True) is True and ctx.artifact_scope == "diagnostic"
                     and ctx.execution_config.get("writing_replay") == self.config, "PURPOSE_REQUIRED")
            spec = self.config
            _require(isinstance(spec, dict) and set(spec) == {"schema_version", "sources", "bindings", "corrections"}
                     and spec["schema_version"] == SCHEMA_VERSION and isinstance(spec["sources"], dict)
                     and spec["sources"] and isinstance(spec["bindings"], dict) and isinstance(spec["corrections"], dict), "CONFIG_INVALID")
            replay, corrections = spec["bindings"], spec["corrections"]
            _require(not set(replay) & set(corrections) and set(replay) | set(corrections) == set(base_inputs_by_unit_id)
                     and len(corrections) <= 5, "UNIT_PARTITION_INVALID")
            prepared = PreparedBid.model_validate(prepared_dict)
            task_map = {task.unit_id: (plan, task) for plan in prepared.sections for task in plan.writing_tasks}
            _require(set(task_map) == set(base_inputs_by_unit_id), "UNIT_PARTITION_INVALID")
            _require(not any(plan.kind == "technical" and not plan.writing_tasks and not plan.fixed_content and plan.render_body
                             for plan in prepared.sections), "FALLBACK_UNSUPPORTED")
            roots, runs = {}, {}
            for alias, value in spec["sources"].items():
                _require(isinstance(alias, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", alias)
                         and isinstance(value, str) and Path(value).is_absolute() and Path(value).is_dir(), "CONFIG_INVALID")
                roots[alias] = Path(value).resolve()
            for item in [*prepared.sources, *prepared.attachments]:
                self._hash_source(item.path, item.sha256)
            for uid, binding in {**replay, **corrections}.items():
                required_keys = _CORRECTION_KEYS if uid in corrections else _BINDING_KEYS
                _require(isinstance(binding, dict) and set(binding) == required_keys and binding.get("alias") in roots
                         and binding.get("node_key") == "N4" and binding.get("unit_id") == uid, "BINDING_INVALID")
                identity = binding["alias"], binding["run_id"]
                _require((roots[identity[0]], identity[1]) != (ctx.executor.store.root.resolve(), ctx.run_id), "SOURCE_CURRENT_RUN")
                if identity not in runs:
                    runs[identity] = WritingOrigin(identity[0], roots[identity[0]], identity[1])
                origin = runs[identity]
                n3 = origin.node("N3")
                _require(n3.get("status") == "succeeded", "SOURCE_PREFIX_INVALID")
                old_prepared = origin.checkpoint(n3, "N3")["data"]["prepared"]
                _require(old_prepared == prepared_dict and digest(old_prepared) == digest(prepared_dict), "PREPARED_MISMATCH")
                for item in [*old_prepared["sources"], *old_prepared.get("attachments", [])]:
                    self._hash_source(item["path"], item["sha256"])
                node, unit, base, old_output, attempt = origin.unit("N4", uid)
                current = copy.deepcopy(base_inputs_by_unit_id[uid])
                current["provider"] = effective_provider_options(current["provider"])
                historical = copy.deepcopy(base)
                historical["provider"] = effective_provider_options(historical["provider"])
                _require(current == historical and digest(current) == digest(historical), "BASE_INPUT_MISMATCH")
                terminal = _one(event for event in origin.events if event.get("event") == "call.completed"
                                and event.get("unit_id") == uid and event.get("node_key") == "N4"
                                and event.get("node_attempt_id") == attempt["node_attempt_id"]
                                and event.get("unit_attempt_id") == attempt["attempt_id"])
                captures = origin.captures(node, uid, attempt)
                _require(len([item for item in captures if item["name"].startswith("request-")]) == 1, "SOURCE_ACTUAL_CALL_REQUIRED")
                call = origin.call(node, uid, attempt, terminal["call_id"], terminal["call_attempt"], passed=True)
                user = {"section": current["context"], "requirements": current["requirements"]}
                _require(call["task"] == user and digest(call["task"]) == digest(user)
                         and terminal.get("prompt_version") == PROMPT_VERSION and terminal.get("model") == self.chat_config.model
                         and origin.config.endpoint == self.chat_config.endpoint and origin.config.service == self.chat_config.service
                         and call["request"] == build_json_request(origin.config, call["task"], SYSTEM_PROMPT)
                         and call["request"] == build_json_request(self.chat_config, user, SYSTEM_PROMPT), "WIRE_MISMATCH")
                reservation = _one(item for item in origin.state["call_budget"]["reservations"]
                                   if item.get("call_id") == call["call_id"] and item.get("call_attempt") == call["call_attempt"])
                usage = call["response"].get("usage", {})
                counts = {local: usage.get(remote) for remote, local in
                          (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens"))}
                _require(reservation.get("settlement_status") == "known_usage_cost_estimate" and reservation.get("usage") == counts
                         and all(type(value) is int and value >= 0 for value in counts.values())
                         and counts["input_tokens"] + counts["output_tokens"] == counts["total_tokens"]
                         and all(terminal.get(key) == value for key, value in counts.items()), "USAGE_MISMATCH")
                proposal = ProposalContent.model_validate(call["candidate"])
                validate_writing_output(current["context"], proposal, current["requirements"])
                plan, task = task_map[uid]
                _require(proposal.model_dump(mode="json") == call["validation"].get("validated_output")
                         and _expected_output(prepared, plan, task, current, proposal) == old_output, "CANDIDATE_CHECKPOINT_MISMATCH")
                evidence = {"source_alias": identity[0], "run_id": identity[1], "node_key": "N4", "unit_id": uid,
                            "source_state_sha256": hashlib.sha256(origin.state_raw).hexdigest(),
                            "unit_input_hash": unit["input_hash"], "unit_output_hash": unit["output_hash"],
                            "producer_version": unit["version"], "call_id": call["call_id"], "call_attempt": call["call_attempt"],
                            "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"]),
                            "candidate_sha256": digest(call["candidate"]), "trace_id": terminal["trace_id"],
                            "span_id": terminal["span_id"], "external_call": False,
                            "historical_usage": counts, "historical_cost_estimate_usd": reservation["known_usage_cost_estimate_usd"]}
                payload = (build_correction_payload(current, call["candidate"], corrections[uid], prepared_dict)
                           if uid in corrections else user)
                # Check the complete calibrated wire and input size before any HTTP admission.
                wire = build_json_request(self.chat_config, payload, SYSTEM_PROMPT)
                target_input = copy.deepcopy(current)
                if uid in corrections:
                    target_input["writing_correction"] = copy.deepcopy(payload["writing_correction"])
                self._entries[uid] = {"base": current, "payload": payload, "candidate": copy.deepcopy(call["candidate"]),
                                      "origin": evidence, "wire_hash": digest(wire), "correction": uid in corrections}
                self._inputs[uid] = target_input
            for origin in runs.values():
                origin.unchanged()
                for path, raw in origin.evidence.items():
                    reference = ctx.store_bytes("writing-origin" + (".jsonl" if path.endswith(".jsonl") else ".json"), raw,
                        "application/x-ndjson" if path.endswith(".jsonl") else "application/json")
                    self._snapshots.append({"source_alias": origin.alias, "run_id": origin.run_id,
                        "source_relative_path": path, "origin_sha256": hashlib.sha256(raw).hexdigest(), "snapshot": reference})
                origin.unchanged()
            self._seal_files()
            for uid, unit in ctx._record.get("units", {}).items():
                if uid not in self._inputs or unit.get("status") != "succeeded":
                    continue
                persisted = ctx.executor.store.read_json(unit["input_ref"])
                _require(persisted == self._inputs[uid] and digest(persisted) == unit["input_hash"]
                         == digest(self._inputs[uid]), "CURRENT_UNIT_INPUT_MISMATCH")
                ctx.executor._checkpoint_data(unit)
                field = "correction_checkpoint_reuse_count" if uid in corrections else "replay_checkpoint_reuse_count"
                total = "correction_completed_count" if uid in corrections else "revalidated_count"
                self._summary[field] += 1
                self._summary[total] += 1
            self._summary.update(binding_count=len(replay), correction_count=len(corrections),
                                 origins=[entry["origin"] for entry in self._entries.values()],
                                 historical_evidence=self._snapshots,
                                 source_file_verification={"policy": "double_full_hash_and_path_binding", "distinct_paths": len(self._files)},
                                 statistics_scope="total completed bound units; this_attempt excludes successful checkpoint reuse")
            _capture(ctx, "writing-replay-preflight", self._summary)
            ctx._observer.check()
            self._ready = (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config))
            return self.summary()
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure("WRITING_REPLAY_PREFLIGHT_FAILED") from error

    def unit_input(self, unit_id, base_input):
        _require(self._ready is not None and unit_id in self._entries, "PREFLIGHT_REQUIRED")
        current = copy.deepcopy(base_input)
        current["provider"] = effective_provider_options(current["provider"])
        _require(current == self._entries[unit_id]["base"] and digest(current) == digest(self._entries[unit_id]["base"]), "BASE_INPUT_MISMATCH")
        return copy.deepcopy(self._inputs[unit_id])

    def generate(self, ctx, context, requirements):
        _require(self._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
        uid = context.get("section_id")
        _require(uid in self._entries, "UNIT_NOT_DECLARED")
        entry = self._entries[uid]
        _require(context == entry["base"]["context"] and requirements == entry["base"]["requirements"]
                 and digest({"section": context, "requirements": requirements}) ==
                     digest({"section": entry["base"]["context"], "requirements": entry["base"]["requirements"]}), "BASE_INPUT_MISMATCH")
        active = ctx._record["units"][uid]
        _require(ctx._active_unit.get("unit_id") == uid and
                 ctx.executor.store.read_json(active["input_ref"]) == self._inputs[uid], "CURRENT_UNIT_INPUT_MISMATCH")
        for snapshot in self._snapshots:
            ctx.executor.store.read_bytes(snapshot["snapshot"])
        self._seal_files()
        validate_writing_context(context, requirements)

        def validate(raw):
            proposal = ProposalContent.model_validate(raw)
            validate_writing_output(context, proposal, requirements)
            return proposal.model_dump(mode="json")

        if entry["correction"]:
            reservations = ctx._state.get("call_budget", {}).get("reservations", [])
            allowed = set(self.config["corrections"])
            _require(not any(item.get("unit_id") == uid for item in reservations), "CORRECTION_ALREADY_ATTEMPTED")
            _require(len(reservations) < len(allowed) and all(item.get("unit_id") in allowed for item in reservations), "CORRECTION_LIMIT_EXCEEDED")
            _require(digest(build_json_request(self.chat_config, entry["payload"], SYSTEM_PROMPT)) == entry["wire_hash"], "WIRE_MISMATCH")
            _capture(ctx, "writing-correction-origin", {"origin": entry["origin"], "diagnostic_only": True,
                "correction_sha256": digest(entry["payload"]["writing_correction"]), "request_sha256": entry["wire_hash"]})
            result = self.live.generate_json(ctx, copy.deepcopy(entry["payload"]), unit_id=uid,
                prompt_version=PROMPT_VERSION, system_prompt=SYSTEM_PROMPT, validate=validate, task_type="writing_correction")
            self._summary["correction_completed_count"] += 1
            self._summary["correction_completed_this_attempt_count"] += 1
            return ProposalContent.model_validate(result)
        origin = entry["origin"]
        with ctx._observer.span("candidate.revalidation", {**ctx._metadata(), "operation": "writing_diagnostic_replay",
                "input_hash": entry["wire_hash"], "output_hash": origin["candidate_sha256"]},
                links=[{"trace_id": origin["trace_id"], "span_id": origin["span_id"]}]):
            _capture(ctx, "writing-replay-candidate", {"origin": origin, "candidate": entry["candidate"],
                                                       "external_call": False, "diagnostic_only": True})
            result = validate(copy.deepcopy(entry["candidate"]))
            _capture(ctx, "writing-replay-validation", {"origin": origin, "validated_output": result,
                "status": "passed", "new_external_calls": 0, "diagnostic_only": True})
            ctx.call_event("candidate.revalidated", {"operation": "writing_diagnostic_replay",
                "input_hash": entry["wire_hash"], "output_hash": digest(result), "status": "passed"})
        ctx._observer.check()
        self._summary["revalidated_count"] += 1
        self._summary["revalidated_this_attempt_count"] += 1
        return ProposalContent.model_validate(result)


def validate_correction_links(ctx, value, package):
    """Rebuild N5's expectations from frozen N3/config, not N4's own report."""
    if ctx.execution_config.get("provider", {}).get("mode") != MODE:
        return []
    _require(ctx.execution_config.get("run_purpose") == "diagnostic_replay" and ctx.artifact_scope == "diagnostic", "PURPOSE_REQUIRED")
    nodes = {node["key"]: node for node in ctx._state["nodes"]}
    _require(nodes.get("N3", {}).get("status") == "succeeded" and nodes.get("N4", {}).get("status") == "succeeded", "SOURCE_PREFIX_INVALID")
    prepared_dict = ctx.executor._checkpoint_data(nodes["N3"])["prepared"]
    prepared = PreparedBid.model_validate(prepared_dict)
    config = ctx.execution_config["writing_replay"]
    tasks = {task.unit_id: (section, task) for section in prepared.sections for task in section.writing_tasks}
    _require(set(config["bindings"]) | set(config["corrections"]) == set(tasks)
             and not set(config["bindings"]) & set(config["corrections"]), "UNIT_PARTITION_INVALID")
    reviews = value.get("writing_review", [])
    by_uid = {review["unit_id"]: review for review in reviews}
    _require(len(by_uid) == len(reviews) and set(by_uid) == set(tasks), "CORRECTION_LINK_MISMATCH")
    package_sources = {item.source_id: item for item in package.sources}
    _require([item.model_dump(mode="json") for item in package.requirements] == prepared_dict["requirements"], "CORRECTION_LINK_MISMATCH")
    package_sections = {item.section_id: item for item in package.sections}
    result = []
    for uid, correction in config["corrections"].items():
        scope = correction_source_scope(correction, prepared_dict)
        refs = [item["source_id"] for item in scope["sources"]]
        _require(all(ref in package_sources and package_sources[ref].sha256 == item["source_sha256"]
                     for ref, item in zip(refs, scope["sources"])), "CORRECTION_LINK_MISMATCH")
        record = nodes["N4"]["units"][uid]
        _require(record.get("status") == "succeeded", "CORRECTION_LINK_MISMATCH")
        payload = ctx.executor.store.read_json(record["input_ref"])
        output = ctx.executor._checkpoint_data(record)
        review = by_uid[uid]
        _require(payload.get("writing_correction", {}).get("source_scope") == scope and
                 payload["writing_correction"].get("review_feedback") == correction["feedback"] and
                 review == output["review"] and
                 review.get("correction_context_requirement_refs") == correction["context_requirement_refs"] and
                 review.get("correction_source_scope_sha256") == digest(scope) and
                 review.get("correction_source_refs") == refs and
                 review.get("semantic_status") == "requires_source_review", "CORRECTION_LINK_MISMATCH")
        section, task = tasks[uid]
        _require(review["must_cover"] == task.must_cover and review["requirement_refs"] == task.requirement_refs
                 and all(set(refs) <= set(block.get("source_refs", [])) for block in output["blocks"]), "CORRECTION_LINK_MISMATCH")
        actual = [block.model_dump(mode="json") for block in package_sections[section.section_id].blocks]
        expected = output["blocks"]
        _require(any(actual[index:index + len(expected)] == expected for index in range(len(actual) - len(expected) + 1)), "CORRECTION_LINK_MISMATCH")
        result.append({"unit_id": uid, "context_requirement_refs": correction["context_requirement_refs"],
                       "source_refs": refs, "source_scope_sha256": digest(scope),
                       "provenance_status": "checked", "satisfaction_status": "not_determined"})
    return result
