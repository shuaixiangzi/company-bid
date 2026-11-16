"""Real local executor evidence with synthetic transports, never network I/O."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import socket

import pytest

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider, ProviderFailure
from company_bid.runtime import Executor, NodeSpec
from company_bid.semantic_materials import semantic_probe
from company_bid.semantic_replay import AuthenticatedSemanticProvider
from company_bid.storage import digest
from test_semantic_materials import inputs, config, reply, wire


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


@pytest.fixture
def original(tmp_path, monkeypatch, request):
    value = inputs(tmp_path, ['请附证明企业依法登记存续的文件。', '请附能够证明公司完成合法登记的资料。'])
    settings = config()
    settings['planning'] = {'semantic_material_recall': {'provider': copy.deepcopy(settings['semantic_provider'])}}
    monkeypatch.setenv('SEMANTIC_TEST_KEY', 'semantic-replay-synthetic-secret')
    calls = []
    def transport(endpoint, payload, secret, timeout):
        user = json.loads(payload['messages'][1]['content'])
        calls.append(user['query_id'])
        return wire(reply(user))
    def handler(ctx, initial):
        provider = ChatProposalProvider(ChatConfig(**settings['semantic_provider']), transport=transport)
        if getattr(request, 'param', False):
            return ctx.run_unit('plan-assemble', initial, lambda: semantic_probe(ctx, initial, provider))
        return semantic_probe(ctx, initial, provider)
    root = tmp_path / 'runtime'
    executor = Executor(root, [NodeSpec('N3', 'original-semantic-v1', handler)], settings)
    rid = executor.create_run(value)
    state = executor.execute(rid)
    assert state['status'] == 'succeeded', state.get('error_code')
    before = snapshot(root / 'runs' / rid)
    def forbidden(*args, **kwargs):
        pytest.fail('Replay must not construct a provider, use network or read a credential')
    monkeypatch.setattr(ChatProposalProvider, '__init__', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('SEMANTIC_TEST_KEY')
    return {'root': root, 'executor': executor, 'run_id': rid, 'state': state, 'value': value,
            'settings': settings, 'calls': calls, 'before': before}


def replay(original, *, selected=(1,), mutate=None, validation=None, settings=None):
    current = copy.deepcopy(original['value'])
    current['requirements'] = [current['requirements'][i] for i in selected]
    if mutate:
        mutate(current)
    settings = copy.deepcopy(settings if settings is not None else original['settings'])
    settings['semantic_replay_origin'] = {'run_id': original['run_id']}
    used = []
    def handler(ctx, value):
        provider = AuthenticatedSemanticProvider(ctx, ChatConfig(**settings['semantic_provider']),
                                                  settings['semantic_replay_origin'])
        assert provider.is_recorded_replay is True
        if validation is not None:
            real = provider.generate_json
            def wrap(context, payload, **kwargs):
                previous = kwargs['validate']
                kwargs['validate'] = lambda candidate: validation(payload, previous(candidate))
                return real(context, payload, **kwargs)
            provider.generate_json = wrap
        result = semantic_probe(ctx, value, provider)
        used.extend(sorted(provider._used))
        return result
    executor = Executor(original['root'], [NodeSpec('N3', 'replayed-semantic-v1', handler)], settings)
    rid = executor.fork(original['run_id'], 'N3', initial_input=current, execution_config=settings)
    return executor, rid, executor.execute(rid), used


def assert_no_new_calls(original, executor, rid):
    state = executor.inspect(rid)
    assert not state.get('call_budget', {}).get('reservations')
    assert inspect_diagnostics(original['root'], rid)['actual_call_attempts'] == 0
    events = (original['root'] / 'runs' / rid / 'events.jsonl').read_text(encoding='utf-8')
    spans = [json.loads(line) for line in (original['root'] / 'runs' / rid / 'spans.jsonl').read_text(encoding='utf-8').splitlines()]
    assert not any(name in events for name in ('"call.started"', '"call.completed"', '"call.failed"'))
    assert not any(s['name'] == 'provider.call' for s in spans)
    assert '依法登记' not in events and 'semantic-replay-synthetic-secret' not in events
    assert original['calls'] == ['Q0', 'Q1']


@pytest.mark.parametrize('original', [False, True], indirect=True)
def test_subset_replays_exact_candidate_with_trace_links_and_controlled_copies(original):
    executor, rid, state, used = replay(original)
    assert state['status'] == 'succeeded', state.get('error_code')
    output = executor.output(rid)['queries'][0]
    old = original['executor'].output(original['run_id'])['queries'][1]
    assert {k: v for k, v in output.items() if k not in {'model_called', 'historical_response_reused'}} == {
        k: v for k, v in old.items() if k not in {'model_called', 'historical_response_reused'}}
    assert output['model_called'] is False and output['historical_response_reused'] is True
    assert used == ['semantic-query-' + digest('Q1')[:24]]
    caps = state['nodes'][0]['attempts'][-1]['captures']
    replay_capture, = [c for c in caps if c['name'].startswith('semantic-replay-semantic-query-')]
    proof = executor.store.read_json(replay_capture['ref'])
    assert proof['new_external_calls'] == proof['historical_calls_charged_again'] == 0
    assert proof['candidate_sha256'] == digest(proof['validated_output'])
    assert proof['origin_unit_id'] == used[0]
    assert not any('Q0' in str(item) for item in proof['validated_output'])
    for item in proof['evidence_snapshots']:
        assert executor.store.read_bytes(item['snapshot_ref']) == executor.store.resolve(item['origin_path']).read_bytes()
    spans = [json.loads(line) for line in (original['root'] / 'runs' / rid / 'spans.jsonl').read_text().splitlines()]
    span, = [s for s in spans if s['name'] == 'material.semantic_replay']
    assert any(link['trace_id'] == proof['origin_trace_id'] and link['span_id'] == proof['origin_span_id'] for link in span['links'])
    assert_no_new_calls(original, executor, rid)
    assert snapshot(original['root'] / 'runs' / original['run_id']) == original['before']


@pytest.mark.parametrize('damage', ['text', 'catalog', 'unrecorded', 'model', 'purpose', 'validator'])
def test_changed_query_config_or_candidate_has_no_fallback(original, damage):
    settings = copy.deepcopy(original['settings'])
    def mutate(value):
        if damage == 'text':
            value['requirements'][0]['text'] += '附加约束。'
        elif damage == 'catalog':
            value['manifest']['attachments'][0]['title'] += ' 新标签'
        elif damage == 'unrecorded':
            value['requirements'][0]['requirement_id'] = 'NEVER-CALLED'
    if damage == 'model':
        settings['semantic_provider']['model'] = 'different-model'
    elif damage == 'purpose':
        settings['run_purpose'] = 'production'
    def validate(payload, result):
        if damage == 'validator':
            result['candidates'][0]['reason'] += ' Changed'
        return result
    executor, rid, state, _ = replay(original, mutate=mutate, settings=settings, validation=validate)
    assert state['status'] == 'failed', state.get('error_code')
    assert_no_new_calls(original, executor, rid)
    assert snapshot(original['root'] / 'runs' / original['run_id']) == original['before']


@pytest.mark.parametrize('damage', ['response', 'reservation', 'span', 'failed_unit'])
def test_origin_response_ledger_or_trace_damage_is_rejected(original, damage):
    folder = original['root'] / 'runs' / original['run_id']
    state = copy.deepcopy(original['state'])
    unit_id = 'semantic-query-' + digest('Q1')[:24]
    unit = state['nodes'][0]['units'][unit_id]
    if damage == 'response':
        cap = next(c for c in state['nodes'][0]['attempts'][0]['captures'] if c.get('unit_id') == unit_id and c['name'].startswith('response-'))
        original['executor'].store.resolve(cap['ref']['path']).write_bytes(b'{}')
    elif damage == 'reservation':
        next(r for r in state['call_budget']['reservations'] if r['unit_id'] == unit_id)['input_hash'] = 'f' * 64
        (folder / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    elif damage == 'failed_unit':
        unit['status'] = 'failed'
        (folder / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    else:
        path = folder / 'spans.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        next(s for s in rows if s['name'] == 'provider.call' and s['attributes'].get('unit_id') == unit_id)['parent_span_id'] = 'f' * 16
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    executor, rid, result, _ = replay(original)
    assert result['status'] == 'failed', result.get('error_code')
    assert_no_new_calls(original, executor, rid)


def test_unselected_response_is_not_authenticated_or_replayed(original):
    state = original['state']
    unit_id = 'semantic-query-' + digest('Q0')[:24]
    cap = next(c for c in state['nodes'][0]['attempts'][0]['captures'] if c.get('unit_id') == unit_id and c['name'].startswith('response-'))
    original['executor'].store.resolve(cap['ref']['path']).write_bytes(b'unselected historical response')
    # Use create_run because fork validates every original node checkpoint.
    settings = copy.deepcopy(original['settings'])
    value = copy.deepcopy(original['value']); value['requirements'] = value['requirements'][1:]
    def handler(ctx, current):
        provider = AuthenticatedSemanticProvider(ctx, ChatConfig(**settings['semantic_provider']), {'run_id': original['run_id']})
        return semantic_probe(ctx, current, provider)
    executor = Executor(original['root'], [NodeSpec('N3', 'subset-only-v1', handler)], settings)
    rid = executor.create_run(value); state = executor.execute(rid)
    assert state['status'] == 'succeeded', state.get('error_code')
    assert_no_new_calls(original, executor, rid)


def test_resume_reuses_successful_query_and_only_revalidates_failed_query(original):
    observed, fail = [], [True]
    def validate(payload, result):
        observed.append(payload['query_id'])
        if payload['query_id'] == 'Q1' and fail[0]:
            raise ProviderFailure('SYNTHETIC_LOCAL_VALIDATOR_FAILURE')
        return result
    executor, rid, state, _ = replay(original, selected=(0, 1), validation=validate)
    assert state['status'] == 'failed'
    successful = copy.deepcopy(state['nodes'][0]['units']['semantic-query-' + digest('Q0')[:24]])
    fail[0] = False
    state = executor.resume(rid)
    assert state['status'] == 'succeeded', state.get('error_code')
    assert observed == ['Q0', 'Q1', 'Q1']
    assert state['nodes'][0]['units']['semantic-query-' + digest('Q0')[:24]] == successful
    assert_no_new_calls(original, executor, rid)
    assert snapshot(original['root'] / 'runs' / original['run_id']) == original['before']


@pytest.mark.parametrize('original', [True], indirect=True)
@pytest.mark.parametrize('damage', ['query_parent', 'query_parent_node', 'parent_failed', 'parent_hash', 'parent_trace', 'parent_node_attempt', 'parent_not_direct'])
def test_nested_query_requires_exact_successful_plan_assemble_parent(original, damage):
    folder = original['root'] / 'runs' / original['run_id']
    state = copy.deepcopy(original['state'])
    parent = state['nodes'][0]['units']['plan-assemble']
    if damage == 'parent_failed':
        parent['status'] = 'failed'
    elif damage == 'parent_trace':
        parent['attempts'][-1]['trace_id'] = 'f' * 32
    elif damage == 'parent_node_attempt':
        parent['attempts'][-1]['node_attempt_id'] = 'f' * 32
    if damage in {'parent_failed', 'parent_trace', 'parent_node_attempt'}:
        (folder / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    else:
        path = folder / 'spans.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        selected_id = 'semantic-query-' + digest('Q1')[:24] if damage in {'query_parent', 'query_parent_node'} else 'plan-assemble'
        selected = next(s for s in rows if s['name'] == 'unit.N3' and s['attributes'].get('unit_id') == selected_id)
        if damage == 'parent_hash':
            selected['attributes']['output_hash'] = 'f' * 64
        elif damage == 'query_parent_node':
            selected['parent_span_id'] = state['nodes'][0]['attempts'][-1]['span_id']
        else:
            selected['parent_span_id'] = 'f' * 16
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
    executor, rid, result, _ = replay(original)
    assert result['status'] == 'failed', result.get('error_code')
    assert_no_new_calls(original, executor, rid)
