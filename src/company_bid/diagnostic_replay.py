"""Privileged local diagnostic candidate revalidation; never a provider cache.

Historical checkpoints remain historical. Only their evidenced raw candidates
are passed through today's complete validator in a new, diagnostic execution.
No credential lookup, HTTP transport, call event, or budget reservation occurs
in this module. The publication boundary is enforced by the normal executor.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from pathlib import Path

from .provider import ChatConfig, ProviderFailure, build_json_request
from .storage import LocalStore, digest


SCHEMA_VERSION = "diagnostic-replay-v1"
SUMMARY_VERSION = "diagnostic-replay-summary-v1"


def _require(condition, reason="SOURCE_INVALID"):
    if not condition:
        raise ProviderFailure("DIAGNOSTIC_REPLAY_" + reason)


def _one(items, reason="SOURCE_AMBIGUOUS"):
    values = list(items)
    _require(len(values) == 1, reason)
    return values[0]


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) and value != "0" * 32


def _json(raw):
    result = json.loads(raw)
    digest(result)  # Reject NaN/Infinity rather than accepting noncanonical JSON.
    return result


def _without_paths(value):
    if isinstance(value, dict):
        return {key: _without_paths(item) for key, item in value.items() if key != "path"}
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    return value


def _options(config, state=None):
    options = config.get("structured_provider") or config.get("provider", {})
    mode = options.get("mode")
    _require(mode in {"chat_completions", "diagnostic_mixed"}, "SOURCE_PROVIDER_INVALID")
    if mode == "diagnostic_mixed":
        # Only options are normalized. Diagnostic provenance must remain
        # explicit in the frozen source run and be proved again per live unit.
        _require(isinstance(state, dict) and state.get("status") in {"succeeded", "failed"},
                 "SOURCE_NOT_SEALED")
        _require(config.get("run_purpose") == "diagnostic_replay" and
                 config.get("debug_capture", True) is True and state.get("capture_policy") is True and
                 state.get("artifact_scope") == "diagnostic", "SOURCE_DIAGNOSTIC_PURPOSE_INVALID")
        _require(state.get("execution_config") == config and state.get("config_hash") == digest(config),
                 "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        spec = config.get("diagnostic_replay")
        _require(isinstance(spec, dict) and set(spec) == {"schema_version", "sources", "bindings"} and
                 spec.get("schema_version") == SCHEMA_VERSION and isinstance(spec.get("sources"), dict) and
                 bool(spec["sources"]) and isinstance(spec.get("bindings"), dict), "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        attempts = state.get("execution_attempts", [])
        _require(attempts and all(attempt.get("status") in {"succeeded", "failed", "paused_debug"} and
                                 attempt.get("ended_at") and attempt.get("trace_status") == "captured"
                                 for attempt in attempts), "SOURCE_NOT_SEALED")
    return ChatConfig(**{key: value for key, value in options.items()
                         if key not in {"mode", "validation_repair_attempts"}})


class _OriginRun:
    """Read existing roots only. Evidence is sealed before any target live call."""
    def __init__(self, alias, root, run_id):
        _require(_id(run_id), "CONFIG_INVALID")
        self.alias, self.root, self.run_id = alias, root, run_id
        self.store = LocalStore(root)  # The caller has already required is_dir().
        self.evidence = {}
        self.state_raw = self._file("state.json")
        self.state = _json(self.state_raw)
        _require(self.state.get("run_id") == run_id and self.state.get("schema_version") == "m1.local.v1")
        _require(self.state.get("status") != "running", "SOURCE_NOT_SEALED")
        self.events = self._rows("events.jsonl")
        self.spans = self._rows("spans.jsonl")
        from .diagnostics import inspect_diagnostics
        report = inspect_diagnostics(root, run_id)
        _require(not report["gaps"] and report["capture_policy"], "SOURCE_DIAGNOSTICS_INCOMPLETE")
        self.config = _options(self.state["execution_config"], self.state)
        options = self.state["execution_config"].get("structured_provider") or self.state["execution_config"].get("provider", {})
        self.mixed_source = options.get("mode") == "diagnostic_mixed"

    def _file(self, name):
        path = self.store.run_dir(self.run_id) / name
        raw = path.read_bytes()
        self.evidence[path.relative_to(self.root).as_posix()] = raw
        return raw

    def _rows(self, name):
        rows = [_json(line) for line in self._file(name).splitlines()]
        _require(all(isinstance(row, dict) for row in rows))
        return rows

    def artifact(self, reference):
        _require(isinstance(reference, dict) and _id(reference.get("artifact_id")))
        path = reference.get("path", "")
        _require(isinstance(path, str) and re.fullmatch(r"runs/[0-9a-f]{32}/artifacts/[^/\\]+", path), "SOURCE_REFERENCE_INVALID")
        raw = self.store.read_bytes(reference)
        self.evidence[path] = raw
        return _json(raw)

    def node(self, key):
        return _one(node for node in self.state["nodes"] if node["key"] == key)

    def checkpoint(self, record, node_key, unit_id=None):
        envelope = self.artifact(record["output_ref"])
        data = envelope.get("data")
        _require(envelope.get("schema_version") == "m1.checkpoint.v1" and
                 envelope.get("node_key") == node_key and envelope.get("unit_id") == unit_id and
                 envelope.get("producer_version") == record["version"] and
                 envelope.get("input_hash") == record["input_hash"] and isinstance(data, dict) and
                 envelope.get("output_hash") == record["output_hash"] == digest(data))
        if unit_id is not None:
            _require(envelope.get("run_id") == self.run_id)
        return envelope

    def unit(self, node_key, unit_id, *, successful=True):
        node = self.node(node_key)
        unit = node["units"][unit_id]
        _require(unit.get("unit_id") == unit_id and unit["version"] == node["version"])
        payload = self.artifact(unit["input_ref"])
        _require(digest(payload) == unit["input_hash"])
        if not successful:
            return node, unit, payload, None, None
        _require(unit["status"] == "succeeded")
        attempt = unit["attempts"][-1]
        _require(attempt["status"] == "succeeded" and _id(attempt["attempt_id"]) and
                 attempt["input_hash"] == unit["input_hash"] and attempt["output_hash"] == unit["output_hash"])
        envelope = self.checkpoint(unit, node_key, unit_id)
        _require(envelope["node_attempt_id"] == attempt["node_attempt_id"])
        span = self.span(attempt["trace_id"], attempt["span_id"])
        node_attempt = _one(item for item in node["attempts"] if item["attempt_id"] == attempt["node_attempt_id"])
        _require(span["name"] == "unit." + node_key and span["status"] != "ERROR" and
                 span["attributes"].get("unit_attempt_id") == attempt["attempt_id"] and
                 span.get("parent_span_id") == node_attempt["span_id"] and
                 span["attributes"].get("input_hash") == unit["input_hash"] and
                 span["attributes"].get("output_hash") == unit["output_hash"])
        _require(self.span(attempt["trace_id"], node_attempt["span_id"])["name"] == "node." + node_key)
        return node, unit, payload, envelope["data"], attempt

    def span(self, trace_id, span_id):
        _require(_id(trace_id) and isinstance(span_id, str) and re.fullmatch(r"[0-9a-f]{16}", span_id) and span_id != "0" * 16)
        return _one(span for span in self.spans if (span["trace_id"], span["span_id"]) == (trace_id, span_id))

    def captures(self, node, unit_id, attempt):
        node_attempt = _one(item for item in node["attempts"] if item["attempt_id"] == attempt["node_attempt_id"])
        selected = [item for item in node_attempt.get("captures", []) if item.get("unit_id") == unit_id and
                    item.get("unit_attempt_id") == attempt["attempt_id"]]
        _require(all(item.get("status") == "captured" and item.get("node_attempt_id") == attempt["node_attempt_id"] and
                     item.get("trace_id") == attempt["trace_id"] for item in selected), "SOURCE_DIAGNOSTICS_INCOMPLETE")
        return selected

    def capture(self, captures, name):
        item = _one(item for item in captures if item["name"] == name)
        _require(item.get("hash") == item["ref"]["sha256"])
        return self.artifact(item["ref"])

    def actual_mixed_unit(self, node, unit_id, attempt, task, captures):
        """A replay-only checkpoint cannot become a new actual-call source."""
        _require(node["key"] == "N2", "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        spec = self.state["execution_config"]["diagnostic_replay"]
        _require("extract_requirements:" + unit_id not in spec["bindings"], "SOURCE_ACTUAL_CALL_REQUIRED")
        _require(not any(item["name"] in {"diagnostic-replay-candidate", "diagnostic-replay-validation"}
                         for item in captures), "SOURCE_ACTUAL_CALL_REQUIRED")
        node_attempt = _one(item for item in node["attempts"] if item["attempt_id"] == attempt["node_attempt_id"])
        preflight_ref = _one(item for item in node_attempt.get("captures", [])
                             if item["name"] == "diagnostic-replay-preflight")
        _require(preflight_ref.get("status") == "captured" and preflight_ref.get("unit_id") is None and
                 preflight_ref.get("unit_attempt_id") is None and
                 preflight_ref.get("node_attempt_id") == attempt["node_attempt_id"] and
                 preflight_ref.get("trace_id") == node_attempt.get("trace_id") == attempt["trace_id"],
                 "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        preflight = self.capture([preflight_ref], "diagnostic-replay-preflight")
        _require(preflight.get("schema_version") == SUMMARY_VERSION and preflight.get("diagnostic_only") is True and
                 preflight.get("publication_status") == "diagnostic_only" and
                 preflight.get("binding_count") == preflight.get("verified_binding_count") == len(spec["bindings"]) and
                 isinstance(preflight.get("origins"), list) and len(preflight["origins"]) == len(spec["bindings"]) and
                 preflight.get("historical_calls_charged_again") == 0, "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        for key, binding in spec["bindings"].items():
            _require(isinstance(binding, dict) and set(binding) == {"alias", "run_id", "node_key", "unit_id"} and
                     binding["alias"] in spec["sources"] and _id(binding["run_id"]) and
                     binding["node_key"] in {"N2", "N2_WINDOW_DIAGNOSTIC"} and
                     key == "extract_requirements:" + binding["unit_id"], "SOURCE_DIAGNOSTIC_METADATA_INVALID")
            origin = _one(item for item in preflight["origins"] if isinstance(item, dict) and
                          (item.get("source_alias"), item.get("run_id"), item.get("node_key"), item.get("unit_id")) ==
                          (binding["alias"], binding["run_id"], binding["node_key"], binding["unit_id"]))
            _require(origin.get("external_call") is False and isinstance(origin.get("calls"), list) and
                     origin["calls"], "SOURCE_DIAGNOSTIC_METADATA_INVALID")
        miss = self.capture(captures, "diagnostic-replay-live-miss")
        _require(miss == {"unit_id": unit_id, "request_sha256": digest(task), "diagnostic_only": True,
                          "reason": "key_not_declared"}, "SOURCE_DIAGNOSTIC_METADATA_INVALID")

    def call(self, node, unit_id, attempt, call_id, call_attempt, *, passed):
        _require(_id(call_id) and type(call_attempt) is int and call_attempt == 1, "LINEAGE_UNSUPPORTED")
        captures = self.captures(node, unit_id, attempt)
        request = self.capture(captures, f"request-{call_id}-{call_attempt}")
        response = self.capture(captures, f"response-{call_id}-{call_attempt}")
        validation = self.capture(captures, f"validation-{call_id}-{call_attempt}")
        events = [event for event in self.events if event.get("call_id") == call_id and
                  event.get("call_attempt") == call_attempt and event.get("node_attempt_id") == attempt["node_attempt_id"]]
        started = _one(event for event in events if event["event"] == "call.started")
        terminal = _one(event for event in events if event["event"] in {"call.failed", "call.completed"})
        _require(terminal["event"] == ("call.completed" if passed else "call.failed") and
                 validation.get("status") == ("passed" if passed else "failed"))
        for event in (started, terminal):
            _require(event.get("node_key") == node["key"] and event.get("unit_id") == unit_id and
                     event.get("unit_attempt_id") == attempt["attempt_id"] and
                     event.get("input_hash") == digest(request))
        _require((started["trace_id"], started["span_id"]) == (terminal["trace_id"], terminal["span_id"]))
        span = self.span(terminal["trace_id"], terminal["span_id"])
        _require(span["name"] == "provider.call" and span["attributes"].get("call_id") == call_id and
                 span["attributes"].get("call_attempt") == call_attempt and
                 span.get("parent_span_id") == attempt["span_id"] and span["trace_id"] == attempt["trace_id"] and
                 (span["status"] != "ERROR" if passed else span["status"] == "ERROR"))
        _require(terminal.get("output_hash") == digest(response) and
                 all(span["attributes"].get(key) == terminal.get(key) for key in
                     ("input_hash", "output_hash", "node_key", "node_attempt_id", "unit_id", "unit_attempt_id",
                      "model", "prompt_version", "validation_status")), "SOURCE_TRACE_MISMATCH")
        reservation = _one(row for row in self.state.get("call_budget", {}).get("reservations", [])
                           if row.get("call_id") == call_id and row.get("call_attempt") == call_attempt and
                           row.get("node_attempt_id") == attempt["node_attempt_id"])
        _require(reservation.get("input_hash") == digest(request) and reservation.get("unit_id") == unit_id and
                 reservation.get("unit_attempt_id") == attempt["attempt_id"])
        if reservation.get("settlement_status") == "known_usage_cost_estimate":
            _require(reservation.get("response_sha256") == digest(response))
        _require(response["choices"][0].get("finish_reason") == "stop")
        candidate = _json(response["choices"][0]["message"]["content"])
        _require(isinstance(candidate, dict))
        task = _json(request["messages"][-1]["content"])
        if not passed:
            failure = self.capture(captures, f"failure-{call_id}-{call_attempt}")
            _require(failure.get("error_code") == terminal.get("error_code") == validation.get("error_code"))
        return {"request": request, "response": response, "candidate": candidate, "task": task,
                "validation": validation, "terminal": terminal, "call_id": call_id, "call_attempt": call_attempt}

    def unchanged(self):
        _require((self.store.run_dir(self.run_id) / "state.json").read_bytes() == self.state_raw, "SOURCE_CHANGED")
        for path, raw in self.evidence.items():
            _require(self.store.resolve(path).read_bytes() == raw, "SOURCE_CHANGED")


class DiagnosticReplay:
    def __init__(self, config, provider_config):
        self.config = copy.deepcopy(config)
        self.provider_config = provider_config
        self._ready = None
        self._entries = {}
        self._runs = {}
        self._loading = set()
        self._loaded_entries = {}
        self._source_windows = {}
        self._source_file_hashes = {}
        self._source_file_paths = {}
        self._summary_lock = threading.Lock()
        self._summary = {"schema_version": SUMMARY_VERSION, "diagnostic_only": True,
                         "publication_status": "diagnostic_only", "binding_count": 0,
                         "verified_binding_count": 0, "revalidated_count": 0, "live_miss_count": 0,
                         "revalidated_this_attempt_count": 0, "checkpoint_reuse_count": 0,
                         "live_checkpoint_reuse_count": 0, "historical_calls_charged_again": 0, "origins": []}

    def summary(self):
        with self._summary_lock:
            return copy.deepcopy(self._summary)

    def _run(self, alias, run_id):
        _require(alias in self.roots, "CONFIG_INVALID")
        _require((self.roots[alias], run_id) != self._target, "SOURCE_CURRENT_RUN")
        key = alias, run_id
        if key not in self._runs:
            self._runs[key] = _OriginRun(alias, self.roots[alias], run_id)
        return self._runs[key]

    def _n1(self, run):
        node = run.node("N1")
        _require(node["status"] == "succeeded")
        value = run.checkpoint(node, "N1")["data"]
        _require(_without_paths(value["manifest"]) == _without_paths(self.current["manifest"]), "SCOPE_MISMATCH")
        for source in [*value["manifest"]["sources"], *value["manifest"].get("attachments", [])]:
            path = Path(source["path"]).resolve()
            _require(any(path.is_relative_to(root) for root in self.roots.values()), "SOURCE_REFERENCE_INVALID")
            self._verify_source_file(path, source["sha256"], source["path"])
        return value, node

    def _verify_source_file(self, path, expected, source_path):
        # A historical N1 may be used by many windows. Its original binaries
        # are not consumed again by those window/proof builders; they operate
        # on the authenticated checkpoint. Hash each distinct resolved path
        # once, then hash ALL of them again before preflight releases any call.
        # No mtime/size shortcut, cross-preflight cache or persistent assertion.
        source_path = Path(source_path)
        _require(source_path.resolve() == path and self._source_file_paths.get(source_path, path) == path,
                 "SOURCE_CHANGED")
        self._source_file_paths[source_path] = path
        _require(path.is_file())
        if path not in self._source_file_hashes:
            _require(hashlib.sha256(path.read_bytes()).hexdigest() == expected)
            self._source_file_hashes[path] = expected
        _require(self._source_file_hashes[path] == expected)

    def _seal_source_files(self):
        for path, expected in self._source_file_hashes.items():
            _require(path.resolve() == path and path.is_file() and
                     hashlib.sha256(path.read_bytes()).hexdigest() == expected, "SOURCE_CHANGED")
        # Preserve original manifest spellings too: a junction/symlink can be
        # retargeted while its formerly resolved file remains intact.
        for source_path, resolved in self._source_file_paths.items():
            _require(source_path.resolve() == resolved, "SOURCE_CHANGED")

    def _source_window(self, run, payload, unit_id):
        from .extraction import _index, _windows, extraction_window_payload
        value, n1 = self._n1(run)
        # _n1 still authenticates the checkpoint bytes, complete data hash and
        # project scope on EVERY lookup; originals are sealed before and after
        # the complete preflight. Only deterministic
        # indexing/window construction is shared within this preflight; this
        # is neither a stored candidate nor a cross-execution validity cache.
        key = n1["output_hash"], digest(payload["window_config"])
        if key not in self._source_windows:
            blocks, _, documents = _index(value["parsed"], value["manifest"])
            self._source_windows[key] = _windows(documents, blocks, payload["window_config"])[0]
        windows = self._source_windows[key]
        window = _one(window for window in windows if window["window_id"] == unit_id)
        _require(window == self.windows[unit_id] and extraction_window_payload(window, payload["window_config"]) == payload,
                 "SOURCE_WINDOW_MISMATCH")
        # Correction proof builders must not be able to mutate another lookup.
        return copy.deepcopy(window), n1

    def _wire(self, run, call, task):
        from .structured import SYSTEM_PROMPT, PROMPT_VERSION
        _require(run.config.endpoint == self.provider_config.endpoint and run.config.service == self.provider_config.service,
                 "PROVIDER_MISMATCH")
        # ``call.task`` was parsed from the authenticated historical request.
        # Compare every JSON value against today's rebuilt task, then retain
        # its original object ordering when rebuilding that historical wire.
        # Canonical digests additionally distinguish bool/int/float values
        # which Python object equality alone can treat as equal.
        _require(call.get("task") == task and digest(call["task"]) == digest(task), "REQUEST_MISMATCH")
        _require(call["terminal"].get("prompt_version") == PROMPT_VERSION and
                 call["terminal"].get("model") == self.provider_config.model and
                 call["request"] == build_json_request(self.provider_config, call["task"], SYSTEM_PROMPT), "REQUEST_MISMATCH")

    def _witnesses(self, coverage, run, full_window):
        from .extraction import validate_live_window
        available = {block["block_id"]: block for block in [*full_window["blocks"], *full_window["adjacent_context"]]}
        witnesses = []
        for group in coverage.get("resolution_groups", []):
            for witness in group.get("source_witnesses", []):
                _require(witness.get("run_id") == run.run_id and witness.get("business_answer_reused") is False and
                         witness.get("semantic_boundary_proven") is False, "LINEAGE_INVALID")
                node, unit, payload, data, attempt = run.unit("N2", witness["unit_id"])
                for name, value in (("producer_version", unit["version"]), ("input_ref", unit["input_ref"]),
                                    ("input_hash", unit["input_hash"]), ("output_ref", unit["output_ref"]),
                                    ("output_hash", unit["output_hash"]), ("trace_id", attempt["trace_id"]),
                                    ("span_id", attempt["span_id"])):
                    _require(witness.get(name) == value, "LINEAGE_INVALID")
                prior_window, _ = self._source_window(run, payload, witness["unit_id"])
                from .extraction import extraction_window_payload
                prior_payload = extraction_window_payload(self.windows[witness["unit_id"]], self.window_config)
                self._load_entry({"alias": run.alias, "run_id": run.run_id, "node_key": "N2", "unit_id": witness["unit_id"]},
                                 {**prior_payload, "task_type": "extract_requirements"}, prior_window)
                prior = validate_live_window(copy.deepcopy(data["raw_model_candidate"]), prior_window)
                quote = prior["requirements"][witness["candidate_index"]]["citation"]
                _require(quote["block_ids"] == group["block_ids"] and quote["quote_sha256"] == witness["quote_sha256"] and
                         witness["source_sha256"] == full_window["source_sha256"], "LINEAGE_INVALID")
                prior_available = {block["block_id"]: block for block in [*prior_window["blocks"], *prior_window["adjacent_context"]]}
                _require(all(prior_available[key] == available[key] for key in group["block_ids"]), "LINEAGE_INVALID")
            if group.get("source_witnesses"):
                witnesses.append(group)
        return witnesses

    def _completed_owner_witness(self, coverage):
        proof = coverage.get("owned_adjacent_requirement")
        if not proof:
            return None
        from .extraction import extraction_window_payload
        binding = proof["binding"]
        window = self.windows[binding["unit_id"]]
        task = {**extraction_window_payload(window, self.window_config), "task_type": "extract_requirements"}
        entry = self._load_entry(binding, task, window)
        _require(entry["origin"] == proof["origin"], "OWNER_WITNESS_MISMATCH")
        return {"binding": copy.deepcopy(binding), "entry": entry, "full_window": copy.deepcopy(window)}

    def _correction(self, first_run, first, final_run, final, coverage, window, context, code, *, standalone_node_key=None):
        from .structured import (
            _repair_context, _repair_coverage, _covers_rejected_candidate,
            _validation_repair_request, _extended_contract_repair,
        )
        actual_context = _repair_context(first["validation"].get("diagnostic_context"))
        _require(first["terminal"].get("error_code") == code, "LINEAGE_INVALID")
        omitted_error_field = actual_context != context
        if omitted_error_field:
            # A historical standalone probe omitted one redundant error-path
            # label from its actual feedback. Authenticate that wire unchanged;
            # never pretend the missing field was sent, or apply this to the
            # ordinary automatic-repair lineage.
            _require(standalone_node_key == "N2_WINDOW_DIAGNOSTIC" and
                     first_run.run_id != final_run.run_id and
                     code == "LIVE_REQUIRED_BLOCK_UNRESOLVED" and _extended_contract_repair(coverage) and
                     type(actual_context) is dict and actual_context == {"field": "unresolved_block_ids"} and
                     type(context) is dict and not context, "LINEAGE_INVALID")
        witnesses = self._witnesses(coverage, first_run, window)
        source_bound = bool(coverage.get("format_continuation") or coverage.get("owned_adjacent_requirement") or
                            coverage.get("citation_gap_restore")) or any(
            group.get("resolution_kind") in {"source_bound_material_heading", "source_bound_contract_blank", "source_bound_contract_pending"}
            for group in coverage.get("resolution_groups", []))
        source_data = self._n1(first_run)[0] if source_bound else None
        owner_witness = self._completed_owner_witness(coverage)
        rebuilt = _repair_coverage(first["candidate"], first["task"], error_code=code,
                                   diagnostic_context=context, source_witnesses=witnesses, source_data=source_data,
                                   owner_witness=owner_witness)
        _require(rebuilt is not None and rebuilt == coverage, "LINEAGE_UNSUPPORTED")
        if omitted_error_field:
            actual_rebuilt = _repair_coverage(first["candidate"], first["task"], error_code=code,
                diagnostic_context=actual_context, source_witnesses=witnesses, source_data=source_data,
                owner_witness=owner_witness)
            _require(actual_rebuilt is not None and actual_rebuilt == rebuilt and
                     digest(actual_rebuilt) == digest(rebuilt) == digest(coverage), "LINEAGE_UNSUPPORTED")
        if source_bound:
            current = _repair_coverage(first["candidate"], first["task"], error_code=code,
                                       diagnostic_context=context, source_data=self.current, owner_witness=owner_witness)
            if omitted_error_field:
                actual_current = _repair_coverage(first["candidate"], first["task"], error_code=code,
                    diagnostic_context=actual_context, source_data=self.current, owner_witness=owner_witness)
                _require(current is not None and actual_current is not None and actual_current == current and
                         digest(actual_current) == digest(current), "SOURCE_CONTEXT_MISMATCH")
            def source_identity(value, source):
                value = copy.deepcopy(value)
                for group in value.get("resolution_groups", []):
                    if group.get("resolution_kind") in {"source_bound_material_heading", "source_bound_contract_blank", "source_bound_contract_pending"}:
                        proof = group["source_proof"]
                        # Both full N1 views were independently validated. Only
                        # their container digests can include fresh read artifacts;
                        # all source text, coordinates and boundary hashes remain.
                        proof.pop("parsed_sha256", None)
                        proof.pop("source_reading_sha256", None)
                        group["source_proof_sha256"] = digest(proof)
                for field in ("owned_adjacent_requirement", "citation_gap_restore"):
                    if value.get(field):
                        proof = value[field]["source_proof"]
                        proof.pop("parsed_sha256", None)
                        proof.pop("source_reading_sha256", None)
                        if field == "citation_gap_restore":
                            # A fresh N0 copies identical sources into a new
                            # run directory. _n1 already compared every other
                            # manifest field and authenticated both originals;
                            # keep that content identity in the comparison.
                            _require(proof.get("manifest_sha256") == digest(source["manifest"]),
                                     "SOURCE_CONTEXT_MISMATCH")
                            proof["manifest_sha256"] = digest(_without_paths(source["manifest"]))
                        value[field]["source_proof_sha256"] = digest(proof)
                        if field == "owned_adjacent_requirement":
                            value[field]["proof_sha256"] = digest({k: v for k, v in value[field].items() if k != "proof_sha256"})
                if value.get("format_continuation"):
                    proof = value["format_continuation"]
                    proof.pop("parsed_sha256", None)
                    proof.pop("source_reading_sha256", None)
                    proof.pop("proof_sha256", None)
                    proof["proof_sha256"] = digest(proof)
                return value
            _require(current is not None and source_identity(current, self.current) == source_identity(coverage, source_data),
                     "SOURCE_CONTEXT_MISMATCH")
        expected = _validation_repair_request(first["task"], code, context, coverage, first["call_id"], first["call_attempt"])
        # The actual feedback message keeps its historical object ordering and
        # call identity. Stored coverage JSON has sorted keys; reserializing it
        # must not relabel the old wire or invent a different feedback prompt.
        self._wire(final_run, final, expected)
        _require(final["task"] == expected and
                 _covers_rejected_candidate(copy.deepcopy(final["candidate"]), coverage, expected), "LINEAGE_INVALID")
        if omitted_error_field:
            return [{"kind": "historical_standalone_v11_redundant_error_field_omission",
                "source_run_id": first_run.run_id, "correction_run_id": final_run.run_id,
                "correction_node_key": standalone_node_key, "error_code": code,
                "source_error_context": copy.deepcopy(actual_context), "sent_feedback_context": copy.deepcopy(context),
                "source_error_context_sha256": digest(actual_context), "sent_feedback_context_sha256": digest(context),
                "source_validation_diagnostic_context_sha256": digest(first["validation"].get("diagnostic_context")),
                "actual_context_coverage_sha256": digest(actual_rebuilt), "sent_context_coverage_sha256": digest(rebuilt),
                "actual_wire_sha256": digest(final["request"]), "actual_task_sha256": digest(final["task"]),
                "omitted_field_was_sent": False, "historical_request_modified": False,
                "historical_candidate_modified": False}]
        return []

    def _load_entry(self, binding, task, full_window):
        identity = binding["alias"], binding["run_id"], binding["node_key"], binding["unit_id"]
        _require(identity not in self._loading, "LINEAGE_CYCLE")
        if identity not in self._loaded_entries:
            self._loading.add(identity)
            try:
                self._loaded_entries[identity] = self._read_entry(binding, task, full_window)
            finally:
                self._loading.remove(identity)
        return copy.deepcopy(self._loaded_entries[identity])

    def _read_entry(self, binding, task, full_window):
        run = self._run(binding["alias"], binding["run_id"])
        node, unit, payload, data, attempt = run.unit(binding["node_key"], binding["unit_id"])
        _require(payload == {key: value for key, value in task.items() if key != "task_type"}, "REQUEST_MISMATCH")
        captures = run.captures(node, unit["unit_id"], attempt)
        if run.mixed_source:
            run.actual_mixed_unit(node, unit["unit_id"], attempt, task, captures)
        completed = [event for event in run.events if event.get("event") == "call.completed" and
                     event.get("unit_attempt_id") == attempt["attempt_id"] and
                     event.get("node_attempt_id") == attempt["node_attempt_id"]]
        terminal = _one(completed)
        final = run.call(node, unit["unit_id"], attempt, terminal["call_id"], terminal["call_attempt"], passed=True)
        _require(data.get("status") == "complete" and data.get("window_id") == unit["unit_id"] and
                 data.get("candidate_origin") == "application_normalized" and
                 data.get("raw_model_candidate") == final["candidate"] and
                 data.get("validated") == final["validation"].get("validated_output"))
        origin_calls = [(run, final)]
        lineage_notes = []
        if binding["node_key"] == "N2":
            window, _ = self._source_window(run, payload, unit["unit_id"])
            relations = [item for item in captures if item["name"].startswith("validation-repair-result-")]
            if not relations:
                _require(len([item for item in captures if item["name"].startswith("request-")]) == 1, "LINEAGE_UNSUPPORTED")
                self._wire(run, final, task)
            else:
                relation_ref = _one(relations)
                relation = run.capture(captures, relation_ref["name"])
                _require(relation.get("schema_version") in {"validation-repair-v5", "validation-repair-v6", "validation-repair-v7", "validation-repair-v8", "validation-repair-v9", "validation-repair-v10", "validation-repair-v11"} and
                         relation.get("status") == "completed" and relation.get("unit_id") == unit["unit_id"] and
                         relation.get("repair_attempt") == 1 and relation.get("second_call_started") is True, "LINEAGE_INVALID")
                source_bound = any(group.get("resolution_kind") == "source_bound_material_heading"
                                   for group in relation.get("required_coverage", {}).get("resolution_groups", []))
                _require((relation["schema_version"] == "validation-repair-v8") == source_bound, "LINEAGE_INVALID")
                from .structured import _extended_contract_repair
                extended = _extended_contract_repair(relation["required_coverage"])
                _require((relation["schema_version"] == "validation-repair-v11") == extended, "LINEAGE_INVALID")
                _require((relation["schema_version"] == "validation-repair-v10") == (not extended and any(
                    group.get("resolution_kind") == "source_bound_contract_blank"
                    for group in relation.get("required_coverage", {}).get("resolution_groups", []))), "LINEAGE_INVALID")
                _require((relation["schema_version"] == "validation-repair-v9") == bool(
                    relation.get("required_coverage", {}).get("format_continuation")), "LINEAGE_INVALID")
                first = run.call(node, unit["unit_id"], attempt, relation["first_call_id"], relation["first_call_attempt"], passed=False)
                self._wire(run, first, task)
                _require(relation.get("first_request_hash") == digest(task) and
                         relation.get("first_provider_input_hash") == digest(first["request"]) and
                         relation.get("first_candidate_hash") == digest(first["candidate"]) and
                         relation.get("second_call_id") == final["call_id"] and
                         relation.get("second_call_attempt") == final["call_attempt"] and
                         relation.get("second_provider_input_hash") == digest(final["request"]) and
                         relation.get("second_candidate_hash") == digest(final["candidate"]) and
                         relation.get("repair_request_hash") == digest(final["task"]) and
                         relation.get("error_code") == first["terminal"].get("error_code"), "LINEAGE_INVALID")
                scheduled = run.capture(captures, "validation-repair-scheduled-" + first["call_id"])
                _require(all(scheduled.get(key) == value for key, value in relation.items()
                             if key not in {"status", "second_call_id", "second_call_attempt", "second_provider_input_hash",
                                            "second_candidate_hash", "second_call_started"}), "LINEAGE_INVALID")
                self._correction(run, first, run, final, relation["required_coverage"], window,
                                 relation["diagnostic_context"], relation["error_code"])
                origin_calls.insert(0, (run, first))
        else:
            binding_data = run.capture(captures, "historical-failure-correction-binding")
            root = Path(binding_data["source_root"]).resolve()
            alias = _one(alias for alias, candidate in self.roots.items() if candidate == root)
            original = self._run(alias, binding_data["source_run_id"])
            window, n1 = self._source_window(original, payload, unit["unit_id"])
            _require(binding_data.get("source_state_sha256") == hashlib.sha256(original.state_raw).hexdigest() and
                     binding_data.get("source_N1_output_hash") == n1["output_hash"] and
                     binding_data.get("source_checkpoint_relative_path") == n1["output_ref"]["path"] and
                     binding_data.get("source_checkpoint_file_sha256") == n1["output_ref"]["sha256"] and
                     binding_data.get("window_id") == unit["unit_id"] and
                     binding_data.get("historical_call_is_not_a_new_request") is True and
                     binding_data.get("task_payload_updated_for_new_prompt") is False and
                     binding_data.get("whole_N2_complete") is False and
                     binding_data.get("payload_sha256") == digest(payload), "LINEAGE_INVALID")
            old_node, old_unit, old_payload, _, _ = original.unit("N2", unit["unit_id"], successful=False)
            _require(old_payload == payload, "REQUEST_MISMATCH")
            call_id = binding_data["historical_failed_call_id"]
            old_attempt = _one(item for item in old_unit["attempts"] if any(
                cap.get("name") == f"request-{call_id}-1" and cap.get("unit_attempt_id") == item["attempt_id"]
                for node_attempt in old_node["attempts"] for cap in node_attempt.get("captures", [])))
            _require(old_attempt["status"] == "failed")
            first = original.call(old_node, unit["unit_id"], old_attempt, call_id, 1, passed=False)
            self._wire(original, first, task)
            for field, expected in (("historical_candidate_sha256", digest(first["candidate"])),
                                    ("historical_provider_input_sha256", digest(first["request"])),
                                    ("historical_response_sha256", digest(first["response"])),
                                    ("historical_task_payload_sha256", digest(task)),
                                    ("original_request_sha256", digest(task)),
                                    ("correction_request_sha256", digest(final["task"])),
                                    ("protected_inventory_sha256", digest(binding_data["required_coverage"]))):
                _require(binding_data.get(field) == expected, "LINEAGE_INVALID")
            first_captures = original.captures(old_node, unit["unit_id"], old_attempt)
            for kind in ("request", "response"):
                capture = _one(item for item in first_captures if item["name"] == f"{kind}-{call_id}-1")
                _require(binding_data.get(f"historical_{kind}_ref") == capture["ref"]["path"], "LINEAGE_INVALID")
            feedback = final["task"]["validation_feedback"]
            lineage_notes = self._correction(original, first, run, final, binding_data["required_coverage"], window,
                             feedback["context"], feedback["code"], standalone_node_key=node["key"])
            _require(feedback["code"] == first["terminal"].get("error_code"), "LINEAGE_INVALID")
            origin_calls.insert(0, (original, first))
        from .extraction import validate_live_window
        try:
            validate_live_window(copy.deepcopy(final["candidate"]), full_window)
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_VALIDATION_FAILED") from error
        origin = {"source_alias": binding["alias"], "run_id": run.run_id, "node_key": node["key"],
                  "unit_id": unit["unit_id"], "historical_producer_version": unit["version"],
                  "candidate_sha256": digest(final["candidate"]), "base_request_sha256": digest(task),
                  "actual_final_wire_sha256": digest(final["request"]), "external_call": False,
                  "calls": [{"source_alias": owner.alias, "run_id": owner.run_id, "call_id": call["call_id"],
                             "call_attempt": call["call_attempt"], "trace_id": call["terminal"]["trace_id"],
                             "span_id": call["terminal"]["span_id"], "request_sha256": digest(call["request"]),
                             "response_sha256": digest(call["response"])} for owner, call in origin_calls]}
        if lineage_notes:
            origin["lineage_notes"] = lineage_notes
        return {"candidate": copy.deepcopy(final["candidate"]), "request_sha256": digest(task), "origin": origin}

    def preflight(self, ctx, value):
        self._ready = None
        self._entries = {}
        self._runs = {}
        self._loading = set()
        self._loaded_entries = {}
        self._source_windows = {}
        self._source_file_hashes = {}
        self._source_file_paths = {}
        try:
            _require(ctx.execution_config.get("run_purpose") == "diagnostic_replay" and
                     ctx.execution_config.get("diagnostic_replay") == self.config and
                     ctx.execution_config.get("debug_capture", True) and
                     getattr(ctx, "artifact_scope", "diagnostic") == "diagnostic", "PURPOSE_REQUIRED")
            _require(ctx.node_key == "N2", "TASK_NOT_ALLOWED")
            self._target = (ctx.executor.store.root.resolve(), ctx.run_id)
            config = self.config
            _require(isinstance(config, dict) and set(config) == {"schema_version", "sources", "bindings"} and
                     config["schema_version"] == SCHEMA_VERSION and isinstance(config["sources"], dict) and
                     config["sources"] and isinstance(config["bindings"], dict), "CONFIG_INVALID")
            self.roots = {}
            for alias, raw in config["sources"].items():
                _require(isinstance(alias, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", alias) and
                         isinstance(raw, str) and Path(raw).is_absolute() and Path(raw).is_dir(), "CONFIG_INVALID")
                root = Path(raw).resolve()
                _require(root not in self.roots.values(), "CONFIG_INVALID")
                self.roots[alias] = root
            self.current = copy.deepcopy(value)
            for source in [*value["manifest"]["sources"], *value["manifest"].get("attachments", [])]:
                path = Path(source["path"]).resolve()
                _require(path.is_relative_to(ctx.executor.store.root) or
                         any(path.is_relative_to(root) for root in self.roots.values()), "SOURCE_REFERENCE_INVALID")
                self._verify_source_file(path, source["sha256"], source["path"])
            from .extraction import _index, _windows, extraction_window_payload
            blocks, _, documents = _index(value["parsed"], value["manifest"])
            extraction = ctx.execution_config.get("extraction", {})
            window_config = {"window_max_chars": extraction.get("window_max_chars", 14000),
                             "window_max_blocks": extraction.get("window_max_blocks", 60)}
            self.window_config = window_config
            windows, _ = _windows(documents, blocks, window_config)
            self.windows = {window["window_id"]: window for window in windows}
            self.current_requests = {"extract_requirements:" + window["window_id"]:
                                     digest({**extraction_window_payload(window, window_config),
                                             "task_type": "extract_requirements"}) for window in windows}
            from .structured import SYSTEM_PROMPT
            self.current_wires = {"extract_requirements:" + window["window_id"]:
                                  digest(build_json_request(self.provider_config,
                                      {**extraction_window_payload(window, window_config),
                                       "task_type": "extract_requirements"}, SYSTEM_PROMPT)) for window in windows}
            self._summary["binding_count"] = len(config["bindings"])
            for key, binding in config["bindings"].items():
                _require(isinstance(binding, dict) and set(binding) == {"alias", "run_id", "node_key", "unit_id"} and
                         binding["node_key"] in {"N2", "N2_WINDOW_DIAGNOSTIC"} and
                         binding["unit_id"] in self.windows and key == "extract_requirements:" + binding["unit_id"], "CONFIG_INVALID")
                payload = extraction_window_payload(self.windows[binding["unit_id"]], window_config)
                task = {**payload, "task_type": "extract_requirements"}
                self._entries[key] = self._load_entry(binding, task, self.windows[binding["unit_id"]])
            completed = [unit for unit in ctx._record.get("units", {}).values() if unit.get("status") == "succeeded"]
            reused = sum("extract_requirements:" + unit["unit_id"] in self._entries for unit in completed)
            self._summary.update(revalidated_count=reused, checkpoint_reuse_count=reused,
                                 live_checkpoint_reuse_count=len(completed) - reused,
                                 statistics_scope="completed bound units in this run; live_miss_count counts this node attempt")
            snapshots = []
            for run in self._runs.values():
                run.unchanged()
                for path, raw in run.evidence.items():
                    suffix = ".jsonl" if path.endswith(".jsonl") else ".json"
                    reference = ctx.store_bytes("historical-evidence" + suffix, raw,
                                                "application/x-ndjson" if suffix == ".jsonl" else "application/json")
                    snapshots.append({"source_alias": run.alias, "run_id": run.run_id,
                                      "source_relative_path": path, "origin_sha256": hashlib.sha256(raw).hexdigest(),
                                      "snapshot": reference})
            self.snapshots = snapshots
            self._seal_source_files()
            self._summary.update(verified_binding_count=len(self._entries),
                                 origins=[entry["origin"] for entry in self._entries.values()], historical_evidence=snapshots,
                                 source_file_verification={"policy": "full_hash_before_and_after_preflight",
                                                           "distinct_paths": len(self._source_file_hashes)})
            self._capture(ctx, "diagnostic-replay-preflight", self._summary)
            ctx._observer.check()
            self._ready = (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config))
            return self.summary()
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_PREFLIGHT_FAILED") from error

    @staticmethod
    def _capture(ctx, name, payload):
        result = ctx.capture(name, payload)
        _require(isinstance(result, dict) and result.get("status") == "captured", "CAPTURE_FAILED")

    def candidate(self, ctx, task_type, unit_id, request, validate):
        _require(self._ready == (ctx.run_id, ctx.attempt_id, digest(ctx.execution_config)), "PREFLIGHT_REQUIRED")
        _require(task_type == "extract_requirements", "TASK_NOT_ALLOWED")
        key = task_type + ":" + unit_id
        _require(key in self.current_requests and self.current_requests[key] == digest(request), "REQUEST_MISMATCH")
        from .structured import SYSTEM_PROMPT
        _require(self.current_wires[key] == digest(build_json_request(self.provider_config, request, SYSTEM_PROMPT)),
                 "REQUEST_MISMATCH")
        # A genuine miss may buy a request only while all declared historical
        # evidence remains intact, including when that miss precedes replay.
        for snapshot in self.snapshots:
            ctx.executor.store.read_bytes(snapshot["snapshot"])
        if key not in self.config["bindings"]:
            with self._summary_lock:
                self._summary["live_miss_count"] += 1
            self._capture(ctx, "diagnostic-replay-live-miss", {"unit_id": unit_id, "request_sha256": digest(request),
                                                           "diagnostic_only": True, "reason": "key_not_declared"})
            return None
        entry = copy.deepcopy(self._entries[key])
        _require(entry["request_sha256"] == digest(request), "REQUEST_MISMATCH")
        origin = entry["origin"]
        links = [{"trace_id": call["trace_id"], "span_id": call["span_id"]} for call in origin["calls"]]
        with ctx._observer.span("candidate.revalidation", {**ctx._metadata(), "operation": "diagnostic_replay",
                                 "input_hash": digest(request), "output_hash": origin["candidate_sha256"]}, links=links):
            self._capture(ctx, "diagnostic-replay-candidate", {"diagnostic_only": True, "origin": origin,
                          "candidate": copy.deepcopy(entry["candidate"]), "external_call": False})
            try:
                result = validate(copy.deepcopy(entry["candidate"]))
            except Exception as error:
                code = getattr(error, "code", "VALIDATION_FAILED")
                code = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) else "VALIDATION_FAILED"
                self._capture(ctx, "diagnostic-replay-validation", {"diagnostic_only": True, "status": "failed",
                              "origin": origin, "error_code": code, "new_external_calls": 0})
                raise
            _require(isinstance(result, dict), "VALIDATOR_INVALID")
            self._capture(ctx, "diagnostic-replay-validation", {"diagnostic_only": True, "status": "passed",
                          "origin": origin, "validated_output": result, "new_external_calls": 0})
            ctx.call_event("candidate.revalidated", {"operation": "diagnostic_replay", "input_hash": digest(request),
                                                     "output_hash": digest(result), "status": "passed"})
        ctx._observer.check()
        with self._summary_lock:
            self._summary["revalidated_count"] += 1
            self._summary["revalidated_this_attempt_count"] += 1
        return result
