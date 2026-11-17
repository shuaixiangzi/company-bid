"""Failure disposition authenticates actual adapter evidence, never fallback."""
import copy
import hashlib
import json
import socket

from PIL import Image
import pytest

from company_bid import material_deepseek_ocr as ocr
from company_bid.material_ocr_failure import authenticate_failed_material_ocr, validate_failed_sources
from company_bid.material_source_review import build_material_source_review
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes


def snapshot(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}


@pytest.fixture
def origin(tmp_path, monkeypatch, request):
    mode = getattr(request, 'param', 'length')
    path = tmp_path / 'private.png'
    Image.new('RGB', (80, 60), 'white').save(path)
    image_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = {'source_id': 'SOURCE', 'source_sha256': 'a' * 64, 'format': 'pdf_pages', 'target': 1,
        'selection_sha256': 'b' * 64, 'crop_box_normalized': None, 'rotate_clockwise': 0,
        'render_dpi': 160, 'group_bindings': [], 'image_sha256': image_sha}
    uid = 'material-ocr-' + digest(identity)[:24]
    options = {'engine': 'deepseek_ocr', 'purpose': 'rich_layout_recognition', 'timeout_seconds': 5,
               'max_attempts': 1, 'max_run_calls': 1, 'max_run_usd': '.0015'}
    options['expected_engine_fingerprint'] = ocr.engine_fingerprint(options)
    config = {'debug_capture': True, 'planning': {'semantic_material_recall': {
        'read_selected_originals': True, 'original_ocr': copy.deepcopy(options)}}}
    review = None
    if mode == 'numeric':
        reference = {'image_sha256': image_sha, 'checks': [
            {'field_id': 'area', 'region_index': 0, 'kind': 'measure', 'expected_text': '740 m²'}]}
        review = {**{k: identity[k] for k in ('source_id', 'source_sha256', 'selection_sha256', 'image_sha256')},
            'review_method': 'agent_source_image_review', 'review_id': 'synthetic-numeric-review',
            'reference': reference, 'reference_sha256': digest(reference)}
        config['planning']['semantic_material_recall']['original_ocr']['source_checks'] = {image_sha: review}
    inputs = {'version': 'material-ocr-index.v3', 'identity': identity, 'renderer': {'Pillow': 'test'},
        'engine_fingerprint': options['expected_engine_fingerprint'], 'options': options,
        'recorded_source': None, 'reviewed_numeric_reference': review}
    raw_payload = {'id': 'ocr-synthetic-failure', 'object': 'ocr.result', 'created': 1,
        'purpose': 'rich_layout_recognition', 'page_count': 1,
        'pages': [{'index': 0, 'finish_reason': 'length' if mode == 'length' else 'stop',
                   'text': 'PRIVATE_FAILED_TEXT 740 m3'}],
        'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3}}
    if mode == 'repeat':
        raw_payload['pages'][0]['text'] = ('<｜ref｜>text<｜/ref｜><｜box｜>[[10,20,30,40]]<｜/box｜>\n') * 5
    if mode == 'markup': raw_payload['pages'][0]['text'] = '<script>unsafe</script>'
    calls = []
    def transport(*args):
        calls.append(1)
        if mode == 'timeout':
            raise ocr.MaterialDeepSeekOCRFailure('MATERIAL_OCR_TIMEOUT_OUTCOME_UNKNOWN', details={
                'child_killed': True, 'server_completion': 'unknown', 'automatic_retry': False})
        if mode == 'transport': raise ocr.MaterialDeepSeekOCRFailure('MATERIAL_OCR_TRANSPORT_OUTCOME_UNKNOWN')
        if mode == 'json': return 200, {}, b'not-json-private-source'
        if isinstance(mode, int): return mode, {}, json_bytes({'error': {'message': 'private-provider-message'}})
        return 200, {}, json_bytes(raw_payload)
    monkeypatch.setenv('COMPANY_BID_DEEPSEEK_API_KEY', 'synthetic-private-credential-123456789')
    monkeypatch.setattr(ocr, '_transport', transport)
    def handler(ctx, initial):
        return ctx.run_unit('plan-assemble', {}, lambda: ctx.run_unit(uid, inputs, lambda: ocr.recognize(
            ctx, image_path=path, image_sha256=image_sha, identity=identity, options=options,
            numeric_reference=review['reference'] if review else None)))
    ex = Executor(tmp_path / 'runtime', [NodeSpec('N3', 'synthetic-failed-ocr', handler)], config)
    rid = ex.create_run({'image_sha256': image_sha})
    state = ex.execute(rid)
    expected = state.get('error_code')
    assert state['status'] == ('succeeded' if mode == 'success' else 'failed') and calls == [1]
    spec = {'binding': {'run_id': rid, 'node_key': 'N3', 'unit_id': uid},
            'expected_error_code': expected or 'MATERIAL_OCR_OUTPUT_INCOMPLETE'}
    if mode == 'numeric':
        evidence_ex = Executor(ex.store.root, [NodeSpec('REVIEW', 'synthetic-review', lambda ctx, value: {
            'evidence': ctx.store_bytes('source-review.json', json_bytes({'observed_image_sha256': image_sha,
                'scope': 'source image independent review; synthetic fixture'}))})], {})
        evidence_run = evidence_ex.create_run({})
        assert evidence_ex.execute(evidence_run)['status'] == 'succeeded'
        ref = evidence_ex.output(evidence_run)['evidence']
        record = build_material_source_review({'identity': {k: identity[k] for k in
            ('source_id', 'source_sha256', 'selection_sha256', 'image_sha256')},
            'ocr': {**spec['binding'], 'status': 'failed', 'error_code': expected,
                'response_sha256': hashlib.sha256(json_bytes(raw_payload)).hexdigest()},
            'reviewer': {'review_id': 'independent-area-review', 'review_method': 'agent_source_image_review',
                'evidence_sha256': ref['sha256']}, 'field': {'field_id': 'area',
                'scope': {'image_region_normalized': [0., 0., 1., 1.]}, 'disposition': 'machine_error',
                'source_observation': 'clear_value', 'recognized_text': '740 m³', 'source_text': '740 m²'}})
        spec.update(source_review=record, source_review_evidence=ref)
    def forbidden(*args, **kwargs):
        pytest.fail('Failure authentication must not invoke provider or network')
    monkeypatch.setattr(ocr, '_transport', forbidden)
    monkeypatch.setattr(ocr, 'recognize', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('COMPANY_BID_DEEPSEEK_API_KEY')
    return {'ex': ex, 'rid': rid, 'uid': uid, 'state': state, 'identity': identity, 'spec': spec,
            'before': snapshot(ex.store.run_dir(rid)), 'calls': calls, 'mode': mode}


def authenticate(origin, mutate=None):
    args = {'specification': copy.deepcopy(origin['spec']), 'image_sha256': origin['identity']['image_sha256'],
            'identity': copy.deepcopy(origin['identity']), 'purpose': 'rich_layout_recognition'}
    if mutate: mutate(args)
    def handler(ctx, value):
        return ctx.run_unit('material-failure-review', value, lambda: authenticate_failed_material_ocr(ctx, **value))
    ex = Executor(origin['ex'].store.root, [NodeSpec('N3', 'failure-disposition-test', handler)], {'debug_capture': True})
    rid = ex.create_run(args)
    state = ex.execute(rid)
    assert not state.get('call_budget', {}).get('reservations') and origin['calls'] == [1]
    ordinary = (ex.store.run_dir(rid) / 'events.jsonl').read_text('utf-8')
    assert '"call.started"' not in ordinary and 'PRIVATE_FAILED_TEXT' not in ordinary
    assert 'private-provider-message' not in ordinary
    return ex, rid, state


@pytest.mark.parametrize('origin', ['length', 'repeat', 'markup', 'timeout', 'transport', 'json', 'numeric',
                                     408, 429, 500, 503, 504], indirect=True)
def test_authenticates_only_failure_and_preserves_original_charge_and_state(origin):
    ex, rid, state = authenticate(origin)
    assert state['status'] == 'succeeded', state.get('error_code')
    result = ex.output(rid)
    assert result['status'] == 'reading_failed_pending_review' and result['reading_status'] == 'ocr_read_failed'
    assert result['identity'] == origin['identity'] and result['identity_sha256'] == digest(origin['identity'])
    assert result['origin_run_id'] == origin['rid'] and result['origin_unit_id'] == origin['uid']
    assert result['validation_run_id'] == rid and result['material_presence'] == 'present_but_unread_or_conflicting'
    assert result['error_code'] == origin['state']['error_code'] and result['machine_text_adopted'] is False
    assert result['qualification_result'] == result['material_presence_conclusion'] == 'not_determined'
    assert result['original_failure_preserved'] is True and result['local_fallback'] is False
    assert result['source_review_authenticated'] == (origin['mode'] == 'numeric')
    assert 'text' not in result and 'response' not in result and 'payload' not in result
    proof = result['provenance']
    assert proof['origin_status'] == 'failed' and proof['origin_run_id'] == origin['rid']
    assert proof['new_external_calls'] == proof['new_fee_reservations'] == 0
    assert proof['raw_response_available'] == (origin['mode'] not in {'timeout', 'transport'})
    assert proof['historical_budget_reservation']['estimated_upper_usd'] == '0.0015'
    spans = [json.loads(s) for s in (ex.store.run_dir(rid) / 'spans.jsonl').read_text('utf-8').splitlines()]
    linked = next(s for s in spans if s['name'] == 'material.ocr_failure_disposition')['links']
    assert linked[0]['trace_id'] == proof['origin_trace_id'] and linked[0]['span_id'] == proof['origin_span_id']
    assert snapshot(origin['ex'].store.run_dir(origin['rid'])) == origin['before']
    assert ex.resume(rid)['status'] == 'succeeded'


@pytest.mark.parametrize('origin', [401, 402, 403, 404, 502, 'success'], indirect=True)
def test_auth_billing_access_other_http_and_successful_units_not_disposed(origin):
    _, _, state = authenticate(origin)
    assert state['status'] == 'failed'
    assert state['error_code'] == ('MATERIAL_OCR_FAILURE_UNIT_NOT_FAILED' if origin['mode'] == 'success'
                                   else 'MATERIAL_OCR_FAILURE_HTTP_STATUS_NOT_ALLOWLISTED')


@pytest.mark.parametrize('code', ['MATERIAL_OCR_BODY_ERROR', 'MATERIAL_OCR_CREDENTIAL_MISSING', 'MATERIAL_OCR_CREDENTIAL_ECHO_REJECTED',
    'MATERIAL_OCR_IMAGE_HASH_MISMATCH', 'MATERIAL_OCR_IMAGE_CHANGED_DURING_CALL', 'MATERIAL_OCR_BUDGET_INVALID',
    'MATERIAL_OCR_DIAGNOSTICS_REQUIRED', 'MATERIAL_OCR_NUMERIC_REFERENCE_INVALID',
    'MATERIAL_OCR_NUMERIC_REFERENCE_SCOPE_MISSING', 'MATERIAL_OCR_ADAPTER_FAILED', 'CHECKPOINT_INVALID'])
def test_unsafe_error_classes_rejected_before_source_read(origin, code):
    _, _, state = authenticate(origin, lambda args: args['specification'].update(expected_error_code=code))
    assert state['error_code'] == 'MATERIAL_OCR_FAILURE_ERROR_NOT_ALLOWLISTED'


@pytest.mark.parametrize('damage', ['identity', 'purpose', 'image', 'expected'])
def test_current_scope_mismatch_rejected(origin, damage):
    def mutate(args):
        if damage == 'identity': args['identity']['crop_box_normalized'] = [.1, .1, .9, .9]
        elif damage == 'purpose': args['purpose'] = 'text_recognition'
        elif damage == 'image': args['image_sha256'] = 'f' * 64
        else: args['specification']['expected_error_code'] = 'MATERIAL_OCR_REPETITIVE_OUTPUT'
    _, _, state = authenticate(origin, mutate)
    assert state['status'] == 'failed' and state['error_code'].startswith('MATERIAL_OCR_FAILURE_')


@pytest.mark.parametrize('damage', ['running', 'extra_attempt', 'raw', 'wire', 'validation', 'span', 'unit_parent',
    'call_event', 'extra_call', 'budget', 'initial', 'unit_error', 'config', 'reservation_event', 'ledger_total'])
def test_missing_or_tampered_failure_evidence_rejected(origin, damage):
    ex, rid, state = origin['ex'], origin['rid'], copy.deepcopy(origin['state'])
    node, = state['nodes']
    unit = node['units'][origin['uid']]
    caps = [c for c in node['attempts'][0]['captures'] if c.get('unit_id') == origin['uid']]
    if damage in {'raw', 'wire', 'validation', 'initial', 'unit_error'}:
        if damage == 'raw': ref = ex.store.read_json(next(c['ref'] for c in caps if c['name'].startswith('response-')))['raw_response_ref']
        elif damage == 'wire': ref = ex.store.read_json(next(c['ref'] for c in caps if c['name'].startswith('request-')))['request_body_ref']
        elif damage == 'validation': ref = next(c['ref'] for c in caps if c['name'].startswith('validation-'))
        elif damage == 'unit_error': ref = next(c['ref'] for c in caps if c['name'] == 'unit-error')
        else: ref = state['initial_input_ref']
        ex.store.resolve(ref['path']).write_bytes(b'{}')
    elif damage in {'span', 'unit_parent', 'call_event', 'extra_call', 'reservation_event'}:
        path = ex.store.run_dir(rid) / ('spans.jsonl' if damage in {'span', 'unit_parent'} else 'events.jsonl')
        rows = [json.loads(s) for s in path.read_text('utf-8').splitlines()]
        if damage == 'span': next(s for s in rows if s['name'] == 'provider.call')['status'] = 'OK'
        elif damage == 'unit_parent': next(s for s in rows if s['name'] == 'unit.N3'
            and s['attributes'].get('unit_id') == origin['uid'])['parent_span_id'] = 'f' * 16
        elif damage == 'extra_call': rows.append(next(e for e in rows if e.get('event') == 'call.started'))
        elif damage == 'reservation_event': rows = [e for e in rows if e.get('event') != 'call.reserved']
        else: next(e for e in rows if e.get('event') == 'call.failed')['error_code'] = 'MATERIAL_OCR_HTTP_ERROR'
        path.write_bytes(b'\n'.join(json_bytes(r) for r in rows) + b'\n')
    else:
        if damage == 'running': state['status'] = 'running'
        elif damage == 'extra_attempt': unit['attempts'].append(copy.deepcopy(unit['attempts'][0]))
        elif damage == 'budget': state['call_budget']['reservations'][0]['input_hash'] = 'f' * 64
        elif damage == 'ledger_total': state['call_budget']['reserved_upper_usd'] = '0'
        else:
            state['execution_config']['planning']['semantic_material_recall']['original_ocr']['purpose'] = 'text_recognition'
            state['config_hash'] = digest(state['execution_config'])
        (ex.store.run_dir(rid) / 'state.json').write_bytes(json_bytes(state))
    _, _, result = authenticate(origin)
    assert result['status'] == 'failed' and result['error_code'].startswith('MATERIAL_OCR_FAILURE_')


@pytest.mark.parametrize('origin', ['numeric'], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'image', 'raw_hash', 'evidence', 'outside', 'blank_review'])
def test_numeric_disposition_requires_exact_independent_review(origin, damage):
    def mutate(args):
        spec = args['specification']
        if damage == 'missing':
            spec.pop('source_review'); spec.pop('source_review_evidence')
        elif damage == 'evidence':
            origin['ex'].store.resolve(spec['source_review_evidence']['path']).write_bytes(b'{}')
        elif damage == 'outside': spec['source_review_evidence']['path'] = '../outside.json'
        else:
            record = spec['source_review']
            data = {k: copy.deepcopy(record[k]) for k in ('identity', 'ocr', 'reviewer', 'field')}
            if damage == 'image': data['identity']['image_sha256'] = 'f' * 64
            elif damage == 'raw_hash': data['ocr']['response_sha256'] = 'f' * 64
            else:
                data['field'].update(disposition='source_blank', source_observation='independently_confirmed_blank', source_text='')
            spec['source_review'] = build_material_source_review(data)
    _, _, state = authenticate(origin, mutate)
    assert state['status'] == 'failed' and state['error_code'].startswith('MATERIAL_OCR_FAILURE_')


def test_map_validation_rejects_null_unknown_fields_and_unpaired_review(origin):
    valid = {origin['identity']['image_sha256']: origin['spec']}
    assert validate_failed_sources(valid) == valid
    for config in (None, {'bad-hash': origin['spec']}, {**valid, 'f' * 64: {}},
                   {origin['identity']['image_sha256']: {**origin['spec'], 'ignored': True}}):
        with pytest.raises(RuntimeError): validate_failed_sources(config)
