"""Historical trace fixture only. Contains NO recognizer or subprocess execution.

Emit the retired v63 event/capture format from injected synthetic bytes so the
read-only inspector keeps diagnosing old runs after the local engine is removed.
"""
import hashlib
import json
from pathlib import Path
import time
import uuid

from PIL import Image

from company_bid.storage import digest

ADAPTER_VERSION = 'material-local-ocr.v1'
FINGERPRINT_SCHEMA = 'material.windows-ocr.engine.v1'
RESPONSE_SCHEMA = 'material.windows-ocr.response.v1'
_SCRIPT = Path(__file__)


class MaterialLocalOCRFailure(RuntimeError):
    def __init__(self, code, *, details=None):
        self.code, self.details = code, details or {}
        super().__init__(code)


def _run_powershell(*args):
    raise AssertionError('This fixture only accepts injected synthetic responses')


def _capture(ctx, name, value):
    if ctx.capture(name, value)['status'] != 'captured':
        raise MaterialLocalOCRFailure('MATERIAL_OCR_CAPTURE_REQUIRED')


def recognize(ctx, *, image_path, image_sha256, identity, options):
    base = {'provider': 'windows_ocr', 'operation': 'material_ocr', 'usage_status': 'local_no_api_charge', 'cost': 0}
    with ctx._observer.span('ocr.local.preflight', {**base, **ctx._metadata()}):
        try:
            _capture(ctx, 'ocr-preflight-request-fixture', {'identity': identity})
            if hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != image_sha256:
                raise MaterialLocalOCRFailure('MATERIAL_OCR_IMAGE_HASH_MISMATCH')
            with Image.open(image_path) as image:
                width, height = image.size
            call_id = uuid.uuid4().hex
            request = {'image_path': image_path, 'image_sha256': image_sha256,
                       'expected_engine_fingerprint': options['expected_engine_fingerprint'],
                       'width': width, 'height': height}
            _capture(ctx, 'ocr-request-' + call_id, {'call_id': call_id, 'identity': identity, 'request': request})
        except MaterialLocalOCRFailure as error:
            ctx.capture('ocr-preflight-error-fixture', {'error_code': error.code})
            ctx.call_event('material_ocr.preflight.failed', {**base, 'error_code': error.code})
            raise
    metadata = {**base, 'call_id': call_id, 'call_attempt': 1, 'input_hash': digest(request)}
    started = time.monotonic()
    with ctx._observer.span('ocr.local.call', {**metadata, **ctx._metadata()}) as span:
        context = span.get_span_context()
        metadata.update(trace_id=format(context.trace_id, '032x'), span_id=format(context.span_id, '016x'))
        ctx.call_event('ocr.local.started', metadata)
        try:
            raw = _run_powershell('recognize', request, 1)
            metadata['output_hash'] = hashlib.sha256(raw['stdout']).hexdigest()
            _capture(ctx, 'ocr-response-' + call_id, {'call_id': call_id, 'stdout_status': 'captured',
                'stdout': raw['stdout'].decode('utf-8'), 'output_sha256': metadata['output_hash']})
            value = json.loads(raw['stdout'])
            result = {**value, 'call_id': call_id}
            _capture(ctx, 'ocr-validation-' + call_id, {'status': 'passed', 'call_id': call_id,
                'validated_output': result, 'validated_output_hash': digest(result)})
            metadata.update(elapsed_ms=round((time.monotonic() - started) * 1000), validation_status='passed')
            span.set_attributes(metadata)
            ctx.call_event('ocr.local.completed', metadata)
            return result
        except MaterialLocalOCRFailure as error:
            _capture(ctx, 'ocr-error-' + call_id, {'status': 'failed', 'call_id': call_id, 'error_code': error.code})
            metadata.update(error_code=error.code, elapsed_ms=round((time.monotonic() - started) * 1000),
                            validation_status='failed')
            span.set_attributes(metadata)
            ctx.call_event('ocr.local.failed', metadata)
            raise
