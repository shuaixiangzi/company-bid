"""Authenticate the stopped N2 prefix before a price-scoring continuation."""
from __future__ import annotations

import copy
import hashlib
import re

from .diagnostic_replay import _id, _one, _require
from .provider import ProviderFailure
from .storage import digest


FAILED_SCORING_REPLAY = "failed_scoring_replay"
_FAILURE = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
_INVALID = "FAILED_SCORING_SOURCE_INVALID"
_CONFIG = "FAILED_SCORING_CONFIG_INVALID"
_TASK = "extract_requirements:"


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
                                  if key not in {FAILED_SCORING_REPLAY, PRICE_SCORING_CONFIG_KEY}}
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
        _require(prefix[0] == self.first_id and len(prefix) >= 2 and
                 set(bindings) == {_TASK + unit for unit in prefix} and
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
        from .extraction_replay_scope import _failed_unit_attempt

        run, unit_id = self.run, binding["unit_id"]
        node, unit, payload, _, _ = run.unit("N2", unit_id, successful=False)
        _require(digest(payload) == digest({key: value for key, value in task.items() if key != "task_type"}),
                 "REQUEST_MISMATCH")
        replay._source_window(run, payload, unit_id)
        attempt = _failed_unit_attempt(run, node, unit, self.n1, _FAILURE, _INVALID)
        captures = run.captures(node, unit_id, attempt)
        _require([item["name"] for item in captures if item["name"].startswith("request-")] ==
                 [f"request-{self.call_id}-1"] and
                 not any(item["name"].startswith("validation-repair-") for item in captures), "LINEAGE_UNSUPPORTED")
        run.actual_mixed_unit(node, unit_id, attempt, task, captures)
        call = run.call(node, unit_id, attempt, self.call_id, 1, passed=False)
        replay._wire(run, call, task)
        validation = call["validation"]
        _require(all(validation.get(field) == value for field, value in
                     (("unit_id", unit_id), ("call_id", self.call_id), ("call_attempt", 1), ("error_code", _FAILURE))),
                 _INVALID)
        raw = call["candidate"]
        context = validation.get("diagnostic_context")
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
        _require(replay._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
        if task_type + ":" + unit_id in replay._entries:
            return
        _require(task_type == "extract_requirements" and unit_id in replay.windows, "TASK_NOT_ALLOWED")
        units = ctx._record.get("units", {})
        order = list(replay.windows)
        preceding = order[:order.index(unit_id)]
        _require(set(self.prefix) <= set(preceding) and
                 all(units.get(unit, {}).get("status") == "succeeded" for unit in preceding),
                 "FAILED_SCORING_CHECKPOINTS_REQUIRED")
        reservations = [row for row in ctx._state.get("call_budget", {}).get("reservations", [])
                        if row.get("node_key") == "N2"]
        _require(not any(row.get("unit_id") == unit_id for row in reservations),
                 "FAILED_SCORING_LIVE_WINDOW_ALREADY_RESERVED")
        _require(not any(row.get("unit_id") in self.prefix for row in reservations), "FAILED_SCORING_CHECKPOINTS_REQUIRED")
        if not self.checkpoints_verified:
            from .extraction import _validate_normalized_window_result

            for unit in self.prefix:
                checkpoint = ctx.executor._checkpoint_data(units[unit])
                entry = replay._entries[_TASK + unit]
                _require(digest(checkpoint.get("raw_model_candidate")) == entry["origin"]["candidate_sha256"],
                         "FAILED_SCORING_CHECKPOINTS_REQUIRED")
                window = self.window if unit == self.unit_id else replay.windows[unit]
                _validate_normalized_window_result(checkpoint, window)
            self.checkpoints_verified = True
