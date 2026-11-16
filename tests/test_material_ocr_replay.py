import copy
import hashlib
import io
import json
import socket
import uuid
from decimal import Decimal

from PIL import Image
import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.material_ocr_replay import replay_material_ocr, _request_body
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import digest, json_bytes


def sha(value):
    return hashlib.sha256(value).hexdigest()


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


@pytest.fixture
def original(tmp_path, monkeypatch, request):
    mode = getattr(request, 'param', 'stop')
    root = tmp_path / 'runtime'
    png = io.BytesIO()
    Image.new('RGB', (80, 60), 'white').save(png, format='PNG')
    image = png.getvalue()
    def produce(ctx, value):
        return {'image': ctx.store_bytes('proof.png', image, 'image/png')}
    image_ex = Executor(root, [NodeSpec('IMAGE', 'synthetic-image', produce)], {'debug_capture': True})
    image_run = image_ex.create_run({})
    assert image_ex.execute(image_run)['status'] == 'succeeded'
    image_ref = image_ex.output(image_run)['image']
    config = {'run_purpose': 'diagnostic_replay', 'debug_capture': True, 'ocr_comparison': {
        'endpoint': 'https://api.deepseek.com/v1/tools/ocr', 'purpose': 'rich_layout_recognition',
        'max_attempts': 1, 'tls_verify': True, 'follow_redirects': False,
        'remaining_project_protection_at_authorization_usd': '0.1'}}
    specs = []
    for sample in ('alpha', 'beta'):
        identity = {'source_id': 'S-' + sample, 'source_sha256': 'a' * 64, 'format': 'pdf_pages', 'target': 1,
                    'selection_sha256': digest(sample), 'crop_box_normalized': [.1, .2, .9, .8],
                    'rotate_clockwise': 90, 'render_dpi': 200, 'group_bindings': [], 'image_sha256': sha(image)}
        specs.append({'unit_id': 'compare-' + sample, 'sample_id': sample,
            'image_file': {'path': str(image_ex.store.resolve(image_ref['path'])), 'sha256': sha(image)},
            'identity': identity, 'dimensions': [80, 60]})
    calls = []
    def perform(ctx, spec):
        call_id = 'ocrcompare-' + uuid.uuid4().hex
        body = _request_body(image, sha(image), 'rich_layout_recognition')
        body_ref = ctx.store_bytes('ocr-request-body.bin', body)
        req = {'call_id': call_id, 'endpoint': config['ocr_comparison']['endpoint'],
               'purpose': 'rich_layout_recognition', 'request_body_ref': body_ref, 'body_sha256': sha(body),
               'image_sha256': sha(image), 'identity': spec['identity'], 'single_png_expected_pages': 1,
               'tls_verify': True, 'follow_redirects': False, 'automatic_retry': False,
               'authorization_header': 'omitted'}
        ctx.capture('request-' + call_id, req)
        ctx.reserve_call(call_id, 1, Decimal('.0015'), Decimal('.003'), 2,
                         unit_id=spec['unit_id'], input_hash=sha(body))
        metadata = {'call_id': call_id, 'call_attempt': 1, 'provider': 'deepseek_official_ocr',
                    'operation': 'ocr_comparison', 'unit_id': spec['unit_id'], 'input_hash': sha(body),
                    'usage_status': 'unknown'}
        with ctx.call_span(metadata) as span:
            metadata.update(trace_id=format(span.get_span_context().trace_id, '032x'),
                            span_id=format(span.get_span_context().span_id, '016x'))
            ctx.call_event('call.started', metadata)
            calls.append(spec['unit_id'])
            payload = {'id': 'ocr-' + uuid.uuid4().hex, 'object': 'ocr.result', 'created': 1,
                       'purpose': 'rich_layout_recognition', 'page_count': 1,
                       'pages': [{'index': 0, 'finish_reason': mode if spec['sample_id'] == 'beta' else 'stop',
                                  'text': 'Synthetic private certificate ' + spec['sample_id']}],
                       'usage': {'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20}}
            raw = json_bytes(payload)
            raw_ref = ctx.store_bytes('ocr-response-raw.bin', raw)
            ctx.capture('response-' + call_id, {'call_id': call_id, 'http_status': 200,
                'raw_response_ref': raw_ref, 'received_raw_sha256': sha(raw), 'raw_capture_status': 'captured',
                'response': payload, 'outcome_error': None})
            passed = payload['pages'][0]['finish_reason'] == 'stop'
            observation = {'sample_id': spec['sample_id'], 'unit_id': spec['unit_id'], 'diagnostic_only': True,
                'call_id': call_id, 'trace_id': metadata['trace_id'], 'span_id': metadata['span_id'],
                'image_sha256': sha(image), 'identity': spec['identity'], 'transport_contract_passed': passed,
                'http_status': 200, 'error_code': None if passed else 'OCR_OUTPUT_INCOMPLETE', 'response': payload,
                'raw_response_ref': raw_ref, 'raw_response_sha256': sha(raw), 'raw_response_complete': True,
                'raw_response_sha256_scope': 'received_body', 'provider_request_id': payload['id'],
                'automatic_retry': False, 'official_returned_page_fee_estimate_cny': '.0015',
                'usd_conservative_reservation_retained': '0.0015'}
            ctx.capture('validation-' + call_id, observation)
            metadata.update(output_hash=sha(raw), validation_status='passed' if passed else 'failed',
                            provider_request_id=payload['id'], usage_status='not_token_billed')
            if not passed:
                metadata['error_code'] = 'OCR_OUTPUT_INCOMPLETE'
            span.set_attributes(metadata)
            ctx.call_event('call.completed' if passed else 'call.failed', metadata)
            return observation
    def handler(ctx, value):
        return {'observations': [ctx.run_unit(s['unit_id'], {'spec': s, 'config': ctx.execution_config['ocr_comparison']},
            lambda s=s: perform(ctx, s)) for s in value['units']]}
    ex = Executor(root, [NodeSpec('OCR_COMPARISON', 'synthetic-comparison-v1', handler)], config)
    rid = ex.create_run({'units': specs})
    state = ex.execute(rid)
    assert state['status'] == 'succeeded', state.get('error_code')
    before = snapshot(root / 'runs' / rid)
    def forbidden(*args, **kwargs):
        pytest.fail('Recorded OCR must not use network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('COMPANY_BID_DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    return {'root': root, 'ex': ex, 'rid': rid, 'state': state, 'specs': specs, 'calls': calls, 'before': before}


def replay(original, mutate=None, *, index=1, capture=True, after=None):
    spec = copy.deepcopy(original['specs'][index])
    binding = {'run_id': original['rid'], 'node_key': 'OCR_COMPARISON', 'unit_id': spec['unit_id']}
    args = {'binding': binding, 'image_sha256': spec['image_file']['sha256'], 'identity': spec['identity'],
            'purpose': 'rich_layout_recognition'}
    if mutate:
        mutate(args)
    def handler(ctx, value):
        def one():
            payload, provenance = replay_material_ocr(ctx, **value)
            if after:
                after(payload)
            return {'payload': payload, 'provenance': provenance}
        return ctx.run_unit('material-ocr-target', value, one)
    config = {'debug_capture': capture, 'run_purpose': 'diagnostic_replay'}
    ex = Executor(original['root'], [NodeSpec('N3', 'new-ocr-validator', handler)], config)
    rid = ex.create_run(args)
    return ex, rid, ex.execute(rid)


def assert_no_new_calls(original, ex, rid):
    assert not ex.inspect(rid).get('call_budget', {}).get('reservations')
    assert inspect_diagnostics(original['root'], rid)['actual_call_attempts'] == 0
    text = (original['root'] / 'runs' / rid / 'events.jsonl').read_text('utf-8')
    assert '"call.started"' not in text and '"call.completed"' not in text and '"call.failed"' not in text
    assert 'Synthetic private certificate' not in text
    assert original['calls'] == ['compare-alpha', 'compare-beta']


def test_replays_exact_api_payload_and_copies_trace_evidence_without_credentials(original):
    ex, rid, state = replay(original)
    assert state['status'] == 'succeeded', state.get('error_code')
    value = ex.output(rid)
    old = original['ex'].output(original['rid'])['observations'][1]
    assert value['payload'] == old['response']
    proof = value['provenance']
    assert proof['origin_call_id'] == old['call_id']
    assert proof['new_external_calls'] == proof['historical_calls_charged_again'] == proof['new_fee_reservations'] == 0
    assert proof['current_quality_validation'] == 'required_by_caller'
    for item in proof['evidence_snapshots']:
        assert ex.store.read_bytes(item['snapshot_ref']) == ex.store.resolve(item['origin_path']).read_bytes()
    spans = [json.loads(line) for line in (ex.store.run_dir(rid) / 'spans.jsonl').read_text('utf-8').splitlines()]
    span, = [s for s in spans if s['name'] == 'material.ocr_replay']
    assert span['links'][0]['trace_id'] == old['trace_id'] and span['links'][0]['span_id'] == old['span_id']
    assert_no_new_calls(original, ex, rid)
    assert snapshot(original['root'] / 'runs' / original['rid']) == original['before']
    assert ex.resume(rid)['status'] == 'succeeded'
    assert_no_new_calls(original, ex, rid)


@pytest.mark.parametrize('damage', ['hash', 'crop', 'rotate', 'dpi', 'group', 'purpose', 'run_id', 'node_key', 'unit_id'])
def test_scope_change_is_rejected_without_live_fallback(original, damage):
    def change(value):
        if damage == 'hash': value['image_sha256'] = 'f' * 64
        elif damage == 'crop': value['identity']['crop_box_normalized'] = None
        elif damage == 'rotate': value['identity']['rotate_clockwise'] = 0
        elif damage == 'dpi': value['identity']['render_dpi'] = 300
        elif damage == 'group': value['identity']['group_bindings'] = [{'group_id': 'different'}]
        elif damage == 'purpose': value['purpose'] = 'text_recognition'
        elif damage == 'run_id': value['binding']['run_id'] = '../outside'
        elif damage == 'node_key': value['binding']['node_key'] = 'N3'
        else: value['binding']['unit_id'] = 'material-ocr-windows'
    ex, rid, state = replay(original, change)
    assert state['status'] == 'failed' and state['error_code'].startswith('MATERIAL_OCR_REPLAY_')
    assert_no_new_calls(original, ex, rid)


@pytest.mark.parametrize('original', ['length'], indirect=True)
def test_successful_observation_checkpoint_cannot_hide_original_truncation(original):
    assert original['state']['status'] == 'succeeded'
    ex, rid, state = replay(original)
    assert state['status'] == 'failed' and state['error_code'] == 'MATERIAL_OCR_REPLAY_SOURCE_RESPONSE_NOT_SUCCESSFUL'
    assert_no_new_calls(original, ex, rid)
    # A failed sibling page does not prevent exact reuse of another good page.
    ex2, rid2, state2 = replay(original, index=0)
    assert state2['status'] == 'succeeded', state2.get('error_code')
    assert_no_new_calls(original, ex2, rid2)


@pytest.mark.parametrize('damage', ['raw', 'image', 'budget', 'provider', 'span_parent', 'failed_unit', 'capture', 'initial', 'extra_call', 'node_version'])
def test_source_corruption_or_unproved_call_is_rejected(original, damage):
    state = copy.deepcopy(original['state'])
    folder = original['root'] / 'runs' / original['rid']
    unit = state['nodes'][0]['units']['compare-beta']
    out = original['ex'].store.read_json(unit['output_ref'])['data']
    if damage == 'raw':
        original['ex'].store.resolve(out['raw_response_ref']['path']).write_bytes(b'{}')
    elif damage == 'image':
        from pathlib import Path
        Path(original['specs'][1]['image_file']['path']).write_bytes(b'changed')
    elif damage == 'span_parent':
        path = folder / 'spans.jsonl'
        rows = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
        next(s for s in rows if s['name'] == 'provider.call' and s['attributes'].get('unit_id') == 'compare-beta')['parent_span_id'] = 'f' * 16
        path.write_bytes(b'\n'.join(json_bytes(r) for r in rows) + b'\n')
    elif damage == 'capture':
        cap = next(c for c in state['nodes'][0]['attempts'][0]['captures']
                   if c.get('unit_id') == 'compare-beta' and c['name'].startswith('response-'))
        cap['status'] = 'write_failed'
        (folder / 'state.json').write_bytes(json_bytes(state))
    elif damage == 'initial':
        original['ex'].store.resolve(state['initial_input_ref']['path']).write_bytes(b'{}')
    elif damage == 'extra_call':
        path = folder / 'events.jsonl'
        rows = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
        duplicate = copy.deepcopy(next(e for e in rows if e.get('event') == 'call.started'
                                       and e.get('call_id') == out['call_id']))
        duplicate['unit_attempt_id'] = 'f' * 32
        rows.append(duplicate)
        path.write_bytes(b'\n'.join(json_bytes(r) for r in rows) + b'\n')
    else:
        if damage == 'budget':
            next(r for r in state['call_budget']['reservations'] if r['unit_id'] == 'compare-beta')['input_hash'] = 'f' * 64
        elif damage == 'provider':
            state['execution_config']['ocr_comparison']['endpoint'] = 'https://untrusted.invalid/ocr'
            state['config_hash'] = digest(state['execution_config'])
        elif damage == 'node_version':
            state['node_manifest'][0]['version'] = 'changed'
        else:
            unit['status'] = 'failed'
        (folder / 'state.json').write_bytes(json_bytes(state))
    ex, rid, result = replay(original)
    assert result['status'] == 'failed' and result['error_code'].startswith('MATERIAL_OCR_REPLAY_')
    assert_no_new_calls(original, ex, rid)


def test_capture_disabled_stops_before_replay(original):
    ex, rid, state = replay(original, capture=False)
    assert state['status'] == 'failed' and state['error_code'] == 'MATERIAL_OCR_REPLAY_CAPTURED_N3_UNIT_REQUIRED'
    assert_no_new_calls(original, ex, rid)


def test_new_quality_failure_remains_failed_and_never_calls_provider(original):
    class QualityFailure(RuntimeError):
        code = 'OCR_UNIT_CONFLICT'
    def reject(_):
        raise QualityFailure()
    ex, rid, state = replay(original, after=reject)
    assert state['status'] == 'failed' and state['error_code'] == 'OCR_UNIT_CONFLICT'
    assert_no_new_calls(original, ex, rid)
