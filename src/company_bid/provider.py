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
from decimal import Decimal
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from .models import ProposalContent, ReferenceExcerpt, WritingTask, RequirementSourceContext


from .writing_prompt import PROMPT_VERSION, SYSTEM_PROMPT


OPENCODE_GO_ENDPOINT = "https://opencode.ai/inference/go/openai/v1/chat/completions"
OPENCODE_GO_MODEL = "deepseek-v4.1-flash"
OPENCODE_GO_USER_AGENT = "company-bid-test/0.1"
DEEPSEEK_DIRECT_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DIRECT_MODEL = "deepseek-flash"


class ProviderFailure(RuntimeError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        self.code = error_code
        super().__init__(error_code)


def validate_writing_context(section: dict, requirements: list[dict] | None = None) -> WritingTask | None:
    if "writing_task" not in section:
        if section.get("source_contexts") or any(item.get("source_contexts") for item in requirements or []):
            raise ProviderFailure("WRITING_SOURCE_CONTEXT_MISMATCH")
        return None
    try:
        task = WritingTask.model_validate(section["writing_task"])
        references = [ReferenceExcerpt.model_validate(item) for item in section.get("reference_excerpts", [])]
        expected_contexts = [RequirementSourceContext.model_validate(context).model_dump(mode="json")
                             for requirement in (requirements or []) for context in requirement.get("source_contexts", [])]
        actual_contexts = [RequirementSourceContext.model_validate(context).model_dump(mode="json")
                           for context in section.get("source_contexts", [])]
    except ValidationError:
        raise ProviderFailure("WRITING_CONTEXT_INVALID") from None
    if task.unit_id != section.get("section_id"):
        raise ProviderFailure("WRITING_CONTEXT_INVALID")
    if task.format_status != "not_prescribed":
        # Unknown/failed format reads must never become permission for free prose.
        raise ProviderFailure("WRITING_FORMAT_NOT_READY")
    if len(references) != len(task.reference_refs) or {item.reference_id for item in references} != set(task.reference_refs):
        raise ProviderFailure("WRITING_REFERENCE_MISMATCH")
    if (actual_contexts != expected_contexts or task.source_context_refs !=
            [context["context_id"] for context in expected_contexts] or
            expected_contexts and {item.get("requirement_id") for item in requirements or []} != set(task.requirement_refs)):
        raise ProviderFailure("WRITING_SOURCE_CONTEXT_MISMATCH")
    return task


def validate_writing_output(section: dict, proposal: ProposalContent, requirements: list[dict] | None = None) -> None:
    task = validate_writing_context(section, requirements)
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
    service: str = "generic"

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ProviderFailure("PROVIDER_ENDPOINT_INVALID")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", self.auth_env):
            raise ProviderFailure("PROVIDER_KEY_ENV_INVALID")
        if self.service not in {"generic", "opencode_go"}:
            raise ProviderFailure("PROVIDER_SERVICE_INVALID")
        if self.service == "opencode_go" and (self.endpoint != OPENCODE_GO_ENDPOINT or self.model != OPENCODE_GO_MODEL):
            raise ProviderFailure("PROVIDER_SERVICE_ENDPOINT_MODEL_INVALID")
        timeout_limit, output_limit = (600, 65536) if self.service == "opencode_go" else (180, 16000)
        if self.endpoint == DEEPSEEK_DIRECT_ENDPOINT and self.model == DEEPSEEK_DIRECT_MODEL:
            output_limit = 32000
        if not self.model or not 1 <= self.max_attempts <= 3 or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= timeout_limit:
            raise ProviderFailure("PROVIDER_CONFIG_INVALID")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= output_limit or not 1 <= self.max_input_chars <= 100000:
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


def build_json_request(config: ChatConfig, user_input: dict, system_prompt: str,
                       image: dict | None = None) -> dict:
    """Build the actual wire object without credentials, I/O, or call records."""
    if len(json.dumps(user_input, ensure_ascii=False)) > config.max_input_chars:
        raise ProviderFailure("PROVIDER_INPUT_BUDGET_EXCEEDED")
    user_content = json.dumps(user_input, ensure_ascii=False)
    if image is not None:
        from PIL import Image
        data, media_type = image.get("data"), image.get("media_type")
        if (not isinstance(data, bytes) or not 1 <= len(data) <= 1_500_000 or
                media_type not in ("image/png", "image/jpeg") or
                image.get("sha256") != hashlib.sha256(data).hexdigest()):
            raise ProviderFailure("PROVIDER_IMAGE_INVALID")
        try:
            with Image.open(io.BytesIO(data)) as decoded:
                if (decoded.format != {"image/png": "PNG", "image/jpeg": "JPEG"}[media_type] or
                        max(decoded.size) > 3072 or decoded.width * decoded.height > 6_000_000):
                    raise ValueError
                decoded.verify()
        except Exception:
            raise ProviderFailure("PROVIDER_IMAGE_INVALID") from None
        user_content = [{"type": "text", "text": user_content}, {"type": "image_url", "image_url": {
            "url": "data:" + media_type + ";base64," + base64.b64encode(data).decode("ascii"), "detail": "high"}}]
    payload = {"model": config.model, "messages": [
        {"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "temperature": 0.2, "max_tokens": config.max_output_tokens, "response_format": {"type": "json_object"}}
    if config.thinking_mode is not None:
        payload["thinking"] = {"type": config.thinking_mode}
    if config.reasoning_effort is not None:
        payload["reasoning_effort"] = config.reasoning_effort
    return payload


def http_request(endpoint: str, payload: dict, secret: str, timeout: int, *, headers: dict | None = None) -> bytes:
    extra = headers or {}
    if (not isinstance(extra, dict) or set(extra) - {"User-Agent", "x-opencode-session"} or
            any(not isinstance(value, str) or not value or "\r" in value or "\n" in value for value in extra.values())):
        raise ProviderFailure("PROVIDER_HEADERS_INVALID")
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                      headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json", **extra})
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        return data


def _go_billing(response) -> dict:
    """Provider-reported charge/allowance are separate from our quota estimate.

    Field meanings: https://opencode.ai/docs/console/api/inference/#cost
    This checks a completed response; server-side extra usage can already have
    charged the first request. It is not a pre-call no-charge guarantee.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    malformed = not isinstance(response, dict) or (usage is not None and not isinstance(usage, dict))
    usage = usage if isinstance(usage, dict) else {}
    opencode = usage.get("opencode")
    malformed = malformed or ("opencode" in usage and not isinstance(opencode, dict))
    opencode = opencode if isinstance(opencode, dict) else {}

    def money(container, key):
        if key not in container:
            return {"status": "unknown", "usd": None}
        value = container[key]
        if type(value) not in (int, float):
            return {"status": "invalid", "usd": None}
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal < 0:
            return {"status": "invalid", "usd": None}
        return {"status": "known", "usd": str(decimal)}

    source = opencode.get("billing_source")
    source_status = "unknown" if "billing_source" not in opencode else "known" if source in ("go", "go-plus", "balance", "byok", "free") else "invalid"
    charge, allowance, listed = money(usage, "cost"), money(opencode, "allowance"), money(opencode, "list_cost")
    estimated = opencode.get("estimated", False)
    required = [source_status, charge["status"], listed["status"]]
    if source in ("go", "go-plus"):
        required.append(allowance["status"])
    invalid = (malformed or "invalid" in required or allowance["status"] == "invalid" or type(estimated) is not bool or
               estimated and source != "byok")
    unknown = "unknown" in required
    code = "PROVIDER_BILLING_INVALID" if invalid else "PROVIDER_BILLING_UNVERIFIED" if unknown else None
    if code is None and (source not in ("go", "go-plus") or Decimal(charge["usd"]) != 0):
        code = "PROVIDER_SUBSCRIPTION_BILLING_REQUIRED"
    return {"service": "opencode_go", "billing_status": "invalid" if invalid else "unknown" if unknown else "known",
            "billing_source": source if source_status == "known" else None, "billing_source_status": source_status,
            "reported_usage_cost": charge,
            "reported_balance_charge": charge if source != "byok" else {"status": "not_applicable", "usd": None},
            "subscription_allowance": allowance, "list_cost": listed,
            "charge_is_estimate": estimated if type(estimated) is bool else None,
            "raw_billing_json": json.dumps({key: usage[key] for key in ("cost", "opencode") if key in usage},
                                           ensure_ascii=False, default=lambda _: "[non-JSON value]"),
            "policy_error_code": code, "quota_basis": "frozen_peak_rate_estimate_not_reported_charge",
            "server_side_extra_usage_may_already_have_charged": True}


def _record_go_billing(ctx, billing: dict, *, call_id: str, attempt: int, response_sha256: str, secret: str) -> None:
    captured = None
    try:
        captured = ctx.capture(f"billing-{call_id}-{attempt}", redact_secret({**billing, "call_id": call_id,
                                "call_attempt": attempt, "response_sha256": response_sha256}, secret))
    finally:
        if billing["policy_error_code"]:
            ctx.stop_provider_calls(billing["policy_error_code"], call_id=call_id, call_attempt=attempt,
                                    response_sha256=response_sha256)
    if isinstance(captured, dict) and captured.get("status") == "write_failed":
        raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")


def _capture_response(ctx, name: str, response, billing: dict | None, *, call_id: str, attempt: int,
                      response_sha256: str, secret: str, rejected_response: bool = False):
    """A failed capture cannot erase an already determined Go policy stop."""
    captured = None
    try:
        captured = ctx.capture(name, response)
        return captured
    finally:
        failed = captured is None or captured.get("status") == "write_failed"
        if billing is not None and billing["policy_error_code"] and (rejected_response or failed):
            # No token settlement has occurred on this early-failure path:
            # retain the full unknown reservation and only latch the policy.
            _record_go_billing(ctx, billing, call_id=call_id, attempt=attempt,
                               response_sha256=response_sha256, secret=secret)


class ChatProposalProvider:
    def __init__(self, config: ChatConfig, transport: Callable = http_request, sleep: Callable = time.sleep):
        self.config = config
        self.transport = transport
        self.sleep = sleep

    def generate(self, ctx, section: dict, requirements: list[dict]) -> ProposalContent:
        validate_writing_context(section, requirements)

        def validate(value):
            proposal = ProposalContent.model_validate(value)
            validate_writing_output(section, proposal, requirements)
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
        from .runtime import (ProviderBudgetLedgerError, ProviderBudgetPersistenceError,
                              ProviderUsageSettlementError, _valid_usage, usage_cost_estimate)

        secret = os.environ.get(self.config.auth_env, "")
        if not secret:
            raise ProviderFailure("PROVIDER_CREDENTIAL_MISSING")
        payload = build_json_request(self.config, user_input, system_prompt, image)
        # UTF-8 byte count plus generous framing allowance is a conservative
        # request-token bound. Reserve at frozen configured peak prices; unknown
        # outcomes keep the reservation, so restart cannot reset the run budget.
        max_input_tokens = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048
        upper_usd = usage_cost_estimate(max_input_tokens, self.config.max_output_tokens,
                                       self.config.input_usd_per_million, self.config.output_usd_per_million)
        headers = None
        if self.config.service == "opencode_go":
            session = getattr(ctx, "run_id", None)
            if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", session):
                raise ProviderFailure("PROVIDER_SESSION_INVALID")
            headers = {"User-Agent": OPENCODE_GO_USER_AGENT, "x-opencode-session": session}
        call_id = uuid.uuid4().hex
        for attempt in range(1, self.config.max_attempts + 1):
            metadata = {"call_id": call_id, "call_attempt": attempt,
                        "provider": "opencode_go" if headers is not None else "chat_completions",
                        "unit_id": unit_id, "operation": task_type,
                        "model": self.config.model, "prompt_version": prompt_version,
                        "input_hash": digest(payload), "usage_status": "unknown"}
            # No authentication headers, endpoint credentials, or key values
            # are serialized. Go's owned non-secret headers have a separate
            # controlled transport capture.
            captured = ctx.capture(f"request-{call_id}-{attempt}", redact_secret(payload, secret))
            if captured["status"] == "write_failed":
                raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
            if headers is not None:
                captured = ctx.capture(f"transport-{call_id}-{attempt}", {"service": "opencode_go",
                                       "endpoint": self.config.endpoint, "headers": headers})
                if captured["status"] == "write_failed":
                    raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
            ctx.reserve_call(call_id, attempt, upper_usd, self.config.max_run_usd, self.config.max_run_calls,
                             input_usd_per_million=self.config.input_usd_per_million,
                             output_usd_per_million=self.config.output_usd_per_million,
                             unit_id=unit_id, input_hash=metadata["input_hash"])
            started = time.monotonic()
            retryable = False
            with ctx.call_span(metadata) as span:
                metadata.update(trace_id=format(span.get_span_context().trace_id, "032x"),
                                span_id=format(span.get_span_context().span_id, "016x"))
                ctx.call_event("call.started", metadata)
                try:
                    if headers is None:
                        response = self.transport(self.config.endpoint, payload, secret, self.config.timeout_seconds)
                    else:
                        response = self.transport(self.config.endpoint, payload, secret, self.config.timeout_seconds, headers=headers)
                    raw_body = response if isinstance(response, bytes) else None
                    if isinstance(response, bytes):
                        if len(response) > 4 * 1024 * 1024:
                            ctx.capture(f"response-error-{call_id}-{attempt}", redact_secret({"body_status": "truncated", "body": response[:65536].decode("utf-8", errors="replace")}, secret), partial=True)
                            raise ProviderFailure("PROVIDER_RESPONSE_TOO_LARGE")
                        try:
                            response = json.loads(response)
                        except (ValueError, UnicodeError):
                            invalid_response = redact_secret({"body_status": "invalid_json", "body": response.decode("utf-8", errors="replace")}, secret)
                            billing = _go_billing(None) if headers is not None else None
                            _capture_response(ctx, f"response-error-{call_id}-{attempt}", invalid_response, billing,
                                              call_id=call_id, attempt=attempt, response_sha256=digest(invalid_response),
                                              secret=secret, rejected_response=True)
                            if billing is not None:
                                raise ProviderFailure("PROVIDER_BILLING_INVALID") from None
                            raise ProviderFailure("PROVIDER_SCHEMA_INVALID") from None
                    clean = redact_secret(response, secret)
                    if headers is not None:
                        try:
                            json.dumps(clean, allow_nan=False)
                        except (ValueError, TypeError, OverflowError):
                            invalid_response = {"body_status": "noncanonical_json", "body": redact_secret(
                                raw_body.decode("utf-8", errors="replace") if raw_body is not None else
                                json.dumps(clean, ensure_ascii=False, default=lambda _: "[non-JSON value]"), secret)}
                            billing = _go_billing(clean)
                            billing.update(billing_status="invalid", policy_error_code="PROVIDER_BILLING_INVALID")
                            _capture_response(ctx, f"response-error-{call_id}-{attempt}", invalid_response, billing,
                                              call_id=call_id, attempt=attempt, response_sha256=digest(invalid_response),
                                              secret=secret, rejected_response=True)
                            raise ProviderFailure("PROVIDER_BILLING_INVALID")
                    billing = _go_billing(clean) if headers is not None else None
                    captured = _capture_response(ctx, f"response-{call_id}-{attempt}", clean, billing,
                                                 call_id=call_id, attempt=attempt,
                                                 response_sha256=digest(clean) if billing is not None else "", secret=secret)
                    if captured["status"] == "write_failed":
                        raise ProviderFailure("DIAGNOSTICS_CAPTURE_FAILED")
                    usage = (response.get("usage") or {}) if isinstance(response, dict) else {}
                    if not isinstance(usage, dict):
                        usage = {}
                    for remote, local in [("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")]:
                        value = usage.get(remote)
                        metadata[local] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                    metadata["usage_status"] = "known" if _valid_usage(metadata["input_tokens"], metadata["output_tokens"], metadata["total_tokens"]) else "unknown"
                    if metadata["usage_status"] == "known":
                        estimate = usage_cost_estimate(metadata["input_tokens"], metadata["output_tokens"],
                                                       self.config.input_usd_per_million, self.config.output_usd_per_million)
                        if estimate <= Decimal("1e308"):
                            metadata["cost"] = float(estimate)
                    if isinstance(clean, dict) and isinstance(clean.get("id"), str):
                        metadata["provider_request_id"] = clean["id"]
                    metadata["output_hash"] = digest(clean)
                    settlement = None
                    try:
                        if metadata["usage_status"] == "known":
                            settlement = ctx.settle_call_usage(call_id, attempt, metadata["input_tokens"], metadata["output_tokens"],
                                metadata["total_tokens"], response_sha256=metadata["output_hash"], unit_id=unit_id)
                    finally:
                        # A rejected billing policy must also latch when usage
                        # settlement/event persistence fails. Never pretend that
                        # an unsuccessful settlement was committed.
                        if billing is not None:
                            _record_go_billing(ctx, billing, call_id=call_id, attempt=attempt,
                                               response_sha256=metadata["output_hash"], secret=secret)
                    if billing is not None and billing["policy_error_code"]:
                        raise ProviderFailure(billing["policy_error_code"])
                    if settlement is not None and settlement["exceeds_reserved_upper"]:
                        raise ProviderFailure("PROVIDER_USAGE_EXCEEDS_RESERVATION")
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
                except (ProviderUsageSettlementError, ProviderBudgetPersistenceError, ProviderBudgetLedgerError) as error:
                    code = error.code
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
        validate_writing_context(section, requirements)
        try:
            result = ProposalContent.model_validate(self.proposals[section["section_id"]])
        except (KeyError, ValidationError):
            raise ProviderFailure("PREPARED_PROPOSAL_MISSING_OR_INVALID") from None
        validate_writing_output(section, result, requirements)
        ctx.capture("prepared-proposal-" + section["section_id"], result.model_dump(mode="json"))
        return result
