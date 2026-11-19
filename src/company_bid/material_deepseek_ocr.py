"""One captured DeepSeek OCR attempt; no local recognizer or fallback.

The HTTP child receives its credential on private stdin. Its parent enforces a
wall-clock deadline and reaps it, including DNS/slow streaming failures. Page
fees and token usage are separate; the shared USD guard keeps its reservation.
"""
from __future__ import annotations

import base64
import copy
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import uuid

from PIL import Image

from .provider import redact_secret
from .storage import digest, json_bytes

ADAPTER_VERSION = "material-deepseek-ocr.v1"
RESPONSE_SCHEMA = "material.deepseek-ocr.response.v1"
ENDPOINT = "https://api.deepseek.com/v1/tools/ocr"
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/guides/ocr/"
PAGE_PRICE_CNY = Decimal("0.0015")
MAX_IMAGE_BYTES = 20_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_IMAGE_DIMENSION = 10000
MAX_IMAGE_PIXELS = 50_000_000
_SHA = re.compile(r"[0-9a-f]{64}")
_PROVIDER_ID = re.compile(r"ocr-[A-Za-z0-9_-]{1,150}")


class MaterialDeepSeekOCRFailure(RuntimeError):
    def __init__(self, code, *, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _require(value, code):
    if not value:
        raise MaterialDeepSeekOCRFailure(code)


def _options(options, *, require_expected=False):
    allowed = {"engine", "endpoint", "purpose", "auth_env", "timeout_seconds", "max_attempts",
               "max_run_calls", "max_run_usd", "reserve_usd_per_page", "expected_engine_fingerprint"}
    _require(isinstance(options, dict) and not set(options) - allowed, "MATERIAL_OCR_OPTIONS_INVALID")
    _require(options.get("engine") == "deepseek_ocr", "MATERIAL_OCR_ENGINE_UNSUPPORTED")
    _require(options.get("endpoint", ENDPOINT) == ENDPOINT, "MATERIAL_OCR_ENDPOINT_INVALID")
    purpose = options.get("purpose", "rich_layout_recognition")
    _require(purpose in {"rich_layout_recognition", "text_recognition"}, "MATERIAL_OCR_PURPOSE_INVALID")
    auth_env = options.get("auth_env", "COMPANY_BID_DEEPSEEK_API_KEY")
    _require(isinstance(auth_env, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", auth_env), "MATERIAL_OCR_KEY_ENV_INVALID")
    timeout = options.get("timeout_seconds", 200)
    _require(type(timeout) in {int, float} and math.isfinite(timeout) and 1 <= timeout <= 210,
             "MATERIAL_OCR_TIMEOUT_INVALID")
    _require(type(options.get("max_attempts", 1)) is int and options.get("max_attempts", 1) == 1,
             "MATERIAL_OCR_AUTOMATIC_RETRY_DISABLED")
    calls = options.get("max_run_calls", 100)
    _require(type(calls) is int and 1 <= calls <= 1000, "MATERIAL_OCR_BUDGET_INVALID")
    try:
        reserve = Decimal(str(options.get("reserve_usd_per_page", "0.0015")))
        cap = Decimal(str(options.get("max_run_usd", "0.15")))
        _require(reserve.is_finite() and cap.is_finite() and reserve >= Decimal("0.0015") and cap >= reserve,
                 "MATERIAL_OCR_BUDGET_INVALID")
    except (ValueError, TypeError, InvalidOperation):
        raise MaterialDeepSeekOCRFailure("MATERIAL_OCR_BUDGET_INVALID") from None
    expected = options.get("expected_engine_fingerprint")
    _require(not require_expected or expected is not None, "MATERIAL_OCR_FINGERPRINT_REQUIRED")
    _require(expected is None or isinstance(expected, dict) or isinstance(expected, str) and _SHA.fullmatch(expected),
             "MATERIAL_OCR_FINGERPRINT_INVALID")
    return {"engine": "deepseek_ocr", "endpoint": ENDPOINT, "purpose": purpose, "auth_env": auth_env,
            "timeout_seconds": timeout, "max_attempts": 1, "max_run_calls": calls,
            "max_run_usd": str(cap), "reserve_usd_per_page": str(reserve),
            "expected_engine_fingerprint": copy.deepcopy(expected)}


def engine_fingerprint(options):
    config = _options(options)
    result = {"schema_version": "material.deepseek-ocr.engine.v1", "adapter_version": ADAPTER_VERSION,
              "engine": "deepseek_ocr", "endpoint": ENDPOINT, "purpose": config["purpose"],
              "api_contract": "deepseek-tools-ocr-20261116", "provider_model_version": "not_reported",
              "pricing_date": "2026-11-16", "page_price_cny": str(PAGE_PRICE_CNY),
              "pricing_source_url": PRICE_SOURCE, "max_image_dimension": MAX_IMAGE_DIMENSION,
              "local_fallback": False}
    expected = config["expected_engine_fingerprint"]
    _require(expected is None or expected == result or expected == digest(result), "MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH")
    return result


def _image(path, image_sha256):
    _require(isinstance(image_sha256, str) and _SHA.fullmatch(image_sha256), "MATERIAL_OCR_IMAGE_HASH_INVALID")
    try:
        path = Path(path).resolve(strict=True)
        _require(path.is_file() and 0 < path.stat().st_size <= MAX_IMAGE_BYTES, "MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED")
        content = path.read_bytes()
        _require(0 < len(content) <= MAX_IMAGE_BYTES, "MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED")
        _require(hashlib.sha256(content).hexdigest() == image_sha256, "MATERIAL_OCR_IMAGE_HASH_MISMATCH")
        with Image.open(BytesIO(content)) as image:
            _require(image.format == "PNG" and getattr(image, "n_frames", 1) == 1, "MATERIAL_OCR_IMAGE_FORMAT_INVALID")
            width, height = image.size
            _require(0 < max(width, height) <= MAX_IMAGE_DIMENSION and width * height <= MAX_IMAGE_PIXELS,
                     "MATERIAL_OCR_IMAGE_DIMENSION_LIMIT_EXCEEDED")
            image.verify()
        return path, content, width, height
    except MaterialDeepSeekOCRFailure:
        raise
    except Exception:
        raise MaterialDeepSeekOCRFailure("MATERIAL_OCR_IMAGE_UNREADABLE") from None


def multipart(image, image_sha256, purpose="rich_layout_recognition"):
    """Same wire as the authenticated comparison; no credential in the body."""
    _require(hashlib.sha256(image).hexdigest() == image_sha256, "MATERIAL_OCR_IMAGE_HASH_MISMATCH")
    _require(purpose in {"rich_layout_recognition", "text_recognition"}, "MATERIAL_OCR_PURPOSE_INVALID")
    boundary = "companybidocrcomparison" + image_sha256[:24]
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode("ascii")
    return head + image + f"\r\n--{boundary}--\r\n".encode("ascii"), "multipart/form-data; boundary=" + boundary


def _request_once(body, media, secret, timeout):
    # This runs only in the bounded HTTP child, never as a fallback path.
    connection = http.client.HTTPSConnection("api.deepseek.com", timeout=min(20, timeout), context=ssl.create_default_context())
    try:
        connection.connect()
        connection.sock.settimeout(timeout)
        connection.request("POST", "/v1/tools/ocr", body=body,
                           headers={"Authorization": "Bearer " + secret, "Content-Type": media,
                                    "Content-Length": str(len(body))})
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        headers = {key: response.getheader(key) for key in ("content-type", "x-request-id", "request-id", "retry-after")
                   if response.getheader(key)}
        return response.status, headers, data
    finally:
        connection.close()


def _transport(body, media, secret, timeout):
    """Hard deadline includes startup, DNS, upload, keepalive and body reads."""
    kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "shell": False, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startup
    request = json_bytes({"body": base64.b64encode(body).decode("ascii"), "media": media,
                          "credential": secret, "timeout": timeout})
    child = subprocess.Popen([sys.executable, "-m", "company_bid.material_deepseek_ocr", "--transport"], **kwargs)
    try:
        output, _stderr = child.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise MaterialDeepSeekOCRFailure("MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN", details={"child_killed": True,
            "server_completion": "unknown", "automatic_retry": False}) from None
    _require(child.returncode == 0 and len(output) <= (MAX_RESPONSE_BYTES + 1) * 2 + 8192,
             "MATERIAL_OCR_TRANSPORT_OUTCOME_UNKNOWN")
    try:
        value = json.loads(output)
        if "error" in value:
            raise MaterialDeepSeekOCRFailure("MATERIAL_OCR_TRANSPORT_OUTCOME_UNKNOWN",
                                            details={"transport_error_class": value["error"]})
        return value["status"], value["headers"], base64.b64decode(value["body"], validate=True)
    except MaterialDeepSeekOCRFailure:
        raise
    except Exception:
        raise MaterialDeepSeekOCRFailure("MATERIAL_OCR_TRANSPORT_OUTCOME_UNKNOWN") from None


def _transport_child():
    try:
        request = json.loads(sys.stdin.buffer.read(MAX_IMAGE_BYTES * 2 + 8192))
        body = base64.b64decode(request["body"], validate=True)
        _require(len(body) <= MAX_IMAGE_BYTES + 2048, "MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED")
        status, headers, raw = _request_once(body, request["media"], request["credential"], request["timeout"])
        output = {"status": status, "headers": headers, "body": base64.b64encode(raw).decode("ascii")}
    except Exception as exc:
        output = {"error": type(exc).__name__}  # Never print message, credential or URL exception.
    sys.stdout.buffer.write(json_bytes(output))


def validate_provider_response(payload, purpose):
    _require(isinstance(payload, dict) and "error" not in payload, "MATERIAL_OCR_BODY_ERROR")
    _require(isinstance(payload.get("id"), str) and _PROVIDER_ID.fullmatch(payload["id"]) and
             payload.get("object") == "ocr.result" and type(payload.get("created")) is int and payload["created"] >= 0
             and payload.get("purpose") == purpose, "MATERIAL_OCR_RESPONSE_SCHEMA_INVALID")
    pages = payload.get("pages")
    _require(type(payload.get("page_count")) is int and payload["page_count"] == 1 and isinstance(pages, list)
             and len(pages) == 1 and isinstance(pages[0], dict) and type(pages[0].get("index")) is int
             and pages[0]["index"] == 0 and isinstance(pages[0].get("text"), str), "MATERIAL_OCR_PAGE_COVERAGE_INVALID")
    _require(pages[0].get("finish_reason") == "stop", "MATERIAL_OCR_OUTPUT_INCOMPLETE")
    usage = payload.get("usage")
    _require(isinstance(usage, dict) and all(type(usage.get(k)) is int and 0 <= usage[k] <= 100_000_000
             for k in ("input_tokens", "output_tokens", "total_tokens")) and
             usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"], "MATERIAL_OCR_USAGE_INVALID")


def returned_billable_pages(status, payload, purpose):
    if (status != 200 or not isinstance(payload, dict) or "error" in payload or payload.get("object") != "ocr.result"
            or payload.get("purpose") != purpose or not isinstance(payload.get("id"), str)
            or not _PROVIDER_ID.fullmatch(payload["id"])):
        return None
    count, pages = payload.get("page_count"), payload.get("pages")
    if (type(count) is int and 1 <= count <= 1000 and isinstance(pages, list) and len(pages) == count and
            all(isinstance(p, dict) and type(p.get("index")) is int and p["index"] == i and
                p.get("finish_reason") in ("stop", "length") and isinstance(p.get("text"), str) for i, p in enumerate(pages))):
        return count
    return None


def parse_response(payload, *, width, height, image_sha256, identity, options, call_id, numeric_reference=None):
    """Shared normalization for live or authenticated recorded responses."""
    from .material_ocr_quality import assess_ocr_response

    config = _options(options, require_expected=True)
    fingerprint = engine_fingerprint(config)
    validate_provider_response(payload, config["purpose"])
    value = assess_ocr_response(payload, width=width, height=height, identity=identity, numeric_reference=numeric_reference)
    _require(value.get("image_sha256") == image_sha256 and value.get("width") == width and value.get("height") == height,
             "MATERIAL_OCR_RESPONSE_IMAGE_INVALID")
    return {**value, "quality_schema_version": value["schema_version"], "schema_version": RESPONSE_SCHEMA,
            "engine_fingerprint": fingerprint, "call_id": call_id, "provider_request_id": payload["id"],
            "provider": "deepseek_official_ocr", "provider_model_version": "not_reported",
            "purpose": config["purpose"], "provider_finish_reason": "stop", "usage": copy.deepcopy(payload["usage"])}


def _capture(ctx, name, payload, secret=""):
    _require(ctx.capture(name, redact_secret(payload, secret))["status"] == "captured", "MATERIAL_OCR_DIAGNOSTICS_REQUIRED")


def _failure(exc, fallback):
    code = getattr(exc, "code", None)
    return MaterialDeepSeekOCRFailure(code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", code)
                                     else fallback, details=getattr(exc, "details", {}))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate OCR response key.")
        result[key] = value
    return result


def recognize(ctx, *, image_path, image_sha256, identity, options, numeric_reference=None,
              purchase_key=None, operation="material_ocr", validate_result=None):
    """One request, durable reservation and capture; errors always propagate."""
    preflight = uuid.uuid4().hex
    tender = operation == "tender_ocr"
    base = {"provider": "deepseek_official_ocr", "operation": "tender_ocr" if tender else "material_ocr"}
    secret = ""
    try:
        _require(tender and isinstance(purchase_key, str) and _SHA.fullmatch(purchase_key) and callable(validate_result)
                 or operation == "material_ocr" and purchase_key is None and validate_result is None,
                 "MATERIAL_OCR_OPERATION_INVALID")
        config = _options(options, require_expected=True)
        fingerprint = engine_fingerprint(config)
        _require(isinstance(identity, dict) and len(json_bytes(identity)) <= 65536
                 and identity.get("image_sha256") == image_sha256, "MATERIAL_OCR_IDENTITY_INVALID")
        secret = os.environ.get(config["auth_env"], "")
        _require(15 < len(secret) < 512 and not any(c.isspace() for c in secret), "MATERIAL_OCR_CREDENTIAL_MISSING")
        _capture(ctx, "ocr-preflight-" + preflight, {"identity": identity, "options": config,
                 "image_sha256": image_sha256, "numeric_reference": numeric_reference}, secret)
        path, image, width, height = _image(image_path, image_sha256)
        body, media = multipart(image, image_sha256, config["purpose"])
        _require(secret.encode("utf-8") not in body, "MATERIAL_OCR_INPUT_CREDENTIAL_CONFLICT")
        body_ref = ctx.store_bytes("ocr-request-body.bin", body, media)
        call_id = uuid.uuid4().hex
        request = {"call_id": call_id, "endpoint": ENDPOINT, "purpose": config["purpose"], "identity": identity,
                   "request_body_ref": body_ref, "body_sha256": hashlib.sha256(body).hexdigest(),
                   "image_sha256": image_sha256, "image_dimensions": [width, height], "expected_pages": 1,
                   "engine_fingerprint": fingerprint, "automatic_retry": False, "local_fallback": False}
        if tender:
            request.update(operation=operation, purchase_key=purchase_key)
        _capture(ctx, "request-" + call_id, request, secret)
        if tender:
            from .material_ocr_budget import reserve_tender_ocr

            reserve_tender_ocr(ctx, purchase_key, call_id, config, input_hash=request["body_sha256"])
        else:
            ctx.reserve_call(call_id, 1, config["reserve_usd_per_page"], config["max_run_usd"], config["max_run_calls"],
                             input_hash=request["body_sha256"])
    except Exception as exc:
        failure = _failure(exc, "MATERIAL_OCR_PREFLIGHT_FAILED")
        try:
            _capture(ctx, "ocr-preflight-error-" + preflight, {"error_code": failure.code,
                     "error_class": type(exc).__name__, "details": failure.details}, secret)
        except MaterialDeepSeekOCRFailure:
            pass
        ctx.call_event(base["operation"] + ".preflight.failed", {**base, "phase": "preflight", "error_code": failure.code})
        raise failure from None

    metadata = {**base, "call_id": call_id, "call_attempt": 1, "input_hash": request["body_sha256"], "usage_status": "unknown"}
    start = time.monotonic()
    with ctx.call_span(metadata) as span:
        context = span.get_span_context()
        metadata.update(trace_id=format(context.trace_id, "032x"), span_id=format(context.span_id, "016x"))
        ctx.call_event("call.started", metadata)
        status, raw, payload, fee = None, None, None, None
        try:
            status, headers, raw = _transport(body, media, secret, config["timeout_seconds"])
            _require(type(status) is int and isinstance(headers, dict) and isinstance(raw, bytes), "MATERIAL_OCR_TRANSPORT_CONTRACT_INVALID")
            metadata["output_hash"] = hashlib.sha256(raw).hexdigest()
            if len(raw) <= MAX_RESPONSE_BYTES:
                try:
                    payload = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                                         object_pairs_hook=_unique_json_object if tender else None)
                except (ValueError, UnicodeError):
                    pass
            # A JSON \u escape must not bypass literal credential redaction.
            secret_echo = secret.encode("utf-8") in raw or payload is not None and secret in json_bytes(payload).decode("utf-8")
            captured_bytes = (json_bytes(redact_secret(payload, secret)) if payload is not None else
                              redact_secret(raw.decode("utf-8", errors="replace"), secret).encode("utf-8")) if secret_echo else raw
            raw_ref = ctx.store_bytes("ocr-response-raw.bin", captured_bytes)
            fee = returned_billable_pages(status, payload, config["purpose"])
            billing = {"provider_returned_pages": fee, "official_page_fee_estimate_cny": str(PAGE_PRICE_CNY * fee) if fee is not None else None,
                       "fee_status": "estimated_returned_pages" if fee is not None else "unknown_reservation_retained",
                       "reserve_usd_retained": config["reserve_usd_per_page"], "usd_is_fx_conversion": False,
                       "token_usage_is_billing": False, "provider_invoice": False, "pricing_source_url": PRICE_SOURCE}
            _capture(ctx, "response-" + call_id, {"call_id": call_id, "http_status": status,
                     "headers": headers, "raw_response_ref": raw_ref, "raw_received_sha256": metadata["output_hash"],
                     "raw_capture_status": "credential_echo_redacted" if secret_echo else "truncated" if len(raw) > MAX_RESPONSE_BYTES else "captured",
                     "response": payload, "billing": billing}, secret)
            _require(not secret_echo, "MATERIAL_OCR_CREDENTIAL_ECHO_REJECTED")
            _require(len(raw) <= MAX_RESPONSE_BYTES, "MATERIAL_OCR_RESPONSE_LIMIT_EXCEEDED")
            _require(status == 200, "MATERIAL_OCR_HTTP_ERROR")
            _require(payload is not None, "MATERIAL_OCR_RESPONSE_NOT_JSON")
            provider_id = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(provider_id, str) and _PROVIDER_ID.fullmatch(provider_id):
                metadata["provider_request_id"] = provider_id
            result = parse_response(payload, width=width, height=height, image_sha256=image_sha256,
                                    identity=identity, options=config, call_id=call_id, numeric_reference=numeric_reference)
            final_path, final_bytes, _, _ = _image(path, image_sha256)
            _require(final_path == path and final_bytes == image, "MATERIAL_OCR_IMAGE_CHANGED_DURING_CALL")
            result.update(billing=billing, response_raw_ref=raw_ref, response_raw_sha256=metadata["output_hash"],
                          actual_external_call=True, response_reused=False, local_fallback=False)
            if tender:
                result["tender_ocr_response"] = validate_result(result)
            _capture(ctx, "validation-" + call_id, {"call_id": call_id, "validation_status": "passed",
                     "validated_output": result, "validated_output_hash": digest(result)}, secret)
            metadata.update(http_status=status, elapsed_ms=round((time.monotonic() - start) * 1000),
                            validation_status="passed", usage_status="not_token_billed")
            span.set_attributes(metadata)
            ctx.call_event("call.completed", metadata)
            return result
        except Exception as exc:
            failure = _failure(exc, "MATERIAL_OCR_ADAPTER_FAILED")
            try:
                _capture(ctx, "validation-" + call_id, {"call_id": call_id, "validation_status": "failed",
                         "error_code": failure.code, "error_class": type(exc).__name__, "details": failure.details,
                         "http_status": status, "raw_response_available": raw is not None,
                         "fee_pages": fee, "reserve_usd_retained": config["reserve_usd_per_page"],
                         "server_completion": "unknown" if status is None else "response_received",
                         "automatic_retry": False, "local_fallback": False}, secret)
            except MaterialDeepSeekOCRFailure as capture_error:
                failure = capture_error
            metadata.update(elapsed_ms=round((time.monotonic() - start) * 1000), error_code=failure.code,
                            validation_status="failed", usage_status="unknown" if fee is None else "not_token_billed")
            if status is not None:
                metadata["http_status"] = status
            span.set_attributes(metadata)
            ctx.call_event("call.failed", metadata)
            raise failure from None


if __name__ == "__main__":
    if sys.argv[1:] != ["--transport"]:
        raise SystemExit(2)
    _transport_child()
