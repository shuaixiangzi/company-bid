"""Authenticate the stopped N2 prefix before a price-scoring continuation."""
from __future__ import annotations

import copy
import hashlib
import re

from .diagnostic_replay import _id, _one, _require
from .provider import ProviderFailure
from .storage import digest


FAILED_SCORING_REPLAY = "failed_scoring_replay"
FAILED_SCORING_ROWS_REPLAY = "failed_scoring_rows_replay"
SCORING_ROW_SOURCE_CONFIG_KEY = "scoring_row_source_resolution"
_FAILURE = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
_INVALID = "FAILED_SCORING_SOURCE_INVALID"
_CONFIG = "FAILED_SCORING_CONFIG_INVALID"
_TASK = "extract_requirements:"
_ROWS_FAILURE = "TABLE_CONTINUATION_NOT_PROVEN"
_ROWS_INVALID = "FAILED_SCORING_ROWS_SOURCE_INVALID"
_ROWS_CONFIG = "FAILED_SCORING_ROWS_CONFIG_INVALID"


class FailedScoringReplay:
    def __init__(self, replay, config):
        from .extraction import PRICE_SCORING_CONFIG_KEY
        from .extraction_replay_scope import FAILED_CELL_WRAP_REPLAY

        extraction = config["extraction"]
        options = extraction[FAILED_SCORING_REPLAY]
        _require(isinstance(options, dict) and set(options) == {"unit_id", "call_id", "source_state_sha256"} and
                 isinstance(options["unit_id"], str) and _id(options["call_id"]) and
                 isinstance(options["source_state_sha256"], str) and
                 re.fullmatch(r"[0-9a-f]{64}", options["source_state_sha256"]), _CONFIG)
        bindings = replay.config.get("bindings") if isinstance(replay.config, dict) else None
        first = replay._failed_cell_wrap
        binding = bindings.get(_TASK + options["unit_id"]) if isinstance(bindings, dict) else None
        provider = config.get("structured_provider") or config.get("provider", {})
        _require(isinstance(binding, dict) and set(binding) == {"alias", "run_id", "node_key", "unit_id"} and
                 binding["node_key"] == "N2" and binding["unit_id"] == options["unit_id"] and
                 first is not None and first["unit_id"] != options["unit_id"] and _TASK + first["unit_id"] in bindings and
                 provider.get("mode") == "diagnostic_mixed", _CONFIG)
        self.unit_id = options["unit_id"]
        self.first_id = first["unit_id"]
        self.call_id = options["call_id"]
        self.state_sha256 = options["source_state_sha256"]
        self.source_binding = binding
        self.source_extraction = {key: value for key, value in extraction.items()
                                  if key not in {FAILED_SCORING_REPLAY, PRICE_SCORING_CONFIG_KEY,
                                                 FAILED_SCORING_ROWS_REPLAY, SCORING_ROW_SOURCE_CONFIG_KEY}}
        _require(FAILED_CELL_WRAP_REPLAY in self.source_extraction, _CONFIG)
        self.prefix = ()
        self.run = None
        self.window = None
        self.checkpoints_verified = False

    def verify_history(self, replay):
        if self.run is not None:
            return
        _require(self.unit_id in replay.windows, _CONFIG)
        order = list(replay.windows)
        prefix = tuple(order[:order.index(self.unit_id) + 1])
        bindings = replay.config["bindings"]
        expected_bindings = {_TASK + unit for unit in prefix}
        if rows := getattr(replay, "_failed_scoring_rows", None):
            expected_bindings.add(_TASK + rows.unit_id)
        _require(prefix[0] == self.first_id and len(prefix) >= 2 and set(bindings) == expected_bindings and
                 all(replay.windows[unit]["blocks"] for unit in prefix), _CONFIG)
        run = replay._run(self.source_binding["alias"], self.source_binding["run_id"])
        first = bindings[_TASK + self.first_id]
        _require(run.mixed_source and run.state.get("status") == "failed" and
                 run.state.get("error_code") == _FAILURE and
                 hashlib.sha256(run.state_raw).hexdigest() == self.state_sha256 and
                 run.state.get("fork_from_node") == "N2" and run.state.get("parent_run_id") == first.get("run_id"), _INVALID)
        historical = run.state["execution_config"]
        _require(digest(historical.get("extraction", {})) == digest(self.source_extraction) and
                 historical["diagnostic_replay"]["bindings"] == {_TASK + self.first_id: first}, _INVALID)
        for unit in prefix[1:]:
            _require(bindings[_TASK + unit] == {**self.source_binding, "unit_id": unit}, _CONFIG)
        _, n1 = replay._n1(run)
        _require(n1["output_hash"] == digest(replay.current), "SCOPE_MISMATCH")
        node = run.node("N2")
        _require(set(node["units"]) == set(prefix) and all(len(node["units"][unit]["attempts"]) == 1 and
                 node["units"][unit]["status"] == ("failed" if unit == self.unit_id else "succeeded")
                 for unit in prefix), _INVALID)
        started = [event for event in run.events if event["event"] == "unit.started" and event.get("node_key") == "N2"]
        calls = [event for event in run.events if event["event"] == "call.started" and event.get("node_key") == "N2"]
        reservations = [row for row in run.state["call_budget"]["reservations"] if row.get("node_key") == "N2"]
        _require(tuple(event.get("unit_id") for event in started) == prefix and
                 tuple(event.get("unit_id") for event in calls) == prefix[1:] and
                 [row.get("unit_id") for row in reservations] == list(prefix[1:]) and
                 len({event.get("call_id") for event in calls}) == len(calls) and
                 calls[-1].get("call_id") == self.call_id, _INVALID)
        for event, row in zip(calls, reservations, strict=True):
            _require(event.get("operation") == "extract_requirements" and
                     event.get("call_id") == row.get("call_id") and _id(event.get("call_id")) and
                     type(event.get("call_attempt")) is type(row.get("call_attempt")) is int and
                     event["call_attempt"] == row["call_attempt"] == 1 and
                     row.get("settlement_status") == "known_usage_cost_estimate", _INVALID)
        self.prefix, self.run, self.n1 = prefix, run, n1

    def verify_success(self, replay, binding, entry, full_window):
        from .extraction import _validate_normalized_window_result

        unit_id = binding["unit_id"]
        node, unit, payload, data, attempt = self.run.unit("N2", unit_id)
        _require(digest(data.get("raw_model_candidate")) == entry["origin"]["candidate_sha256"], _INVALID)
        try:
            _validate_normalized_window_result(data, full_window)
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
        if unit_id == self.first_id:
            replay._source_window(self.run, payload, unit_id)
            source_attempt = self.run.captures(node, unit_id, attempt)
            staged = self.run.capture(source_attempt, "diagnostic-replay-candidate")
            validated = self.run.capture(source_attempt, "diagnostic-replay-validation")
            origin = entry["origin"]
            _require(staged == {"diagnostic_only": True, "origin": origin, "candidate": entry["candidate"],
                                "external_call": False} and
                     validated == {"diagnostic_only": True, "status": "passed", "origin": origin,
                                   "validated_output": data["validated"], "new_external_calls": 0}, _INVALID)
            spans = [span for span in self.run.spans if span["name"] == "candidate.revalidation" and
                     span.get("parent_span_id") == attempt["span_id"] and span["trace_id"] == attempt["trace_id"]]
            span = _one(spans, _INVALID)
            _require(span["status"] != "ERROR" and
                     span["links"] == [{"trace_id": call["trace_id"], "span_id": call["span_id"]}
                                       for call in origin["calls"]], "SOURCE_TRACE_MISMATCH")
            entry["origin"]["lineage_notes"].append({"historical_successful_replay_run_id": self.run.run_id,
                                                      "historical_checkpoint_sha256": unit["output_hash"]})

    def read_failure(self, replay, binding, task, full_window):
        from .extraction import (PRICE_SCORING_CONFIG_KEY, PlanningError, _index, _verified_price_scoring_resolution,
                                 _windows, extraction_window_config, extraction_window_payload, validate_live_window)
        run, unit_id = self.run, binding["unit_id"]
        unit, call = _read_failed_call(replay, run, self.n1, unit_id, self.call_id, task, _FAILURE, _INVALID)
        raw = call["candidate"]
        context = call["validation"].get("diagnostic_context")
        try:
            validate_live_window(copy.deepcopy(raw), full_window)
        except PlanningError as error:
            _require(error.code == _FAILURE and error.diagnostic_context == context, _INVALID)
        else:
            _require(False, _INVALID)

        value = replay.current
        blocks, _, docs = _index(value["parsed"], value["manifest"])
        window_config = extraction_window_config(replay._target_extraction)
        windows, _ = _windows(docs, blocks, window_config, manifest=value["manifest"])
        _require(tuple(window["window_id"] for window in windows) == tuple(replay.windows) and
                 [window["window_id"] for window in windows if PRICE_SCORING_CONFIG_KEY in window] == [unit_id],
                 "FAILED_SCORING_PROOF_MISMATCH")
        window = _one(window for window in windows if window["window_id"] == unit_id)
        _require(digest({key: value for key, value in window.items() if key != PRICE_SCORING_CONFIG_KEY}) == digest(full_window) and
                 digest({**extraction_window_payload(window, window_config), "task_type": "extract_requirements"}) == digest(task),
                 "REQUEST_MISMATCH")
        try:
            proof = _verified_price_scoring_resolution(copy.deepcopy(raw), window)
            validate_live_window(copy.deepcopy(raw), window)
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
        _require(isinstance(proof, dict) and isinstance(context, dict) and
                 context.get("field") == "unresolved_block_ids" and len(context.get("block_ids", [])) == 2 and
                 sorted(proof.get("resolved_block_ids", [])) == sorted(context["block_ids"]) and
                 proof.get("raw_candidate_sha256") == digest(raw), "FAILED_SCORING_PROOF_MISMATCH")
        terminal = call["terminal"]
        self.window = window
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(raw),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False, "calls": [{
                      "source_alias": run.alias, "run_id": run.run_id, "call_id": self.call_id, "call_attempt": 1,
                      "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                      "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"basis": "authenticated_price_scoring_source_resolution",
                                     "historical_status": "failed", "historical_candidate_modified": False,
                                     "historical_error_code": _FAILURE, "historical_diagnostic_context": context,
                                     "source_N1_output_hash": self.n1["output_hash"],
                                     "source_state_sha256": self.state_sha256, "source_resolution_sha256": digest(proof)}]}
        return {"candidate": copy.deepcopy(raw), "request_sha256": digest(task), "origin": origin}

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, {self.unit_id: self.window}, "FAILED_SCORING")


class FailedScoringRowsReplay:
    """Authenticate the prior replay run and its one failed scoring-table call."""

    def __init__(self, replay, ctx):
        extraction = replay._target_extraction
        options = extraction[FAILED_SCORING_ROWS_REPLAY]
        _require(isinstance(options, dict) and set(options) == {"unit_id", "call_id", "source_state_sha256"} and
                 isinstance(options["unit_id"], str) and _id(options["call_id"]) and
                 isinstance(options["source_state_sha256"], str) and
                 re.fullmatch(r"[0-9a-f]{64}", options["source_state_sha256"]), _ROWS_CONFIG)
        self.predecessor = replay._failed_scoring
        bindings = replay.config.get("bindings") if isinstance(replay.config, dict) else None
        self.source_binding = bindings.get(_TASK + options["unit_id"]) if isinstance(bindings, dict) else None
        provider = ctx.execution_config.get("structured_provider") or ctx.execution_config.get("provider", {})
        _require(self.predecessor is not None and isinstance(self.source_binding, dict) and
                 set(self.source_binding) == {"alias", "run_id", "node_key", "unit_id"} and
                 self.source_binding.get("node_key") == "N2" and self.source_binding.get("unit_id") == options["unit_id"] and
                 self.source_binding.get("alias") == self.predecessor.source_binding["alias"] and
                 provider.get("mode") == "diagnostic_mixed" and type(extraction.get("window_concurrency")) is int and
                 extraction["window_concurrency"] == 1 and type(provider.get("validation_repair_attempts")) is int and
                 provider["validation_repair_attempts"] == 0 and replay.provider_config.max_attempts == 1, _ROWS_CONFIG)
        self.unit_id = options["unit_id"]
        self.call_id = options["call_id"]
        self.state_sha256 = options["source_state_sha256"]
        self.ctx = ctx
        self.prefix = ()
        self.run = None
        self.window = None
        self.prior_checkpoint_hashes = {}
        self.checkpoints_verified = False

    def verify_history(self, replay):
        if self.run is not None:
            return
        self.predecessor.verify_history(replay)
        order = list(replay.windows)
        _require(self.unit_id in replay.windows and replay.windows[self.unit_id]["blocks"], _ROWS_CONFIG)
        prefix = self.predecessor.prefix + (self.unit_id,)
        _require(tuple(order[:len(prefix)]) == prefix, _ROWS_CONFIG)
        bindings = replay.config["bindings"]
        run = replay._run(self.source_binding["alias"], self.source_binding["run_id"])
        _require(run.mixed_source and run.state.get("status") == "failed" and run.state.get("error_code") == _ROWS_FAILURE and
                 hashlib.sha256(run.state_raw).hexdigest() == self.state_sha256 and run.state.get("fork_from_node") == "N2" and
                 run.state.get("parent_run_id") == self.predecessor.run.run_id, _ROWS_INVALID)
        historical = run.state["execution_config"]
        source_extraction = {key: value for key, value in replay._target_extraction.items()
                             if key not in {FAILED_SCORING_ROWS_REPLAY, SCORING_ROW_SOURCE_CONFIG_KEY}}
        source_bindings = {_TASK + unit: bindings[_TASK + unit] for unit in self.predecessor.prefix}
        source_replay = {**replay.config, "bindings": source_bindings}
        _require(run.state.get("config_hash") == digest(historical) and
                 digest(historical.get("extraction", {})) == digest(source_extraction) and
                 digest(historical.get("diagnostic_replay")) == digest(source_replay), _ROWS_INVALID)
        _, n1 = replay._n1(run)
        _require(n1["output_hash"] == digest(replay.current) and
                 n1["output_ref"] == self.predecessor.n1["output_ref"], "SCOPE_MISMATCH")
        current_n1 = [node for node in self.ctx._state.get("nodes", []) if node["key"] == "N1"]
        if current_n1:
            target = _one(current_n1)
            _require(self.ctx._state.get("parent_run_id") == run.run_id and self.ctx._state.get("fork_from_node") == "N2" and
                     target.get("output_ref") == n1["output_ref"] and target.get("output_hash") == n1["output_hash"],
                     "SCOPE_MISMATCH")
        node = run.node("N2")
        _require(set(node["units"]) == set(prefix) and all(len(node["units"][unit]["attempts"]) == 1 and
                 node["units"][unit]["status"] == ("failed" if unit == self.unit_id else "succeeded")
                 for unit in prefix), _ROWS_INVALID)
        started = [event for event in run.events if event["event"] == "unit.started" and event.get("node_key") == "N2"]
        _require(tuple(event.get("unit_id") for event in started) == prefix, _ROWS_INVALID)
        call = _one((event for event in run.events if event["event"] == "call.started" and event.get("node_key") == "N2"), _ROWS_INVALID)
        fee = _one((row for row in run.state["call_budget"]["reservations"] if row.get("node_key") == "N2"), _ROWS_INVALID)
        _require(call.get("operation") == "extract_requirements" and fee.get("settlement_status") == "known_usage_cost_estimate" and
                 type(call.get("call_attempt")) is type(fee.get("call_attempt")) is int and
                 all(record.get("unit_id") == self.unit_id and record.get("call_id") == self.call_id and
                     record["call_attempt"] == 1 for record in (call, fee)), _ROWS_INVALID)
        attempt = _one(node["attempts"], _ROWS_INVALID)
        preflight = run.capture(attempt["captures"], "diagnostic-replay-preflight")
        _require(preflight.get("binding_count") == preflight.get("verified_binding_count") == len(self.predecessor.prefix) and
                 len(preflight.get("origins", [])) == len(self.predecessor.prefix), _ROWS_INVALID)
        self.prefix, self.run, self.n1, self.preflight = prefix, run, n1, preflight

    def verify_success(self, replay, binding, entry, full_window):
        from .extraction import _validate_normalized_window_result

        unit_id = binding["unit_id"]
        node, unit, payload, data, attempt = self.run.unit("N2", unit_id)
        replay._source_window(self.run, payload, unit_id)
        origin = entry["origin"]
        known_origin = _one((row for row in self.preflight["origins"] if row.get("unit_id") == unit_id), _ROWS_INVALID)
        _require(digest(known_origin) == digest(origin) and
                 digest(data.get("raw_model_candidate")) == origin["candidate_sha256"] and data.get("status") == "complete" and
                 data.get("window_id") == unit_id and
                 digest({**payload, "task_type": "extract_requirements"}) == origin["base_request_sha256"], _ROWS_INVALID)
        window = self.predecessor.window if unit_id == self.predecessor.unit_id else full_window
        _validate_normalized_window_result(data, window)
        captures = self.run.captures(node, unit_id, attempt)
        _require(not any(record["name"].startswith(("request-", "validation-repair-")) for record in captures), _ROWS_INVALID)
        staged = self.run.capture(captures, "diagnostic-replay-candidate")
        validated = self.run.capture(captures, "diagnostic-replay-validation")
        _require(staged == {"diagnostic_only": True, "origin": origin, "candidate": entry["candidate"], "external_call": False} and
                 validated == {"diagnostic_only": True, "status": "passed", "origin": origin,
                               "validated_output": data["validated"], "new_external_calls": 0}, _ROWS_INVALID)
        span = _one((span for span in self.run.spans if span["name"] == "candidate.revalidation" and
                     span.get("parent_span_id") == attempt["span_id"] and span["trace_id"] == attempt["trace_id"]), _ROWS_INVALID)
        _require(span["status"] != "ERROR" and span["attributes"].get("input_hash") == origin["base_request_sha256"] and
                 span["attributes"].get("output_hash") == origin["candidate_sha256"] and
                 span["links"] == [{"trace_id": call["trace_id"], "span_id": call["span_id"]} for call in origin["calls"]],
                 "SOURCE_TRACE_MISMATCH")
        self.prior_checkpoint_hashes[unit_id] = unit["output_hash"]
        origin.setdefault("lineage_notes", []).append({"historical_successful_replay_run_id": self.run.run_id,
                                                        "historical_checkpoint_sha256": unit["output_hash"]})

    def read_failure(self, replay, binding, task, full_window):
        from .extraction import (PlanningError, _index, _verified_scoring_row_resolution, _windows,
                                 extraction_window_config, extraction_window_payload, validate_live_window)

        unit_id, run = binding["unit_id"], self.run
        unit, call = _read_failed_call(replay, run, self.n1, unit_id, self.call_id, task, _ROWS_FAILURE, _ROWS_INVALID)
        raw = call["candidate"]
        context = call["validation"].get("diagnostic_context")
        try:
            validate_live_window(copy.deepcopy(raw), full_window)
        except PlanningError as error:
            _require(error.code == _ROWS_FAILURE and error.diagnostic_context == context, _ROWS_INVALID)
        else:
            _require(False, _ROWS_INVALID)

        value = replay.current
        blocks, _, docs = _index(value["parsed"], value["manifest"])
        window_config = extraction_window_config(replay._target_extraction)
        windows, _ = _windows(docs, blocks, window_config, manifest=value["manifest"])
        _require(tuple(window["window_id"] for window in windows) == tuple(replay.windows) and
                 [window["window_id"] for window in windows if SCORING_ROW_SOURCE_CONFIG_KEY in window] == [unit_id],
                 "FAILED_SCORING_ROWS_PROOF_MISMATCH")
        window = _one(window for window in windows if window["window_id"] == unit_id)
        legacy = {key: value for key, value in window.items() if key != SCORING_ROW_SOURCE_CONFIG_KEY}
        _require(digest(legacy) == digest(full_window) and
                 digest({**extraction_window_payload(window, window_config), "task_type": "extract_requirements"}) == digest(task),
                 "REQUEST_MISMATCH")
        try:
            proof = _verified_scoring_row_resolution(copy.deepcopy(raw), window)
            validate_live_window(copy.deepcopy(raw), window)
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
        _require(isinstance(proof, dict) and proof.get("raw_candidate_sha256") == digest(raw),
                 "FAILED_SCORING_ROWS_PROOF_MISMATCH")
        terminal = call["terminal"]
        self.window = window
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(raw),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False, "calls": [{
                      "source_alias": run.alias, "run_id": run.run_id, "call_id": self.call_id, "call_attempt": 1,
                      "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                      "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"basis": "authenticated_scoring_row_source_resolution",
                                     "historical_status": "failed", "historical_candidate_modified": False,
                                     "historical_error_code": _ROWS_FAILURE, "historical_diagnostic_context": context,
                                     "source_N1_output_hash": self.n1["output_hash"],
                                     "source_state_sha256": self.state_sha256, "source_resolution_sha256": digest(proof)}]}
        return {"candidate": copy.deepcopy(raw), "request_sha256": digest(task), "origin": origin}

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        windows = {self.predecessor.unit_id: self.predecessor.window, self.unit_id: self.window}
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, windows, "FAILED_SCORING_ROWS",
                              self.prior_checkpoint_hashes)


def _read_failed_call(replay, run, n1, unit_id, call_id, task, failure, invalid):
    from .extraction_replay_scope import _failed_unit_attempt

    node, unit, payload, _, _ = run.unit("N2", unit_id, successful=False)
    _require(digest(payload) == digest({key: value for key, value in task.items() if key != "task_type"}),
             "REQUEST_MISMATCH")
    replay._source_window(run, payload, unit_id)
    attempt = _failed_unit_attempt(run, node, unit, n1, failure, invalid)
    captures = run.captures(node, unit_id, attempt)
    _require([item["name"] for item in captures if item["name"].startswith("request-")] == [f"request-{call_id}-1"] and
             not any(item["name"].startswith("validation-repair-") for item in captures), "LINEAGE_UNSUPPORTED")
    run.actual_mixed_unit(node, unit_id, attempt, task, captures)
    call = run.call(node, unit_id, attempt, call_id, 1, passed=False)
    replay._wire(run, call, task)
    validation = call["validation"]
    _require(all(validation.get(field) == value for field, value in
                 (("unit_id", unit_id), ("call_id", call_id), ("call_attempt", 1), ("error_code", failure))), invalid)
    return unit, call


def _allow_cold_candidate(gate, replay, ctx, task_type, unit_id, enriched_windows, error_prefix, prior_hashes=None):
    _require(replay._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
    if task_type + ":" + unit_id in replay._entries:
        return
    _require(task_type == "extract_requirements" and unit_id in replay.windows, "TASK_NOT_ALLOWED")
    required = error_prefix + "_CHECKPOINTS_REQUIRED"
    units = ctx._record.get("units", {})
    order = list(replay.windows)
    preceding = order[:order.index(unit_id)]
    _require(set(gate.prefix) <= set(preceding) and
             all(units.get(unit, {}).get("status") == "succeeded" for unit in preceding), required)
    reservations = [row for row in ctx._state.get("call_budget", {}).get("reservations", []) if row.get("node_key") == "N2"]
    _require(not any(row.get("unit_id") == unit_id for row in reservations), error_prefix + "_LIVE_WINDOW_ALREADY_RESERVED")
    _require(not any(row.get("unit_id") in gate.prefix for row in reservations), required)
    if prior_hashes is not None:
        fresh_preceding = preceding[len(gate.prefix):]
        _require(set(prior_hashes) == set(gate.prefix[:-1]) and
                 [row.get("unit_id") for row in reservations] == fresh_preceding and
                 all(type(row.get("call_attempt")) is int and row["call_attempt"] == 1 for row in reservations), required)
    if not gate.checkpoints_verified:
        from .extraction import _validate_normalized_window_result

        for unit in gate.prefix:
            checkpoint = ctx.executor._checkpoint_data(units[unit])
            entry = replay._entries[_TASK + unit]
            _require(digest(checkpoint.get("raw_model_candidate")) == entry["origin"]["candidate_sha256"], required)
            _require(prior_hashes is None or unit not in prior_hashes or digest(checkpoint) == prior_hashes[unit], required)
            _validate_normalized_window_result(checkpoint, enriched_windows.get(unit, replay.windows[unit]))
        gate.checkpoints_verified = True
