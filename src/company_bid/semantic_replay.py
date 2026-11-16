"""Authenticate selected historical semantic calls without transport or billing."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .diagnostic_replay import _OriginRun
from .provider import ChatConfig, ProviderFailure, build_json_request
from .semantic_materials import PROMPT_VERSION, SYSTEM_PROMPT, validate_response
from .storage import digest


REPLAY_VERSION = "authenticated-semantic-replay.v2"


def _require(condition, code):
    if not condition:
        raise ProviderFailure("SEMANTIC_REPLAY_" + code)


def _one(rows, code):
    values = list(rows)
    _require(len(values) == 1, code)
    return values[0]


class _SemanticOrigin(_OriginRun):
    """Use the shared unit/call proofs without its extraction-provider adapter.

    Read the global immutable ledger once; only requested units have their
    checkpoints, candidates, requests and responses authenticated.
    """
    def __init__(self, store, run_id):
        self.alias, self.root, self.run_id = "semantic-origin", store.root, run_id
        self.store, self.evidence = store, {}
        _require(store.run_dir(run_id).is_dir(), "ORIGIN_MISSING")
        self.state_raw = self._file("state.json")
        self.state = json.loads(self.state_raw)
        _require(self.state.get("run_id") == run_id and self.state.get("schema_version") == "m1.local.v1"
            and self.state.get("status") in {"succeeded", "paused_debug"}, "ORIGIN_NOT_SEALED")
        _require(self.state.get("config_hash") == digest(self.state.get("execution_config"))
            and self.state.get("capture_policy") is True
            and self.state.get("observability", {}).get("trace_status") == "captured"
            and self.state.get("observability", {}).get("capture_status") == "captured", "ORIGIN_DIAGNOSTICS_INVALID")
        self.events, self.spans = self._rows("events.jsonl"), self._rows("spans.jsonl")
        node = self.node("N3")
        _require(node.get("status") == "succeeded" and node.get("attempts")
            and node["attempts"][-1]["status"] == "succeeded", "SUCCESSFUL_ORIGINAL_N3_REQUIRED")

    def unit(self, node_key, unit_id, *, successful=True):
        if not successful or not unit_id.startswith("semantic-query-"):
            return super().unit(node_key, unit_id, successful=successful)
        node, unit, payload, _, _ = super().unit(node_key, unit_id, successful=False)
        _require(node_key == "N3" and unit.get("status") == "succeeded" and unit.get("attempts"),
                 "SUCCESSFUL_ORIGINAL_QUERY_REQUIRED")
        attempt = unit["attempts"][-1]
        _require(attempt.get("status") == "succeeded"
            and isinstance(attempt.get("attempt_id"), str) and re.fullmatch(r"[0-9a-f]{32}", attempt["attempt_id"])
            and attempt["attempt_id"] != "0" * 32 and attempt.get("input_hash") == unit["input_hash"]
            and attempt.get("output_hash") == unit["output_hash"], "QUERY_ATTEMPT_INVALID")
        envelope = self.checkpoint(unit, node_key, unit_id)
        _require(envelope.get("node_attempt_id") == attempt.get("node_attempt_id"), "QUERY_CHECKPOINT_INVALID")
        node_attempt = _one((a for a in node["attempts"] if a["attempt_id"] == attempt["node_attempt_id"]),
                            "QUERY_NODE_ATTEMPT_INVALID")
        span = self.span(attempt["trace_id"], attempt["span_id"])
        node_span = self.span(attempt["trace_id"], node_attempt["span_id"])
        _require(span["name"] == "unit.N3" and span["status"] != "ERROR"
            and span["attributes"].get("unit_attempt_id") == attempt["attempt_id"]
            and span["attributes"].get("unit_id") == unit_id
            and span["attributes"].get("node_attempt_id") == attempt["node_attempt_id"]
            and all(span["attributes"].get(key) == unit[key] for key in ("input_hash", "output_hash"))
            and node_span["name"] == "node.N3" and node_span["status"] != "ERROR"
            and node_attempt.get("status") == "succeeded" and node_attempt["trace_id"] == attempt["trace_id"],
            "QUERY_TRACE_INVALID")
        if "plan-assemble" in node.get("units", {}):
            # The real planner runs these queries inside one successful outer
            # plan-assemble checkpoint. Authenticate that exact direct parent;
            # arbitrary ancestors or merely same-trace spans are not allowed.
            _, parent, _, _, parent_attempt = super().unit("N3", "plan-assemble")
            _require(parent["status"] == "succeeded" and parent_attempt["status"] == "succeeded"
                and parent_attempt["node_attempt_id"] == attempt["node_attempt_id"]
                and parent_attempt["trace_id"] == attempt["trace_id"]
                and span.get("parent_span_id") == parent_attempt["span_id"], "QUERY_PARENT_INVALID")
        else:
            _require(span.get("parent_span_id") == node_attempt["span_id"], "QUERY_PARENT_INVALID")
        return node, unit, payload, envelope["data"], attempt


class AuthenticatedSemanticProvider:
    """Replay exact raw candidates only; there is no live-provider fallback."""
    is_recorded_replay = True

    def __init__(self, ctx, chat_config: ChatConfig, binding: dict):
        _require(ctx.execution_config.get("run_purpose") == "diagnostic_replay"
            and ctx.artifact_scope == "diagnostic" and ctx.node_key == "N3"
            and ctx.execution_config.get("debug_capture", True) is True
            and ctx._state.get("capture_policy") is True, "DIAGNOSTIC_N3_REQUIRED")
        _require(isinstance(binding, dict) and set(binding) == {"run_id"}
            and isinstance(binding["run_id"], str) and re.fullmatch(r"[0-9a-f]{32}", binding["run_id"])
            and binding["run_id"] != ctx.run_id, "BINDING_INVALID")
        _require(isinstance(chat_config, ChatConfig), "PROVIDER_CONFIG_INVALID")
        self.config, self.binding = chat_config, copy.deepcopy(binding)
        self._identity = (ctx.run_id, ctx.attempt_id, ctx.node_key, ctx.executor.store.root, digest(ctx.execution_config))
        self.origin = _SemanticOrigin(ctx.executor.store, binding["run_id"])
        original_options = self.origin.state["execution_config"].get("planning", {}).get("semantic_material_recall", {}).get("provider")
        _require(isinstance(original_options, dict) and ChatConfig(**original_options) == chat_config,
                 "PROVIDER_CONFIG_CHANGED")
        self._snapshots, self._used = {}, set()

    def _snapshot_evidence(self, ctx):
        for path, raw in self.origin.evidence.items():
            if path not in self._snapshots:
                name = "semantic-origin-" + hashlib.sha256(path.encode()).hexdigest()[:20]
                name += ".jsonl" if path.endswith(".jsonl") else ".json"
                ref = ctx.store_bytes(name, raw, "application/x-ndjson" if path.endswith(".jsonl") else "application/json")
                _require(ref["sha256"] == hashlib.sha256(raw).hexdigest(), "SNAPSHOT_HASH_CHANGED")
                self._snapshots[path] = {"origin_path": path, "snapshot_ref": ref}
        return [copy.deepcopy(self._snapshots[path]) for path in self.origin.evidence]

    def generate_json(self, ctx, user_input: dict, *, unit_id: str, prompt_version: str,
                      system_prompt: str, validate, task_type: str, image: dict | None = None) -> dict:
        _require(self._identity == (ctx.run_id, ctx.attempt_id, ctx.node_key, ctx.executor.store.root,
                                    digest(ctx.execution_config)), "CONTEXT_CHANGED")
        _require(image is None and prompt_version == PROMPT_VERSION and system_prompt == SYSTEM_PROMPT
            and task_type == "material_semantic_recall", "OPERATION_CHANGED")
        _require(isinstance(user_input, dict) and isinstance(user_input.get("query_id"), str)
            and unit_id == "semantic-query-" + digest(user_input["query_id"])[:24]
            and ctx._active_unit.get("unit_id") == unit_id and unit_id not in self._used, "QUERY_IDENTITY_INVALID")
        self.origin.unchanged()
        _require(unit_id in self.origin.node("N3").get("units", {}), "UNRECORDED_QUERY")
        node, unit, unit_input, output, attempt = self.origin.unit("N3", unit_id)
        _require(unit_input.get("route") == "semantic_recall_needed" and unit_input.get("payload") == user_input
            and unit_input.get("prompt_version") == prompt_version and output.get("requirement_id") == user_input["query_id"]
            and output.get("model_called") is True, "UNIT_INPUT_CHANGED")
        call = _one((row for row in self.origin.events if row.get("unit_attempt_id") == attempt["attempt_id"]
            and row.get("event") in {"call.completed", "call.failed"}), "SINGLE_SUCCESSFUL_CALL_REQUIRED")
        _require(call["event"] == "call.completed" and call.get("call_attempt") == 1
            and call.get("validation_status") == "passed" and call.get("operation") == task_type
            and call.get("prompt_version") == prompt_version and call.get("model") == self.config.model,
            "ORIGIN_CALL_NOT_VALIDATED")
        relation = self.origin.call(node, unit_id, attempt, call["call_id"], 1, passed=True)
        wire = build_json_request(self.config, user_input, system_prompt)
        _require(relation["request"] == wire, "EXACT_REQUEST_CHANGED")
        candidate = relation["candidate"]
        _require(relation["validation"] == {"call_id": call["call_id"], "call_attempt": 1,
            "unit_id": unit_id, "status": "passed", "validated_output": candidate}
            and output.get("response_sha256") == digest(candidate), "ORIGINAL_VALIDATION_CHANGED")
        reservation = _one((r for r in self.origin.state.get("call_budget", {}).get("reservations", [])
            if r.get("call_id") == call["call_id"] and r.get("call_attempt") == 1), "RESERVATION_INVALID")
        _require(reservation.get("settlement_status") == "known_usage_cost_estimate"
            and reservation.get("response_sha256") == digest(relation["response"]), "USAGE_NOT_SETTLED")
        request_captures = self.origin.captures(node, unit_id, attempt)
        request_ref = _one((c["ref"] for c in request_captures if c["name"] == "request-" + call["call_id"] + "-1"), "REQUEST_CAPTURE_INVALID")
        response_ref = _one((c["ref"] for c in request_captures if c["name"] == "response-" + call["call_id"] + "-1"), "RESPONSE_CAPTURE_INVALID")
        metadata = {"unit_id": unit_id, "input_hash": digest(wire), "origin_run_id": self.origin.run_id}
        with ctx._observer.span("material.semantic_replay", metadata,
                               links=[{"trace_id": call["trace_id"], "span_id": call["span_id"]}]):
            snapshots = self._snapshot_evidence(ctx)
            proof = {"version": REPLAY_VERSION, "origin_run_id": self.origin.run_id,
                "origin_node_key": "N3", "origin_unit_id": unit_id, "origin_unit_input_sha256": unit["input_hash"],
                "origin_unit_output_sha256": unit["output_hash"], "origin_call_id": call["call_id"],
                "origin_trace_id": call["trace_id"], "origin_span_id": call["span_id"],
                "request_sha256": digest(wire), "response_sha256": digest(relation["response"]),
                "candidate_sha256": digest(candidate), "request_snapshot": self._snapshots[request_ref["path"]]["snapshot_ref"],
                "response_snapshot": self._snapshots[response_ref["path"]]["snapshot_ref"],
                "evidence_snapshots": snapshots, "historical_usage": copy.deepcopy(relation["response"].get("usage")),
                "historical_budget_reservation": copy.deepcopy(reservation), "new_external_calls": 0,
                "historical_calls_charged_again": 0, "qualification_result": "not_determined"}
            try:
                validated = validate_response(copy.deepcopy(candidate), user_input)
                result = validate(copy.deepcopy(candidate))
                _require(validated == candidate and result == candidate, "VALIDATOR_CHANGED_CANDIDATE")
                self.origin.unchanged()
            except Exception as error:
                code = getattr(error, "error_code", "SEMANTIC_REPLAY_VALIDATION_FAILED")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,100}", code):
                    code = "SEMANTIC_REPLAY_VALIDATION_FAILED"
                captured = ctx.capture("semantic-replay-failure-" + unit_id, {**proof, "status": "failed", "error_code": code}, partial=True)
                _require(captured["status"] != "write_failed", "CAPTURE_FAILED")
                raise
            captured = ctx.capture("semantic-replay-" + unit_id, {**proof, "status": "passed", "validated_output": result})
            _require(captured["status"] == "captured", "CAPTURE_FAILED")
            self.origin.unchanged()
            ctx.call_event("material.semantic_response_replayed", {**metadata, "output_hash": digest(result), "validation_status": "passed"})
        self._used.add(unit_id)
        return result
