"""One restricted proposal adapter, with a record for every actual HTTP attempt."""

from __future__ import annotations

import hashlib
import base64
import io
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from .models import ProposalContent, ReferenceExcerpt, WritingTask


from .writing_prompt import PROMPT_VERSION, SYSTEM_PROMPT


class ProviderFailure(RuntimeError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        self.code = error_code
        super().__init__(error_code)


def validate_writing_context(section: dict) -> WritingTask | None:
    if "writing_task" not in section:
        return None
    try:
        task = WritingTask.model_validate(section["writing_task"])
        references = [ReferenceExcerpt.model_validate(item) for item in section.get("reference_excerpts", [])]
    except ValidationError:
        raise ProviderFailure("WRITING_CONTEXT_INVALID") from None
    if task.unit_id != section.get("section_id"):
        raise ProviderFailure("WRITING_CONTEXT_INVALID")
    if task.format_status != "not_prescribed":
        # Unknown/failed format reads must never become permission for free prose.
        raise ProviderFailure("WRITING_FORMAT_NOT_READY")
    if len(references) != len(task.reference_refs) or {item.reference_id for item in references} != set(task.reference_refs):
        raise ProviderFailure("WRITING_REFERENCE_MISMATCH")
    return task


def validate_writing_output(section: dict, proposal: ProposalContent) -> None:
    task = validate_writing_context(section)
    if task and {item.item_index for item in proposal.coverage} != set(range(len(task.must_cover))):
        raise ProviderFailure("WRITING_COVERAGE_INCOMPLETE")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def redact_secret(value, secret: str):
    """Redact an actual credential even when a provider echoes it in free text."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {redact_secret(str(k), secret): redact_secret(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secret(v, secret) for v in value]
    return value


@dataclass(frozen=True)
class ChatConfig:
    endpoint: str
    model: str
    auth_env: str = "COMPANY_BID_API_KEY"
    max_attempts: int = 2
    timeout_seconds: int = 90
    max_output_tokens: int = 2500
    max_input_chars: int = 24000
    max_run_calls: int = 2
    max_run_usd: float = 0.02
    input_usd_per_million: float = 0.30
    output_usd_per_million: float = 1.20
    thinking_mode: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ProviderFailure("PROVIDER_ENDPOINT_INVALID")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", self.auth_env):
            raise ProviderFailure("PROVIDER_KEY_ENV_INVALID")
        if not self.model or not 1 <= self.max_attempts <= 3 or not 1 <= self.timeout_seconds <= 180:
            raise ProviderFailure("PROVIDER_CONFIG_INVALID")
        if not 1 <= self.max_output_tokens <= 8000 or not 1 <= self.max_input_chars <= 100000:
            raise ProviderFailure("PROVIDER_BUDGET_INVALID")
        if not 1 <= self.max_run_calls <= 200 or self.thinking_mode not in (None, "enabled", "disabled"):
            raise ProviderFailure("PROVIDER_CONFIG_INVALID")
        if (self.reasoning_effort not in (None, "low", "high", "max") or
                self.reasoning_effort is not None and self.thinking_mode != "enabled"):
            raise ProviderFailure("PROVIDER_CONFIG_INVALID")
        if any(not math.isfinite(x) or x <= 0 for x in (self.max_run_usd, self.input_usd_per_million, self.output_usd_per_million)):
            raise ProviderFailure("PROVIDER_BUDGET_INVALID")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_request(endpoint: str, payload: dict, secret: str, timeout: int) -> bytes:
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                      headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        return data


class ChatProposalProvider:
    def __init__(self, config: ChatConfig, transport: Callable = http_request, sleep: Callable = time.sleep):
        self.config = config
        self.transport = transport
        self.sleep = sleep

    def generate(self, ctx, section: dict, requirements: list[dict]) -> ProposalContent:
        validate_writing_context(section)

        def validate(value):
            proposal = ProposalContent.model_validate(value)
            validate_writing_output(section, proposal)
            return proposal.model_dump(mode="json")

        result = self.generate_json(ctx, {"section": section, "requirements": requirements},
                                    unit_id=section.get("section_id"), prompt_version=PROMPT_VERSION,
                                    system_prompt=SYSTEM_PROMPT, validate=validate, task_type="writing")
        return ProposalContent.model_validate(result)

    def generate_json(self, ctx, user_input: dict, *, unit_id: str, prompt_version: str,
                      system_prompt: str, validate: Callable, task_type: str, image: dict | None = None) -> dict:
        """One gateway for narrow extraction, mapping and writing candidates.

        Validation executes inside the actual attempt: a 200 with invalid
        quotations is a failed call, with its raw response retained privately.
        """
        secret = os.environ.get(self.config.auth_env, "")
        if not secret:
            raise ProviderFailure("PROVIDER_CREDENTIAL_MISSING")
        if len(json.dumps(user_input, ensure_ascii=False)) > self.config.max_input_chars:
            raise ProviderFailure("PROVIDER_INPUT_BUDGET_EXCEEDED")
        user_content = json.dumps(user_input, ensure_ascii=False)
        if image is not None:
            from PIL import Image
            data = image.get("data")
            media_type = image.get("media_type")
            if (not isinstance(data, bytes) or not 1 <= len(data) <= 1_500_000
                    or media_type not in ("image/png", "image/jpeg")
                    or image.get("sha256") != hashlib.sha256(data).hexdigest()):
                raise ProviderFailure("PROVIDER_IMAGE_INVALID")
            try:
                with Image.open(io.BytesIO(data)) as decoded:
                    if (decoded.format != {"image/png": "PNG", "image/jpeg": "JPEG"}[media_type]
                            or max(decoded.size) > 3072 or decoded.width * decoded.height > 6_000_000):
                        raise ValueError
                    decoded.verify()
            except Exception:
                raise ProviderFailure("PROVIDER_IMAGE_INVALID") from None
            user_content = [{"type": "text", "text": user_content},
                            {"type": "image_url", "image_url": {
                                "url": "data:" + media_type + ";base64," + base64.b64encode(data).decode("ascii"), "detail": "high"}}]
        payload = {"model": self.config.model, "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ], "temperature": 0.2, "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"}}
        if self.config.thinking_mode is not None:
            payload["thinking"] = {"type": self.config.thinking_mode}
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        # UTF-8 byte count plus generous framing allowance is a conservative
        # request-token bound. Reserve at frozen configured peak prices; unknown
        # outcomes keep the reservation, so restart cannot reset the run budget.
        max_input_tokens = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048
        upper_usd = (max_input_tokens * self.config.input_usd_per_million +
                     self.config.max_output_tokens * self.config.output_usd_per_million) / 1_000_000
        call_id = uuid.uuid4().hex
        for attempt in range(1, self.config.max_attempts + 1):
            metadata = {"call_id": call_id, "call_attempt": attempt, "provider": "chat_completions",
                        "unit_id": unit_id, "operation": task_type,
                        "model": self.config.model, "prompt_version": prompt_version,
                        "input_hash": digest(payload), "usage_status": "unknown"}
            # No headers, endpoint credentials, or key values are serialized.
            captured = ctx.capture(f"request-{call_id}-{attempt}", redact_secret(payload, secret))
            if captured["status"] == "write_failed":
                raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
            ctx.reserve_call(call_id, attempt, upper_usd, self.config.max_run_usd, self.config.max_run_calls)
            started = time.monotonic()
            retryable = False
            with ctx.call_span(metadata) as span:
                metadata.update(trace_id=format(span.get_span_context().trace_id, "032x"),
                                span_id=format(span.get_span_context().span_id, "016x"))
                ctx.call_event("call.started", metadata)
                try:
                    response = self.transport(self.config.endpoint, payload, secret, self.config.timeout_seconds)
                    if isinstance(response, bytes):
                        if len(response) > 4 * 1024 * 1024:
                            ctx.capture(f"response-error-{call_id}-{attempt}", redact_secret({"body_status": "truncated", "body": response[:65536].decode("utf-8", errors="replace")}, secret), partial=True)
                            raise ProviderFailure("PROVIDER_RESPONSE_TOO_LARGE")
                        try:
                            response = json.loads(response)
                        except (ValueError, UnicodeError):
                            ctx.capture(f"response-error-{call_id}-{attempt}", redact_secret({"body_status": "invalid_json", "body": response.decode("utf-8", errors="replace")}, secret))
                            raise ProviderFailure("PROVIDER_SCHEMA_INVALID") from None
                    clean = redact_secret(response, secret)
                    captured = ctx.capture(f"response-{call_id}-{attempt}", clean)
                    if captured["status"] == "write_failed":
                        raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
                    usage = response.get("usage") or {}
                    if not isinstance(usage, dict):
                        usage = {}
                    for remote, local in [("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")]:
                        value = usage.get(remote)
                        metadata[local] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                    metadata["usage_status"] = "known" if all(metadata[k] is not None for k in ("input_tokens", "output_tokens", "total_tokens")) else "unknown"
                    if metadata["usage_status"] == "known":
                        metadata["cost"] = (metadata["input_tokens"] * self.config.input_usd_per_million + metadata["output_tokens"] * self.config.output_usd_per_million) / 1_000_000
                    if isinstance(clean.get("id"), str):
                        metadata["provider_request_id"] = clean["id"]
                    metadata["output_hash"] = digest(clean)
                    choices = clean.get("choices") or []
                    if not choices or choices[0].get("finish_reason") != "stop":
                        raise ProviderFailure("PROVIDER_TRUNCATED_OR_INCOMPLETE")
                    candidate = json.loads(choices[0]["message"]["content"])
                    if not isinstance(candidate, dict):
                        raise ProviderFailure("PROVIDER_SCHEMA_INVALID")
                    try:
                        proposal = validate(candidate)
                    except (ProviderFailure, ValidationError):
                        raise
                    except Exception as error:
                        error_code = getattr(error, "code", "PROVIDER_SCHEMA_INVALID" if isinstance(error, (ValueError, KeyError, TypeError, AttributeError)) else "PROVIDER_VALIDATION_FAILED")
                        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", error_code):
                            error_code = "PROVIDER_VALIDATION_FAILED"
                        context = getattr(error, "diagnostic_context", None)
                        if isinstance(context, dict):
                            captured = ctx.capture(f"validation-{call_id}-{attempt}", redact_secret({
                                "status": "failed", "error_code": error_code,
                                "call_id": call_id, "call_attempt": attempt, "unit_id": unit_id,
                                "diagnostic_context": context}, secret))
                            if captured["status"] == "write_failed":
                                raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED") from None
                        raise ProviderFailure(error_code) from None
                    if not isinstance(proposal, dict):
                        raise ProviderFailure("PROVIDER_VALIDATOR_CONTRACT_INVALID")
                    validation_capture = ctx.capture(f"validation-{call_id}-{attempt}", {
                        "call_id": call_id, "call_attempt": attempt, "unit_id": unit_id,
                        "status": "passed", "validated_output": proposal})
                    if validation_capture["status"] == "write_failed":
                        raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
                    metadata.update(elapsed_ms=round((time.monotonic()-started)*1000), validation_status="passed")
                    from .observability import safe_metadata
                    span.set_attributes(safe_metadata(metadata))
                    ctx.call_event("call.completed", metadata)
                    return proposal
                except HTTPError as error:
                    code = "PROVIDER_AUTH_FAILED" if error.code in (401, 403) else "PROVIDER_HTTP_ERROR"
                    metadata["http_status"] = error.code
                    retryable = error.code == 429 or 500 <= error.code < 600
                    try:
                        raw_error = error.read(65537)
                        error_capture = {"http_status": error.code, "body_status": "truncated" if len(raw_error) > 65536 else "captured",
                                         "body": raw_error[:65536].decode("utf-8", errors="replace")}
                    except Exception:
                        error_capture = {"http_status": error.code, "body_status": "unavailable"}
                    captured = ctx.capture(f"response-error-{call_id}-{attempt}", redact_secret(error_capture, secret), partial=error_capture["body_status"] != "captured")
                    if captured["status"] == "write_failed":
                        code, retryable = "DIAGNOSTICS_CAPTURE_FAILED", False
                except (TimeoutError, URLError):
                    code, retryable = "PROVIDER_NETWORK_ERROR", True
                    captured = ctx.capture(f"response-error-{call_id}-{attempt}", {"error_code": code, "response_status": "unavailable", "billing_status": "unknown_may_be_charged"}, partial=True)
                    if captured["status"] == "write_failed":
                        code, retryable = "DIAGNOSTICS_CAPTURE_FAILED", False
                except ProviderFailure as error:
                    code = error.error_code
                except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as error:
                    code = "PROVIDER_SCHEMA_INVALID"
                    # Only schema locations/types; exception strings may echo credentials.
                    details = [{"loc": list(item["loc"]), "type": item["type"]}
                               for item in error.errors(include_input=False, include_url=False)] if isinstance(error, ValidationError) else []
                    ctx.capture(f"validation-{call_id}-{attempt}", redact_secret({"status": "failed", "error_code": code,
                                "call_id": call_id, "call_attempt": attempt, "unit_id": unit_id, "schema_errors": details}, secret))
                except Exception as error:
                    # An unexpected adapter failure is still an actual attempt.
                    # Keep a safe private stack and a terminal event, never the
                    # exception message or local values that may echo a key.
                    from .runtime import _error_diagnostic
                    code = "PROVIDER_EXECUTION_ERROR"
                    ctx.capture(f"adapter-error-{call_id}-{attempt}", redact_secret(_error_diagnostic(error), secret))
                if code != "DIAGNOSTICS_CAPTURE_FAILED":
                    ctx.capture(f"failure-{call_id}-{attempt}", {"status": "failed", "error_code": code,
                                "call_id": call_id, "call_attempt": attempt, "unit_id": unit_id})
                metadata.update(error_code=code, validation_status="failed", elapsed_ms=round((time.monotonic()-started)*1000))
                from .observability import safe_metadata
                span.set_attributes(safe_metadata(metadata))
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, code))
                ctx.call_event("call.failed", metadata)
            if not retryable or attempt == self.config.max_attempts:
                raise ProviderFailure(code)
            ctx.call_event("call.retry_scheduled", {**metadata, "retry_reason": code})
            self.sleep(min(2 ** (attempt - 1), 4))
        raise AssertionError("bounded attempt loop must return or fail")


class PreparedProposalProvider:
    """Explicit human/agent prepared content. Does not masquerade as an HTTP call."""
    def __init__(self, proposals: dict):
        self.proposals = proposals

    def generate(self, ctx, section: dict, requirements: list[dict]) -> ProposalContent:
        validate_writing_context(section)
        try:
            result = ProposalContent.model_validate(self.proposals[section["section_id"]])
        except (KeyError, ValidationError):
            raise ProviderFailure("PREPARED_PROPOSAL_MISSING_OR_INVALID") from None
        validate_writing_output(section, result)
        ctx.capture("prepared-proposal-" + section["section_id"], result.model_dump(mode="json"))
        return result
