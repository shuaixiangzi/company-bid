"""Real N3 crop projection and strict offline coverage before any OCR call."""
import copy
import hashlib
import socket
from pathlib import Path

from PIL import Image
import pytest

from company_bid import material_content_index, material_deepseek_ocr, material_ocr_index
from company_bid.evidence import EvidenceSelection, _selected_png, prepare_evidence
from company_bid.runtime import Executor, NodeSpec
from company_bid.storage import digest, json_bytes
from test_material_ocr_index import _manifest, _pdf


def run(tmp_path, monkeypatch, *, mode='live', mutate=None):
    root = tmp_path / 'runtime'
    calls, replayed, inputs_seen = [], [], []
    def forbidden(*args, **kwargs):
        pytest.fail('Tests must not reach network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setenv('COMPANY_BID_DEEPSEEK_API_KEY', 'synthetic-private-credential-123456789')
    def transport(body, media, secret, timeout):
        assert mode == 'live', 'offline coverage must fail before any paid path'
        calls.append(body)
        payload = {'id': 'ocr-test-region', 'object': 'ocr.result', 'created': 1,
            'purpose': 'rich_layout_recognition', 'page_count': 1,
            'pages': [{'index': 0, 'finish_reason': 'stop', 'text': 'REGION_PRIVATE_CANARY 740 m2'}],
            'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3}}
        return 200, {}, json_bytes(payload)
    monkeypatch.setattr(material_deepseek_ocr, '_transport', transport)
    def recorded(ctx, binding, identity, dimensions, provider, fingerprint, reference):
        replayed.append(copy.deepcopy(identity))
        return {'image_sha256': identity['image_sha256'], 'text': 'REGION_PRIVATE_CANARY',
            'lines': [{'text': 'REGION_PRIVATE_CANARY', 'words': [], 'bounding_box': [1, 2, 20, 30]}],
            'call_id': 'synthetic-recorded', 'recorded_response': {'origin_run_id': 'a' * 32}}
    monkeypatch.setattr(material_ocr_index, '_recorded', recorded)
    def prepare(ctx, value):
        manifest = copy.deepcopy(value['manifest'])
        proof = prepare_evidence(ctx, manifest['sources'], manifest['evidence_selections'], root)
        manifest['sources'].extend(proof['sources'])
        manifest['attachments'].extend(proof['attachments'])
        return {'manifest': manifest}
    def handler(ctx, value):
        manifest = value['manifest']
        index = material_content_index.build_material_content_index(ctx, manifest, {'documents': []})
        selection = EvidenceSelection.model_validate(manifest['evidence_selections'][0])
        image = manifest['attachments'][0]
        child = selection.model_copy(update={'selection_id': 'READ-REGION', 'crop_box_normalized': (.2, .2, .8, .8)})
        with Image.open(image['path']) as png:
            child_bytes, _ = _selected_png(png, child)
        child_hash = hashlib.sha256(child_bytes).hexdigest()
        options = {'engine': 'deepseek_ocr', 'max_run_calls': 1, 'max_run_usd': '.0015'}
        options['expected_engine_fingerprint'] = material_deepseek_ocr.engine_fingerprint(options)
        options['reading_crops'] = {image['sha256']: {'selection': child.model_dump(mode='json'),
            'image_sha256': child_hash, 'parent_selection_sha256': digest(selection.model_dump(mode='json')),
            'review_method': 'agent_source_image_review', 'review_id': 'synthetic-region-review'}}
        if mode != 'live':
            options['recorded_only'] = True
            if mode in {'recorded', 'parent-binding'}:
                options['recorded_sources'] = {child_hash if mode == 'recorded' else image['sha256']: {
                    'run_id': 'a' * 32, 'node_key': 'N3', 'unit_id': 'material-ocr-' + 'b' * 24}}
        if mutate:
            mutate(options, image)
        inputs_seen.append({'parent_hash': image['sha256'], 'child_hash': child_hash, 'child_bytes': child_bytes})
        result = material_ocr_index.transcribe_selected_index(ctx, manifest, index, options)
        return {'manifest': manifest, 'index': result}
    ex = Executor(root, [NodeSpec('N0', 'synthetic-proof', prepare), NodeSpec('N3', 'synthetic-crop', handler)], {})
    rid = ex.create_run({'manifest': _manifest(_pdf(tmp_path))})
    state = ex.execute(rid)
    return ex, rid, state, calls, replayed, inputs_seen


@pytest.mark.parametrize('mode', ['live', 'recorded'])
def test_crop_is_distinct_candidate_text_original_attachment_and_unread_scope_remain(tmp_path, monkeypatch, mode):
    ex, rid, state, calls, replayed, seen = run(tmp_path, monkeypatch, mode=mode)
    assert state['status'] == 'succeeded', state.get('error_code')
    output = ex.output(rid)
    image, = output['manifest']['attachments']
    meta, = output['index']['units']
    projection = meta['reading_region_projection']
    assert image['sha256'] == seen[0]['parent_hash'] != meta['ocr_image_sha256'] == seen[0]['child_hash']
    assert hashlib.sha256(Path(image['path']).read_bytes()).hexdigest() == image['sha256']
    assert meta['transcription_scope'] == 'partial_selected_region'
    assert meta['status'] == 'needs_ocr' and meta['reading_completeness'] == 'not_established'
    assert meta['coordinate_space'] == 'reading_region_pixels'
    assert projection['whole_image_read'] is projection['automatic_fact_adoption'] is False
    assert projection['original_appendix_replaced'] is False
    assert output['index']['coverage']['status_counts']['read'] == 0
    doc = output['index']['parsed']['documents'][0]
    unit = next(u for u in doc['units'] if u.get('material_content_ocr_candidate_readable'))
    blocks = [b for b in unit['blocks'] if b['origin'] == 'ocr' and b['kind'] == 'paragraph']
    assert blocks and all('/reading-region[' + seen[0]['child_hash'] + ']' in b['locator'] for b in blocks)
    assert len(calls) == (1 if mode == 'live' else 0)
    if mode == 'live':
        assert seen[0]['child_bytes'] in calls[0]
    else:
        assert len(replayed) == 1 and replayed[0]['image_sha256'] == seen[0]['child_hash']
    assert ex.resume(rid)['status'] == 'succeeded'
    assert len(calls) == (1 if mode == 'live' else 0)


@pytest.mark.parametrize('mode,code', [('missing', 'MATERIAL_OCR_RECORDED_ONLY_COVERAGE_MISSING'),
    ('parent-binding', 'MATERIAL_OCR_UNUSED_REPLAY_OR_REVIEW_SCOPE')])
def test_offline_requires_child_binding_and_never_calls_live(tmp_path, monkeypatch, mode, code):
    _, _, state, calls, replayed, _ = run(tmp_path, monkeypatch, mode=mode)
    assert state['status'] == 'failed' and state['error_code'] == code
    assert calls == replayed == []


@pytest.mark.parametrize('damage,code', [('hash', 'MATERIAL_OCR_REGION_FROZEN_IMAGE_HASH_MISMATCH'),
    ('unused', 'MATERIAL_OCR_UNUSED_READING_CROP_SCOPE'), ('flag', 'MATERIAL_OCR_RECORDED_ONLY_INVALID')])
def test_invalid_crop_stops_before_adapter(tmp_path, monkeypatch, damage, code):
    def mutate(options, image):
        if damage == 'hash': options['reading_crops'][image['sha256']]['image_sha256'] = 'f' * 64
        elif damage == 'unused': options['reading_crops'] = {'f' * 64: options['reading_crops'][image['sha256']]}
        else: options['recorded_only'] = 'yes'
    _, _, state, calls, replayed, _ = run(tmp_path, monkeypatch, mutate=mutate)
    assert state['status'] == 'failed' and state['error_code'] == code
    assert calls == replayed == []
