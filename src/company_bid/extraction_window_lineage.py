"""Authenticate an ordered continuation of paid N2 failures and checkpoints."""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass

from .contract_attachment_citation_split import SOURCE_CONFIG_KEY
from .diagnostic_replay import _id, _one, _require
from .extraction_failed_scoring_replay import (
    _allow_cold_candidate, _read_failed_call, _verify_replayed_checkpoint, _TASK,
)
from .extraction_numbered_clause_replay import rebuild_frozen_contract_windows
from .storage import digest


CONFIG_KEY = "failed_window_lineage"
CONFIG_INVALID = "FAILED_WINDOW_LINEAGE_CONFIG_INVALID"
SOURCE_INVALID = "FAILED_WINDOW_LINEAGE_SOURCE_INVALID"
PROOF_MISMATCH = "FAILED_WINDOW_LINEAGE_PROOF_MISMATCH"
# These settings are validator inputs only. They cannot change the frozen
# window order, source manifest, model prompt, or request body.
_REVIEW_KEYS = (SOURCE_CONFIG_KEY,)


def lineage_extraction_keys():
    return {CONFIG_KEY, *_REVIEW_KEYS}


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _without(data, *omitted):
    return {key: value for key, value in data.items() if key not in omitted}


def _review_scopes(settings, key):
    if key not in settings:
        return []
    reviews = settings[key]
    return reviews if isinstance(reviews, list) else [reviews]


@dataclass(frozen=True)
class _StoppedRun:
    proof: dict
    actual: tuple[str, ...]
    inherited: tuple[str, ...]
    run: object
    n1: dict
    preflight: dict
    windows: dict
    window_config: dict


class FailedWindowLineage:
    """Replay successful and failed actual calls, then authenticate every copy."""

    def __init__(self, replay, ctx):
        settings = ctx.execution_config["extraction"]
        proofs = settings[CONFIG_KEY]
        provider = ctx.execution_config.get("structured_provider") or ctx.execution_config.get("provider", {})
        self.base = replay._failed_contract_heading_only_annex
        _require(self.base is not None and isinstance(proofs, list) and proofs and
                 type(settings.get("window_concurrency")) is int and settings["window_concurrency"] == 1 and
                 provider.get("mode") == "diagnostic_mixed" and replay.provider_config.max_attempts == 1 and
                 type(provider.get("validation_repair_attempts")) is int and provider["validation_repair_attempts"] == 0,
                 CONFIG_INVALID)
        for proof in proofs:
            _require(isinstance(proof, dict) and set(proof) == {
                "unit_id", "call_id", "source_state_sha256", "original_error", "raw_candidate_sha256", "result_sha256"
            } and isinstance(proof["unit_id"], str) and _id(proof["call_id"]) and
                all(_hex(proof[key], 64) for key in ("source_state_sha256", "raw_candidate_sha256", "result_sha256")),
                CONFIG_INVALID)
            original = proof["original_error"]
            _require(isinstance(original, dict) and set(original) == {"error_code", "diagnostic_context"} and
                     isinstance(original["error_code"], str) and re.fullmatch(r"[A-Z0-9_]{1,80}", original["error_code"]) and
                     (original["diagnostic_context"] is None or isinstance(original["diagnostic_context"], dict)), CONFIG_INVALID)
        self.proofs = copy.deepcopy(proofs)
        ids = [proof["unit_id"] for proof in proofs]
        _require(len(ids) == len(set(ids)) and len({proof["call_id"] for proof in proofs}) == len(proofs), CONFIG_INVALID)
        for key in _REVIEW_KEYS:
            reviews = _review_scopes(settings, key)
            _require(all(isinstance(review, dict) and review.get("window_id") in ids for review in reviews) and
                     len({review["window_id"] for review in reviews}) == len(reviews), CONFIG_INVALID)
        self.ctx = ctx
        self.unit_id = ids[-1]
        self.stages = ()
        self.windows = {}
        self.checkpoints_verified = False
        self.prior_checkpoint_hashes = {}

    def added_extraction_keys(self):
        return lineage_extraction_keys()

    @staticmethod
    def successor_gate(replay):
        return None

    def actual_units(self, replay):
        order = tuple(replay.windows)
        ids = [self.base.unit_id, *(proof["unit_id"] for proof in self.proofs)]
        _require(all(unit in replay.windows and replay.windows[unit]["blocks"] for unit in ids), CONFIG_INVALID)
        positions = [order.index(unit) for unit in ids]
        _require(positions == sorted(set(positions)), CONFIG_INVALID)
        return order[positions[0] + 1:positions[-1] + 1]

    def _check_source_config(self, historical, replay, inherited, prior_proofs):
        target = self.ctx.execution_config
        provider_key = "structured_provider" if target.get("structured_provider") else "provider"
        _require(_without(historical, "extraction", "diagnostic_replay", provider_key) ==
                 _without(target, "extraction", "diagnostic_replay", provider_key), SOURCE_INVALID)
        _require(_without(historical[provider_key], "max_run_calls", "max_run_usd") ==
                 _without(target[provider_key], "max_run_calls", "max_run_usd"), SOURCE_INVALID)
        settings = replay._target_extraction
        expected = _without(settings, CONFIG_KEY, *_REVIEW_KEYS)
        if prior_proofs:
            expected[CONFIG_KEY] = prior_proofs
        historical_extraction = historical["extraction"]
        for key in _REVIEW_KEYS:
            reviews = [scope for scope in _review_scopes(settings, key) if scope["window_id"] in inherited]
            _require(digest(_review_scopes(historical_extraction, key)) == digest(reviews), SOURCE_INVALID)
        source_bindings = {_TASK + unit: replay.config["bindings"][_TASK + unit] for unit in inherited}
        _require(digest(_without(historical_extraction, *_REVIEW_KEYS)) == digest(expected) and
                 digest(historical.get("diagnostic_replay")) == digest({**replay.config, "bindings": source_bindings}),
                 SOURCE_INVALID)

    def _check_source_calls(self, run, prefix, actual, proof):
        node = run.node("N2")
        _require(set(node["units"]) == set(prefix) and all(len(node["units"][unit]["attempts"]) == 1 and
                 node["units"][unit]["status"] == ("failed" if unit == proof["unit_id"] else "succeeded")
                 for unit in prefix), SOURCE_INVALID)
        started = tuple(event.get("unit_id") for event in run.events
                        if event["event"] == "unit.started" and event.get("node_key") == "N2")
        calls = [event for event in run.events if event["event"] == "call.started" and event.get("node_key") == "N2"]
        fees = [row for row in run.state["call_budget"]["reservations"] if row.get("node_key") == "N2"]
        _require(started == prefix and tuple(event.get("unit_id") for event in calls) ==
                 tuple(row.get("unit_id") for row in fees) == actual and calls[-1].get("call_id") == proof["call_id"] and
                 len({event.get("call_id") for event in calls}) == len(actual), SOURCE_INVALID)
        for event, fee in zip(calls, fees, strict=True):
            _require(event.get("operation") == "extract_requirements" and _id(event.get("call_id")) and
                     event.get("call_id") == fee.get("call_id") and fee.get("settlement_status") == "known_usage_cost_estimate" and
                     type(event.get("call_attempt")) is type(fee.get("call_attempt")) is int and
                     event["call_attempt"] == fee["call_attempt"] == 1, SOURCE_INVALID)
        attempt = _one(node["attempts"], SOURCE_INVALID)
        return run.capture(attempt["captures"], "diagnostic-replay-preflight")

    def verify_history(self, replay):
        if self.stages:
            return
        self.base.verify_history(replay)
        order = tuple(replay.windows)
        all_actual = self.actual_units(replay)
        bindings = replay.config["bindings"]
        self.prefix = self.base.prefix + all_actual
        _require(self.prefix == order[:len(self.prefix)] and set(bindings) == {_TASK + unit for unit in self.prefix},
                 CONFIG_INVALID)
        self.windows, self.window_config = rebuild_frozen_contract_windows(replay, PROOF_MISMATCH)
        stages = []
        inherited, previous_run = self.base.prefix, self.base.run
        for i, proof in enumerate(self.proofs):
            end = order.index(proof["unit_id"]) + 1
            actual = order[len(inherited):end]
            binding = bindings[_TASK + proof["unit_id"]]
            _require(binding == {"alias": self.base.source_binding["alias"], "run_id": binding.get("run_id"),
                                 "node_key": "N2", "unit_id": proof["unit_id"]} and
                     all(bindings[_TASK + unit] == {**binding, "unit_id": unit} for unit in actual), CONFIG_INVALID)
            run = replay._run(binding["alias"], binding["run_id"])
            _require(run.mixed_source and run.state.get("status") == "failed" and
                     run.state.get("error_code") == proof["original_error"]["error_code"] and
                     hashlib.sha256(run.state_raw).hexdigest() == proof["source_state_sha256"] and
                     run.state.get("fork_from_node") == "N2" and run.state.get("parent_run_id") == previous_run.run_id,
                     SOURCE_INVALID)
            historical = run.state["execution_config"]
            self._check_source_config(historical, replay, inherited, self.proofs[:i])
            _, n1 = replay._n1(run)
            _require(n1["output_hash"] == digest(replay.current) and n1["output_ref"] == self.base.n1["output_ref"],
                     "SCOPE_MISMATCH")
            prefix = order[:end]
            preflight = self._check_source_calls(run, prefix, actual, proof)
            _require(preflight.get("binding_count") == preflight.get("verified_binding_count") == len(inherited) and
                     len(preflight.get("origins", [])) == len(inherited), SOURCE_INVALID)
            windows, window_config = rebuild_frozen_contract_windows(replay, PROOF_MISMATCH,
                                                                     extraction=historical["extraction"])
            stages.append(_StoppedRun(proof, actual, inherited, run, n1, preflight, windows, window_config))
            inherited, previous_run = prefix, run
        current_n1 = [node for node in self.ctx._state.get("nodes", []) if node["key"] == "N1"]
        if current_n1:
            n1 = stages[-1].n1
            target = _one(current_n1)
            _require(self.ctx._state.get("parent_run_id") == previous_run.run_id and self.ctx._state.get("fork_from_node") == "N2" and
                     target.get("output_ref") == n1["output_ref"] and target.get("output_hash") == n1["output_hash"],
                     "SCOPE_MISMATCH")
        self.stages = tuple(stages)

    def _prior_data(self, stage, unit_id):
        pos = self.prefix.index(unit_id)
        return stage.run.unit("N2", self.prefix[pos - 1])[3] if pos else None

    def _read_failure(self, replay, stage, task):
        from .extraction import PlanningError, _validate_normalized_window_result, run_extraction_window, validate_live_window

        proof, run = stage.proof, stage.run
        unit_id, call_id = proof["unit_id"], proof["call_id"]
        original = proof["original_error"]
        code, context = original["error_code"], original["diagnostic_context"]
        unit, call = _read_failed_call(replay, run, stage.n1, unit_id, call_id, task, code, SOURCE_INVALID)
        raw = call["candidate"]
        _require(digest(raw) == proof["raw_candidate_sha256"] and
                 digest(call["validation"].get("diagnostic_context")) == digest(context), PROOF_MISMATCH)
        prior = self._prior_data(stage, unit_id)
        try:
            validate_live_window(copy.deepcopy(raw), stage.windows[unit_id], predecessor_result=prior)
        except PlanningError as error:
            _require(error.code == code and digest(error.diagnostic_context) == digest(context), PROOF_MISMATCH)
        else:
            _require(False, PROOF_MISMATCH)

        def saved_candidate(_ctx, _task, _unit, _payload, validate):
            return validate(copy.deepcopy(raw))

        window = self.windows[unit_id]
        result = run_extraction_window(None, window, self.window_config, saved_candidate, predecessor_result=prior)
        _validate_normalized_window_result(result, window, predecessor_result=prior)
        _require(digest(result) == proof["result_sha256"] and result["raw_model_candidate"] == raw, PROOF_MISMATCH)
        terminal = call["terminal"]
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(raw),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False, "calls": [{
                      "source_alias": run.alias, "run_id": run.run_id, "call_id": call_id, "call_attempt": 1,
                      "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                      "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"basis": "authenticated_stopped_window_revalidation", "historical_status": "failed",
                                     "historical_candidate_modified": False, "historical_error_code": code,
                                     "historical_diagnostic_context": context, "source_N1_output_hash": stage.n1["output_hash"],
                                     "source_state_sha256": proof["source_state_sha256"],
                                     "revalidated_checkpoint_sha256": proof["result_sha256"]}]}
        return {"candidate": copy.deepcopy(raw), "request_sha256": digest(task), "origin": origin}

    def _read_actual(self, replay, stage, unit_id, task):
        from .extraction import _validate_normalized_window_result

        run = stage.run
        node, unit, payload, data, attempt = run.unit("N2", unit_id)
        _require(digest({**payload, "task_type": "extract_requirements"}) == digest(task), "REQUEST_MISMATCH")
        replay._source_window(run, payload, unit_id)
        captures = run.captures(node, unit_id, attempt)
        run.actual_mixed_unit(node, unit_id, attempt, task, captures)
        reservation = _one(row for row in run.state["call_budget"]["reservations"]
                           if row.get("node_key") == "N2" and row.get("unit_id") == unit_id)
        call_id = reservation["call_id"]
        _require([item["name"] for item in captures if item["name"].startswith("request-")] == [f"request-{call_id}-1"] and
                 not any(item["name"].startswith("validation-repair-") for item in captures), SOURCE_INVALID)
        call = run.call(node, unit_id, attempt, call_id, 1, passed=True)
        replay._wire(run, call, task)
        raw, terminal = call["candidate"], call["terminal"]
        _require(data.get("candidate_origin") == "application_normalized" and
                 data.get("raw_model_candidate") == raw and data.get("validated") == call["validation"].get("validated_output"),
                 SOURCE_INVALID)
        _validate_normalized_window_result(data, stage.windows[unit_id], predecessor_result=self._prior_data(stage, unit_id))
        output_hash = unit["output_hash"]
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(raw),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False, "calls": [{
                      "source_alias": run.alias, "run_id": run.run_id, "call_id": call_id, "call_attempt": 1,
                      "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                      "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"historical_actual_checkpoint_sha256": output_hash}]}
        return {"candidate": copy.deepcopy(raw), "request_sha256": digest(task), "origin": origin}, output_hash

    def read_entry(self, replay, binding, task, full_window, read_legacy):

        self.verify_history(replay)
        unit_id = binding["unit_id"]
        entry = read_legacy(binding, task, full_window) if unit_id in self.base.prefix else None
        known_hash = None
        for stage in self.stages:
            if unit_id in stage.inherited:
                _require(entry is not None, SOURCE_INVALID)
                output_hash = _verify_replayed_checkpoint(stage, replay, unit_id, entry,
                                                          stage.windows[unit_id], SOURCE_INVALID,
                                                          predecessor_result=self._prior_data(stage, unit_id))
                _require(known_hash is None or output_hash == known_hash, SOURCE_INVALID)
                known_hash = output_hash
            elif unit_id in stage.actual:
                if unit_id == stage.proof["unit_id"]:
                    entry = self._read_failure(replay, stage, task)
                    known_hash = stage.proof["result_sha256"]
                else:
                    entry, known_hash = self._read_actual(replay, stage, unit_id, task)
        _require(entry is not None and known_hash is not None, SOURCE_INVALID)
        if unit_id != self.unit_id:
            self.prior_checkpoint_hashes[unit_id] = known_hash
        return entry

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, self.windows, "FAILED_WINDOW_LINEAGE",
                              self.prior_checkpoint_hashes, verify_cold_identity=True,
                              terminal_result_hash=self.proofs[-1]["result_sha256"])
        if _TASK + unit_id not in replay._entries:
            _require(all(row.get("settlement_status") == "known_usage_cost_estimate" and row.get("exceeds_reserved_upper") is False
                         for row in ctx._state.get("call_budget", {}).get("reservations", []) if row.get("node_key") == "N2"),
                     "FAILED_WINDOW_LINEAGE_CHECKPOINTS_REQUIRED")
