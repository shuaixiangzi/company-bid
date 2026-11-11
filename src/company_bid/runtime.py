"""M1 single-process, local CLI executor.

One atomic state file is authoritative per run. OS locks prevent overlapping
local commands; they are not PostgreSQL transactions, leases or fencing tokens.
Handlers must use run_unit for durable partial progress and must not perform
unbounded retries or run other nodes themselves.
"""

from __future__ import annotations

import copy
import re
import time
import traceback
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .observability import Observer, ObservabilityWriteError, redact_payload, utc_now
from .storage import ArtifactIntegrityError, LocalStore, StorageError, digest, process_lock


class RuntimeContractError(RuntimeError):
    code = "NODE_CONTRACT_INVALID"


class ConfigurationChangedError(RuntimeContractError):
    code = "CONFIGURATION_CHANGED_REQUIRES_CHILD_RUN"


class CheckpointInvalidError(RuntimeContractError):
    code = "CHECKPOINT_INVALID"

    def __init__(self, node_key: str):
        self.node_key = node_key
        super().__init__("A successful checkpoint is invalid; create a repair child run.")


class CaptureFailedError(RuntimeContractError):
    code = "DIAGNOSTICS_CAPTURE_FAILED"


class ProviderBudgetError(RuntimeContractError):
    code = "PROVIDER_RUN_BUDGET_EXCEEDED"


@dataclass(frozen=True)
class NodeSpec:
    key: str
    version: str
    handler: Callable[["NodeContext", dict], dict]
    config_keys: tuple[str, ...] | None = None


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise RuntimeContractError(label + " must be a stable identifier.")
    return value


def _safe_error(exc: BaseException) -> dict:
    # Exception text may contain source material, response bodies or secrets.
    # Preserve only a stable code and a bounded Python class identifier.
    code = getattr(exc, "code", "FILE_PERMISSION_DENIED" if isinstance(exc, PermissionError) else "NODE_EXECUTION_FAILED")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
        code = "NODE_EXECUTION_FAILED"
    return {"error_code": code, "error_type": type(exc).__name__[:80]}


def _error_diagnostic(exc: BaseException) -> dict:
    # A useful stack without exception text, source lines, local variables or
    # request bodies. These frames live only in the controlled capture store.
    details = {**_safe_error(exc), "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                                           for frame in traceback.extract_tb(exc.__traceback__)[-30:]]}
    if isinstance(exc, OSError):
        details["os_error"] = {key: getattr(exc, key, None) for key in ("errno", "winerror", "filename", "filename2")}
    cause, seen = exc, set()
    for _ in range(8):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, ArtifactIntegrityError):
            details["artifact_integrity"] = cause.diagnostic_context()
            break
        cause = cause.__cause__
    return details


class NodeContext:
    def __init__(self, executor: "Executor", state: dict, record: dict, attempt: dict, observer: Observer, trace_id: str):
        self.executor, self._state, self._record, self._attempt, self._observer = executor, state, record, attempt, observer
        self.run_id, self.node_key, self.attempt_id = state["run_id"], record["key"], attempt["attempt_id"]
        self.trace_id = trace_id
        self.execution_config = copy.deepcopy(state["execution_config"])
        self.artifact_dir = executor.store.run_dir(self.run_id) / "work" / self.attempt_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._capture_failed = False
        self._active_unit: dict = {}

    def _persist(self) -> None:
        self.executor._save(self._state)

    def _metadata(self, **extra) -> dict:
        return {"node_key": self.node_key, "node_attempt_id": self.attempt_id, "trace_id": self.trace_id,
                "job_attempt_id": self._attempt.get("job_attempt_id"), **self._active_unit, **extra}

    def call_event(self, event: str, metadata: dict | None = None) -> None:
        """Record only allowlisted metadata. Full requests belong in capture()."""
        self._observer.event(event, {**(metadata or {}), **self._metadata()})

    def call_span(self, metadata: dict | None = None):
        """Provider adapters can wrap each actual request in a genuine child span."""
        return self._observer.span("provider.call", {**(metadata or {}), **self._metadata()})

    def capture(self, name: str, payload: dict, *, partial: bool = False) -> dict:
        """Controlled JSON capture. Failure is durable and blocks node success.

        An explicit disabled policy does not block execution. Calling capture
        never puts its payload, exception message, or filename into events.
        """
        _identifier(name, "Capture name")
        record = {"name": name, "time": utc_now(), "status": "disabled_by_policy", **self._metadata()}
        if self._state["capture_policy"]:
            try:
                reference = self.executor.store.put_json(self.run_id, "capture", redact_payload(payload))
                record.update(status="partial" if partial else "captured", ref=reference, hash=reference["sha256"])
            except Exception:
                record.update(status="write_failed", error_code="DIAGNOSTICS_CAPTURE_FAILED")
                self._capture_failed = True
        self._attempt.setdefault("captures", []).append(record)
        self._persist()
        self.call_event("diagnostics.partial" if record["status"] == "partial" else "diagnostics.captured" if record["status"] == "captured" else
                        "diagnostics.capture_failed" if record["status"] == "write_failed" else "diagnostics.disabled",
                        {"capture_status": record["status"]})
        return copy.deepcopy(record)

    def store_bytes(self, name: str, content: bytes, media_type: str = "application/octet-stream") -> dict:
        _identifier(name, "Artifact name")
        if not isinstance(content, bytes):
            raise RuntimeContractError("Binary artifacts must be bytes.")
        reference = self.executor.store.put_bytes(self.run_id, name, content, media_type)
        self.call_event("artifact.stored", {"artifact_id": reference["artifact_id"], "sha256": reference["sha256"],
                                           "size_bytes": reference["size_bytes"]})
        return reference

    def read_artifact(self, reference: dict) -> bytes:
        return self.executor.store.read_bytes(reference)

    def reserve_call(self, call_id: str, call_attempt: int, estimated_upper_usd: float,
                     max_run_usd: float, max_run_calls: int) -> None:
        """Durably reserve BEFORE an actual request; unknown cost stays reserved.

        This operates under execute's OS lock. It is an M1 local budget guard,
        not a multi-Worker quota, and never claims provider exactly-once calls.
        """
        _identifier(call_id, "Call identifier")
        try:
            estimate, limit = Decimal(str(estimated_upper_usd)), Decimal(str(max_run_usd))
            if not estimate.is_finite() or not limit.is_finite() or estimate < 0 or limit <= 0:
                raise ValueError
            if (not isinstance(call_attempt, int) or isinstance(call_attempt, bool) or call_attempt < 1 or
                    not isinstance(max_run_calls, int) or isinstance(max_run_calls, bool) or max_run_calls < 1):
                raise ValueError
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ProviderBudgetError("Invalid local provider budget.") from exc
        budget = self._state.setdefault("call_budget", {"max_run_usd": str(limit), "max_run_calls": max_run_calls,
                                                        "reserved_upper_usd": "0", "reservation_count": 0, "reservations": []})
        if Decimal(budget["max_run_usd"]) != limit or budget["max_run_calls"] != max_run_calls:
            raise ConfigurationChangedError("The frozen call budget cannot change within a run.")
        if any(item["call_id"] == call_id and item["call_attempt"] == call_attempt for item in budget["reservations"]):
            raise ProviderBudgetError("This actual call attempt was already reserved; reconcile before retrying.")
        reserved = Decimal(budget["reserved_upper_usd"])
        if budget["reservation_count"] + 1 > max_run_calls or reserved + estimate > limit:
            raise ProviderBudgetError("The local run provider budget is exhausted.")
        budget["reservation_count"] += 1
        budget["reserved_upper_usd"] = str(reserved + estimate)
        budget["reservations"].append({"call_id": call_id, "call_attempt": call_attempt,
                                       "estimated_upper_usd": str(estimate), "reserved_at": utc_now(),
                                       "node_key": self.node_key, "node_attempt_id": self.attempt_id,
                                       "settlement_status": "reserved_unknown_actual_cost"})
        self._persist()
        self.call_event("call.reserved", {"call_id": call_id, "call_attempt": call_attempt,
                                          "count": budget["reservation_count"]})

    def run_unit(self, unit_id: str, input_data: dict, fn: Callable[[], dict]) -> dict:
        _identifier(unit_id, "Unit identifier")
        if not isinstance(input_data, dict):
            raise RuntimeContractError("Unit input must be a JSON object.")
        input_data = redact_payload(input_data)
        input_hash = digest(input_data)
        units = self._record["units"]
        if unit_id not in units:
            units[unit_id] = {"unit_id": unit_id, "status": "pending", "input_hash": input_hash,
                              "input_ref": self.executor.store.put_json(self.run_id, "unit-input", input_data),
                              "version": self._record["version"], "attempts": []}
        record = units[unit_id]
        if record["input_hash"] != input_hash or record["version"] != self._record["version"]:
            raise ConfigurationChangedError("Changed unit input requires a child run.")
        if record["status"] == "succeeded":
            try:
                output = self.executor._checkpoint_data(record)
            except ArtifactIntegrityError as exc:
                raise CheckpointInvalidError(self.node_key) from exc
            reuse_metadata = self._metadata(unit_id=unit_id, unit_attempt_id=record["attempts"][-1]["attempt_id"],
                                            output_hash=record["output_hash"], reused_from_run_id=self.run_id)
            with self._observer.span("unit.reuse", reuse_metadata,
                                     links=record["attempts"][-1:]):
                self._observer.event("unit.reused", reuse_metadata)
            return output

        attempt = {"attempt_id": uuid.uuid4().hex, "node_attempt_id": self.attempt_id, "started_at": utc_now(),
                   "status": "running", "input_hash": input_hash}
        record["attempts"].append(attempt)
        record["status"] = "running"
        self._persist()
        began = time.monotonic()
        metadata = self._metadata(unit_id=unit_id, unit_attempt_id=attempt["attempt_id"], input_hash=input_hash)
        checkpointed = False
        previous_unit = self._active_unit
        try:
            with self._observer.span("unit." + self.node_key, metadata) as span:
                self._active_unit = {"unit_id": unit_id, "unit_attempt_id": attempt["attempt_id"]}
                self._observer.event("unit.started", metadata)
                attempt["trace_id"] = format(span.get_span_context().trace_id, "032x")
                attempt["span_id"] = format(span.get_span_context().span_id, "016x")
                self._persist()
                output = fn()
                if not isinstance(output, dict):
                    raise RuntimeContractError("Unit output must be a JSON object.")
                if self._capture_failed:
                    raise CaptureFailedError("Required unit capture did not persist.")
                output = redact_payload(output)
                self.executor.store.validate_embedded_artifacts(output)
                reference = self.executor._write_checkpoint(self._state, self._record, attempt, input_hash, output, unit_id)
                span.set_attribute("output_hash", digest(output))
                span.set_attribute("validation_status", "passed")
            self._observer.check()
            record.update(status="succeeded", output_ref=reference, output_hash=digest(output))
            attempt.update(status="succeeded", ended_at=utc_now(), output_hash=record["output_hash"],
                           duration_ms=round((time.monotonic() - began) * 1000, 3))
            self._persist()
            checkpointed = True
            self._observer.event("unit.checkpointed", {**metadata, "span_id": attempt["span_id"],
                                  "output_hash": record["output_hash"], "duration_ms": attempt["duration_ms"]})
            return output
        except Exception as exc:
            try:
                self.capture("unit-error", _error_diagnostic(exc))
            except Exception:
                self._capture_failed = True
                self._state["observability"]["capture_status"] = "write_failed"
            if not checkpointed:
                attempt.update(status="failed", ended_at=utc_now(), duration_ms=round((time.monotonic()-began)*1000, 3), **_safe_error(exc))
                record["status"] = "failed"
                self._persist()
            self._observer.event("unit.failed", {**metadata, "span_id": attempt.get("span_id"),
                                  "duration_ms": attempt.get("duration_ms"), **_safe_error(exc)})
            raise
        finally:
            self._active_unit = previous_unit


class Executor:
    def __init__(self, root: Path, nodes: list[NodeSpec], execution_config: dict):
        if not nodes or len({node.key for node in nodes}) != len(nodes):
            raise RuntimeContractError("Provide at least one node with unique keys.")
        for node in nodes:
            _identifier(node.key, "Node key")
            _identifier(node.version, "Node version")
            if not callable(node.handler):
                raise RuntimeContractError("A node must have a handler.")
        if not isinstance(execution_config, dict):
            raise RuntimeContractError("Execution configuration must be a JSON object.")
        self.store = LocalStore(root)
        self.nodes = list(nodes)
        self.execution_config = redact_payload(execution_config)
        digest(self.execution_config)

    def _save(self, state: dict) -> None:
        state["revision"] += 1
        state["updated_at"] = utc_now()
        self.store.write_state(state)

    def _manifest(self) -> list[dict]:
        return [{"key": node.key, "version": node.version,
                 "config_keys": list(node.config_keys) if node.config_keys is not None else None} for node in self.nodes]

    def create_run(self, initial_input: dict) -> str:
        if not isinstance(initial_input, dict):
            raise RuntimeContractError("Initial input must be a JSON object.")
        run_id = uuid.uuid4().hex
        self.store.run_dir(run_id).mkdir(parents=True, exist_ok=False)
        initial_input = redact_payload(initial_input)
        reference = self.store.put_json(run_id, "initial-input", initial_input)
        state = {"schema_version": "m1.local.v1", "storage_mode": "single_process_local_m1", "run_id": run_id,
                 "parent_run_id": None, "fork_from_node": None, "created_at": utc_now(), "updated_at": utc_now(),
                 "revision": 0, "status": "queued", "cursor": 0, "initial_input_ref": reference,
                 "initial_input_hash": digest(initial_input), "execution_config": copy.deepcopy(self.execution_config),
                 "config_hash": digest(self.execution_config), "node_manifest": self._manifest(),
                 "capture_policy": bool(self.execution_config.get("debug_capture", True)), "execution_attempts": [],
                 "observability": {"trace_status": "not_started", "capture_status": "not_started" if self.execution_config.get("debug_capture", True) else "disabled_by_policy"},
                 "nodes": [{"key": node.key, "version": node.version, "status": "pending", "attempts": [], "units": {}}
                           for node in self.nodes]}
        self.store.write_state(state)
        return run_id

    def inspect(self, run_id: str) -> dict:
        state = self.store.read_state(run_id)
        if state["status"] == "running":
            # A read cannot know whether the owner is still alive. An open
            # execution has no complete trace claim, including after hard kill.
            state["observability"]["trace_status"] = "partial"
        return state

    def _check_config(self, state: dict) -> None:
        if state["config_hash"] != digest(self.execution_config) or state["node_manifest"] != self._manifest():
            raise ConfigurationChangedError("Frozen configuration or node versions changed; create a child run.")

    def _checkpoint_data(self, record: dict) -> dict:
        envelope = self.store.read_json(record["output_ref"])
        if (not isinstance(envelope, dict) or envelope.get("schema_version") != "m1.checkpoint.v1" or
                envelope.get("producer_version") != record["version"] or
                envelope.get("input_hash") != record["input_hash"] or
                not isinstance(envelope.get("data"), dict) or digest(envelope["data"]) != record["output_hash"]):
            raise ArtifactIntegrityError("Checkpoint metadata no longer matches its immutable output.")
        self.store.validate_embedded_artifacts(envelope["data"])
        return envelope["data"]

    def _write_checkpoint(self, state: dict, record: dict, attempt: dict, input_hash: str, output: dict, unit_id=None) -> dict:
        return self.store.put_json(state["run_id"], "checkpoint", {
            "schema_version": "m1.checkpoint.v1", "producer_version": record["version"], "input_hash": input_hash,
            "output_hash": digest(output), "run_id": state["run_id"], "node_key": record["key"],
            "node_attempt_id": attempt.get("node_attempt_id", attempt["attempt_id"]), "unit_id": unit_id,
            "source_refs": [], "data": output})

    def _validate(self, state: dict) -> None:
        try:
            initial = self.store.read_json(state["initial_input_ref"])
            if digest(initial) != state["initial_input_hash"]:
                raise ArtifactIntegrityError("Frozen input changed.")
        except ArtifactIntegrityError as exc:
            raise CheckpointInvalidError(self.nodes[0].key) from exc
        previous_hash = state["initial_input_hash"]
        seen_pending = False
        for record in state["nodes"]:
            try:
                if record["status"] == "succeeded":
                    if seen_pending or record["input_hash"] != previous_hash:
                        raise ArtifactIntegrityError("Checkpoint prefix is invalid.")
                    self._checkpoint_data(record)
                    previous_hash = record["output_hash"]
                else:
                    seen_pending = True
                for unit in record["units"].values():
                    if digest(self.store.read_json(unit["input_ref"])) != unit["input_hash"]:
                        raise ArtifactIntegrityError("Frozen unit input changed.")
                    if unit["status"] == "succeeded":
                        self._checkpoint_data(unit)
            except (ArtifactIntegrityError, KeyError, TypeError) as exc:
                raise CheckpointInvalidError(record["key"]) from exc
        expected_cursor = next((i for i, item in enumerate(state["nodes"]) if item["status"] != "succeeded"), len(self.nodes))
        if state["cursor"] != expected_cursor:
            raise CheckpointInvalidError(state["nodes"][min(expected_cursor, len(self.nodes) - 1)]["key"])

    def output(self, run_id: str, node_key: str | None = None) -> dict:
        state = self.store.read_state(run_id)
        self._validate(state)
        record = next((item for item in reversed(state["nodes"]) if item["key"] == node_key), None) if node_key else state["nodes"][-1]
        if record is None or record["status"] != "succeeded":
            raise RuntimeContractError("Requested node has no successful output.")
        return self._checkpoint_data(record)

    def execute(self, run_id: str, stop_after: str | None = None) -> dict:
        if stop_after is not None and stop_after not in {node.key for node in self.nodes}:
            raise RuntimeContractError("Unknown stop node.")
        with process_lock(self.store.run_dir(run_id) / ".lock"):
            state = self.store.read_state(run_id)
            self._check_config(state)
            try:
                self._validate(state)
            except CheckpointInvalidError as exc:
                state.update(status="failed", error_code=exc.code, checkpoint_invalid_at=exc.node_key)
                self._save(state)
                raise
            if state["status"] == "succeeded":
                return state
            if state["cursor"] == len(self.nodes):
                # Reconcile a durable final checkpoint even when the process or
                # diagnostic sink failed before its caller saw completion.
                state.update(status="succeeded", error_code=None)
                self._save(state)
                return state
            if stop_after is not None and self.nodes.index(next(node for node in self.nodes if node.key == stop_after)) < state["cursor"]:
                raise RuntimeContractError("Successful nodes cannot be rerun in place; create a child run.")

            observer = Observer(self.store.run_dir(run_id), run_id)
            job_attempt = {"attempt_id": uuid.uuid4().hex, "status": "running", "started_at": utc_now(), "trace_status": "partial"}
            previous_links = [{"trace_id": item.get("trace_id"), "span_id": item.get("span_id")} for item in state["execution_attempts"][-1:]]
            previous_links.extend(item["reused_trace"] for item in state["nodes"] if item.get("reused_trace"))
            for item in state["execution_attempts"]:
                if item["status"] == "running":
                    item.update(status="interrupted", ended_at=utc_now(), error_code="PROCESS_INTERRUPTED", trace_status="partial")
            for node in state["nodes"]:
                for item in node["attempts"]:
                    if item["status"] == "running":
                        item.update(status="interrupted", ended_at=utc_now(), error_code="PROCESS_INTERRUPTED")
                for unit in node["units"].values():
                    for item in unit["attempts"]:
                        if item["status"] == "running":
                            item.update(status="interrupted", ended_at=utc_now(), error_code="PROCESS_INTERRUPTED")
            state["execution_attempts"].append(job_attempt)
            state.update(status="running", error_code=None)
            state["observability"]["trace_status"] = "partial"
            self._save(state)
            current_attempt = None
            current_record = None
            context = None
            try:
                with observer.span("job.execute", {"run_id": run_id, "job_attempt_id": job_attempt["attempt_id"]}, previous_links) as job_span:
                    job_attempt.update(trace_id=format(job_span.get_span_context().trace_id, "032x"),
                                       span_id=format(job_span.get_span_context().span_id, "016x"))
                    self._save(state)
                    observer.event("job.claimed", {"job_attempt_id": job_attempt["attempt_id"], "trace_id": job_attempt["trace_id"]})
                    for reused in state["nodes"][:state["cursor"]]:
                        observer.event("node.reused", {"node_key": reused["key"], "node_version": reused["version"],
                                       "input_hash": reused["input_hash"], "output_hash": reused["output_hash"],
                                       "artifact_id": reused["output_ref"]["artifact_id"],
                                       "reused_from_run_id": reused.get("reused_from_run_id", run_id)})
                    for index in range(state["cursor"], len(self.nodes)):
                        spec, record = self.nodes[index], state["nodes"][index]
                        current_record = record
                        context = None
                        input_data = (self.store.read_json(state["initial_input_ref"]) if index == 0 else
                                      self._checkpoint_data(state["nodes"][index - 1]))
                        input_hash = digest(input_data)
                        attempt = {"attempt_id": uuid.uuid4().hex, "job_attempt_id": job_attempt["attempt_id"],
                                   "status": "running", "started_at": utc_now(), "input_hash": input_hash,
                                   "captures": []}
                        current_attempt = attempt
                        record.update(status="running", input_hash=input_hash)
                        record["attempts"].append(attempt)
                        self._save(state)
                        metadata = {"node_key": spec.key, "node_attempt_id": attempt["attempt_id"], "node_version": spec.version,
                                    "input_hash": input_hash, "trace_id": job_attempt["trace_id"]}
                        began = time.monotonic()
                        with observer.span("node." + spec.key, metadata) as node_span:
                            observer.event("node.started", metadata)
                            attempt.update(trace_id=format(node_span.get_span_context().trace_id, "032x"),
                                           span_id=format(node_span.get_span_context().span_id, "016x"))
                            self._save(state)
                            context = NodeContext(self, state, record, attempt, observer, attempt["trace_id"])
                            context.capture("node-input", input_data)
                            if context._capture_failed:
                                raise CaptureFailedError("Required input capture did not persist.")
                            result = spec.handler(context, copy.deepcopy(input_data))
                            if not isinstance(result, dict):
                                raise RuntimeContractError("Node output must be a JSON object.")
                            result = redact_payload(result)
                            self.store.validate_embedded_artifacts(result)
                            context.capture("node-output", result)
                            if context._capture_failed:
                                raise CaptureFailedError("Required debug capture did not persist.")
                            reference = self._write_checkpoint(state, record, attempt, input_hash, result)
                            node_span.set_attribute("output_hash", digest(result))
                            node_span.set_attribute("validation_status", "passed")
                        observer.check()
                        attempt.update(status="succeeded", ended_at=utc_now(), output_hash=digest(result),
                                       duration_ms=round((time.monotonic() - began) * 1000, 3))
                        record.update(status="succeeded", output_ref=reference, output_hash=digest(result))
                        state["cursor"] = index + 1
                        # Completion and next cursor are committed together. A
                        # final step is succeeded, never paused_debug.
                        state["status"] = "succeeded" if index == len(self.nodes) - 1 else "paused_debug" if spec.key == stop_after else "running"
                        self._save(state)
                        current_attempt = None
                        current_record = None
                        observer.event("node.checkpointed", {**metadata, "span_id": attempt["span_id"],
                                       "output_hash": digest(result), "duration_ms": attempt["duration_ms"]})
                        if state["status"] == "paused_debug":
                            observer.event("node.paused", {**metadata, "span_id": attempt["span_id"], "status": "paused_debug"})
                        if state["status"] == "succeeded":
                            observer.event("artifact.published", {**metadata, "span_id": attempt["span_id"],
                                           "artifact_id": reference["artifact_id"], "output_hash": digest(result)})
                        if spec.key == stop_after:
                            break
                    job_attempt.update(status="succeeded" if state["status"] == "succeeded" else "paused_debug", ended_at=utc_now())
                observer.check()
                job_attempt["trace_status"] = "captured"
            except Exception as exc:
                error = _safe_error(exc)
                if context is not None:
                    try:
                        context.capture("node-error", _error_diagnostic(exc))
                    except Exception:
                        state["observability"]["capture_status"] = "write_failed"
                if isinstance(exc, ObservabilityWriteError) and not observer.export_error:
                    state["observability"]["event_status"] = "write_failed"
                state.update(status="failed", **error)
                job_attempt.update(status="failed", ended_at=utc_now(), **error)
                if current_attempt is not None:
                    current_attempt.update(status="failed", ended_at=utc_now(), duration_ms=round((time.monotonic()-began)*1000, 3), **error)
                    current_record["status"] = "failed"
                job_attempt["trace_status"] = observer.trace_status
                try:
                    observer.event("node.failed" if current_record else "job.failed", {"node_key": current_record["key"] if current_record else None,
                        "node_attempt_id": current_attempt.get("attempt_id") if current_attempt else None,
                        "span_id": current_attempt.get("span_id") if current_attempt else job_attempt.get("span_id"),
                        "job_attempt_id": job_attempt["attempt_id"],
                        "trace_id": job_attempt.get("trace_id"), **error})
                except ObservabilityWriteError:
                    state["observability"]["event_status"] = "write_failed"
            finally:
                observer.close()
                if observer.export_error:
                    job_attempt["trace_status"] = "write_failed"
                state["observability"]["current_trace_status"] = job_attempt["trace_status"]
                previous_trace_states = [item.get("trace_status", "partial") for item in state["execution_attempts"]]
                state["observability"]["trace_status"] = ("write_failed" if "write_failed" in previous_trace_states else
                                                           "partial" if "partial" in previous_trace_states else "captured")
                captures = [capture["status"] for record in state["nodes"] for attempt in record["attempts"] for capture in attempt.get("captures", [])]
                state["observability"]["capture_status"] = ("partial" if "partial" in captures or ("write_failed" in captures and "captured" in captures) else
                                                             "write_failed" if "write_failed" in captures else
                                                             "captured" if "captured" in captures else "disabled_by_policy")
                self._save(state)
            return copy.deepcopy(state)

    def resume(self, run_id: str, stop_after: str | None = None) -> dict:
        return self.execute(run_id, stop_after)

    def fork(self, run_id: str, from_node: str, initial_input: dict | None = None,
             execution_config: dict | None = None) -> str:
        return self._fork(run_id, from_node, initial_input, execution_config, repair=False)

    def repair(self, run_id: str) -> str:
        """Keep the damaged parent; recompute from the earliest bad checkpoint."""
        state = self.store.read_state(run_id)
        try:
            self._validate(state)
        except CheckpointInvalidError as exc:
            return self._fork(run_id, exc.node_key, None, None, repair=True)
        raise RuntimeContractError("This run has no damaged checkpoint to repair.")

    def _fork(self, run_id: str, from_node: str, initial_input: dict | None,
              execution_config: dict | None, repair: bool) -> str:
        keys = [node.key for node in self.nodes]
        if from_node not in keys:
            raise RuntimeContractError("Unknown fork node.")
        start = keys.index(from_node)
        with process_lock(self.store.run_dir(run_id) / ".lock"):
            parent = self.store.read_state(run_id)
            if initial_input is not None and start != 0:
                raise ConfigurationChangedError("Changing frozen input requires a fork from the first node.")
            # A repair may ignore corruption at/after its starting point, but
            # every reused prefix checkpoint is independently verified.
            for index, record in enumerate(parent["nodes"][:start]):
                if record["status"] != "succeeded" or record["key"] != keys[index] or record["version"] != self.nodes[index].version:
                    raise ConfigurationChangedError("The requested prefix cannot be reused with these node versions.")
                try:
                    self._checkpoint_data(record)
                except ArtifactIntegrityError as exc:
                    raise CheckpointInvalidError(record["key"]) from exc
                new_config = execution_config if execution_config is not None else self.execution_config
                old_config = parent["execution_config"]
                # Capture policy changes affect diagnostics, not node output.
                config_keys = self.nodes[index].config_keys
                old_manifest = parent["node_manifest"][index]
                old_keys = old_manifest.get("config_keys")
                if old_manifest.get("key") != record["key"] or old_keys != (list(config_keys) if config_keys is not None else None):
                    raise ConfigurationChangedError("The selected prefix has changed configuration dependencies; fork earlier.")
                consumed_keys = ((set(old_config) | set(new_config)) - {"debug_capture"}
                                 if config_keys is None or old_keys is None else set(config_keys) | set(old_keys))
                if any(old_config.get(key) != redact_payload(new_config).get(key) for key in consumed_keys):
                    raise ConfigurationChangedError("The selected prefix consumes changed configuration; fork earlier.")
            if not repair:
                self._validate(parent)
            initial = initial_input if initial_input is not None else self.store.read_json(parent["initial_input_ref"])
            config = execution_config if execution_config is not None else self.execution_config
            child_executor = Executor(self.store.root, self.nodes, config)
            child_id = child_executor.create_run(initial)
            child = self.store.read_state(child_id)
            child.update(parent_run_id=run_id, fork_from_node=from_node, fork_reason="checkpoint_repair" if repair else "rerun",
                         cursor=start)
            for index in range(start):
                original = parent["nodes"][index]
                reused = copy.deepcopy(original)
                reused.update(attempts=[], reused_from_run_id=run_id, reused_from_artifact_id=original["output_ref"]["artifact_id"])
                origin_attempt = original["attempts"][-1] if original["attempts"] else original.get("reused_trace", {})
                reused["reused_trace"] = {key: origin_attempt.get(key) for key in ("trace_id", "span_id")}
                child["nodes"][index] = reused
            # The caller chooses the earliest affected node for config changes.
            # The exact config diff is retained for review, never hidden.
            child["configuration_changes"] = sorted(key for key in set(parent["execution_config"]) | set(child["execution_config"])
                                                     if parent["execution_config"].get(key) != child["execution_config"].get(key))
            child_executor._save(child)
            return child_id

    def diff(self, old_run: str, new_run: str) -> dict:
        """Controlled comparison: references and counts, never copied source text.

        Call totals describe recorded attempts, not a provider billing ledger.
        The CLI saves this result under its controlled comparisons directory.
        """
        import json
        import math

        old, new = self.inspect(old_run), self.inspect(new_run)

        def content_summary(data: dict) -> dict:
            content = data.get("package", data.get("prepared", data))
            if not isinstance(content, dict):
                content = {}
            validation = data.get("validation", {})
            if not isinstance(validation, dict):
                validation = {}
            result = {}
            for plural, singular in (("requirements", "requirement"), ("sections", "section"), ("issues", "issue")):
                items = content.get(plural)
                result[singular + "_count"] = len(items) if isinstance(items, list) else validation.get(singular + "_count")
            issues = content.get("issues")
            result["issue_ids"] = ([item["issue_id"] for item in issues if isinstance(item, dict) and isinstance(item.get("issue_id"), str)]
                                   if isinstance(issues, list) else data.get("issue_ids"))
            return result

        def node_details(state: dict, index: int) -> dict:
            node = state["nodes"][index]
            previous = state["nodes"][index - 1] if index else None
            input_ref = previous.get("output_ref") if previous else state["initial_input_ref"]
            input_hash = previous.get("output_hash") if previous else state["initial_input_hash"]
            output = {"ref": node.get("output_ref"), "hash": node.get("output_hash"),
                      "validation": "unavailable", "summary": None}
            if node.get("output_ref"):
                try:
                    data = self._checkpoint_data(node)
                    output.update(validation="valid", summary=content_summary(data))
                except (ArtifactIntegrityError, KeyError, TypeError):
                    output["validation"] = "checkpoint_invalid"
            return {"status": node["status"], "version": node["version"], "attempt_count": len(node["attempts"]),
                    "input": {"ref": input_ref, "hash": node.get("input_hash", input_hash)}, "output": output,
                    "reused_from_run_id": node.get("reused_from_run_id")}

        def calls(state: dict) -> dict:
            path = self.store.run_dir(state["run_id"]) / "events.jsonl"
            result = {"status": "available", "started_events": 0, "completed_events": 0, "failed_events": 0,
                      "unfinished_started_attempts": 0, "known_usage_attempts": 0, "unknown_usage_attempts": 0,
                      "known_token_counts": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
                      "estimated_cost_usd": None, "cost_basis": "estimate_from_frozen_rates_not_invoice",
                      "billing_verified": False, "invalid_event_records": 0}
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                result["status"] = "unavailable" if state["execution_attempts"] else "not_started"
                if state["execution_attempts"]:
                    for key in ("started_events", "completed_events", "failed_events", "unfinished_started_attempts",
                                "known_usage_attempts", "unknown_usage_attempts"):
                        result[key] = None
                return result
            terminal, started = [], set()
            for line in lines:
                try:
                    event = json.loads(line)
                    name = event["event"]
                    identity = (event.get("call_id"), event.get("call_attempt"))
                    if name == "call.started":
                        result["started_events"] += 1
                        started.add(identity)
                    elif name in ("call.completed", "call.failed"):
                        result["completed_events" if name == "call.completed" else "failed_events"] += 1
                        terminal.append(event)
                except (ValueError, TypeError, KeyError):
                    result["invalid_event_records"] += 1
            finished = {(event.get("call_id"), event.get("call_attempt")) for event in terminal}
            result["unfinished_started_attempts"] = len(started - finished)
            costs = []
            for event in terminal:
                known = True
                for key in result["known_token_counts"]:
                    value = event.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        result["known_token_counts"][key] = (result["known_token_counts"][key] or 0) + value
                    else:
                        known = False
                result["known_usage_attempts" if known else "unknown_usage_attempts"] += 1
                cost = event.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
                    costs.append(cost)
            result["unknown_usage_attempts"] += result["unfinished_started_attempts"]
            result["estimated_cost_usd"] = round(sum(costs), 12) if costs else None
            if (result["invalid_event_records"] or state["observability"].get("event_status") == "write_failed" or
                    result["unfinished_started_attempts"]):
                result["status"] = "partial"
            return result

        changes = sorted(key for key in set(old["execution_config"]) | set(new["execution_config"])
                         if key not in old["execution_config"] or key not in new["execution_config"] or
                         old["execution_config"][key] != new["execution_config"][key])
        old_nodes = {item["key"]: index for index, item in enumerate(old["nodes"])}
        new_nodes = {item["key"]: index for index, item in enumerate(new["nodes"])}
        comparisons = []
        for key in dict.fromkeys([*old_nodes, *new_nodes]):
            before = node_details(old, old_nodes[key]) if key in old_nodes else None
            after = node_details(new, new_nodes[key]) if key in new_nodes else None
            comparisons.append({"key": key, "old_status": before["status"] if before else None,
                                "new_status": after["status"] if after else None,
                                "input_changed": (before["input"]["hash"] if before else None) != (after["input"]["hash"] if after else None),
                                "output_changed": (before["output"]["hash"] if before else None) != (after["output"]["hash"] if after else None),
                                "reused": bool(after and after["reused_from_run_id"]), "attempt_count": after["attempt_count"] if after else 0,
                                "before": before, "after": after})
        return {"schema_version": "m1.diff.v1", "content_scope": "controlled_diagnostics",
                "old_run_id": old_run, "new_run_id": new_run, "parent_run_id": new["parent_run_id"],
                "initial_input_changed": old["initial_input_hash"] != new["initial_input_hash"],
                "initial_input": {"before": {"ref": old["initial_input_ref"], "hash": old["initial_input_hash"]},
                                  "after": {"ref": new["initial_input_ref"], "hash": new["initial_input_hash"]}},
                "configuration_changes": changes,
                "configuration": {"before": copy.deepcopy(old["execution_config"]), "after": copy.deepcopy(new["execution_config"])},
                "calls": {"before": calls(old), "after": calls(new)}, "nodes": comparisons}
