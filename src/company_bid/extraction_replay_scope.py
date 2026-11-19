"""Supply the frozen N2-only inputs to the shared replay reader."""
from __future__ import annotations

import copy
import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar

from .diagnostic_replay import DiagnosticReplay, _id, _one, _require
from .extraction_failed_scoring_replay import (
    FAILED_RESPONSE_COVER_REPLAY, FAILED_SCORING_REPLAY, FAILED_SCORING_ROWS_REPLAY,
    RESPONSE_COVER_SOURCE_CONFIG_KEY, SCORING_ROW_SOURCE_CONFIG_KEY,
    FailedResponseCoverReplay, FailedScoringReplay, FailedScoringRowsReplay,
)
from .extraction_illustrations import CONFIG_KEY
from .provider import ProviderFailure
from .storage import digest


FAILED_CELL_WRAP_REPLAY = "failed_cell_wrap_replay"
_LABEL_FAILURE = "EXTRACTION_LABEL_NOT_GROUNDED"
_CELL_WRAP_PROOF = "native_pdf_cell_lf_join_v1"
_window_inputs = ContextVar("replay_window_inputs", default=None)


@contextmanager
def _scope(regions, manifest):
    token = _window_inputs.set((regions, manifest) if regions is not None else None)
    try:
        yield
    finally:
        _window_inputs.reset(token)


def replay_window_inputs(config, manifest):
    """Legacy wire requests omit whole-document reviews. Only replay binds them here."""
    scope = _window_inputs.get()
    if manifest is not None or scope is None:
        return config, manifest
    regions, authenticated_manifest = scope
    return {**config, CONFIG_KEY: regions}, authenticated_manifest


class ExtractionDiagnosticReplay(DiagnosticReplay):
    _failed_cell_wrap = None
    _failed_scoring = None
    _failed_scoring_rows = None
    _failed_response_cover = None

    def preflight(self, ctx, value):
        self._ready = None
        self._scope_caches = {}
        self._failed_cell_wrap = None
        self._failed_scoring = None
        self._failed_scoring_rows = None
        self._failed_response_cover = None
        self._target_extraction = ctx.execution_config.get("extraction", {})
        if FAILED_CELL_WRAP_REPLAY in self._target_extraction:
            self._failed_cell_wrap = self._failed_cell_wrap_options(ctx.execution_config)
        from .extraction import PRICE_SCORING_CONFIG_KEY

        _require((PRICE_SCORING_CONFIG_KEY in self._target_extraction) ==
                 (FAILED_SCORING_REPLAY in self._target_extraction), "FAILED_SCORING_CONFIG_INVALID")
        if FAILED_SCORING_REPLAY in self._target_extraction:
            self._failed_scoring = FailedScoringReplay(self, ctx.execution_config)
        _require((SCORING_ROW_SOURCE_CONFIG_KEY in self._target_extraction) ==
                 (FAILED_SCORING_ROWS_REPLAY in self._target_extraction), "FAILED_SCORING_ROWS_CONFIG_INVALID")
        if FAILED_SCORING_ROWS_REPLAY in self._target_extraction:
            self._failed_scoring_rows = FailedScoringRowsReplay(self, ctx)
        _require((RESPONSE_COVER_SOURCE_CONFIG_KEY in self._target_extraction) ==
                 (FAILED_RESPONSE_COVER_REPLAY in self._target_extraction), "FAILED_RESPONSE_COVER_CONFIG_INVALID")
        if FAILED_RESPONSE_COVER_REPLAY in self._target_extraction:
            self._failed_response_cover = FailedResponseCoverReplay(self, ctx)
        with _scope(self._target_extraction.get(CONFIG_KEY), value["manifest"]):
            return super().preflight(ctx, value)

    def _source_window(self, run, payload, unit_id):
        # The same N1 may have several N2 children with different reviews.
        # Authenticate a binding against that historical run's own decisions.
        regions = run.state["execution_config"].get("extraction", {}).get(CONFIG_KEY)
        manifest = self._n1(run)[0]["manifest"] if regions is not None else None
        with _scope(regions, manifest):
            self._source_windows = self._scope_caches.setdefault(digest(regions), {})
            return super()._source_window(run, payload, unit_id)

    def _failed_cell_wrap_options(self, config):
        options = self._target_extraction[FAILED_CELL_WRAP_REPLAY]
        _require(isinstance(options, dict) and set(options) == {"unit_id", "call_id", "source_state_sha256"} and
                 isinstance(options["unit_id"], str) and _id(options["call_id"]) and
                 isinstance(options["source_state_sha256"], str) and
                 re.fullmatch(r"[0-9a-f]{64}", options["source_state_sha256"]), "FAILED_CELL_WRAP_CONFIG_INVALID")
        key = "extract_requirements:" + options["unit_id"]
        bindings = self.config.get("bindings") if isinstance(self.config, dict) else None
        binding = bindings.get(key) if isinstance(bindings, dict) else None
        provider = config.get("structured_provider") or config.get("provider", {})
        concurrency = self._target_extraction.get("window_concurrency", 1)
        retries = provider.get("validation_repair_attempts", 0)
        _require(isinstance(binding, dict) and binding.get("node_key") == "N2" and
                 binding.get("unit_id") == options["unit_id"] and
                 type(concurrency) is int and concurrency == 1 and
                 type(retries) is int and retries == 0 and self.provider_config.max_attempts == 1,
                 "FAILED_CELL_WRAP_CONFIG_INVALID")
        return copy.deepcopy(options)

    def _read_entry(self, binding, task, full_window):
        cover = self._failed_response_cover
        if cover is not None:
            cover.verify_history(self)
            unit_id = binding["unit_id"]
            if unit_id == cover.unit_id:
                return cover.read_failure(self, binding, task, full_window)
            if unit_id in cover.predecessor.prefix:
                entry = self._read_before_response_cover(binding, task, full_window)
            else:
                entry = super()._read_entry(binding, task, full_window)
            cover.verify_success(self, binding, entry, full_window)
            return entry
        return self._read_before_response_cover(binding, task, full_window)

    def _read_before_response_cover(self, binding, task, full_window):
        rows = self._failed_scoring_rows
        if rows is None:
            return self._read_preceding_entry(binding, task, full_window)
        rows.verify_history(self)
        if binding["unit_id"] == rows.unit_id:
            return rows.read_failure(self, binding, task, full_window)
        entry = self._read_preceding_entry(binding, task, full_window)
        rows.verify_success(self, binding, entry, full_window)
        return entry

    def _read_preceding_entry(self, binding, task, full_window):
        scoring = self._failed_scoring
        if scoring is not None:
            scoring.verify_history(self)
            if binding["unit_id"] == scoring.unit_id:
                return scoring.read_failure(self, binding, task, full_window)
        options = self._failed_cell_wrap
        if options is None or binding["unit_id"] != options["unit_id"]:
            entry = super()._read_entry(binding, task, full_window)
            if scoring is not None:
                scoring.verify_success(self, binding, entry, full_window)
            return entry
        unit_id = binding["unit_id"]
        _require(unit_id == next(iter(self.windows)), "FAILED_CELL_WRAP_CONFIG_INVALID")
        run = self._run(binding["alias"], binding["run_id"])
        _require(not run.mixed_source and run.state.get("status") == "failed" and
                 run.state.get("error_code") == _LABEL_FAILURE and
                 hashlib.sha256(run.state_raw).hexdigest() == options["source_state_sha256"],
                 "FAILED_CELL_WRAP_SOURCE_INVALID")
        frozen_config = run.state["execution_config"]
        source_extraction = (scoring.source_extraction if scoring is not None else self._target_extraction)
        source_extraction = {key: value for key, value in source_extraction.items() if key != FAILED_CELL_WRAP_REPLAY}
        _require(run.state.get("config_hash") == digest(frozen_config) and
                 digest(frozen_config.get("extraction", {})) == digest(source_extraction), "SCOPE_MISMATCH")
        node, unit, payload, _, _ = run.unit("N2", unit_id, successful=False)
        _require(digest(payload) == digest({key: value for key, value in task.items() if key != "task_type"}),
                 "REQUEST_MISMATCH")
        _, n1 = self._source_window(run, payload, unit_id)
        _require(n1["output_hash"] == digest(self.current), "SCOPE_MISMATCH")
        _require(set(node["units"]) == {unit_id}, "FAILED_CELL_WRAP_SOURCE_INVALID")
        attempt = _failed_unit_attempt(run, node, unit, n1, _LABEL_FAILURE, "FAILED_CELL_WRAP_SOURCE_INVALID")
        call = _failed_cell_wrap_call(run, node, unit_id, attempt, options["call_id"])
        self._wire(run, call, task)
        proof = _failed_label_proof(call, full_window)
        terminal = call["terminal"]
        origin = {"source_alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": unit_id,
                  "historical_producer_version": unit["version"], "candidate_sha256": digest(call["candidate"]),
                  "base_request_sha256": digest(task), "actual_final_wire_sha256": digest(call["request"]),
                  "external_call": False,
                  "calls": [{"source_alias": run.alias, "run_id": run.run_id, "call_id": call["call_id"],
                             "call_attempt": 1, "trace_id": terminal["trace_id"], "span_id": terminal["span_id"],
                             "request_sha256": digest(call["request"]), "response_sha256": digest(call["response"])}],
                  "lineage_notes": [{"basis": _CELL_WRAP_PROOF, "historical_status": "failed",
                                     "historical_candidate_modified": False, "historical_error_code": _LABEL_FAILURE,
                                     "historical_diagnostic_context": call["validation"]["diagnostic_context"],
                                     "source_N1_output_hash": n1["output_hash"],
                                     "source_state_sha256": options["source_state_sha256"], "cell_proof": proof}]}
        entry = {"candidate": copy.deepcopy(call["candidate"]), "request_sha256": digest(task), "origin": origin}
        if scoring is not None:
            scoring.verify_success(self, binding, entry, full_window)
        return entry

    def candidate(self, ctx, task_type, unit_id, request, validate):
        if self._failed_response_cover is not None:
            self._failed_response_cover.allow_candidate(self, ctx, task_type, unit_id)
        elif self._failed_scoring_rows is not None:
            self._failed_scoring_rows.allow_candidate(self, ctx, task_type, unit_id)
        elif self._failed_scoring is not None:
            self._failed_scoring.allow_candidate(self, ctx, task_type, unit_id)
        elif self._failed_cell_wrap is not None and unit_id != self._failed_cell_wrap["unit_id"]:
            replay_unit = ctx._record.get("units", {}).get(self._failed_cell_wrap["unit_id"], {})
            _require(replay_unit.get("status") == "succeeded", "FAILED_CELL_WRAP_CHECKPOINT_REQUIRED")
            if task_type + ":" + unit_id not in self.config["bindings"]:
                reservations = ctx._state.get("call_budget", {}).get("reservations", [])
                _require(not any(row.get("node_key") == "N2" and row.get("unit_id") == unit_id for row in reservations),
                         "FAILED_CELL_WRAP_LIVE_WINDOW_ALREADY_RESERVED")
        return super().candidate(ctx, task_type, unit_id, request, validate)


def _failed_unit_attempt(run, node, unit, n1, error_code, invalid_reason):
    job = _one(run.state["execution_attempts"])
    node_attempt = _one(node["attempts"])
    attempt = _one(unit["attempts"])
    _require(node["status"] == unit["status"] == "failed" and
             all(item.get("status") == "failed" and item.get("ended_at") and item.get("error_code") == error_code
                 for item in (job, node_attempt, attempt)) and
             job.get("trace_status") == "captured" and node_attempt.get("job_attempt_id") == job["attempt_id"] and
             attempt.get("node_attempt_id") == node_attempt["attempt_id"] and
             attempt.get("input_hash") == unit["input_hash"] and
             node_attempt.get("input_hash") == n1["output_hash"] and
             attempt.get("trace_id") == node_attempt.get("trace_id") == job["trace_id"] and
             not any(record.get("output_ref") or record.get("output_hash") for record in (node, unit, attempt)),
             invalid_reason)
    unit_span = run.span(attempt["trace_id"], attempt["span_id"])
    node_span = run.span(node_attempt["trace_id"], node_attempt["span_id"])
    _require(unit_span["name"] == "unit.N2" and node_span["name"] == "node.N2" and
             unit_span["status"] == node_span["status"] == "ERROR" and
             unit_span.get("parent_span_id") == node_attempt["span_id"] and
             node_span.get("parent_span_id") == job["span_id"] and
             unit_span["attributes"].get("unit_attempt_id") == attempt["attempt_id"] and
             unit_span["attributes"].get("input_hash") == unit["input_hash"] and
             node_span["attributes"].get("input_hash") == n1["output_hash"], "SOURCE_TRACE_MISMATCH")
    unit_failed = _one(item for item in run.events if item["event"] == "unit.failed" and
                       item.get("unit_attempt_id") == attempt["attempt_id"])
    node_failed = _one(item for item in run.events if item["event"] == "node.failed" and
                       item.get("node_attempt_id") == node_attempt["attempt_id"])
    for event, record in ((unit_failed, attempt), (node_failed, node_attempt)):
        _require(event.get("error_code") == error_code and event.get("node_key") == "N2" and
                 event.get("job_attempt_id") == job["attempt_id"] and
                 event.get("span_id") == record["span_id"] and event.get("trace_id") == record["trace_id"],
                 invalid_reason)
    return attempt


def _failed_cell_wrap_call(run, node, unit_id, attempt, call_id):
    captures = run.captures(node, unit_id, attempt)
    _require([item["name"] for item in captures if item["name"].startswith("request-")] == [f"request-{call_id}-1"] and
             not any(item["name"].startswith("validation-repair-") for item in captures), "LINEAGE_UNSUPPORTED")
    started = _one(event for event in run.events if event["event"] == "call.started" and event.get("node_key") == "N2")
    _require(started.get("call_id") == call_id and started.get("call_attempt") == 1 and
             started.get("operation") == "extract_requirements", "FAILED_CELL_WRAP_SOURCE_INVALID")
    call = run.call(node, unit_id, attempt, call_id, 1, passed=False)
    reservation = _one(row for row in run.state.get("call_budget", {}).get("reservations", []) if row.get("node_key") == "N2")
    _require(reservation.get("call_id") == call_id and reservation.get("call_attempt") == 1 and
             reservation.get("settlement_status") == "known_usage_cost_estimate" and
             reservation.get("response_sha256") == digest(call["response"]) and
             call["validation"].get("error_code") == _LABEL_FAILURE and
             all(call["validation"].get(field) == value for field, value in
                 (("unit_id", unit_id), ("call_id", call_id), ("call_attempt", 1))), "FAILED_CELL_WRAP_SOURCE_INVALID")
    return call


def _failed_label_proof(call, window):
    from .extraction import validate_live_window

    context = call["validation"].get("diagnostic_context")
    _require(isinstance(context, dict) and context.get("candidate_type") == "requirement" and
             context.get("field") == "attachment_keywords" and
             all(type(context.get(key)) is int and context[key] >= 0 for key in ("candidate_index", "label_index")),
             "FAILED_CELL_WRAP_SOURCE_INVALID")
    try:
        result = validate_live_window(copy.deepcopy(call["candidate"]), window)
    except Exception as error:
        raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
    proofs = []
    for candidate_index, requirement in enumerate(result["requirements"]):
        citation = requirement["citation"]
        attachments = [entry for entry in citation["label_grounding"] if entry["field"] == "attachment_keywords"]
        for label_index, label in enumerate(attachments):
            if label["basis"] == _CELL_WRAP_PROOF:
                proofs.append((candidate_index, label_index, citation, label))
    candidate_index, label_index, citation, label = _one(proofs, "FAILED_CELL_WRAP_PROOF_MISMATCH")
    match = _one(label["matches"], "FAILED_CELL_WRAP_PROOF_MISMATCH")
    proof = match["cell_proof"]
    raw_requirement = call["candidate"]["requirements"][candidate_index]
    _require((candidate_index, label_index) == (context["candidate_index"], context["label_index"]) and
             label["label"] == raw_requirement["attachment_keywords"][label_index] and
             all(citation.get(field) == context.get(field) for field in ("block_id", "block_ids", "locator", "source_id")) and
             proof["rule"] == _CELL_WRAP_PROOF and proof["block_id"] in citation["block_ids"] and
             proof["source_id"] == context["source_id"] and proof["source_sha256"] == window["source_sha256"],
             "FAILED_CELL_WRAP_PROOF_MISMATCH")
    return proof
