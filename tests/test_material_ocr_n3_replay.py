"""Actual adapter captures in a synthetic N3; no provider/network calls."""
import copy
import hashlib
import json
import socket

from PIL import Image
import pytest

from company_bid import material_deepseek_ocr as ocr
from company_bid.material_ocr_replay import replay_material_ocr
from company_bid.material_ocr_quality import assess_ocr_response
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}


@pytest.fixture
def original(tmp_path, monkeypatch, request):
    mode, nested, purpose = getattr(request, 'param', ('failed', True, 'rich_layout_recognition'))
    png = tmp_path / 'proof.png'
    Image.new('RGB', (80, 60), 'white').save(png)
    identity = {'source_id': 'synthetic', 'source_sha256': 'a' * 64, 'format': 'pdf_pages', 'target': 1,
        'selection_sha256': 'b' * 64, 'crop_box_normalized': [.1, .2, .9, .8], 'rotate_clockwise': 90,
        'render_dpi': 160, 'group_bindings': [], 'image_sha256': sha(png.read_bytes())}
    options = {'engine': 'deepseek_ocr', 'purpose': purpose, 'timeout_seconds': 5,
               'max_attempts': 1, 'max_run_calls': 1, 'max_run_usd': '.0015'}
    options['expected_engine_fingerprint'] = ocr.engine_fingerprint(options)
    config = {'debug_capture': True, 'run_purpose': 'diagnostic_replay', 'planning': {
        'semantic_material_recall': {'read_selected_originals': True, 'original_ocr': options}}}
    uid = 'material-ocr-' + digest(identity)[:24]
    inputs = {'version': 'material-ocr-index.v2', 'identity': identity, 'renderer': {'Pillow': 'test'},
        'engine_fingerprint': options['expected_engine_fingerprint'], 'options': options,
        'recorded_source': None, 'reviewed_numeric_reference': None}
    payload = {'id': 'ocr-synthetic-n3', 'object': 'ocr.result', 'created': 1, 'purpose': purpose,
        'page_count': 1, 'pages': [{'index': 0, 'finish_reason': 'stop', 'text': 'PRIVATE_SOURCE_CANARY 740 m3'}],
        'usage': {'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20}}
    calls = []
    def transport(*args):
        calls.append(1)
        return 200, {}, json_bytes(payload)
    monkeypatch.setenv('COMPANY_BID_DEEPSEEK_API_KEY', 'synthetic-private-credential-123456789')
    monkeypatch.setattr(ocr, '_transport', transport)
    def perform(ctx):
        result = {'input_sha256': digest(inputs), 'identity': identity, 'dimensions': [80, 60],
            'response': ocr.recognize(ctx, image_path=png, image_sha256=identity['image_sha256'],
                identity=identity, options=options), 'quality_status': 'machine_transcription_unverified',
            'reading_completeness': 'not_established', 'layout_status': 'regions_and_tables_unverified',
            'qualification_result': 'not_determined', 'automatic_assembly_allowed': False}
        result['output_sha256'] = digest(result)
        return result
    def plan(ctx):
        result = ctx.run_unit(uid, inputs, lambda: perform(ctx))
        if mode == 'failed':
            def fail():
                raise RuntimeError('synthetic later page failure')
            ctx.run_unit('later-failed-page', {}, fail)
        return result
    def handler(ctx, initial):
        return ctx.run_unit('plan-assemble', {}, lambda: plan(ctx)) if nested else plan(ctx)
    nodes = [NodeSpec('N3', 'production-ocr-synthetic', handler)]
    if mode == 'paused_debug':
        nodes.append(NodeSpec('N4', 'unused', lambda ctx, value: value))
    ex = Executor(tmp_path / 'runtime', nodes, config)
    rid = ex.create_run({'frozen_image_sha256': identity['image_sha256']})
    state = ex.execute(rid, stop_after='N3' if mode == 'paused_debug' else None)
    assert state['status'] == mode and calls == [1]
    def forbidden(*args, **kwargs):
        pytest.fail('Replay must not use network or the OCR adapter')
    monkeypatch.setattr(ocr, '_transport', forbidden)
    monkeypatch.setattr(ocr, 'recognize', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('COMPANY_BID_DEEPSEEK_API_KEY')
    return dict(ex=ex, rid=rid, uid=uid, state=state, identity=identity, payload=payload, calls=calls,
                before=snapshot(ex.store.run_dir(rid)))


def replay(original, mutate=None, reference=None):
    args = {'binding': {'run_id': original['rid'], 'node_key': 'N3', 'unit_id': original['uid']},
            'image_sha256': original['identity']['image_sha256'], 'identity': copy.deepcopy(original['identity']),
            'purpose': original['payload']['purpose']}
    if mutate:
        mutate(args)
    def handler(ctx, value):
        def page():
            payload, provenance = replay_material_ocr(ctx, **value)
            if reference:
                assess_ocr_response(payload, width=80, height=60, identity=value['identity'], numeric_reference=reference)
            return {'payload': payload, 'provenance': provenance}
        return ctx.run_unit('material-ocr-new', value, page)
    ex = Executor(original['ex'].store.root, [NodeSpec('N3', 'new-version', handler)],
                  {'debug_capture': True, 'run_purpose': 'diagnostic_replay'})
    rid = ex.create_run(args)
    state = ex.execute(rid)
    assert not state.get('call_budget', {}).get('reservations')
    assert original['calls'] == [1]
    logs = (ex.store.run_dir(rid) / 'events.jsonl').read_text('utf-8')
    assert '"call.started"' not in logs and 'PRIVATE_SOURCE_CANARY' not in logs
    return ex, rid, state


@pytest.mark.parametrize('original', [(s, n, p) for s in ('succeeded', 'failed', 'paused_debug')
    for n in (True, False) for p in ('rich_layout_recognition', 'text_recognition')], indirect=True)
def test_successful_page_reused_even_after_parent_failure_or_pause(original):
    ex, rid, state = replay(original)
    assert state['status'] == 'succeeded', state.get('error_code')
    value = ex.output(rid)
    assert value['payload'] == original['payload']
    proof = value['provenance']
    assert proof['origin_node_key'] == 'N3' and proof['origin_unit_id'] == original['uid']
    assert proof['new_external_calls'] == proof['new_fee_reservations'] == 0
    assert snapshot(original['ex'].store.run_dir(original['rid'])) == original['before']
    spans = [json.loads(s) for s in (ex.store.run_dir(rid) / 'spans.jsonl').read_text('utf-8').splitlines()]
    linked = next(s for s in spans if s['name'] == 'material.ocr_replay')['links']
    assert linked[0]['trace_id'] == proof['origin_trace_id'] and linked[0]['span_id'] == proof['origin_span_id']
    assert ex.resume(rid)['status'] == 'succeeded'


@pytest.mark.parametrize('damage', ['identity', 'purpose', 'image', 'unit'])
def test_changed_scope_rejected(original, damage):
    def change(args):
        if damage == 'identity': args['identity']['rotate_clockwise'] = 0
        elif damage == 'purpose': args['purpose'] = 'text_recognition'
        elif damage == 'image': args['image_sha256'] = 'c' * 64
        else: args['binding']['unit_id'] = 'material-ocr-' + 'c' * 24
    _, _, state = replay(original, change)
    assert state['status'] == 'failed' and state['error_code'].startswith('MATERIAL_OCR_REPLAY_')


@pytest.mark.parametrize('damage', ['running', 'failed_unit', 'second_attempt', 'raw', 'wire', 'validation',
    'budget', 'extra_call', 'call_parent', 'unit_parent', 'config', 'initial', 'output'])
def test_source_tampering_rejected(original, damage):
    ex, rid = original['ex'], original['rid']
    state = copy.deepcopy(original['state'])
    node = state['nodes'][0]
    unit = node['units'][original['uid']]
    output = ex.store.read_json(unit['output_ref'])['data']
    caps = [c for c in node['attempts'][0]['captures'] if c.get('unit_id') == original['uid']]
    if damage in {'raw', 'wire', 'validation', 'initial', 'output'}:
        if damage == 'raw': ref = output['response']['response_raw_ref']
        elif damage == 'wire':
            ref = ex.store.read_json(next(c['ref'] for c in caps if c['name'].startswith('request-')))['request_body_ref']
        elif damage == 'validation': ref = next(c['ref'] for c in caps if c['name'].startswith('validation-'))
        elif damage == 'initial': ref = state['initial_input_ref']
        else: ref = unit['output_ref']
        ex.store.resolve(ref['path']).write_bytes(b'{}')
    elif damage in {'extra_call', 'call_parent', 'unit_parent'}:
        path = ex.store.run_dir(rid) / ('events.jsonl' if damage == 'extra_call' else 'spans.jsonl')
        rows = [json.loads(s) for s in path.read_text('utf-8').splitlines()]
        if damage == 'extra_call': rows.append(next(e for e in rows if e.get('event') == 'call.started'))
        else:
            span = next(s for s in rows if s['name'] == 'provider.call') if damage == 'call_parent' else next(
                s for s in rows if s['name'] == 'unit.N3' and s['attributes'].get('unit_id') == original['uid'])
            span['parent_span_id'] = 'f' * 16
        path.write_bytes(b'\n'.join(json_bytes(r) for r in rows) + b'\n')
    else:
        if damage == 'running': state['status'] = 'running'
        elif damage == 'failed_unit': unit['status'] = 'failed'
        elif damage == 'second_attempt': unit['attempts'].append(copy.deepcopy(unit['attempts'][0]))
        elif damage == 'budget': state['call_budget']['reservations'][0]['input_hash'] = 'f' * 64
        else:
            state['execution_config']['planning']['semantic_material_recall']['original_ocr']['purpose'] = 'text_recognition'
            state['config_hash'] = digest(state['execution_config'])
        (ex.store.run_dir(rid) / 'state.json').write_bytes(json_bytes(state))
    _, _, result = replay(original)
    assert result['status'] == 'failed' and result['error_code'].startswith('MATERIAL_OCR_REPLAY_')


def test_new_reference_still_blocks_old_successful_numeric_error(original):
    reference = {'image_sha256': original['identity']['image_sha256'], 'checks': [
        {'field_id': 'area', 'kind': 'measure', 'region_index': 0, 'expected_text': '740 m²'}]}
    _, _, state = replay(original, reference=reference)
    assert state['status'] == 'failed' and state['error_code'] == 'MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH'
    assert snapshot(original['ex'].store.run_dir(original['rid'])) == original['before']
