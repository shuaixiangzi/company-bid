"""Explicit company-proof quality failures, never successful OCR or facts."""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata

from .material_content_index import MaterialContentFailure, _require
from .material_source_review import validate_material_source_review
from .storage import digest

VERSION = 'material-ocr-disposition.v1'
QUALITY_FAILURES = frozenset({
    'MATERIAL_OCR_OUTPUT_INCOMPLETE', 'MATERIAL_OCR_REPETITIVE_OUTPUT',
    'MATERIAL_OCR_REGION_LIMIT', 'MATERIAL_OCR_TEXT_LIMIT', 'MATERIAL_OCR_CONTROL_CHARACTER_INVALID',
    'MATERIAL_OCR_COORDINATES_INVALID', 'MATERIAL_OCR_PAGE_COVERAGE_INVALID',
    'MATERIAL_OCR_HTML_UNSAFE', 'MATERIAL_OCR_MARKUP_INVALID', 'MATERIAL_OCR_TABLE_INVALID',
    'MATERIAL_OCR_TABLE_LIMIT', 'MATERIAL_OCR_TABLE_SPAN_INVALID', 'MATERIAL_OCR_TABLE_SPAN_OVERLAP',
    'MATERIAL_OCR_TABLE_GRID_INCOMPLETE', 'MATERIAL_OCR_NUMERIC_LIMIT',
    'MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH',
})


def validate_dispositions(mapping):
    _require(isinstance(mapping, dict) and len(mapping) <= 2000, 'MATERIAL_OCR_DISPOSITION_CONFIG_INVALID')
    for image_hash, spec in mapping.items():
        _require(isinstance(image_hash, str) and re.fullmatch(r'[0-9a-f]{64}', image_hash)
                 and isinstance(spec, dict) and set(spec) in (
                     {'expected_error_code'}, {'expected_error_code', 'source_review', 'source_review_evidence'}),
                 'MATERIAL_OCR_DISPOSITION_CONFIG_INVALID')
        _require(isinstance(spec['expected_error_code'], str) and spec['expected_error_code'] in QUALITY_FAILURES,
                 'MATERIAL_OCR_DISPOSITION_ERROR_NOT_ALLOWED')
        if 'source_review' in spec:
            validate_material_source_review(spec['source_review'])
            _require(isinstance(spec['source_review_evidence'], dict), 'MATERIAL_OCR_DISPOSITION_REVIEW_EVIDENCE_INVALID')
        _require(spec['expected_error_code'] != 'MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH' or 'source_review' in spec,
                 'MATERIAL_OCR_DISPOSITION_SOURCE_REVIEW_REQUIRED')
    return copy.deepcopy(mapping)


def authenticate_review(ctx, spec, identity, provenance):
    if 'source_review' not in spec:
        return None
    review = validate_material_source_review(spec['source_review'])
    _require(review['identity'] == {k: identity[k] for k in
        ('source_id', 'source_sha256', 'selection_sha256', 'image_sha256')}, 'MATERIAL_OCR_DISPOSITION_REVIEW_SOURCE_CHANGED')
    _require(all(review['ocr'][key] == provenance[origin] for key, origin in
        [('run_id', 'origin_run_id'), ('node_key', 'origin_node_key'), ('unit_id', 'origin_unit_id'),
         ('response_sha256', 'raw_response_sha256')]), 'MATERIAL_OCR_DISPOSITION_REVIEW_RESPONSE_CHANGED')
    _require(review['ocr']['status'] == 'succeeded' and review['ocr']['error_code'] is None,
             'MATERIAL_OCR_DISPOSITION_REVIEW_RESPONSE_CHANGED')
    ref = spec['source_review_evidence']
    _require(isinstance(ref.get('path'), str) and re.fullmatch(r'runs/[0-9a-f]{32}/artifacts/[^/\\]+', ref['path']),
             'MATERIAL_OCR_DISPOSITION_REVIEW_EVIDENCE_INVALID')
    raw = ctx.read_artifact(ref)
    _require(hashlib.sha256(raw).hexdigest() == review['reviewer']['evidence_sha256'],
             'MATERIAL_OCR_DISPOSITION_REVIEW_EVIDENCE_CHANGED')
    return review


def recorded_quality_disposition(ctx, *, binding, identity, dimensions, provider, reference, specification):
    """Authenticate successful historical bytes, retain today's failed check.

    Outer unit records a disposition. Its nested validation unit remains failed;
    successful resume reuses the disposition and never reruns that validation.
    """
    from .material_ocr_replay import replay_material_ocr
    from .material_ocr_quality import assess_ocr_response, MaterialOCRQualityFailure

    validate_dispositions({identity['image_sha256']: specification})
    raw, provenance = replay_material_ocr(ctx, binding=binding, image_sha256=identity['image_sha256'],
        identity=identity, purpose=provider.get('purpose', 'rich_layout_recognition'))
    review = authenticate_review(ctx, specification, identity, provenance)
    validation_input = {'identity': identity, 'provenance': provenance, 'reference': reference,
                        'expected_error_code': specification['expected_error_code']}

    def validate():
        return assess_ocr_response(raw, width=dimensions[0], height=dimensions[1], identity=identity,
                                   numeric_reference=reference)
    try:
        ctx.run_unit('material-ocr-validation-' + digest(identity)[:24], validation_input, validate)
    except MaterialOCRQualityFailure as error:
        _require(error.code == specification['expected_error_code'], 'MATERIAL_OCR_DISPOSITION_ERROR_CHANGED')
        code = error.code
        if code == 'MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH':
            field = review['field']
            checks = [c for c in reference['checks'] if c['field_id'] == error.details.get('field_id')]
            normalize = lambda text: re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()
            _require(len(checks) == 1 and field['disposition'] == 'machine_error'
                     and normalize(checks[0]['expected_text']) == normalize(field['source_text']),
                     'MATERIAL_OCR_DISPOSITION_REVIEW_REFERENCE_CHANGED')
            if 'table_index' in checks[0]:
                _require(field['scope'].get('table_cell') == {k: checks[0][k] for k in ('table_index', 'row', 'column')},
                         'MATERIAL_OCR_DISPOSITION_REVIEW_REFERENCE_CHANGED')
    else:
        raise MaterialContentFailure('MATERIAL_OCR_DISPOSITION_EXPECTED_FAILURE_MISSING')
    result = {'schema_version': VERSION, 'identity': copy.deepcopy(identity), 'identity_sha256': digest(identity),
              'status': 'reading_failed_pending_review', 'error_code': code,
              'origin_run_id': provenance['origin_run_id'], 'origin_unit_id': provenance['origin_unit_id'],
              'validation_run_id': ctx.run_id, 'provenance': provenance, 'source_review': review,
              'source_review_authenticated': review is not None, 'machine_text_adopted': False,
              'material_presence': 'present_but_unread_or_conflicting', 'qualification_result': 'not_determined',
              'reading_completeness': 'not_established', 'automatic_assembly_allowed': False}
    captured = ctx.capture('material-ocr-failure-disposition', result)
    _require(captured['status'] == 'captured', 'MATERIAL_OCR_DISPOSITION_CAPTURE_REQUIRED')
    ctx.call_event('material.ocr_failure.disposed', {'operation': 'material_ocr_failure_disposition',
        'error_code': code, 'validation_status': 'failed', 'reused_from_run_id': provenance['origin_run_id'],
        'input_hash': digest(identity), 'output_hash': digest(result)})
    return result
