"""Read-only evidence index: expose missing records instead of claiming health."""
from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .storage import LocalStore, StorageError, digest
from .runtime import ProviderBudgetLedgerError, call_budget_summary


def inspect_diagnostics(root: Path, run_id: str) -> dict:
    if not Path(root).is_dir():
        raise StorageError("Diagnostic root does not exist.")
    store = LocalStore(root)
    state = store.read_state(run_id)
    directory = store.run_dir(run_id)
    gaps, integrity, json_cache, content_limitations = [], {}, {}, []
    artifact_identities = {}

    def gap(code, **metadata):
        gaps.append({"code": code, **metadata})

    def rows(name):
        path = directory / name
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if state["execution_attempts"]:
                gap("DIAGNOSTIC_FILE_MISSING", file=name)
            return []
        except OSError as error:
            gap("DIAGNOSTIC_FILE_UNREADABLE", file=name, error_type=type(error).__name__)
            return []
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            gap("DIAGNOSTIC_ENCODING_INVALID", file=name)
            text = raw.decode("utf-8", errors="replace")
        result = []
        for number, line in enumerate(text.splitlines(), 1):
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError
                if name == "events.jsonl":
                    if (not isinstance(item.get("event"), str)
                            or not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", item["event"])
                            or item.get("run_id") != run_id or not isinstance(item.get("time"), str)):
                        raise ValueError
                elif (item.get("schema_version") != "m1.otel.span.v1"
                      or not isinstance(item.get("name"), str)
                      or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("trace_id", "")))
                      or not re.fullmatch(r"[0-9a-f]{16}", str(item.get("span_id", "")))):
                    raise ValueError
                result.append(item)
            except (ValueError, TypeError):
                gap("DIAGNOSTIC_ROW_INVALID", file=name, line=number)
        return result

    events, spans = rows("events.jsonl"), rows("spans.jsonl")
    span_ids = {(x.get("trace_id"), x.get("span_id")) for x in spans}
    spans_by_id = {(x.get("trace_id"), x.get("span_id")): x for x in spans}

    def ref(reference):
        if not reference:
            return None
        if (not isinstance(reference, dict) or not isinstance(reference.get("artifact_id"), str)
                or not isinstance(reference.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))
                or isinstance(reference.get("size_bytes"), bool) or not isinstance(reference.get("size_bytes"), int)
                or reference["size_bytes"] < 0):
            gap("ARTIFACT_REFERENCE_INVALID")
            return {"integrity": "invalid"}
        identity = (reference["path"], reference["sha256"], reference["size_bytes"])
        artifact_id = reference["artifact_id"]
        if artifact_id in artifact_identities and artifact_identities[artifact_id] != identity:
            gap("ARTIFACT_REFERENCE_CONFLICT", artifact_id=artifact_id)
        artifact_identities[artifact_id] = identity
        key = (artifact_id, *identity)
        if key not in integrity:
            try:
                store.read_bytes(reference)
                status = "valid"
            except Exception:
                status = "invalid"
                gap("ARTIFACT_INTEGRITY_FAILED", artifact_id=artifact_id)
            integrity[key] = {**{name: reference[name] for name in
                               ("artifact_id", "path", "sha256", "size_bytes", "media_type") if name in reference},
                              "integrity": status}
        return integrity[key]

    def artifact_json(reference, *, node_key=None, unit_id=None):
        checked = ref(reference)
        if checked is None or checked["integrity"] != "valid":
            return None
        key = (reference["path"], reference["sha256"], reference["size_bytes"])
        if key not in json_cache:
            try:
                value = store.read_json(reference)
                # json.loads also accepts NaN/Infinity; immutable runtime JSON
                # does not. Treat noncanonical/corrupt JSON as evidence gaps.
                digest(value)
                json_cache[key] = value
            except Exception:
                gap("ARTIFACT_JSON_INVALID", artifact_id=reference["artifact_id"], node_key=node_key, unit_id=unit_id)
                json_cache[key] = None
        return json_cache[key]

    def embedded_artifacts(value):
        if isinstance(value, dict):
            if {"artifact_id", "path", "sha256", "size_bytes"}.issubset(value):
                ref(value)
            for child in value.values():
                embedded_artifacts(child)
        elif isinstance(value, list):
            for child in value:
                embedded_artifacts(child)

    def checkpoint(record, node_key, unit_id=None):
        if record.get("status") != "succeeded":
            return
        if not record.get("output_ref"):
            gap("CHECKPOINT_OUTPUT_MISSING", node_key=node_key, unit_id=unit_id)
            return
        envelope = artifact_json(record["output_ref"], node_key=node_key, unit_id=unit_id)
        if envelope is None:
            return
        mismatches = []
        if not isinstance(envelope, dict):
            mismatches.append("envelope")
        else:
            for key, expected in (("schema_version", "m1.checkpoint.v1"), ("producer_version", record.get("version")),
                                  ("input_hash", record.get("input_hash")), ("output_hash", record.get("output_hash")),
                                  ("node_key", node_key), ("unit_id", unit_id)):
                if envelope.get(key) != expected:
                    mismatches.append(key)
            data = envelope.get("data")
            if not isinstance(data, dict) or digest(data) != record.get("output_hash"):
                mismatches.append("data_hash")
            if isinstance(data, dict):
                embedded_artifacts(data)
        if mismatches:
            gap("CHECKPOINT_METADATA_MISMATCH", node_key=node_key, unit_id=unit_id, fields=mismatches)
        else:
            return envelope

    if not state.get("initial_input_ref"):
        gap("INITIAL_INPUT_MISSING")
    initial = artifact_json(state.get("initial_input_ref"))
    if initial is not None and digest(initial) != state.get("initial_input_hash"):
        gap("INITIAL_INPUT_HASH_MISMATCH")

    def reading_limitation(value, attempt_id, artifact_id, inherited_from=None):
        """Recognize a complete ledger of partial source reading, including old captures.

        Do not reinterpret arbitrary partial captures, bad JSON or a missing
        ledger as available evidence. Content is not declared semantically read.
        """
        def invalid():
            gap("READING_LEDGER_INVALID", attempt_id=attempt_id)
            return False
        if not isinstance(value, dict) or value.get("schema_version") != "1.0":
            return invalid()
        coverage, documents = value.get("coverage"), value.get("documents")
        if not isinstance(coverage, dict) or not isinstance(documents, list) or not documents:
            return invalid()
        try:
            units = [unit for doc in documents for unit in doc["units"]]
            counts = Counter(unit["coverage"]["status"] for unit in units)
            unresolved = coverage["unresolved_units"]
            roles = coverage["required_roles"]
            if (not isinstance(roles, list) or "tender" not in roles or
                    any(role not in {"tender", "company_material", "reference_bid", "project_supplement"} for role in roles)):
                return invalid()
            expected_unresolved = [{"source_id": doc["source_id"], "unit_id": unit["unit_id"], "locator": unit["locator"],
                "page": unit.get("page"), "status": unit["coverage"]["status"], "error_code": unit["coverage"].get("error_code")}
                for doc in documents for unit in doc["units"] if unit["coverage"]["status"] not in {"read", "empty"}]
            expected_required = [entry for entry in expected_unresolved if next(
                doc["role"] for doc in documents if doc["source_id"] == entry["source_id"]) in roles]
            scope = coverage.get("diagnostic_pdf_range")
            frozen_scope = state.get("execution_config", {}).get("reading", {}).get("diagnostic_pdf_range")
            if scope is not None or frozen_scope is not None:
                if not isinstance(scope, dict) or not isinstance(frozen_scope, dict) or len(documents) != 1:
                    return invalid()
                doc = documents[0]
                first, last, total = (scope[key] for key in ("first_page", "last_page", "source_page_count"))
                if (state.get("artifact_scope") != "diagnostic" or
                        state["execution_config"].get("run_purpose") != "diagnostic_replay" or
                        coverage.get("publication_status") != "diagnostic_only" or
                        coverage.get("coverage_basis") != "diagnostic_original_pdf_range" or
                        any(type(value) is not int for value in (first, last, total)) or
                        not 1 <= first <= last <= total or doc.get("diagnostic_pdf_range") != scope or
                        doc["role"] != "tender" or doc.get("format") != "pdf" or
                        (doc["source_id"], doc["sha256"]) != (scope["source_id"], scope["source_sha256"]) or
                        scope != {**frozen_scope, "source_page_count": total,
                                  "outside_scope_page_count": total - (last - first + 1)} or
                        len(units) != last - first + 1 or
                        any((unit["page"], unit["locator"]) != (i, f"pdf/page[{i}]")
                            for i, unit in enumerate(units, first))):
                    return invalid()
            if (type(coverage["complete"]) is not bool or coverage["complete"] != (not expected_unresolved) or
                    type(coverage["required_complete"]) is not bool or not isinstance(unresolved, list) or
                    not units or any(type(doc.get("expected_units")) is not int or
                        doc["expected_units"] < 1 or len(doc["units"]) != doc["expected_units"] for doc in documents) or
                    len(documents) != coverage["document_count"] or len(units) != coverage["unit_count"] or
                    len({doc["source_id"] for doc in documents}) != len(documents) or
                    len({unit["unit_id"] for unit in units}) != len(units) or
                    any(unit["source_id"] != doc["source_id"] for doc in documents for unit in doc["units"]) or
                    set(counts) - {"read", "empty", "needs_ocr", "failed", "unsupported"} or
                    coverage["counts"] != {key: counts[key] for key in ("read", "empty", "needs_ocr", "failed", "unsupported")} or
                    unresolved != expected_unresolved or coverage["required_unresolved_units"] != expected_required or
                    coverage["required_complete"] != (not expected_required) or
                    coverage["status"] != ("failed" if expected_required else "partial" if expected_unresolved else "complete")):
                return invalid()
        except (KeyError, TypeError):
            return invalid()
        if scope and scope["outside_scope_page_count"]:
            content_limitations.append({"code": "DIAGNOSTIC_ORIGINAL_PDF_RANGE", "attempt_id": attempt_id,
                "artifact_id": artifact_id, **({"reused_from_run_id": inherited_from} if inherited_from else {}), **scope})
        if not unresolved:
            return False
        content_limitations.append({"code": "SOURCE_READING_PARTIAL", "attempt_id": attempt_id,
            "artifact_id": artifact_id, "coverage_status": coverage["status"],
            "required_complete": coverage["required_complete"], "unit_count": coverage["unit_count"],
            "unresolved_unit_count": len(unresolved), "diagnostic_ledger_status": "captured"})
        return True

    def attempt(item, *, inherited=False):
        result = {key: item.get(key) for key in ("attempt_id", "node_attempt_id", "job_attempt_id", "started_at", "ended_at",
                  "status", "duration_ms", "input_hash", "output_hash", "trace_id", "span_id", "error_code", "error_type")}
        pair = (item.get("trace_id"), item.get("span_id"))
        result["trace_evidence"] = "inherited" if inherited else "present" if pair in span_ids else "missing"
        if not inherited and pair not in span_ids:
            gaps.append({"code": "ATTEMPT_TRACE_MISSING", "attempt_id": item["attempt_id"]})
        result["captures"] = [{**{key: c.get(key) for key in ("name", "status", "unit_id", "unit_attempt_id", "error_code")},
                               "artifact": ref(c.get("ref"))} for c in item.get("captures", [])]
        for capture in item.get("captures", []):
            source_limitation = False
            if capture.get("name") == "reading-coverage" and capture.get("status") in {"captured", "partial"}:
                source_limitation = reading_limitation(artifact_json(capture.get("ref")), item["attempt_id"],
                                                       (capture.get("ref") or {}).get("artifact_id"))
            if capture["status"] == "write_failed" or capture["status"] == "partial" and not source_limitation:
                gaps.append({"code": "CAPTURE_INCOMPLETE", "attempt_id": item["attempt_id"], "name": capture["name"], "status": capture["status"]})
        return result

    nodes = []
    previous = ref(state["initial_input_ref"])
    previous_hash = state.get("initial_input_hash")
    seen_pending = False
    for node in state["nodes"]:
        inherited = bool(node.get("reused_from_run_id"))
        output = ref(node.get("output_ref"))
        if node["status"] == "succeeded":
            if seen_pending or node.get("input_hash") != previous_hash:
                gap("CHECKPOINT_CHAIN_MISMATCH", node_key=node["key"])
            envelope = checkpoint(node, node["key"])
            if (inherited and node["key"] == "N1" and envelope and
                    state["execution_config"].get("reading", {}).get("diagnostic_pdf_range")):
                reading_limitation(envelope["data"].get("parsed"), envelope.get("node_attempt_id"),
                                   node["output_ref"]["artifact_id"], envelope.get("run_id"))
            previous_hash = node.get("output_hash")
        else:
            seen_pending = True
        for unit in node["units"].values():
            if not unit.get("input_ref"):
                gap("UNIT_INPUT_MISSING", node_key=node["key"], unit_id=unit["unit_id"])
            unit_input = artifact_json(unit.get("input_ref"), node_key=node["key"], unit_id=unit["unit_id"])
            if unit_input is not None and digest(unit_input) != unit.get("input_hash"):
                gap("UNIT_INPUT_HASH_MISMATCH", node_key=node["key"], unit_id=unit["unit_id"])
            checkpoint(unit, node["key"], unit["unit_id"])
        units = [{"unit_id": unit["unit_id"], "status": unit["status"], "version": unit["version"],
                  "input_hash": unit["input_hash"], "output_hash": unit.get("output_hash"),
                  "input": ref(unit["input_ref"]), "output": ref(unit.get("output_ref")),
                  "attempts": [attempt(item, inherited=inherited) for item in unit["attempts"]]}
                 for unit in node["units"].values()]
        nodes.append({"node_key": node["key"], "status": node["status"], "version": node["version"],
                      "input_hash": node.get("input_hash"), "output_hash": node.get("output_hash"), "input": previous, "output": output,
                      "reused_from_run_id": node.get("reused_from_run_id"),
                      "reused_from_artifact_id": node.get("reused_from_artifact_id"), "reused_trace": node.get("reused_trace"),
                      "attempts": [attempt(item) for item in node["attempts"]], "units": units,
                      "unit_counts": dict(Counter(unit["status"] for unit in node["units"].values()))})
        previous = output

    expected_cursor = next((index for index, node in enumerate(state["nodes"]) if node["status"] != "succeeded"), len(state["nodes"]))
    if state.get("cursor") != expected_cursor:
        gap("CHECKPOINT_CURSOR_MISMATCH")

    budgets = state.get("call_budget", {})
    try:
        budget_summary = call_budget_summary(budgets)
    except ProviderBudgetLedgerError:
        gap("CALL_BUDGET_LEDGER_INVALID")
        budget_summary = {"ledger_status": "invalid", "effective_budget_charge_usd": None, "cost_is_provider_invoice": False}
    reservations = budgets.get("reservations", []) if budget_summary["ledger_status"] == "available" else []
    by_call = {(item["node_attempt_id"], item["call_id"], item["call_attempt"]): item for item in reservations}
    calls, usage_events = {}, []
    for event in events:
        if not event.get("call_id") or event.get("event") not in ("call.started", "call.completed", "call.failed", "call.retry_scheduled", "call.usage_recorded"):
            continue
        identity = (event.get("node_attempt_id"), event["call_id"], event.get("call_attempt"))
        if event["event"] == "call.usage_recorded":
            usage_events.append((identity, event))
            continue
        item = calls.setdefault(identity, {"call_id": identity[1], "call_attempt": identity[2], "events": []})
        item["events"].append(event["event"])
        if event["event"] == "call.retry_scheduled":
            item.setdefault("retries", []).append({key: event.get(key) for key in ("retry_reason", "trace_id", "span_id")})
            continue
        item.update({key: event[key] for key in ("node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "model", "provider", "prompt_version",
                    "operation", "input_hash", "output_hash", "trace_id", "span_id", "elapsed_ms", "input_tokens", "output_tokens", "total_tokens",
                    "cost", "usage_status", "error_code", "validation_status", "provider_request_id") if key in event})
    for identity, event in usage_events:
        item = calls.get(identity)
        if item is None or not any(name in item["events"] for name in ("call.started", "call.completed", "call.failed")):
            gap("CALL_USAGE_WITHOUT_ATTEMPT", call_id=identity[1], call_attempt=identity[2])
            continue
        item["events"].append("call.usage_recorded")
        item.setdefault("usage_events", []).append({key: event.get(key) for key in
            ("trace_id", "span_id", "unit_id", "unit_attempt_id", "input_tokens", "output_tokens", "total_tokens", "cost", "output_hash")})
    for call in calls.values():
        names = call["events"]
        call["outcome"] = "completed" if "call.completed" in names else "failed" if "call.failed" in names else "unknown"
        if names.count("call.started") != 1 or sum(names.count(x) for x in ("call.completed", "call.failed")) != 1:
            gaps.append({"code": "CALL_ATTEMPT_INCOMPLETE", "call_id": call["call_id"], "call_attempt": call["call_attempt"]})
        if (call.get("trace_id"), call.get("span_id")) not in span_ids:
            gaps.append({"code": "CALL_TRACE_MISSING", "call_id": call["call_id"], "call_attempt": call["call_attempt"]})
        else:
            span = spans_by_id[(call["trace_id"], call["span_id"])]
            if (span.get("name") != "provider.call" or span.get("attributes", {}).get("call_id") != call["call_id"]
                    or span.get("attributes", {}).get("call_attempt") != call["call_attempt"]):
                gap("CALL_TRACE_IDENTITY_MISMATCH", call_id=call["call_id"], call_attempt=call["call_attempt"])
        if any((row.get("trace_id"), row.get("span_id")) != (call.get("trace_id"), call.get("span_id")) for row in call.get("retries", [])):
            gap("CALL_RETRY_TRACE_MISMATCH", call_id=call["call_id"], call_attempt=call["call_attempt"])
        reservation = by_call.get((call.get("node_attempt_id"), call["call_id"], call["call_attempt"]))
        usage_events = call.get("usage_events", [])
        if len(usage_events) > 1:
            gap("CALL_USAGE_EVENT_DUPLICATE", call_id=call["call_id"], call_attempt=call["call_attempt"])
        known = reservation and reservation.get("settlement_status") == "known_usage_cost_estimate"
        if known and not usage_events:
            gap("CALL_USAGE_EVENT_MISSING", call_id=call["call_id"], call_attempt=call["call_attempt"])
        for row in usage_events:
            if (row.get("trace_id"), row.get("span_id")) != (call.get("trace_id"), call.get("span_id")):
                gap("CALL_USAGE_TRACE_MISMATCH", call_id=call["call_id"], call_attempt=call["call_attempt"])
            if (not known or {key: row.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")} != reservation["usage"] or
                    row.get("output_hash") != reservation["response_sha256"] or
                    row.get("unit_id") != reservation.get("unit_id") or row.get("unit_attempt_id") != reservation.get("unit_attempt_id")):
                gap("CALL_USAGE_LEDGER_MISMATCH", call_id=call["call_id"], call_attempt=call["call_attempt"])
            elif Decimal(reservation["known_usage_cost_estimate_usd"]) <= Decimal("1e308") and row.get("cost") != float(reservation["known_usage_cost_estimate_usd"]):
                gap("CALL_USAGE_LEDGER_MISMATCH", call_id=call["call_id"], call_attempt=call["call_attempt"])
        if known:
            # Billing metadata is read from the unique durable attempt record;
            # damaged or duplicate log events cannot change budget charges.
            call.update(usage_status="known", **reservation["usage"], usage_evidence="durable_usage_estimate")
            cost = Decimal(reservation["known_usage_cost_estimate_usd"])
            if cost <= Decimal("1e308"):
                call["cost"] = float(cost)
            else:
                call.pop("cost", None)
        if reservation:
            call["settlement_status"] = reservation.get("settlement_status", "reserved_unknown_actual_cost")
    for reservation in reservations:
        identity = (reservation["node_attempt_id"], reservation["call_id"], reservation["call_attempt"])
        if identity not in calls:
            gaps.append({"code": "RESERVATION_WITHOUT_REQUEST_RECORD", "call_id": reservation["call_id"], "call_attempt": reservation["call_attempt"]})
    execution = [attempt(item) for item in state["execution_attempts"]]
    if state["status"] == "running":
        gaps.append({"code": "EXECUTION_OPEN_TRACE_MAY_BE_PARTIAL"})
    if state["observability"].get("trace_status") in ("partial", "write_failed"):
        gaps.append({"code": "RUN_TRACE_INCOMPLETE", "status": state["observability"]["trace_status"]})
    from .material_ocr_diagnostics import index_local_ocr
    local_ocr = index_local_ocr(state, events, spans, artifact_json=artifact_json, ref=ref, gap=gap)
    ocr_calls = [c for c in calls.values() if c.get("provider") == "deepseek_official_ocr"]
    ocr_replays = [{**{key: event.get(key) for key in ("node_key", "unit_id", "unit_attempt_id",
        "input_hash", "output_hash", "validation_status", "trace_id", "span_id")},
        "origin_run_id": event.get("reused_from_run_id")}
        for event in events if event.get("event") == "material.ocr_response_replayed"]
    ocr_rejections = [{key: event.get(key) for key in ("node_key", "unit_id", "unit_attempt_id",
        "error_code", "trace_id", "span_id")} for event in events
        if event.get("event") == "material.ocr_replay.rejected" or
        event.get("event") == "material_ocr.preflight.failed" and event.get("provider") == "deepseek_official_ocr"]
    return {"schema_version": "chain.diagnostic-index.v1", "run_id": run_id, "run_status": state["status"],
            **local_ocr,
            "deepseek_ocr_calls": ocr_calls, "deepseek_ocr_actual_attempts": len(ocr_calls),
            "deepseek_ocr_failed_attempts": sum(c["outcome"] == "failed" for c in ocr_calls),
            "deepseek_ocr_replayed_sources": ocr_replays, "deepseek_ocr_replay_or_preflight_failures": ocr_rejections,
            "material_ocr_local_fallback_enabled": False,
            "parent_run_id": state.get("parent_run_id"), "fork_from_node": state.get("fork_from_node"),
            "observability": state["observability"], "capture_policy": state["capture_policy"],
            "evidence_status": "incomplete" if gaps else "metadata_only_capture_disabled" if not state["capture_policy"] else
                               "available_with_content_limitations" if content_limitations else "available",
            "gaps": gaps, "content_limitations": content_limitations, "nodes": nodes, "execution_attempts": execution, "calls": list(calls.values()),
            "actual_call_attempts": len(calls), "unknown_usage_attempts": sum(c.get("usage_status") not in {"known", "not_token_billed"} for c in calls.values()),
            "non_token_billed_attempts": sum(c.get("usage_status") == "not_token_billed" for c in calls.values()),
            "known_estimated_cost_usd": sum(c["cost"] for c in calls.values() if isinstance(c.get("cost"), (int, float))),
            "cost_is_provider_invoice": False, "call_reservations": budgets, "budget_summary": budget_summary,
            "storage_write_retries": [{key: item.get(key) for key in
                ("operation", "retry_number", "state_revision", "winerror", "error_code", "recorded_at")}
                for item in state.get("storage_write_retries", [])],
            "event_count": len(events), "span_count": len(spans), "artifacts_checked": len(integrity),
            "next_node": next((n["key"] for n in state["nodes"] if n["status"] != "succeeded"), None),
            "scope": "Read-only local diagnostic evidence index; availability is not semantic acceptance or team authorization."}
