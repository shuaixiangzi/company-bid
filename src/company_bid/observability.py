"""Bounded metadata events, redacted captures and real OpenTelemetry spans."""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .storage import json_bytes


class RuntimeDependencyError(RuntimeError):
    code = "OTEL_DEPENDENCY_MISSING"


class ObservabilityWriteError(RuntimeError):
    code = "OBSERVABILITY_WRITE_FAILED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SECRET_KEY = re.compile(r"(?i)(authorization|cookie|password|passwd|api[_-]?key|secret|credential|access[_-]?token|refresh[_-]?token|signature|^sig$|^token$)")
_SAFE_METADATA = {"run_id", "job_attempt_id", "node_key", "node_attempt_id", "unit_id", "unit_attempt_id", "trace_id", "span_id",
                  "call_id", "call_attempt", "provider_request_id", "provider", "model", "prompt_version", "schema_version",
                  "node_version", "input_hash", "output_hash", "artifact_id", "sha256", "status", "error_code", "error_type",
                  "duration_ms", "elapsed_ms", "wait_ms", "count", "size_bytes", "input_tokens", "output_tokens", "total_tokens", "cost", "usage_status", "validation_status",
                  "retry_reason", "reused_from_run_id", "capture_status", "attempt_number", "phase", "operation", "http_status"}
_TOKEN_VALUE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,160}$")


def redact_payload(value: Any) -> Any:
    """Credentials are removed before serialization, including signed URLs.

    Source text remains in controlled captures, never ordinary events/spans.
    Fields containing credentials should be structured rather than free text.
    """
    secrets: set[str] = set()

    def secret_field(key: Any, entry: Any) -> bool:
        # An environment-variable *name* is configuration, not a credential.
        # Keep only this explicit field/format exception; never exempt an
        # arbitrary api_key field or arbitrary value ending in "_env".
        if str(key) == "api_key_env" and isinstance(entry, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", entry):
            return False
        return bool(_SECRET_KEY.search(str(key)))

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, entry in item.items():
                if secret_field(key, entry) and isinstance(entry, str) and entry:
                    secrets.add(entry)
                collect(entry)
        elif isinstance(item, (list, tuple)):
            for entry in item:
                collect(entry)

    collect(value)

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {clean(str(key)): "[REDACTED]" if secret_field(key, entry) else clean(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(entry) for entry in item]
        if isinstance(item, str):
            for secret in sorted(secrets, key=len, reverse=True):
                item = item.replace(secret, "[REDACTED]")
            item = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", item)
            item = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", item)

            def clean_url(match: re.Match) -> str:
                try:
                    parsed = urlsplit(match.group(0))
                    host = parsed.netloc.rsplit("@", 1)[-1]
                    query = [(key, "[REDACTED]" if _SECRET_KEY.search(key) or key.lower().startswith("x-amz-") else val)
                             for key, val in parse_qsl(parsed.query, keep_blank_values=True)]
                    return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
                except ValueError:
                    return "[REDACTED_URL]"

            return re.sub(r"https?://[^\s<>\"']+", clean_url, item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        raise TypeError("Capture payload must contain only JSON values.")

    return clean(value)


def safe_metadata(value: dict | None) -> dict:
    result = {}
    for key, item in (value or {}).items():
        if key not in _SAFE_METADATA or _SECRET_KEY.search(key):
            continue
        if item is None or isinstance(item, (bool, int, float)):
            result[key] = item
        elif isinstance(item, str) and _TOKEN_VALUE.fullmatch(item) and "?" not in item:
            result[key] = item
    return result


def append_json(path: Path, value: dict) -> None:
    import os

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise ObservabilityWriteError("A local diagnostic record could not be persisted.") from exc


class Observer:
    def __init__(self, run_dir: Path, run_id: str):
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
        except ImportError as exc:
            raise RuntimeDependencyError("Install the pinned opentelemetry-sdk dependency to execute runs.") from exc

        self.run_dir, self.run_id = Path(run_dir), run_id
        self.trace_status = "captured"
        self.export_error = None
        self._mutex = threading.Lock()
        owner = self

        class DurableExporter(SpanExporter):
            def export(self, spans):
                try:
                    with owner._mutex:
                        for span in spans:
                            context = span.context
                            # Explicit OTLP-compatible identity/timestamp/attribute
                            # mapping; these are SDK spans, not synthetic events.
                            append_json(owner.run_dir / "spans.jsonl", {
                                "schema_version": "m1.otel.span.v1", "name": span.name,
                                "trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x"),
                                "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                                "start_time_unix_nano": span.start_time, "end_time_unix_nano": span.end_time,
                                "kind": span.kind.name, "status": span.status.status_code.name,
                                "attributes": safe_metadata(dict(span.attributes or {})),
                                "events": [{"name": event.name, "time_unix_nano": event.timestamp,
                                            "attributes": safe_metadata(dict(event.attributes or {}))} for event in span.events],
                                "links": [{"trace_id": format(link.context.trace_id, "032x"),
                                           "span_id": format(link.context.span_id, "016x")} for link in span.links],
                                "resource": {"service.name": "company-bid-m1"},
                            })
                    return SpanExportResult.SUCCESS
                except Exception:
                    owner.trace_status = "write_failed"
                    owner.export_error = "TRACE_WRITE_FAILED"
                    return SpanExportResult.FAILURE

            def shutdown(self):
                return None

        self.provider = TracerProvider(resource=Resource.create({"service.name": "company-bid-m1"}))
        self.provider.add_span_processor(SimpleSpanProcessor(DurableExporter()))
        self.tracer = self.provider.get_tracer("company_bid.m1", "1")

    @contextmanager
    def span(self, name: str, metadata: dict | None = None, links: list[dict] | None = None) -> Iterator[Any]:
        from opentelemetry.trace import Link, SpanContext, TraceFlags

        span_links = []
        for link in links or []:
            if link.get("trace_id") and link.get("span_id"):
                span_links.append(Link(SpanContext(trace_id=int(link["trace_id"], 16), span_id=int(link["span_id"], 16),
                                                   is_remote=False, trace_flags=TraceFlags(1))))
        with self.tracer.start_as_current_span(name, attributes=safe_metadata(metadata), links=span_links,
                                              record_exception=False, set_status_on_exception=False) as current:
            try:
                yield current
            except BaseException:
                from opentelemetry.trace import Status, StatusCode

                current.set_status(Status(StatusCode.ERROR))
                raise

    def event(self, event: str, metadata: dict | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", event):
            raise ValueError("Event names must be stable identifiers.")
        from opentelemetry.trace import get_current_span
        context = get_current_span().get_span_context()
        correlation = ({"trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x")}
                       if context.is_valid else {})
        append_json(self.run_dir / "events.jsonl", {"time": utc_now(), "event": event,
                                                    "run_id": self.run_id, **correlation, **safe_metadata(metadata)})

    def check(self) -> None:
        if self.export_error:
            raise ObservabilityWriteError("A local OpenTelemetry span could not be persisted.")

    def close(self) -> None:
        self.provider.force_flush()
        self.provider.shutdown()
