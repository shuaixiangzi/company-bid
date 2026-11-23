"""Authenticate stopped N2 prefixes before allowing the next serial request."""
from __future__ import annotations

import copy
import hashlib
import re

from .contract_acceptance_sources import CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_KEY
from .contract_annex_sources import CONTRACT_ANNEX_SOURCE_CONFIG_KEY
from .contract_heading_only_annex import SOURCE_CONFIG_KEY as HEADING_ONLY_ANNEX_CONFIG_KEY
from .contract_interested_parties_sources import CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_KEY
from .diagnostic_replay import _id, _one, _require
from .extraction_illustrations import ILLUSTRATION_SOURCE_CONFIG_KEY
from .provider import ProviderFailure
from .response_cell_reflow_sources import RESPONSE_CELL_REFLOW_CONFIG_KEY as RESPONSE_CELL_REFLOW_SOURCE_CONFIG_KEY
from .response_cover_sources import RESPONSE_COVER_CONFIG_KEY as RESPONSE_COVER_SOURCE_CONFIG_KEY
from .storage import digest


FAILED_SCORING_REPLAY = "failed_scoring_replay"
FAILED_SCORING_ROWS_REPLAY = "failed_scoring_rows_replay"
FAILED_RESPONSE_COVER_REPLAY = "failed_response_cover_replay"
FAILED_RESPONSE_REVIEW_TABLE_REPLAY = "failed_response_review_table_replay"
RESPONSE_REVIEW_TABLE_SOURCE_CONFIG_KEY = "response_review_table_source_resolution"
FAILED_RESPONSE_LETTER_REPLAY = "failed_response_letter_replay"
RESPONSE_LETTER_SOURCE_CONFIG_KEY = "response_letter_source_resolution"
FAILED_RESPONSE_CELL_REFLOW_REPLAY = "failed_response_cell_reflow_replay"
FAILED_ILLUSTRATION_REPLAY = "failed_illustration_replay"
FAILED_CONTRACT_ANNEX_REPLAY = "failed_contract_annex_replay"
FAILED_CONTRACT_INTERESTED_PARTIES_REPLAY = "failed_contract_interested_parties_replay"
FAILED_CONTRACT_INSURANCE_ACCEPTANCE_REPLAY = "failed_contract_insurance_acceptance_replay"
FAILED_NUMBERED_CONTRACT_CLAUSE_REPLAY = "failed_numbered_contract_clause_replay"
FAILED_CONTRACT_OPTIONAL_FIELDS_REPLAY = "failed_contract_optional_fields_replay"
FAILED_CONTRACT_HEADING_ONLY_ANNEX_REPLAY = "failed_contract_heading_only_annex_replay"
_HEADING_ONLY_ANNEX_KEYS = {FAILED_CONTRACT_HEADING_ONLY_ANNEX_REPLAY, HEADING_ONLY_ANNEX_CONFIG_KEY}
SCORING_ROW_SOURCE_CONFIG_KEY = "scoring_row_source_resolution"
_FAILURE = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
_INVALID = "FAILED_SCORING_SOURCE_INVALID"
_CONFIG = "FAILED_SCORING_CONFIG_INVALID"
_TASK = "extract_requirements:"
_ROWS_FAILURE = "TABLE_CONTINUATION_NOT_PROVEN"
_ROWS_INVALID = "FAILED_SCORING_ROWS_SOURCE_INVALID"
_ROWS_CONFIG = "FAILED_SCORING_ROWS_CONFIG_INVALID"
_COVER_INVALID = "FAILED_RESPONSE_COVER_SOURCE_INVALID"
_COVER_CONFIG = "FAILED_RESPONSE_COVER_CONFIG_INVALID"
_REVIEW_TABLE_KEYS = {FAILED_RESPONSE_REVIEW_TABLE_REPLAY, RESPONSE_REVIEW_TABLE_SOURCE_CONFIG_KEY}
_LETTER_KEYS = {FAILED_RESPONSE_LETTER_REPLAY, RESPONSE_LETTER_SOURCE_CONFIG_KEY}
_CELL_REFLOW_KEYS = {FAILED_RESPONSE_CELL_REFLOW_REPLAY, RESPONSE_CELL_REFLOW_SOURCE_CONFIG_KEY}
_ILLUSTRATION_KEYS = {FAILED_ILLUSTRATION_REPLAY, ILLUSTRATION_SOURCE_CONFIG_KEY}
_CONTRACT_ANNEX_KEYS = {FAILED_CONTRACT_ANNEX_REPLAY, CONTRACT_ANNEX_SOURCE_CONFIG_KEY}
_CONTRACT_INTERESTED_PARTIES_KEYS = {FAILED_CONTRACT_INTERESTED_PARTIES_REPLAY,
                                   CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_KEY}
_CONTRACT_INSURANCE_ACCEPTANCE_KEYS = {FAILED_CONTRACT_INSURANCE_ACCEPTANCE_REPLAY,
                                       CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_KEY}


class FailedScoringReplay:
    def __init__(self, replay, config):
        from .extraction import PRICE_SCORING_CONFIG_KEY
        from .extraction_replay_scope import FAILED_CELL_WRAP_REPLAY
        from .extraction_window_lineage import lineage_extraction_keys

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
                                                 FAILED_SCORING_ROWS_REPLAY, SCORING_ROW_SOURCE_CONFIG_KEY,
                                                 FAILED_RESPONSE_COVER_REPLAY, RESPONSE_COVER_SOURCE_CONFIG_KEY,
                                                 *_REVIEW_TABLE_KEYS, *_LETTER_KEYS, *_CELL_REFLOW_KEYS,
                                                 *_ILLUSTRATION_KEYS, *_CONTRACT_ANNEX_KEYS,
                                                 *_CONTRACT_INTERESTED_PARTIES_KEYS,
                                                 *_CONTRACT_INSURANCE_ACCEPTANCE_KEYS,
                                                 FAILED_NUMBERED_CONTRACT_CLAUSE_REPLAY,
                                                 FAILED_CONTRACT_OPTIONAL_FIELDS_REPLAY, *_HEADING_ONLY_ANNEX_KEYS,
                                                 *lineage_extraction_keys()}}
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
        if cover := getattr(replay, "_failed_response_cover", None):
            expected_bindings.update(_TASK + unit for unit in cover.actual_units(replay))
        if table := getattr(replay, "_failed_response_review_table", None):
            expected_bindings.update(_TASK + unit for unit in table.actual_units(replay))
        if letter := getattr(replay, "_failed_response_letter", None):
            expected_bindings.update(_TASK + unit for unit in letter.actual_units(replay))
        if cell := getattr(replay, "_failed_response_cell_reflow", None):
            expected_bindings.update(_TASK + unit for unit in cell.actual_units(replay))
        if illustration := getattr(replay, "_failed_illustration", None):
            expected_bindings.update(_TASK + unit for unit in illustration.actual_units(replay))
        if annex := getattr(replay, "_failed_contract_annex", None):
            expected_bindings.update(_TASK + unit for unit in annex.actual_units(replay))
        if interested_parties := getattr(replay, "_failed_contract_interested_parties", None):
            expected_bindings.update(_TASK + unit for unit in interested_parties.actual_units(replay))
        if insurance_acceptance := getattr(replay, "_failed_contract_insurance_acceptance", None):
            expected_bindings.update(_TASK + unit for unit in insurance_acceptance.actual_units(replay))
        if numbered_clause := getattr(replay, "_failed_numbered_contract_clause", None):
            expected_bindings.update(_TASK + unit for unit in numbered_clause.actual_units(replay))
        if optional_fields := getattr(replay, "_failed_contract_optional_fields", None):
            expected_bindings.update(_TASK + unit for unit in optional_fields.actual_units(replay))
        if heading_only_annex := getattr(replay, "_failed_contract_heading_only_annex", None):
            expected_bindings.update(_TASK + unit for unit in heading_only_annex.actual_units(replay))
        if lineage := getattr(replay, "_failed_window_lineage", None):
            expected_bindings.update(_TASK + unit for unit in lineage.actual_units(replay))
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
        from .extraction_window_lineage import lineage_extraction_keys

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
                             if key not in {FAILED_SCORING_ROWS_REPLAY, SCORING_ROW_SOURCE_CONFIG_KEY,
                                            FAILED_RESPONSE_COVER_REPLAY, RESPONSE_COVER_SOURCE_CONFIG_KEY,
                                            *_REVIEW_TABLE_KEYS, *_LETTER_KEYS, *_CELL_REFLOW_KEYS,
                                            *_ILLUSTRATION_KEYS, *_CONTRACT_ANNEX_KEYS,
                                            *_CONTRACT_INTERESTED_PARTIES_KEYS,
                                            *_CONTRACT_INSURANCE_ACCEPTANCE_KEYS,
                                            FAILED_NUMBERED_CONTRACT_CLAUSE_REPLAY,
                                            FAILED_CONTRACT_OPTIONAL_FIELDS_REPLAY, *_HEADING_ONLY_ANNEX_KEYS,
                                            *lineage_extraction_keys()}}
        source_bindings = {_TASK + unit: bindings[_TASK + unit] for unit in self.predecessor.prefix}
        source_replay = {**replay.config, "bindings": source_bindings}
        _require(run.state.get("config_hash") == digest(historical) and
                 digest(historical.get("extraction", {})) == digest(source_extraction) and
                 digest(historical.get("diagnostic_replay")) == digest(source_replay), _ROWS_INVALID)
        _, n1 = replay._n1(run)
        _require(n1["output_hash"] == digest(replay.current) and
                 n1["output_ref"] == self.predecessor.n1["output_ref"], "SCOPE_MISMATCH")
        current_n1 = [node for node in self.ctx._state.get("nodes", []) if node["key"] == "N1"]
        if current_n1 and getattr(replay, "_failed_response_cover", None) is None:
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
        unit_id = binding["unit_id"]
        window = self.predecessor.window if unit_id == self.predecessor.unit_id else full_window
        self.prior_checkpoint_hashes[unit_id] = _verify_replayed_checkpoint(self, replay, unit_id, entry, window, _ROWS_INVALID)

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


class _StoppedResponseReplay:
    """Authenticate an immutable failed parent and its ordered N2 prefix."""
    extra_option_keys = frozenset()

    def __init__(self, replay, ctx):
        extraction = ctx.execution_config["extraction"]
        options = extraction[self.replay_key]
        _require(isinstance(options, dict) and
                 set(options) == {"unit_id", "call_id", "source_state_sha256"} | self.extra_option_keys and
                 isinstance(options["unit_id"], str) and _id(options["call_id"]) and
                 isinstance(options["source_state_sha256"], str) and
                 re.fullmatch(r"[0-9a-f]{64}", options["source_state_sha256"]), self.config_error)
        self.predecessor = self.previous_gate(replay)
        bindings = replay.config.get("bindings") if isinstance(replay.config, dict) else None
        self.source_binding = bindings.get(_TASK + options["unit_id"]) if isinstance(bindings, dict) else None
        provider = ctx.execution_config.get("structured_provider") or ctx.execution_config.get("provider", {})
        _require(self.predecessor is not None and isinstance(self.source_binding, dict) and
                 set(self.source_binding) == {"alias", "run_id", "node_key", "unit_id"} and
                 self.source_binding.get("node_key") == "N2" and self.source_binding.get("unit_id") == options["unit_id"] and
                 self.source_binding.get("alias") == self.predecessor.source_binding["alias"] and
                 provider.get("mode") == "diagnostic_mixed" and type(extraction.get("window_concurrency")) is int and
                 extraction["window_concurrency"] == 1 and type(provider.get("validation_repair_attempts")) is int and
                 provider["validation_repair_attempts"] == 0 and replay.provider_config.max_attempts == 1, self.config_error)
        self.unit_id = options["unit_id"]
        self.call_id = options["call_id"]
        self.state_sha256 = options["source_state_sha256"]
        self.ctx = ctx
        self.prefix = ()
        self.run = None
        self.window = None
        self.prior_checkpoint_hashes = {}
        self.checkpoints_verified = False

    @staticmethod
    def successor_gate(replay):
        return None

    def added_extraction_keys(self):
        return {self.replay_key, self.proof_key}

    def verify_history(self, replay):
        if self.run is not None:
            return
        actual = self.actual_units(replay)
        self.predecessor.verify_history(replay)
        prefix = self.predecessor.prefix + actual
        bindings = replay.config["bindings"]
        successor = self.successor_gate(replay)
        declared = prefix
        later_keys = set()
        later = successor
        while later is not None:
            declared += later.actual_units(replay)
            later_keys.update(later.added_extraction_keys())
            later = later.successor_gate(replay)
        _require(prefix == tuple(replay.windows)[:len(prefix)] and set(bindings) == {_TASK + unit for unit in declared} and
                 all(bindings[_TASK + unit] == {**self.source_binding, "unit_id": unit} for unit in actual), self.config_error)
        run = replay._run(self.source_binding["alias"], self.source_binding["run_id"])
        _require(run.mixed_source and run.state.get("status") == "failed" and
                 run.state.get("error_code") == self.historical_failure and
                 hashlib.sha256(run.state_raw).hexdigest() == self.state_sha256 and run.state.get("fork_from_node") == "N2" and
                 run.state.get("parent_run_id") == self.predecessor.run.run_id, self.source_error)
        historical = run.state["execution_config"]
        source_extraction = {key: value for key, value in replay._target_extraction.items()
                             if key not in {self.replay_key, self.proof_key, *later_keys}}
        source_bindings = {_TASK + unit: bindings[_TASK + unit] for unit in self.predecessor.prefix}
        source_replay = {**replay.config, "bindings": source_bindings}
        _require(run.state.get("config_hash") == digest(historical) and
                 digest(historical.get("extraction", {})) == digest(source_extraction) and
                 digest(historical.get("diagnostic_replay")) == digest(source_replay), self.source_error)
        _, n1 = replay._n1(run)
        _require(n1["output_hash"] == digest(replay.current) and
                 n1["output_ref"] == self.predecessor.n1["output_ref"], "SCOPE_MISMATCH")
        current_n1 = [node for node in self.ctx._state.get("nodes", []) if node["key"] == "N1"]
        if current_n1 and successor is None:
            target = _one(current_n1)
            _require(self.ctx._state.get("parent_run_id") == run.run_id and self.ctx._state.get("fork_from_node") == "N2" and
                     target.get("output_ref") == n1["output_ref"] and target.get("output_hash") == n1["output_hash"],
                     "SCOPE_MISMATCH")
        node = run.node("N2")
        _require(set(node["units"]) == set(prefix) and all(len(node["units"][unit]["attempts"]) == 1 and
                 node["units"][unit]["status"] == ("failed" if unit == self.unit_id else "succeeded")
                 for unit in prefix), self.source_error)
        started = [event for event in run.events if event["event"] == "unit.started" and event.get("node_key") == "N2"]
        calls = [event for event in run.events if event["event"] == "call.started" and event.get("node_key") == "N2"]
        fees = [row for row in run.state["call_budget"]["reservations"] if row.get("node_key") == "N2"]
        _require(tuple(event.get("unit_id") for event in started) == prefix and
                 tuple(event.get("unit_id") for event in calls) == tuple(row.get("unit_id") for row in fees) == actual and
                 len({event.get("call_id") for event in calls}) == len(actual) and
                 calls[-1].get("call_id") == self.call_id, self.source_error)
        for event, fee in zip(calls, fees, strict=True):
            _require(event.get("operation") == "extract_requirements" and _id(event.get("call_id")) and
                     event.get("call_id") == fee.get("call_id") and fee.get("settlement_status") == "known_usage_cost_estimate" and
                     type(event.get("call_attempt")) is type(fee.get("call_attempt")) is int and
                     event["call_attempt"] == fee["call_attempt"] == 1, self.source_error)
        attempt = _one(node["attempts"], self.source_error)
        preflight = run.capture(attempt["captures"], "diagnostic-replay-preflight")
        _require(preflight.get("binding_count") == preflight.get("verified_binding_count") == len(source_bindings) and
                 len(preflight.get("origins", [])) == len(source_bindings), self.source_error)
        self.prefix, self.run, self.n1, self.preflight = prefix, run, n1, preflight

    def verify_success(self, replay, binding, entry, full_window):
        from .extraction import _validate_normalized_window_result

        unit_id = binding["unit_id"]
        if unit_id in self.predecessor.prefix:
            window = self.predecessor_windows().get(unit_id, full_window)
            output_hash = _verify_replayed_checkpoint(self, replay, unit_id, entry, window, self.source_error)
        else:
            node, unit, _, data, attempt = self.run.unit("N2", unit_id)
            captures = self.run.captures(node, unit_id, attempt)
            calls = entry["origin"]["calls"]
            _require(len(calls) == 1 and calls[0]["run_id"] == self.run.run_id and
                     [row["name"] for row in captures if row["name"].startswith("request-")] ==
                     [f"request-{calls[0]['call_id']}-1"] and
                     not any(row["name"].startswith("validation-repair-") for row in captures), self.source_error)
            _validate_normalized_window_result(data, full_window)
            output_hash = unit["output_hash"]
            entry["origin"].setdefault("lineage_notes", []).append({"historical_actual_checkpoint_sha256": output_hash})
        self.prior_checkpoint_hashes[unit_id] = output_hash

    def read_original_failure(self, replay, unit_id, task, full_window):
        from .extraction import PlanningError, validate_live_window

        run = self.run
        unit, call = _read_failed_call(replay, run, self.n1, unit_id, self.call_id, task,
                                       self.historical_failure, self.source_error)
        raw = call["candidate"]
        context = call["validation"].get("diagnostic_context")
        try:
            validate_live_window(copy.deepcopy(raw), full_window)
        except PlanningError as error:
            _require(error.code == self.historical_failure and error.diagnostic_context == context, self.source_error)
        else:
            _require(False, self.source_error)
        return unit, call, context

    def read_failure(self, replay, binding, task, full_window):
        from .extraction import (_index, _windows, extraction_window_config, extraction_window_payload,
                                 validate_live_window)

        unit_id = binding["unit_id"]
        unit, call, context = self.read_original_failure(replay, unit_id, task, full_window)
        raw = call["candidate"]
        value = replay.current
        blocks, _, docs = _index(value["parsed"], value["manifest"])
        window_config = extraction_window_config(replay._target_extraction)
        windows, _ = _windows(docs, blocks, window_config, manifest=value["manifest"])
        _require(tuple(window["window_id"] for window in windows) == tuple(replay.windows) and
                 [window["window_id"] for window in windows if self.proof_key in window] == [unit_id], self.proof_error)
        window = _one(window for window in windows if window["window_id"] == unit_id)
        _require(digest({key: value for key, value in window.items() if key != self.proof_key}) == digest(full_window) and
                 digest({**extraction_window_payload(window, window_config), "task_type": "extract_requirements"}) == digest(task),
                 "REQUEST_MISMATCH")
        try:
            proof = self.verified_proof(copy.deepcopy(raw), window)
            validate_live_window(copy.deepcopy(raw), window)
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
        _require(isinstance(proof, dict) and proof.get("raw_candidate_sha256") == digest(raw), self.proof_error)
        self.verify_source_resolution(proof, context)
        self.window = window
        return self.failure_entry(unit, call, context, task, proof)

    def failure_entry(self, unit, call, context, task, proof):
        run, unit_id, raw, terminal = self.run, self.unit_id, call["candidate"], call["terminal"]
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(raw),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False, "calls": [{
                      "source_alias": run.alias, "run_id": run.run_id, "call_id": self.call_id, "call_attempt": 1,
                      "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                      "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"basis": self.lineage_basis,
                                     "historical_status": "failed", "historical_candidate_modified": False,
                                     "historical_error_code": self.historical_failure, "historical_diagnostic_context": context,
                                     "source_N1_output_hash": self.n1["output_hash"],
                                     "source_state_sha256": self.state_sha256, "source_resolution_sha256": digest(proof)}]}
        return {"candidate": copy.deepcopy(raw), "request_sha256": digest(task), "origin": origin}

    def enriched_windows(self):
        return {**self.predecessor_windows(), self.unit_id: self.window}

    def verify_source_resolution(self, proof, context):
        self.check_proof_context(proof, context)

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, self.enriched_windows(), self.cold_error,
                              self.prior_checkpoint_hashes, verify_cold_identity=True)


class FailedResponseCoverReplay(_StoppedResponseReplay):
    """Authenticate six replayed units and three actual calls in the stopped parent."""

    replay_key = FAILED_RESPONSE_COVER_REPLAY
    proof_key = RESPONSE_COVER_SOURCE_CONFIG_KEY
    config_error = _COVER_CONFIG
    source_error = _COVER_INVALID
    proof_error = "FAILED_RESPONSE_COVER_PROOF_MISMATCH"
    cold_error = "FAILED_RESPONSE_COVER"
    historical_failure = _FAILURE
    lineage_basis = "authenticated_response_cover_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_scoring_rows

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_response_review_table", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[5] == self.predecessor.unit_id and order[8] == self.unit_id and
                 all(replay.windows[unit]["blocks"] for unit in order[:9]), _COVER_CONFIG)
        return order[6:9]

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_response_cover_resolution

        return _verified_response_cover_resolution(raw, window)

    def check_proof_context(self, proof, context):
        _require(isinstance(context, dict) and context.get("field") == "unresolved_block_ids" and
                 len(context.get("block_ids", [])) == 1 and
                 proof.get("resolved_block_ids") == context["block_ids"], self.proof_error)

    def predecessor_windows(self):
        scoring = self.predecessor.predecessor
        return {scoring.unit_id: scoring.window, self.predecessor.unit_id: self.predecessor.window}


class FailedResponseReviewTableReplay(_StoppedResponseReplay):
    """Authenticate nine unchanged checkpoints and the first review-table response."""

    replay_key = FAILED_RESPONSE_REVIEW_TABLE_REPLAY
    proof_key = RESPONSE_REVIEW_TABLE_SOURCE_CONFIG_KEY
    config_error = "FAILED_RESPONSE_REVIEW_TABLE_CONFIG_INVALID"
    source_error = "FAILED_RESPONSE_REVIEW_TABLE_SOURCE_INVALID"
    proof_error = "FAILED_RESPONSE_REVIEW_TABLE_PROOF_MISMATCH"
    cold_error = "FAILED_RESPONSE_REVIEW_TABLE"
    historical_failure = _ROWS_FAILURE
    lineage_basis = "authenticated_response_review_table_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_response_cover

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_response_letter", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[8] == self.predecessor.unit_id and order[9] == self.unit_id and
                 replay.windows[self.unit_id]["blocks"], self.config_error)
        return (self.unit_id,)

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_response_review_table_resolution

        return _verified_response_review_table_resolution(raw, window)

    def check_proof_context(self, proof, context):
        selection = proof.get("proof")
        _require(isinstance(context, dict) and context.get("candidate_type") == "requirement" and
                 type(context.get("candidate_index")) is int and
                 context["candidate_index"] == proof.get("whole_form_requirement_index") and
                 isinstance(selection, dict) and selection.get("content_block_ids") == context.get("block_ids"),
                 self.proof_error)

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedResponseLetterReplay(_StoppedResponseReplay):
    """Authenticate the unchanged ten-window prefix and W11's first response."""

    replay_key = FAILED_RESPONSE_LETTER_REPLAY
    proof_key = RESPONSE_LETTER_SOURCE_CONFIG_KEY
    config_error = "FAILED_RESPONSE_LETTER_CONFIG_INVALID"
    source_error = "FAILED_RESPONSE_LETTER_SOURCE_INVALID"
    proof_error = "FAILED_RESPONSE_LETTER_PROOF_MISMATCH"
    cold_error = "FAILED_RESPONSE_LETTER"
    historical_failure = "FORMAT_HEADING_NOT_GROUNDED"
    lineage_basis = "authenticated_response_letter_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_response_review_table

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_response_cell_reflow", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[9] == self.predecessor.unit_id and order[10] == self.unit_id and
                 replay.windows[self.unit_id]["blocks"], self.config_error)
        return (self.unit_id,)

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_response_letter_resolution

        return _verified_response_letter_resolution(raw, window)

    def check_proof_context(self, proof, context):
        selection = proof.get("proof")
        selected = selection.get("selected_block_ids") if isinstance(selection, dict) else None
        _require(isinstance(context, dict) and context.get("candidate_type") == "format_chapter" and
                 context.get("field") == "title" and type(context.get("candidate_index")) is int and
                 type(proof.get("format_index")) is int and context["candidate_index"] == proof["format_index"] and
                 proof.get("owner_window_id") == self.predecessor.unit_id and
                 isinstance(selected, list) and bool(selected) and selected == context.get("block_ids") and
                 selected[0] == context.get("block_id"),
                 self.proof_error)

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedResponseCellReflowReplay(_StoppedResponseReplay):
    """Authenticate the unchanged eleven-window prefix and W12's first response."""

    replay_key = FAILED_RESPONSE_CELL_REFLOW_REPLAY
    proof_key = RESPONSE_CELL_REFLOW_SOURCE_CONFIG_KEY
    config_error = "FAILED_RESPONSE_CELL_REFLOW_CONFIG_INVALID"
    source_error = "FAILED_RESPONSE_CELL_REFLOW_SOURCE_INVALID"
    proof_error = "FAILED_RESPONSE_CELL_REFLOW_PROOF_MISMATCH"
    cold_error = "FAILED_RESPONSE_CELL_REFLOW"
    historical_failure = "EXTRACTION_LABEL_NOT_GROUNDED"
    lineage_basis = "authenticated_response_cell_reflow_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_response_letter

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_illustration", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[10] == self.predecessor.unit_id and order[11] == self.unit_id and
                 replay.windows[self.unit_id]["blocks"], self.config_error)
        return (self.unit_id,)

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_response_cell_reflow_resolution

        return _verified_response_cell_reflow_resolution(raw, window)

    def check_proof_context(self, proof, context):
        _require(isinstance(context, dict) and context.get("candidate_type") == "requirement" and
                 context.get("field") == "attachment_keywords" and
                 digest(proof.get("original_error")) ==
                 digest({"error_code": self.historical_failure, "diagnostic_context": context}),
                 self.proof_error)

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedIllustrationReplay(_StoppedResponseReplay):
    """Authenticate the unchanged fourteen-window prefix and W15's first response."""

    replay_key = FAILED_ILLUSTRATION_REPLAY
    proof_key = ILLUSTRATION_SOURCE_CONFIG_KEY
    config_error = "FAILED_ILLUSTRATION_CONFIG_INVALID"
    source_error = "FAILED_ILLUSTRATION_SOURCE_INVALID"
    proof_error = "FAILED_ILLUSTRATION_PROOF_MISMATCH"
    cold_error = "FAILED_ILLUSTRATION"
    historical_failure = "ILLUSTRATION_CONTEXT_NOT_CITABLE"
    lineage_basis = "authenticated_illustration_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_response_cell_reflow

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_contract_annex", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[11] == self.predecessor.unit_id and order[14] == self.unit_id and
                 all(replay.windows[unit]["blocks"] for unit in order[12:15]), self.config_error)
        return order[12:15]

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_illustration_resolution

        return _verified_illustration_resolution(raw, window)

    def check_proof_context(self, proof, context):
        _require(isinstance(context, dict) and context.get("candidate_type") == "requirement" and
                 type(context.get("candidate_index")) is int and context["candidate_index"] >= 0 and
                 digest(proof.get("original_error")) ==
                 digest({"error_code": self.historical_failure, "diagnostic_context": context}),
                 self.proof_error)

    def verify_source_resolution(self, proof, context):
        from .extraction import PlanningError, verify_illustration_predecessor

        self.check_proof_context(proof, context)
        preceding_unit = self.prefix[-2]
        _require(proof["proof"]["predecessor"]["window_id"] == preceding_unit, self.proof_error)
        # This checkpoint's original call and complete output are authenticated
        # by verify_success regardless of the configured binding iteration order.
        _, _, _, data, _ = self.run.unit("N2", preceding_unit)
        try:
            verify_illustration_predecessor(data, proof)
        except PlanningError as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_" + self.proof_error) from error

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedContractAnnexReplay(_StoppedResponseReplay):
    """Authenticate the unchanged 23-window prefix and W24's first response."""

    replay_key = FAILED_CONTRACT_ANNEX_REPLAY
    proof_key = CONTRACT_ANNEX_SOURCE_CONFIG_KEY
    config_error = "FAILED_CONTRACT_ANNEX_CONFIG_INVALID"
    source_error = "FAILED_CONTRACT_ANNEX_SOURCE_INVALID"
    proof_error = "FAILED_CONTRACT_ANNEX_PROOF_MISMATCH"
    cold_error = "FAILED_CONTRACT_ANNEX"
    historical_failure = _FAILURE
    lineage_basis = "authenticated_contract_annex_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_illustration

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_contract_interested_parties", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[14] == self.predecessor.unit_id and order[23] == self.unit_id and
                 all(replay.windows[unit]["blocks"] for unit in order[15:24]), self.config_error)
        return order[15:24]

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_contract_annex_resolution

        return _verified_contract_annex_resolution(raw, window)

    def check_proof_context(self, proof, context):
        _require(isinstance(context, dict) and context.get("field") == "unresolved_block_ids" and
                 isinstance(context.get("block_ids"), list) and len(context["block_ids"]) == 1 and
                 proof.get("resolved_block_ids") == context["block_ids"] and
                 digest(proof.get("original_error")) ==
                 digest({"error_code": self.historical_failure, "diagnostic_context": context}), self.proof_error)

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedContractInterestedPartiesReplay(_StoppedResponseReplay):
    """Authenticate W25's actual success and W26's first response against v88."""

    replay_key = FAILED_CONTRACT_INTERESTED_PARTIES_REPLAY
    proof_key = CONTRACT_INTERESTED_PARTIES_SOURCE_CONFIG_KEY
    config_error = "FAILED_CONTRACT_INTERESTED_PARTIES_CONFIG_INVALID"
    source_error = "FAILED_CONTRACT_INTERESTED_PARTIES_SOURCE_INVALID"
    proof_error = "FAILED_CONTRACT_INTERESTED_PARTIES_PROOF_MISMATCH"
    cold_error = "FAILED_CONTRACT_INTERESTED_PARTIES"
    historical_failure = _FAILURE
    lineage_basis = "authenticated_contract_interested_parties_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_contract_annex

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_contract_insurance_acceptance", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[23] == self.predecessor.unit_id and order[25] == self.unit_id and
                 all(replay.windows[unit]["blocks"] for unit in order[24:26]), self.config_error)
        return order[24:26]

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_contract_interested_parties_resolution

        return _verified_contract_interested_parties_resolution(raw, window)

    def verify_source_resolution(self, resolution, context):
        from .extraction import PlanningError, verify_contract_interested_parties_predecessor

        _require(isinstance(context, dict) and context.get("field") == "unresolved_block_ids" and
                 isinstance(context.get("block_ids"), list) and len(context["block_ids"]) == 1 and
                 resolution.get("resolved_block_ids") == context["block_ids"] and
                 resolution.get("original_error") ==
                 {"error_code": self.historical_failure, "diagnostic_context": context}, self.proof_error)
        preceding_unit = self.prefix[-2]
        _require(resolution["proof"]["predecessor"]["window_id"] == preceding_unit, self.proof_error)
        # The actual call and complete checkpoint are also verified by
        # verify_success, independent of binding iteration order.
        _, _, _, data, _ = self.run.unit("N2", preceding_unit)
        try:
            verify_contract_interested_parties_predecessor(data, resolution["proof"])
        except PlanningError as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_" + self.proof_error) from error

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


class FailedContractInsuranceAcceptanceReplay(_StoppedResponseReplay):
    """Authenticate W27–29's actual successes and W30's first response against v89."""

    replay_key = FAILED_CONTRACT_INSURANCE_ACCEPTANCE_REPLAY
    proof_key = CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_CONFIG_KEY
    config_error = "FAILED_CONTRACT_INSURANCE_ACCEPTANCE_CONFIG_INVALID"
    source_error = "FAILED_CONTRACT_INSURANCE_ACCEPTANCE_SOURCE_INVALID"
    proof_error = "FAILED_CONTRACT_INSURANCE_ACCEPTANCE_PROOF_MISMATCH"
    cold_error = "FAILED_CONTRACT_INSURANCE_ACCEPTANCE"
    historical_failure = _FAILURE
    lineage_basis = "authenticated_contract_insurance_acceptance_source_resolution"

    @staticmethod
    def previous_gate(replay):
        return replay._failed_contract_interested_parties

    @staticmethod
    def successor_gate(replay):
        return getattr(replay, "_failed_numbered_contract_clause", None)

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(len(order) == 59 and order[25] == self.predecessor.unit_id and order[29] == self.unit_id and
                 all(replay.windows[unit]["blocks"] for unit in order[26:30]), self.config_error)
        return order[26:30]

    @staticmethod
    def verified_proof(raw, window):
        from .extraction import _verified_contract_insurance_acceptance_resolution

        return _verified_contract_insurance_acceptance_resolution(raw, window)

    def check_proof_context(self, resolution, context):
        _require(isinstance(context, dict) and context.get("field") == "unresolved_block_ids" and
                 isinstance(context.get("block_ids"), list) and len(context["block_ids"]) == 3 and
                 resolution.get("resolved_block_ids") == context["block_ids"] and
                 resolution.get("original_error") ==
                 {"error_code": self.historical_failure, "diagnostic_context": context}, self.proof_error)

    def read_failure(self, replay, binding, task, full_window):
        from .extraction import (PlanningError, _verified_contract_insurance_acceptance_resolution,
                                 extraction_window_payload, verify_contract_insurance_acceptance_predecessor)

        entry = super().read_failure(replay, binding, task, full_window)
        preceding_unit = self.prefix[-2]
        resolution = _verified_contract_insurance_acceptance_resolution(entry["candidate"], self.window)
        proof = resolution["proof"]
        _require(proof["predecessor"]["window_id"] == preceding_unit, self.proof_error)
        preceding_binding = replay.config["bindings"][_TASK + preceding_unit]
        preceding_window = replay.windows[preceding_unit]
        preceding_task = {**extraction_window_payload(preceding_window, replay.window_config),
                          "task_type": "extract_requirements"}
        # Authenticate the predecessor's original response and output before
        # using it as proof, even if W30 appears before W29 in the bindings.
        preceding_entry = replay._load_entry(preceding_binding, preceding_task, preceding_window)
        preceding_call = _one(preceding_entry["origin"]["calls"], self.source_error)
        _, unit, _, data, _ = self.run.unit("N2", preceding_unit)
        _require(preceding_call.get("run_id") == self.run.run_id and
                 self.prior_checkpoint_hashes.get(preceding_unit) == unit["output_hash"], self.source_error)
        try:
            verify_contract_insurance_acceptance_predecessor(data, proof)
        except PlanningError as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_" + self.proof_error) from error
        entry["origin"]["calls"].append(preceding_call)
        entry["origin"]["lineage_notes"][0]["authenticated_predecessor"] = {
            "unit_id": preceding_unit,
            "checkpoint_sha256": unit["output_hash"],
            "candidate_sha256": preceding_entry["origin"]["candidate_sha256"],
        }
        return entry

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()


def _verify_replayed_checkpoint(gate, replay, unit_id, entry, window, invalid, *, predecessor_result=None):
    from .extraction import _validate_normalized_window_result

    run = gate.run
    node, unit, payload, data, attempt = run.unit("N2", unit_id)
    replay._source_window(run, payload, unit_id)
    origin = entry["origin"]
    known_origin = _one((row for row in gate.preflight["origins"] if row.get("unit_id") == unit_id), invalid)
    _require(digest(known_origin) == digest(origin) and
             digest(data.get("raw_model_candidate")) == origin["candidate_sha256"] and data.get("status") == "complete" and
             data.get("window_id") == unit_id and
             digest({**payload, "task_type": "extract_requirements"}) == origin["base_request_sha256"], invalid)
    if predecessor_result is None:
        _validate_normalized_window_result(data, window)
    else:
        _validate_normalized_window_result(data, window, predecessor_result=predecessor_result)
    captures = run.captures(node, unit_id, attempt)
    _require(not any(record["name"].startswith(("request-", "validation-repair-")) for record in captures), invalid)
    staged = run.capture(captures, "diagnostic-replay-candidate")
    validated = run.capture(captures, "diagnostic-replay-validation")
    _require(staged == {"diagnostic_only": True, "origin": origin, "candidate": entry["candidate"], "external_call": False} and
             validated == {"diagnostic_only": True, "status": "passed", "origin": origin,
                           "validated_output": data["validated"], "new_external_calls": 0}, invalid)
    span = _one((span for span in run.spans if span["name"] == "candidate.revalidation" and
                 span.get("parent_span_id") == attempt["span_id"] and span["trace_id"] == attempt["trace_id"]), invalid)
    _require(span["status"] != "ERROR" and span["attributes"].get("input_hash") == origin["base_request_sha256"] and
             span["attributes"].get("output_hash") == origin["candidate_sha256"] and
             span["links"] == [{"trace_id": call["trace_id"], "span_id": call["span_id"]} for call in origin["calls"]],
             "SOURCE_TRACE_MISMATCH")
    origin.setdefault("lineage_notes", []).append({"historical_successful_replay_run_id": run.run_id,
                                                    "historical_checkpoint_sha256": unit["output_hash"]})
    return unit["output_hash"]


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


def _allow_cold_candidate(gate, replay, ctx, task_type, unit_id, enriched_windows, error_prefix, prior_hashes=None,
                          *, verify_cold_identity=False, terminal_result_hash=None):
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
    if verify_cold_identity:
        _require(len({row.get("call_id") for row in reservations}) == len(reservations), required)
        for row in reservations:
            unit = row["unit_id"]
            attempt = _one((item for item in units[unit].get("attempts", []) if item.get("status") == "succeeded"), required)
            _require(_id(row.get("call_id")) and row.get("unit_attempt_id") == attempt.get("attempt_id") and
                     row.get("input_hash") == replay.current_wires[_TASK + unit], required)
    if not gate.checkpoints_verified:
        from .extraction import _validate_normalized_window_result

        preceding_checkpoint = None
        for unit in gate.prefix:
            checkpoint = ctx.executor._checkpoint_data(units[unit])
            entry = replay._entries[_TASK + unit]
            _require(digest(checkpoint.get("raw_model_candidate")) == entry["origin"]["candidate_sha256"], required)
            _require(prior_hashes is None or unit not in prior_hashes or digest(checkpoint) == prior_hashes[unit], required)
            if verify_cold_identity:
                _require(checkpoint.get("status") == "complete" and checkpoint.get("window_id") == unit, required)
            window = enriched_windows.get(unit, replay.windows[unit])
            if terminal_result_hash is None:
                _validate_normalized_window_result(checkpoint, window)
            else:
                if unit == gate.unit_id:
                    _require(digest(checkpoint) == terminal_result_hash, required)
                _validate_normalized_window_result(checkpoint, window, predecessor_result=preceding_checkpoint)
            preceding_checkpoint = checkpoint
        gate.checkpoints_verified = True
