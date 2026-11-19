"""Explicit two-unit partition beside nine authenticated historical writings.

This diagnostic adapter is deliberately not a fallback/cache. The complete
frozen N3 is checked before constructing a live provider, and live eligibility
is independently derived from source-bound self-drafted format instructions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from .commitment_prompt import PENDING_LABEL, PROMPT_VERSION, SYSTEM_PROMPT
from .commitment_planning import (plan_commitment_tasks, commitment_source_scope,
                                  commitment_allowlist, commitment_identity, _compact)
from .models import PreparedBid, ProposalContent
from .provider import (ChatConfig, ChatProposalProvider, ProviderFailure,
                       build_json_request, validate_writing_output)
from .storage import digest

MODE = "diagnostic_writing_commitment_partition"
SCHEMA_VERSION = "source-bound-commitment-partition-v1"
PROFILE = "pending_company_confirmation_v1"
REVIEW_PACKET_SCHEMA = "source-bound-reviewed-commitments-v1"
REVIEW_REBIND_V1 = "exact-writing-inputs-v1"
REVIEW_REBIND_V2 = "exact-writing-inputs-v2"
REVIEW_INSTRUCTION = "区分验收不合格与质量保证期问题的触发条件及处理动作，不将验收重制义务扩展为质保义务；其余正文和覆盖定位保持原字。"
ECHO_INSTRUCTION = "仅删除与原请求完全相等的coverage_contract多余回显；正文、步骤和coverage保持原字。"
ORIGINAL_MERGED_STEP = "纠正复核：对验收不合格或质量保证期内质量问题，按甲方意见免费修改、改正、重新制作或编制，并由项目负责人复核纠正结果。"
REVIEWED_STEP = "纠正复核：对验收不合格项，按甲方意见免费修改或重新制作或编制，并按合同约定期限重新提请验收；对质量保证期内不符合合同质量标准的问题，免费修改和改正，并承担鉴定及其他一切有关费用。由拟设置的项目负责人组织复核纠正结果。"


def _require(condition, code):
    if not condition:
        raise ProviderFailure("COMMITMENT_" + code)


def _strict_json(text):
    def pairs(items):
        _require(len(items) == len({key for key, _ in items}), "DUPLICATE_JSON_KEY")
        return dict(items)
    def constant(_):
        raise ProviderFailure("COMMITMENT_NONFINITE_JSON")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def checked_commitment_partition(prepared, config, provenance=None):
    _require(isinstance(config, dict) and set(config) - {"reviewed_candidates"} == {"schema_version", "profile", "allowed_units", "direct_provider"}
             and config["schema_version"] == SCHEMA_VERSION and config["profile"] == PROFILE, "CONFIG_INVALID")
    expected = commitment_allowlist(prepared)
    _require(len(expected) == 2 and config["allowed_units"] == expected, "ALLOWLIST_CHANGED")
    tasks = plan_commitment_tasks(prepared)
    current = {task.unit_id: (section, task) for section in PreparedBid.model_validate(prepared).sections
               for task in section.writing_tasks}
    for sid, task in tasks.items():
        if provenance is not None:
            bindings = provenance["plan"]["tasks"].get(task.unit_id)
            _require(bindings is not None, "PROJECT_CONSTRAINT_MISSING")
            forbidden = list(task.forbidden_fields)
            for binding in bindings:
                for constraint in binding["constraints"]:
                    if constraint["forbidden_field"] not in forbidden:
                        forbidden.append(constraint["forbidden_field"])
            task = task.model_copy(update={"forbidden_fields": forbidden})
        _require(task.unit_id in current and current[task.unit_id][0].section_id == sid
                 and current[task.unit_id][1] == task
                 and set(task.requirement_refs) <= set(current[task.unit_id][0].requirement_refs), "TASK_CHANGED")
    options = config["direct_provider"]
    _require(isinstance(options, dict) and options.get("mode") == "chat_completions"
             and options.get("max_attempts") == 1, "SINGLE_ATTEMPT_REQUIRED")
    ChatConfig(**{key: item for key, item in options.items() if key != "mode"})
    return expected


def build_partition_unit_input(prepared, section, task, options, constraints, proposals, config):
    from .writing_candidate_import import build_import_unit_input
    base = build_import_unit_input(prepared, section, task, options, constraints, proposals)
    if task.unit_id in config.get("allowed_units", {}):
        identity = commitment_identity(prepared)
        scope = commitment_source_scope(prepared, task)
        identity_refs = {item["source_id"] for key in ("project", "company") for item in identity[key]["sources"]}
        source_refs = identity_refs | {item["source_id"] for item in base["sources"]} | {item["source_id"] for item in scope["sources"]}
        base["sources"] = [{"source_id": source.source_id, "sha256": source.sha256}
                           for source in prepared.sources if source.source_id in source_refs]
        base["provider"] = copy.deepcopy(config["direct_provider"])
        base["commitment_drafting"] = {"profile": PROFILE, "prompt_version": PROMPT_VERSION,
            "system_prompt_sha256": digest(SYSTEM_PROMPT), "binding": copy.deepcopy(config["allowed_units"][task.unit_id]),
            "source_scope": scope, "identity": identity,
            "company_confirmation": "pending",
            "satisfaction_status": "not_determined"}
    return base


def commitment_payload(base):
    return {"section": base["context"], "requirements": base["requirements"],
            "proposed_commitment": base["commitment_drafting"],
            "coverage_contract": {
                "expected_item_indices": list(range(len(base["context"]["writing_task"]["must_cover"]))),
                "item_source": "section.writing_task.must_cover",
                "content_indices": "Zero-based indices of the actual returned paragraphs and steps arrays",
            }}


def validate_commitment_candidate(base, value):
    proposal = ProposalContent.model_validate(value)
    validate_writing_output(base["context"], proposal, base["requirements"])
    text = "\n".join([*proposal.paragraphs, *proposal.steps])
    _require(PENDING_LABEL in proposal.paragraphs[0] and "待签字" in text and "待盖公章" in text,
             "PENDING_STATUS_REQUIRED")
    identity = base["commitment_drafting"]["identity"]
    _require(all(identity[key]["value"]["name"] in text for key in ("project", "company"))
             and "响应人：【待公司确认】" not in _compact(text), "KNOWN_IDENTITY_REQUIRED")
    source = _compact("\n".join(item["text"] for item in base["commitment_drafting"]["source_scope"]["requirements"]))
    # A lexical guard complements (never replaces) manual semantic review.
    for token in ("7×24", "7x24", "7*24", "终身", "永久", "无条件免费"):
        _require(token not in _compact(text) or token in source, "EXTRA_PROMISE")
    for token in re.findall(r"\d+\s*(?:小时|分钟|个工作日|工作日)", text):
        _require(_compact(token) in source, "EXTRA_PROMISE")
    _require(not re.search(r"(?:我方|本公司|本单位)(?:已承诺|已经承诺|已完成签章|已满足全部)", text),
             "COMPANY_STATUS_INVENTED")
    return proposal.model_dump(mode="json")


def commitment_review(base):
    binding = base["commitment_drafting"]
    result = {"profile": PROFILE, "prompt_version": PROMPT_VERSION,
            "source_scope_sha256": digest(binding["source_scope"]), "identity_sha256": digest(binding["identity"]),
            "identity_source_refs": commitment_identity_source_refs(base), "company_confirmation": "pending",
            "readonly_requirement_refs": [item["requirement_id"] for item in binding["source_scope"]["requirements"]
                if item["requirement_id"] not in base["context"]["writing_task"]["requirement_refs"]],
            "readonly_source_refs": [item["source_id"] for item in binding["source_scope"]["sources"]],
            "readonly_context_satisfies_requirements": False,
            "satisfaction_status": "not_determined", "external_call_required": True}
    if "reviewed_candidate" in base:
        result.update(external_call_required=False, execution="reviewed_offline_import",
                      reviewed_candidate=copy.deepcopy(base["reviewed_candidate"]))
    return result


def commitment_identity_source_refs(base):
    identity = base["commitment_drafting"]["identity"]
    return list(dict.fromkeys(item["source_id"] for key in ("project", "company") for item in identity[key]["sources"]))


def commitment_content_source_refs(base):
    return list(dict.fromkeys([*commitment_identity_source_refs(base),
        *(item["source_id"] for item in base["commitment_drafting"]["source_scope"]["sources"])]))


def _reviewed_transform(base, raw, operation):
    """Two explicit reviewed operations; no generic repair or text editing."""
    result = copy.deepcopy(raw)
    title = base["context"]["title"]
    if title.startswith("售后服务"):
        _require(operation == {"kind": "remove_exact_coverage_contract_echo_v1", "instruction": ECHO_INSTRUCTION},
                 "REVIEW_OPERATION_INVALID")
        _require(set(raw) == {"paragraphs", "steps", "coverage", "coverage_contract"}
                 and digest(raw["coverage_contract"]) == digest(commitment_payload(base)["coverage_contract"]),
                 "REVIEW_ECHO_CHANGED")
        result.pop("coverage_contract")
    else:
        _require(title.startswith("质量保证") and set(operation) ==
            {"kind", "instruction", "step_index", "original_step", "replacement_step", "source_requirements"}
            and operation["kind"] == "separate_acceptance_warranty_step_v1"
            and operation["instruction"] == REVIEW_INSTRUCTION
            and type(operation["step_index"]) is int and operation["step_index"] == 3
            and operation["original_step"] == ORIGINAL_MERGED_STEP
            and operation["replacement_step"] == REVIEWED_STEP, "REVIEW_OPERATION_INVALID")
        _require(set(raw) == {"paragraphs", "steps", "coverage"} and len(raw["steps"]) > 3
                 and raw["steps"][3] == ORIGINAL_MERGED_STEP, "REVIEW_ORIGINAL_STEP_CHANGED")
        requirements = base["commitment_drafting"]["source_scope"]["requirements"]
        acceptance = [item for item in requirements if "验收程序" in item["text"]]
        warranty = [item for item in requirements if "质量保证期" in item["text"] and "验收合格" in item["text"]]
        _require(len(acceptance) == len(warranty) == 1 and operation["source_requirements"] ==
                 {"acceptance": acceptance[0], "warranty": warranty[0]}, "REVIEW_SOURCE_CHANGED")
        a, w = _compact(acceptance[0]["text"]), _compact(warranty[0]["text"])
        _require(all(text in a for text in ("验收不合格", "按甲方意见免费进行修改或重新制作或编制", "重新提请验收"))
                 and all(text in w for text in ("甲方在质量保证期间发现", "不符合本合同规定的质量标准",
                     "免费修改和改正", "鉴定及其他一切有关费用")), "REVIEW_SOURCE_UNSUPPORTED")
        result["steps"][3] = REVIEWED_STEP
    validate_commitment_candidate(base, result)
    return result


def _review_origin(ctx, spec):
    from .diagnostic_replay import _OriginRun
    from .diagnostics import inspect_diagnostics

    class ReviewedOrigin(_OriginRun):
        def __init__(self):
            _require(isinstance(spec, dict) and set(spec) ==
                {"root", "run_id", "state_sha256", "events_sha256", "spans_sha256"}
                and re.fullmatch(r"[0-9a-f]{32}", spec["run_id"])
                and Path(spec["root"]).is_absolute()
                and Path(spec["root"]).resolve() == ctx.executor.store.root, "REVIEW_ORIGIN_INVALID")
            self.alias, self.root, self.run_id = "reviewed-commitment-origin", ctx.executor.store.root, spec["run_id"]
            self.store, self.evidence = ctx.executor.store, {}
            self.state_raw = self._file("state.json")
            _require(hashlib.sha256(self.state_raw).hexdigest() == spec["state_sha256"], "REVIEW_ORIGIN_HASH_CHANGED")
            self.state = _strict_json(self.state_raw)
            _require(self.state.get("run_id") == self.run_id and self.state.get("status") == "failed"
                     and self.state.get("schema_version") == "m1.local.v1", "REVIEW_ORIGIN_INVALID")
            self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")
            for name in ("events", "spans"):
                raw = self.evidence[(self.store.run_dir(self.run_id) / (name + ".jsonl")).relative_to(self.root).as_posix()]
                _require(hashlib.sha256(raw).hexdigest() == spec[name + "_sha256"], "REVIEW_ORIGIN_HASH_CHANGED")
            diagnostics = inspect_diagnostics(self.root, self.run_id)
            _require(not diagnostics["gaps"] and diagnostics["capture_policy"], "REVIEW_ORIGIN_DIAGNOSTICS_INCOMPLETE")
    return ReviewedOrigin()


def _review_rebuild_repaired_n2(n1, old_n2):
    from .extraction import _index, _merge_results, _repair_numbered_procedure_spans, _windows

    _require(n1.keys() <= old_n2.keys() and digest({key: old_n2[key] for key in n1}) == digest(n1),
             "REVIEW_REBIND_N2_SOURCE_CHANGED")
    extracted = old_n2["extracted"]
    _require("source_clause_repairs" not in extracted and extracted["parsed_hash"] == digest(n1["parsed"]),
             "REVIEW_REBIND_N2_SOURCE_CHANGED")
    blocks, _, documents = _index(n1["parsed"], n1["manifest"])
    windows, _ = _windows(documents, blocks, extracted["window_config"], manifest=n1["manifest"])
    results = extracted["window_results"]
    _require([window["window_id"] for window in windows] == [result["window_id"] for result in results],
             "REVIEW_REBIND_N2_WINDOWS_CHANGED")
    original_requirements, original_formats = _merge_results(results)
    _require(original_requirements == extracted["requirements"] and original_formats == extracted["format_chapters"],
             "REVIEW_REBIND_N2_ORIGINAL_MERGE_CHANGED")

    projected, repairs = _repair_numbered_procedure_spans(windows, results)
    requirements, formats = _merge_results(projected)
    _require(len(repairs) == 1 and repairs[0]["rule"] == "native_numbered_procedure_with_selected_attachment_tail_v1"
             and formats == original_formats and len(requirements) == len(original_requirements),
             "REVIEW_REBIND_N2_REPAIR_UNSUPPORTED")
    repair = repairs[0]
    changed = [(old["requirement_id"], new["requirement_id"])
               for old, new in zip(original_requirements, requirements) if old != new]
    _require(changed == [(repair["original"]["requirement_id"], repair["restored"]["requirement_id"])],
             "REVIEW_REBIND_N2_REPAIR_UNSUPPORTED")
    expected = {**old_n2, "extracted": {**extracted, "requirements": requirements, "source_clause_repairs": repairs}}
    return expected, windows


def _review_n2_window_checkpoints(origin, prior_node, current_node, extracted, windows):
    from .extraction import extraction_window_payload

    old_units, new_units = prior_node["units"], current_node["units"]
    results = extracted["window_results"]
    _require(len(windows) == len(old_units) == len(new_units) and set(old_units) == set(new_units) ==
             {window["window_id"] for window in windows}, "REVIEW_REBIND_N2_WINDOWS_CHANGED")
    for window, result in zip(windows, results):
        uid = window["window_id"]
        expected_input = extraction_window_payload(window, extracted["window_config"])
        expected_input_hash, expected_output_hash = digest(expected_input), digest(result)
        for record in (old_units[uid], new_units[uid]):
            _require(record["status"] == "succeeded" and record["unit_id"] == uid and
                     record["input_hash"] == expected_input_hash and record["output_hash"] == expected_output_hash,
                     "REVIEW_REBIND_N2_WINDOW_CHECKPOINT_CHANGED")
            source_input = origin.artifact(record["input_ref"])
            _require(digest(source_input) == expected_input_hash, "REVIEW_REBIND_N2_WINDOW_CHECKPOINT_CHANGED")
            # The original N4 and its N2 window checkpoint can have different
            # run IDs when the former inherited the latter from an ancestor.
            envelope = origin.artifact(record["output_ref"])
            _require(envelope.get("schema_version") == "m1.checkpoint.v1" and envelope.get("node_key") == "N2" and
                     envelope.get("unit_id") == uid and envelope.get("producer_version") == record["version"] and
                     envelope.get("run_id") == record["output_ref"]["path"].split("/")[1] and
                     envelope.get("input_hash") == expected_input_hash and
                     envelope.get("output_hash") == expected_output_hash and digest(envelope.get("data")) == expected_output_hash,
                     "REVIEW_REBIND_N2_WINDOW_CHECKPOINT_CHANGED")


def _review_rebind_n2(origin, spec, nodes, original_n3, current_n3):
    prior_n1, current_n1 = origin.node("N1"), nodes["N1"]
    _require(prior_n1["status"] == current_n1["status"] == "succeeded" and
             prior_n1["output_ref"] == current_n1["output_ref"] and
             prior_n1["output_hash"] == current_n1["output_hash"], "REVIEW_REBIND_SOURCE_CHANGED")
    prior_n2, current_n2 = origin.node("N2"), nodes["N2"]
    _require(prior_n2["status"] == current_n2["status"] == "succeeded" and
             prior_n2["output_hash"] == spec["origin_n2_output_sha256"] and
             current_n2["output_hash"] == spec["current_n2_output_sha256"] and
             prior_n2["output_hash"] != current_n2["output_hash"] and
             prior_n2["input_hash"] == current_n2["input_hash"] == prior_n1["output_hash"] and
             original_n3["input_hash"] == prior_n2["output_hash"] and
             current_n3["input_hash"] == current_n2["output_hash"] and current_n3["status"] == "succeeded",
             "REVIEW_REBIND_N2_BINDING_CHANGED")
    n1 = origin.checkpoint(prior_n1, "N1")["data"]
    _require(n1 == origin.checkpoint(current_n1, "N1")["data"], "REVIEW_REBIND_SOURCE_CHANGED")
    original = origin.checkpoint(prior_n2, "N2")["data"]
    current = origin.checkpoint(current_n2, "N2")["data"]
    expected, windows = _review_rebuild_repaired_n2(n1, original)
    _require(digest(current) == digest(expected), "REVIEW_REBIND_N2_PROJECTION_CHANGED")
    _review_n2_window_checkpoints(origin, prior_n2, current_n2, original["extracted"], windows)
    return {"n1_output_ref": prior_n1["output_ref"], "n1_output_sha256": prior_n1["output_hash"],
            "origin_n2_output_ref": prior_n2["output_ref"], "current_n2_output_ref": current_n2["output_ref"],
            "n2_source_clause_repair_sha256": expected["extracted"]["source_clause_repairs"][0]["proof_sha256"],
            "n2_window_count": len(windows), "n2_window_results_sha256": digest(original["extracted"]["window_results"])}


def _review_rebind_origin(ctx, origin, binding, current_n3, original_n3, frozen):
    """Explicitly revalidate unchanged writing against a newly assembled N3.

    V1 requires the original N1 and N2. V2 authenticates one independently
    reconstructed N2 merge repair against the same N1 and window inputs.
    Constraints, facts, tasks, prompt and serialized requests are checked separately.
    """
    from .project_constraint_runtime import load_project_constraints
    spec = binding.get("source_rebind")
    automatic = ctx.execution_config.get("pipeline", "prepared") == "automatic"
    fields = {"version", "origin_n3_output_sha256", "current_n3_output_sha256"}
    version = spec.get("version") if isinstance(spec, dict) else None
    _require(version in (REVIEW_REBIND_V1, REVIEW_REBIND_V2), "REVIEW_REBIND_INVALID")
    if version == REVIEW_REBIND_V2:
        fields |= {"origin_n2_output_sha256", "current_n2_output_sha256"}
        _require(automatic, "REVIEW_REBIND_N2_AUTOMATIC_REQUIRED")
    _require(set(spec) == fields and all(isinstance(spec[key], str) and re.fullmatch(r"[0-9a-f]{64}", spec[key])
                 for key in fields - {"version"})
        and spec["origin_n3_output_sha256"] == original_n3["output_hash"]
        and spec["current_n3_output_sha256"] == current_n3["output_hash"], "REVIEW_REBIND_INVALID")
    n2_proof = {}
    if automatic:
        nodes = {node["key"]: node for node in ctx._state["nodes"]}
        if version == REVIEW_REBIND_V1:
            for key in ("N1", "N2"):
                old = origin.node(key)
                _require(old["status"] == nodes[key]["status"] == "succeeded"
                    and old["output_ref"] == nodes[key]["output_ref"], "REVIEW_REBIND_SOURCE_CHANGED")
                origin.checkpoint(old, key)
        else:
            n2_proof = _review_rebind_n2(origin, spec, nodes, original_n3, current_n3)
        plan = original_n3["units"]["plan-assemble"]
        # N3 can be an exact prefix inherited by the originating N4 run.
        # Its envelope belongs to that earlier producer, not origin.run_id.
        origin.artifact(plan["input_ref"])
        origin.artifact(plan["output_ref"])
    origin_ctx = SimpleNamespace(executor=ctx.executor, _state=origin.state,
        run_id=origin.run_id, execution_config=origin.state["execution_config"], node_key=ctx.node_key,
        capture=lambda name, data: ctx.capture("review-origin-" + name, data),
        call_event=lambda name, data: ctx.call_event(name, {**data, "origin_run_id": origin.run_id}))
    original_prepared = PreparedBid.model_validate(frozen["prepared"])
    original_constraints = load_project_constraints(origin_ctx, frozen["prepared"])
    proof = {**spec, **n2_proof, "origin_n3_output_ref": original_n3["output_ref"],
             "current_n3_output_ref": current_n3["output_ref"]}
    return original_prepared, original_constraints, proof


def _same_writing_input(original, current):
    old, new = copy.deepcopy(original), copy.deepcopy(current)
    old.pop("project_constraint_artifact_refs", None)
    new.pop("project_constraint_artifact_refs", None)
    return digest(old) == digest(new)


def _review_constraint_rebind(origin, current):
    """Authenticate the exact consequence of the proved N2 merge repair."""
    _require(isinstance(origin, dict) and isinstance(current, dict), "REVIEW_REBIND_CONSTRAINT_PLAN_CHANGED")
    old, new = origin["plan"], current["plan"]
    derived_key = "requirements_sha256"
    old_requirements, new_requirements = old[derived_key], new[derived_key]
    _require(old_requirements != new_requirements and
             digest({key: value for key, value in old.items() if key != derived_key}) ==
             digest({key: value for key, value in new.items() if key != derived_key}) and
             digest(old) == origin["plan_sha256"] and digest(new) == current["plan_sha256"],
             "REVIEW_REBIND_CONSTRAINT_PLAN_CHANGED")
    return {"origin_plan_sha256": origin["plan_sha256"], "current_plan_sha256": current["plan_sha256"],
            "origin_requirements_sha256": old_requirements, "current_requirements_sha256": new_requirements}


def _same_writing_input_after_clause_repair(original, current, plan_proof):
    key = "project_constraint_plan_sha256"
    _require(original.get(key) == plan_proof["origin_plan_sha256"] and
             current.get(key) == plan_proof["current_plan_sha256"], "REVIEW_REBIND_CONSTRAINT_PLAN_CHANGED")
    return _same_writing_input(original, {**current, key: original[key]})


def _review_config_rebind(original, current, binding):
    """Permit one N3-only option while retaining exact writing authentication.

    This comparison is not sufficient to reuse a candidate: the caller must
    still validate the pinned N1/N2/N3, every writing input and the actual wire.
    An absent planning container may be created solely for the allowed option.
    """
    if digest(original) == digest(current):
        return None
    spec = binding.get("source_rebind")
    _require(isinstance(spec, dict) and spec.get("version") in (REVIEW_REBIND_V1, REVIEW_REBIND_V2),
             "REVIEW_ORIGIN_CONFIG_CHANGED")
    old, new = copy.deepcopy(original), copy.deepcopy(current)
    old_planning, new_planning = old.pop("planning", {}), new.pop("planning", {})
    _require(isinstance(old_planning, dict) and isinstance(new_planning, dict),
             "REVIEW_ORIGIN_CONFIG_CHANGED")
    key = "semantic_material_recall"
    old_present, new_present = key in old_planning, key in new_planning
    old_value, new_value = old_planning.pop(key, None), new_planning.pop(key, None)
    _require((old_present != new_present or digest(old_value) != digest(new_value))
             and digest(old) == digest(new) and digest(old_planning) == digest(new_planning),
             "REVIEW_ORIGIN_CONFIG_CHANGED")
    return {"allowed_config_path": "planning.semantic_material_recall",
            "origin_execution_config_sha256": digest(original),
            "current_execution_config_without_review_sha256": digest(current),
            "origin_option_present": old_present, "current_option_present": new_present}


def _prepare_reviewed(ctx, provider, prepared, constraints):
    binding = provider.config.get("reviewed_candidates")
    _require(isinstance(binding, dict) and set(binding) in ({"path", "sha256"}, {"path", "sha256", "source_rebind"})
             and isinstance(binding["path"], str) and Path(binding["path"]).is_absolute()
             and re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]), "REVIEW_CONFIG_INVALID")
    raw = provider.importer._read(binding["path"], binding["sha256"])
    packet = _strict_json(raw)
    _require(set(packet) == {"schema_version", "origin", "entries"}
             and packet["schema_version"] == REVIEW_PACKET_SCHEMA
             and set(packet["entries"]) == set(provider._inputs) and len(packet["entries"]) == 2, "REVIEW_PACKET_INVALID")
    origin = _review_origin(ctx, packet["origin"])
    expected_config = copy.deepcopy(ctx.execution_config)
    expected_config["commitment_drafting"].pop("reviewed_candidates")
    current_n3 = next(item for item in ctx._state["nodes"] if item["key"] == "N3")
    if "project_answer_snapshot" in expected_config:
        # Authenticate the project decision and prove that the ACTUAL two
        # writing inputs equal those this original reviewed packet consumed.
        from .project_review import project_review_input
        effective = project_review_input(ctx, ctx.executor._checkpoint_data(current_n3))
        checked = PreparedBid.model_validate(effective["prepared"])
        current_bases = {task.unit_id: build_partition_unit_input(checked, section, task,
                         provider.options, constraints, effective["proposals"], provider.config)
                         for section in checked.sections for task in section.writing_tasks
                         if task.unit_id in provider._inputs}
        _require(current_bases == provider._inputs, "REVIEW_CURRENT_INPUT_CHANGED")
        expected_config.pop("project_answer_snapshot")
    if "review_before_writing" in expected_config and "review_before_writing" not in origin.state["execution_config"]:
        _require(type(expected_config.pop("review_before_writing")) is bool, "REVIEW_ORIGIN_CONFIG_CHANGED")
    config_rebind = _review_config_rebind(origin.state["execution_config"], expected_config, binding)
    original_n3 = origin.node("N3")
    _require(original_n3.get("status") == "succeeded", "REVIEW_N3_PREFIX_CHANGED")
    frozen = origin.checkpoint(original_n3, "N3")["data"]
    rebind = None
    original_prepared, original_constraints = prepared, constraints
    if "source_rebind" in binding:
        original_prepared, original_constraints, rebind = _review_rebind_origin(
            ctx, origin, binding, current_n3, original_n3, frozen)
        if config_rebind is not None:
            rebind["configuration_rebind"] = config_rebind
        if rebind["version"] == REVIEW_REBIND_V2:
            rebind["project_constraint_plan_rebind"] = _review_constraint_rebind(original_constraints, constraints)
    else:
        _require(current_n3["output_ref"] == original_n3["output_ref"], "REVIEW_N3_PREFIX_CHANGED")
        _require(frozen["prepared"] == prepared.model_dump(mode="json"), "REVIEW_PREPARED_CHANGED")
    original_options = origin.state["execution_config"]["provider"]
    original_config = origin.state["execution_config"]["commitment_drafting"]
    entries = {}
    for section in original_prepared.sections:
        for task in section.writing_tasks:
            uid = task.unit_id
            if uid not in provider._inputs:
                continue
            base = build_partition_unit_input(original_prepared, section, task, original_options, original_constraints,
                                               frozen["proposals"], original_config)
            current_base = provider._inputs[uid]
            if rebind and rebind["version"] == REVIEW_REBIND_V2:
                same_input = _same_writing_input_after_clause_repair(base, current_base, rebind["project_constraint_plan_rebind"])
            else:
                same_input = _same_writing_input(base, current_base) if rebind else digest(base) == digest(current_base)
            _require(same_input, "REVIEW_CURRENT_INPUT_CHANGED")
            entry = packet["entries"][uid]
            _require(set(entry) == {"raw_candidate", "raw_candidate_sha256", "candidate", "candidate_sha256",
                "operation", "unit_input_hash", "request_sha256", "response_sha256", "call_id", "trace_id", "span_id",
                "origin_status"}, "REVIEW_ENTRY_INVALID")
            success = task.title.startswith("质量保证")
            node, record, input_value, output, attempt = origin.unit("N4", uid, successful=success)
            _require(record["status"] == entry["origin_status"] == ("succeeded" if success else "failed")
                     and input_value == base and digest(input_value) == entry["unit_input_hash"], "REVIEW_ORIGIN_INPUT_CHANGED")
            if not success:
                attempt = record["attempts"][-1]
                span = origin.span(attempt["trace_id"], attempt["span_id"])
                parent = next(item for item in node["attempts"] if item["attempt_id"] == attempt["node_attempt_id"])
                _require(attempt["status"] == "failed" and attempt["input_hash"] == record["input_hash"]
                         and span["name"] == "unit.N4" and span["status"] == "ERROR"
                         and span["parent_span_id"] == parent["span_id"]
                         and span["attributes"].get("unit_attempt_id") == attempt["attempt_id"], "REVIEW_FAILED_UNIT_CHANGED")
            calls = [item for item in origin.events if item.get("event") == ("call.completed" if success else "call.failed")
                     and item.get("unit_attempt_id") == attempt["attempt_id"]]
            _require(len(calls) == 1 and calls[0]["call_attempt"] == 1, "REVIEW_ACTUAL_CALL_REQUIRED")
            event = calls[0]
            relation = origin.call(node, uid, attempt, event["call_id"], 1, passed=success)
            captures = origin.captures(node, uid, attempt)
            _require(sum(item["name"].startswith("request-") for item in captures) == 1, "REVIEW_SINGLE_ATTEMPT_REQUIRED")
            wire = build_json_request(ChatConfig(**{k: v for k, v in base["provider"].items() if k != "mode"}),
                                      commitment_payload(base), SYSTEM_PROMPT)
            if rebind:
                current_wire = build_json_request(
                    ChatConfig(**{k: v for k, v in current_base["provider"].items() if k != "mode"}),
                    commitment_payload(current_base), SYSTEM_PROMPT)
                _require(current_wire == wire, "REVIEW_REBIND_WIRE_CHANGED")
            _require(relation["request"] == wire and digest(wire) == entry["request_sha256"]
                     and digest(relation["response"]) == entry["response_sha256"]
                     and all(entry[key] == event[key] for key in ("call_id", "trace_id", "span_id"))
                     and event.get("prompt_version") == PROMPT_VERSION and event.get("operation") == "commitment_drafting"
                     and event.get("model") == base["provider"]["model"], "REVIEW_ORIGIN_WIRE_CHANGED")
            original = relation["candidate"]
            _require(digest(original) == entry["raw_candidate_sha256"] and original == entry["raw_candidate"],
                     "REVIEW_RAW_CANDIDATE_CHANGED")
            if success:
                _verify_live_unit(ctx, original_prepared, uid, base, original_constraints, origin)
            else:
                _require(relation["validation"] == {"call_attempt": 1, "call_id": event["call_id"],
                    "unit_id": uid, "status": "failed", "error_code": "PROVIDER_SCHEMA_INVALID",
                    "schema_errors": [{"loc": ["coverage_contract"], "type": "extra_forbidden"}]}, "REVIEW_ORIGIN_FAILURE_CHANGED")
            candidate = _reviewed_transform(base, original, entry["operation"])
            if rebind:
                _require(_reviewed_transform(current_base, original, entry["operation"]) == candidate,
                         "REVIEW_REBIND_CANDIDATE_CHANGED")
            _require(candidate == entry["candidate"] and digest(candidate) == entry["candidate_sha256"],
                     "REVIEW_CANDIDATE_CHANGED")
            proof = {"packet_sha256": binding["sha256"], "entry_sha256": digest(entry),
                "origin_run_id": origin.run_id, "origin_status": entry["origin_status"],
                **{key: entry[key] for key in ("unit_input_hash", "request_sha256", "response_sha256", "raw_candidate_sha256",
                                              "candidate_sha256", "call_id", "trace_id", "span_id")},
                "operation": entry["operation"]["kind"], "source_scope_sha256": digest(base["commitment_drafting"]["source_scope"]),
                "new_external_calls": 0, "company_confirmation": "pending", "satisfaction_status": "not_determined"}
            if rebind:
                proof["source_rebind"] = {**rebind, "origin_unit_input_sha256": digest(base),
                    "current_unit_input_sha256": digest(current_base), "unchanged_request_sha256": digest(wire),
                    "ignored_input_fields": ["project_constraint_artifact_refs"]}
            entries[uid] = {"candidate": candidate, "proof": proof, "usage": relation["response"].get("usage", {}),
                            "base": copy.deepcopy(provider._inputs[uid])}
            provider._inputs[uid]["reviewed_candidate"] = proof
    _require(set(entries) == set(provider._inputs), "REVIEW_REBIND_UNIT_SET_CHANGED")
    origin.unchanged()
    provider._reviewed, provider._review_origin, provider._review_packet_raw = entries, origin, raw


class CommitmentPartitionProvider:
    def __init__(self, provider_options, import_config, config):
        from .writing_candidate_import import DiagnosticWritingCandidateImport
        _require(provider_options.get("mode") == MODE, "PROVIDER_INVALID")
        self.options, self.config = copy.deepcopy(provider_options), copy.deepcopy(config)
        self.importer = DiagnosticWritingCandidateImport(provider_options, import_config, commitment_partition=config)
        self._inputs, self._ready, self._ctx = {}, None, None
        self._live = None
        self._reviewed, self._review_origin, self._review_packet_raw = {}, None, None

    def _prepare(self, ctx, prepared_dict, bases):
        _require(ctx.execution_config.get("commitment_drafting") == self.config, "CONFIG_CHANGED")
        prepared = PreparedBid.model_validate(prepared_dict)
        from .project_constraint_runtime import load_project_constraints
        constraints = load_project_constraints(ctx, prepared_dict)
        allowed = checked_commitment_partition(prepared_dict, self.config, constraints)
        n3 = next(node for node in ctx._state["nodes"] if node["key"] == "N3")
        frozen = ctx.executor._checkpoint_data(n3)
        _require(frozen["prepared"] == prepared_dict, "CURRENT_PREPARED_MISMATCH")
        expected = {task.unit_id: build_partition_unit_input(prepared, section, task, self.options, constraints,
                    frozen["proposals"], self.config) for section in prepared.sections for task in section.writing_tasks}
        _require(expected == bases, "CURRENT_INPUT_MISMATCH")
        old_bases = {uid: base for uid, base in bases.items() if uid not in allowed}
        # The importer checks the WHOLE frozen plan, then explicitly proves the
        # extra two tasks. It never receives a fabricated prepared subset.
        self.importer._prepare(ctx, prepared_dict, old_bases)
        self._inputs = {uid: copy.deepcopy(bases[uid]) for uid in allowed}
        source_map = {source.source_id: source for source in prepared.sources}
        for base in self._inputs.values():
            for source in base["sources"]:
                item = source_map[source["source_id"]]
                self.importer._read(item.path, item.sha256)
        self.importer._seal()
        if "reviewed_candidates" in self.config:
            _prepare_reviewed(ctx, self, prepared, constraints)
        return prepared, old_bases

    def preflight(self, ctx, prepared_dict, bases):
        self._ready = None
        prepared, old_bases = self._prepare(ctx, prepared_dict, bases)
        self.importer.preflight(ctx, prepared_dict, old_bases)
        evidence = None
        for uid, base in self._inputs.items():
            record = ctx._record.get("units", {}).get(uid)
            if record and record.get("status") == "succeeded":
                _require(record["input_hash"] == digest(base)
                         and ctx.executor.store.read_json(record["input_ref"]) == base, "CHECKPOINT_INPUT_CHANGED")
                ctx.executor._checkpoint_data(record)
                if evidence is None:
                    from .project_constraint_runtime import load_project_constraints
                    constraints = load_project_constraints(ctx, prepared_dict)
                    evidence = _current_evidence(ctx)
                if self._reviewed:
                    _verify_reviewed_unit(ctx, prepared, uid, base, constraints, evidence, self)
                else:
                    _verify_live_unit(ctx, prepared, uid, base, constraints, evidence)
        # All authorization, historical footprint and source checks precede
        # construction. There is no branch to pay for an unrecognized task.
        if self._reviewed:
            self._review_packet_ref = ctx.store_bytes("reviewed-commitments.json", self._review_packet_raw, "application/json")
            self._origin_snapshot_refs = [{"origin_path": path, "snapshot": ctx.store_bytes(
                "reviewed-commitment-origin" + Path(path).suffix, raw,
                "application/x-ndjson" if path.endswith(".jsonl") else "application/json")}
                for path, raw in self._review_origin.evidence.items()]
        else:
            self._live = ChatProposalProvider(ChatConfig(**{key: item for key, item in self.config["direct_provider"].items()
                                                           if key != "mode"}))
        self._ctx = ctx
        self._ready = (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config))
        capture = ctx.capture("commitment-partition-preflight", self.summary())
        _require(capture.get("status") == "captured", "CAPTURE_REQUIRED")
        ctx._observer.check()

    def unit_input(self, uid, base):
        _require(self._ready is not None, "PREFLIGHT_REQUIRED")
        if uid not in self._inputs:
            return self.importer.unit_input(uid, base)
        # Pipeline supplies the fully reconstructed partition base for new units.
        _require(base == (self._reviewed[uid]["base"] if self._reviewed else self._inputs[uid]), "CURRENT_INPUT_MISMATCH")
        return copy.deepcopy(self._inputs[uid])

    def generate(self, ctx, context, requirements):
        _require(self._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
        uid = context.get("section_id")
        if uid not in self._inputs:
            return self.importer.generate(ctx, context, requirements)
        base = self._inputs[uid]
        _require(base["context"] == context and base["requirements"] == requirements, "CURRENT_INPUT_MISMATCH")
        record = ctx._record["units"][uid]
        _require(ctx._active_unit.get("unit_id") == uid
                 and ctx.executor.store.read_json(record["input_ref"]) == base, "CHECKPOINT_INPUT_CHANGED")
        self.importer._seal()
        if self._reviewed:
            self._review_origin.unchanged()
            entry = self._reviewed[uid]
            proof = entry["proof"]
            with ctx._observer.span("candidate.reviewed_import", {**ctx._metadata(), "operation": "commitment_reviewed_import",
                    "input_hash": digest(base), "output_hash": proof["candidate_sha256"]},
                    links=[{"trace_id": proof["trace_id"], "span_id": proof["span_id"]}]):
                capture = ctx.capture("commitment-reviewed-candidate-" + uid,
                    {"candidate": entry["candidate"], "binding": proof, "external_call": False})
                _require(capture.get("status") == "captured", "REVIEW_CAPTURE_REQUIRED")
                ctx.call_event("candidate.revalidated", {"operation": "commitment_reviewed_import",
                    "input_hash": digest(base), "output_hash": proof["candidate_sha256"], "status": "passed"})
            ctx._observer.check()
            return ProposalContent.model_validate(entry["candidate"])
        result = self._live.generate_json(ctx, commitment_payload(base), unit_id=uid, prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT, validate=lambda value: validate_commitment_candidate(base, value),
            task_type="commitment_drafting")
        return ProposalContent.model_validate(result)

    def summary(self):
        rows = [row for row in (self._ctx._state.get("call_budget", {}).get("reservations", []) if self._ctx else [])
                if row.get("unit_id") in self._inputs]
        known = sum((Decimal(row["known_usage_cost_estimate_usd"]) for row in rows
                     if row.get("settlement_status") == "known_usage_cost_estimate"), Decimal(0))
        old_usage = [{"unit_id": uid, "origin_run_id": entry["origin"]["origin_run_id"],
                      "call_id": entry["origin"]["call_id"],
                      "reported_usage": copy.deepcopy(entry["origin"]["historical_usage"])}
                     for uid, entry in self.importer._entries.items()]
        unknown = sum((Decimal(row["estimated_upper_usd"]) for row in rows
                       if row.get("settlement_status") != "known_usage_cost_estimate"), Decimal(0))
        result = {"mode": MODE, "diagnostic_only": True, "company_confirmation": "pending",
                "satisfaction_status": "not_determined", "historical": self.importer.summary(),
                "historical_usage": old_usage, "historical_cost_status": "preserved_in_origin_not_recomputed_or_charged",
                "historical_calls_charged_again": 0, "new_unit_count": len(self._inputs),
                "new_actual_call_attempts": len(rows), "new_known_usage_estimate_usd": str(known),
                "new_unknown_reserved_upper_usd": str(unknown), "new_protected_charge_usd": str(known + unknown),
                "new_unknown_attempt_count": sum(row.get("settlement_status") != "known_usage_cost_estimate" for row in rows),
                "new_usage_scope": "this run's actual new calls only; historical cost is not recharged",
                "allowed_units": copy.deepcopy(self.config.get("allowed_units", {}))}
        if self._reviewed:
            result["reviewed_imports"] = {"count": len(self._reviewed), "new_external_calls": 0,
                "historical_calls_charged_again": 0,
                "origins": [{"unit_id": uid, **copy.deepcopy(entry["proof"]), "usage": copy.deepcopy(entry["usage"])}
                            for uid, entry in self._reviewed.items()],
                "packet_snapshot": getattr(self, "_review_packet_ref", None),
                "origin_snapshots": getattr(self, "_origin_snapshot_refs", [])}
        return result


def _current_evidence(ctx):
    """Reuse strict artifact/span/call linkage checks for sealed current units.

    The general replay reader requires a finished source run. Here only N4's
    successful units are read, while N5/the resumed N4 legitimately runs. Its
    full completed-run constructor is therefore replaced by this explicit
    current-run reader; all unit/call integrity checks remain in force.
    """
    from .diagnostic_replay import _OriginRun

    class CurrentEvidence(_OriginRun):
        def __init__(self):
            self.alias, self.run_id = "current-commitments", ctx.run_id
            self.store, self.root = ctx.executor.store, ctx.executor.store.root
            self.state, self.evidence = ctx._state, {}
            self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")

    return CurrentEvidence()


def _verify_live_unit(ctx, prepared, uid, base, constraints, evidence):
    from .writing_candidate_import import expected_import_output, expected_import_review
    section, task = next((section, task) for section in prepared.sections for task in section.writing_tasks
                         if task.unit_id == uid)
    node, record, actual_input, output, attempt = evidence.unit("N4", uid)
    _require(actual_input == base and record["input_hash"] == digest(base), "CHECKPOINT_INPUT_CHANGED")
    events = [item for item in evidence.events if item.get("event") == "call.completed"
              and item.get("unit_id") == uid and item.get("unit_attempt_id") == attempt["attempt_id"]]
    _require(len(events) == 1, "ACTUAL_CALL_REQUIRED")
    event = events[0]
    relation = evidence.call(node, uid, attempt, event["call_id"], event["call_attempt"], passed=True)
    captures = evidence.captures(node, uid, attempt)
    _require(sum(item["name"].startswith("request-") for item in captures) == 1, "SINGLE_ATTEMPT_REQUIRED")
    config = ChatConfig(**{key: item for key, item in base["provider"].items() if key != "mode"})
    wire = build_json_request(config, commitment_payload(base), SYSTEM_PROMPT)
    _require(relation["request"] == wire and event.get("prompt_version") == PROMPT_VERSION
             and event.get("operation") == "commitment_drafting" and event.get("model") == config.model,
             "CALL_INPUT_CHANGED")
    validated = validate_commitment_candidate(base, relation["candidate"])
    validation = relation["validation"]
    _require(validation == {"call_id": event["call_id"], "call_attempt": 1, "unit_id": uid,
                            "status": "passed", "validated_output": validated}, "VALIDATION_CHANGED")
    candidate = ProposalContent.model_validate(validated)
    expected_review = expected_import_review(section, task, base, candidate, constraints)
    expected_review["commitment_drafting"] = commitment_review(base)
    blocks = expected_import_output(prepared, section, task, base, candidate)
    _require(output == {"blocks": blocks, "review": expected_review}, "OUTPUT_CHANGED")
    return {"unit_id": uid, "section_id": section.section_id, "candidate_sha256": digest(validated),
            "request_sha256": digest(wire), "response_sha256": digest(relation["response"]),
            "call_id": event["call_id"], "trace_id": event["trace_id"], "span_id": event["span_id"],
            "source_scope_sha256": digest(base["commitment_drafting"]["source_scope"]),
            "provenance_status": "checked", "company_confirmation": "pending",
            "satisfaction_status": "not_determined"}, output


def _verify_reviewed_unit(ctx, prepared, uid, base, constraints, evidence, provider):
    """Prove an offline edit independently of its unchanged originating call."""
    from .writing_candidate_import import expected_import_output, expected_import_review
    section, task = next((section, task) for section in prepared.sections for task in section.writing_tasks
                         if task.unit_id == uid)
    node, record, actual_input, output, attempt = evidence.unit("N4", uid)
    entry = provider._reviewed[uid]
    proof = entry["proof"]
    _require(actual_input == base and record["input_hash"] == digest(base)
             and base["reviewed_candidate"] == proof, "REVIEW_CHECKPOINT_INPUT_CHANGED")
    _require(not any(row.get("unit_id") == uid for row in ctx._state.get("call_budget", {}).get("reservations", []))
             and not any(row.get("unit_id") == uid and row.get("event") in
                         {"call.started", "call.completed", "call.failed"} for row in evidence.events),
             "REVIEW_EXTERNAL_CALL_FORBIDDEN")
    spans = [row for row in evidence.spans if row.get("name") == "candidate.reviewed_import"
             and row.get("trace_id") == attempt["trace_id"]
             and row.get("parent_span_id") == attempt["span_id"]]
    _require(len(spans) == 1, "REVIEW_IMPORT_SPAN_REQUIRED")
    span = spans[0]
    expected_link = {"trace_id": proof["trace_id"], "span_id": proof["span_id"]}
    _require(span["status"] != "ERROR" and span.get("links") == [expected_link]
             and span["attributes"].get("operation") == "commitment_reviewed_import"
             and span["attributes"].get("unit_attempt_id") == attempt["attempt_id"]
             and span["attributes"].get("input_hash") == digest(base)
             and span["attributes"].get("output_hash") == proof["candidate_sha256"], "REVIEW_IMPORT_SPAN_CHANGED")
    events = [row for row in evidence.events if row.get("event") == "candidate.revalidated"
              and row.get("unit_id") == uid and row.get("unit_attempt_id") == attempt["attempt_id"]]
    _require(len(events) == 1 and all(events[0].get(key) == value for key, value in {
        "operation": "commitment_reviewed_import", "input_hash": digest(base),
        "output_hash": proof["candidate_sha256"], "status": "passed",
        "trace_id": span["trace_id"], "span_id": span["span_id"]}.items()), "REVIEW_IMPORT_EVENT_CHANGED")
    captures = evidence.captures(node, uid, attempt)
    _require(not any(row["name"].startswith(("request-", "response-", "validation-")) for row in captures),
             "REVIEW_EXTERNAL_CALL_FORBIDDEN")
    named = [row for row in captures if row["name"] == "commitment-reviewed-candidate-" + uid]
    # Capture records bind the unit/attempt/trace; the matching revalidation
    # event above supplies the active import span (capture rows have no span_id).
    _require(len(named) == 1, "REVIEW_CAPTURE_REQUIRED")
    captured = evidence.capture(named, "commitment-reviewed-candidate-" + uid)
    _require(captured == {"candidate": entry["candidate"], "binding": proof, "external_call": False},
             "REVIEW_CAPTURE_CHANGED")
    candidate = ProposalContent.model_validate(validate_commitment_candidate(base, entry["candidate"]))
    expected_review = expected_import_review(section, task, base, candidate, constraints)
    expected_review["commitment_drafting"] = commitment_review(base)
    blocks = expected_import_output(prepared, section, task, base, candidate)
    _require(output == {"blocks": blocks, "review": expected_review}, "REVIEW_OUTPUT_CHANGED")
    return {"unit_id": uid, "section_id": section.section_id, **copy.deepcopy(proof),
            "import_trace_id": span["trace_id"], "import_span_id": span["span_id"],
            "provenance_status": "checked", "execution": "reviewed_offline_import"}, output


def validate_commitment_partition_links(ctx, value, package):
    """N5 proves historical nine and either live or reviewed extra two paths."""
    if ctx.execution_config.get("provider", {}).get("mode") != MODE:
        return None
    from .project_constraint_runtime import load_project_constraints
    from .writing_candidate_import import validate_candidate_import_links
    try:
        provider = CommitmentPartitionProvider(ctx.execution_config["provider"],
            ctx.execution_config["writing_candidate_import"], ctx.execution_config["commitment_drafting"])
        nodes = {node["key"]: node for node in ctx._state["nodes"]}
        _require(nodes.get("N4", {}).get("status") == "succeeded", "N4_REQUIRED")
        frozen = ctx.executor._checkpoint_data(nodes["N3"])
        prepared = PreparedBid.model_validate(frozen["prepared"])
        _require(package.project == prepared.project and package.company == prepared.company, "PACKAGE_IDENTITY_CHANGED")
        constraints = load_project_constraints(ctx, frozen["prepared"])
        bases = {task.unit_id: build_partition_unit_input(prepared, section, task, provider.options,
            constraints, frozen["proposals"], provider.config) for section in prepared.sections for task in section.writing_tasks}
        provider._prepare(ctx, frozen["prepared"], bases)
        reviews = value.get("writing_review", [])
        by_uid = {item["unit_id"]: item for item in reviews}
        _require(len(reviews) == len(by_uid) == len(bases) == 11 and set(by_uid) == set(bases), "REVIEW_CHANGED")
        historical = validate_candidate_import_links(ctx, value, package, partition_provider=provider.importer)
        evidence = _current_evidence(ctx)
        actual = {section.section_id: [block.model_dump(mode="json") for block in section.blocks]
                  for section in package.sections}
        live, reviewed = [], []
        for uid, base in provider._inputs.items():
            if provider._reviewed:
                link, output = _verify_reviewed_unit(ctx, prepared, uid, base, constraints, evidence, provider)
                reviewed.append(link)
            else:
                link, output = _verify_live_unit(ctx, prepared, uid, base, constraints, evidence)
                live.append(link)
            _require(by_uid[uid] == output["review"], "REVIEW_CHANGED")
            blocks, content = output["blocks"], actual[link["section_id"]]
            _require(sum(content[index:index + len(blocks)] == blocks
                         for index in range(len(content) - len(blocks) + 1)) == 1, "ASSEMBLY_CHANGED")
        provider.importer._seal()
        if provider._review_origin is not None:
            provider._review_origin.unchanged()
        reservations = ctx._state.get("call_budget", {}).get("reservations", [])
        _require(not any(row.get("node_key") == "N4" and row.get("unit_id") not in provider._inputs
                         for row in reservations), "HISTORICAL_LIVE_CALL_FORBIDDEN")
        provider._ctx = ctx
        return {"historical_unit_count": 9, "historical_correction_links": historical,
                "new_unit_count": 2, "new_actual_call_links": live,
                "reviewed_new_unit_links": reviewed,
                "company_confirmation": "pending", "satisfaction_status": "not_determined",
                "usage": provider.summary()}
    except ProviderFailure:
        raise
    except Exception as error:
        raise ProviderFailure("COMMITMENT_N5_INVALID") from error
