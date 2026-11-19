import copy
import uuid

import pytest

from company_bid.material_deepseek_ocr import engine_fingerprint
from company_bid.material_ocr_budget import reserve_tender_ocr, validate_pipeline_ocr_budget
from company_bid.ocr import renderer_version
from company_bid.pipeline import build_executor
from company_bid.provider import ProviderFailure
from company_bid.runtime import Executor, NodeSpec, call_budget_summary
from company_bid.storage import atomic_json, digest, process_lock


def config():
    return {'planning': {'semantic_material_recall': {
        'original_ocr': {'engine': 'deepseek_ocr', 'max_run_usd': '0.15', 'max_run_calls': 100}}},
        'provider': {'mode': 'chat_completions', 'max_run_usd': 0.15, 'max_run_calls': 100}}


def test_equal_caps_share_one_run_guard():
    value = config()
    value['structured_provider'] = copy.deepcopy(value['provider'])
    value['mapping_provider'] = copy.deepcopy(value['provider'])
    validate_pipeline_ocr_budget(value)


@pytest.mark.parametrize('location', ['provider', 'structured_provider', 'mapping_provider', 'semantic', 'ocr', 'commitment'])
def test_conflicting_caps_rejected_before_executor_or_any_call(tmp_path, location):
    value = config()
    bad = {'mode': 'chat_completions', 'max_run_usd': 0.2, 'max_run_calls': 100}
    if location == 'semantic':
        value['planning']['semantic_material_recall'].update(enabled=True, provider=bad)
    elif location == 'ocr':
        value['ocr'] = {'provider': bad}
    elif location == 'commitment':
        value['provider']['mode'] = 'diagnostic_writing_commitment_partition'
        value['commitment_drafting'] = {'direct_provider': bad}
    else:
        value[location] = bad
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        build_executor(tmp_path / 'not-created', value)
    assert not (tmp_path / 'not-created').exists()


@pytest.mark.parametrize('override', [{'max_run_calls': 99}, {'max_run_calls': True},
                                     {'max_run_usd': 'NaN'}, {'max_run_usd': None}])
def test_invalid_or_conflicting_count_cannot_relax_budget(override):
    value = config()
    value['provider'].update(override)
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        validate_pipeline_ocr_budget(value)


def test_replay_only_providers_do_not_add_a_live_budget_requirement():
    value = config()
    value['provider']['mode'] = 'diagnostic_writing_candidate_import'
    value['provider']['max_run_usd'] = 9
    value['planning']['semantic_material_recall'].update(enabled=True, recorded_source={'run_id': 'historical'},
                                                         provider={'max_run_usd': 9})
    validate_pipeline_ocr_budget(value)


def test_legacy_readonly_inspection_remains_possible():
    validate_pipeline_ocr_budget({'planning': {'semantic_material_recall': {'original_ocr': {'engine': 'windows_ocr'}}}})


def test_n1_ocr_implicit_chat_mode_is_checked_before_its_first_charge():
    value = config()
    value['provider'] = {'mode': 'prepared'}
    value['ocr'] = {'provider': {'max_run_usd': 0.2, 'max_run_calls': 100}}
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        validate_pipeline_ocr_budget(value)


def tender_config(**overrides):
    value = {'engine': 'deepseek_ocr', 'max_run_usd': '0.15', 'max_run_calls': 100,
             'reserve_usd_per_page': '0.0015', **overrides}
    return {**value, 'expected_engine_fingerprint': engine_fingerprint(value),
            'expected_renderer_version': renderer_version()}


@pytest.mark.parametrize('other_ocr', [False, True])
def test_official_tender_ocr_and_live_chat_shared_caps(tmp_path, other_ocr):
    value = config()
    if not other_ocr:
        value.pop('planning')
    value['ocr'] = tender_config()
    validate_pipeline_ocr_budget(value)

    value['structured_provider'] = {**value['provider'], 'max_run_calls': 99}
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        build_executor(tmp_path / 'no-runtime', value)
    assert not (tmp_path / 'no-runtime').exists()


def test_two_official_ocr_consumers_share_caps_with_offline_chat():
    value = config()
    value['provider'] = {'mode': 'prepared'}
    value['ocr'] = tender_config(max_run_calls=10)
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        validate_pipeline_ocr_budget(value)


def test_offline_n1_retains_its_historical_cap_when_only_n2_can_spend(tmp_path):
    value = {'pipeline': 'automatic', 'run_purpose': 'diagnostic_replay',
             'provider': {'mode': 'prepared'}, 'structured_provider': config()['provider'],
             'ocr': {**tender_config(max_run_calls=5, max_run_usd='0.0075'), 'recorded_targets': []}}
    executor = build_executor(tmp_path, value)
    assert executor.execution_config['ocr']['max_run_calls'] == 5
    assert executor.execution_config['structured_provider']['max_run_calls'] == 100
    value['ocr']['live_targets'] = []
    with pytest.raises(ProviderFailure):
        build_executor(tmp_path, value)


def test_replayed_n1_cannot_relax_live_n3_ocr_cap(tmp_path):
    value = config()
    value.update(pipeline='automatic', run_purpose='diagnostic_replay',
                 ocr={**tender_config(max_run_calls=5), 'recorded_targets': []})
    value['structured_provider'] = {**value['provider'], 'max_run_calls': 101}
    with pytest.raises(ProviderFailure, match='MATERIAL_OCR_SHARED_RUN_BUDGET_MISMATCH'):
        build_executor(tmp_path, value)


def purchase_stub(root):
    dispatches = []
    provider = tender_config()
    first_page = digest({'source': 'frozen', 'page': 1})

    def handler(ctx, value):
        def purchase():
            key = value.get('purchase_key', first_page)
            body = digest({'version': value.get('renderer', 'original')})
            call_id = uuid.uuid4().hex
            ctx.capture('request-' + call_id, {'purchase_key': key, 'body_sha256': body, 'call_id': call_id})
            reserve_tender_ocr(ctx, key, call_id, provider, input_hash=body)
            dispatches.append((ctx.run_id, key))
            if value.get('ambiguous'):
                raise ProviderFailure('OCR_TRANSPORT_TIMEOUT')
            return {'call_id': call_id}

        return ctx.run_unit('tender-page', value, purchase)

    return Executor(root, [NodeSpec('N1', 'test-tender-purchase-v1', handler)], {}), first_page, dispatches


def test_ambiguous_tender_dispatch_preserves_charge_across_resume_and_sibling(tmp_path):
    executor, key, dispatches = purchase_stub(tmp_path)
    source = executor.create_run({'ambiguous': True})
    sibling = executor.create_run({'renderer': 'other'})
    first = executor.execute(source)
    assert first['error_code'] == 'OCR_TRANSPORT_TIMEOUT'
    original_ledger = first['call_budget']
    assert call_budget_summary(original_ledger)['effective_budget_charge_usd'] == '0.0015'

    resumed = executor.resume(source)
    assert resumed['error_code'] == 'OCR_PURCHASE_ALREADY_RESERVED'
    assert resumed['call_budget'] == original_ledger
    duplicate = executor.execute(sibling)
    assert duplicate['error_code'] == 'OCR_PURCHASE_ALREADY_RESERVED'
    assert not duplicate.get('call_budget')
    assert dispatches == [(source, key)]

    distinct = executor.create_run({'purchase_key': digest({'source': 'frozen', 'page': 2})})
    assert executor.execute(distinct)['status'] == 'succeeded'
    assert len(dispatches) == 2


def test_busy_tender_intent_and_pre_reservation_crash_do_not_consume_quota(tmp_path):
    executor, key, dispatches = purchase_stub(tmp_path)
    old_run = executor.create_run({})
    candidate = executor.create_run({})
    pointer = tmp_path / 'ocr-purchases' / (key + '.json')
    atomic_json(pointer, {'schema_version': 'tender.ocr.purchase-intent.v1', 'purchase_key': key,
                          'run_id': old_run, 'call_id': uuid.uuid4().hex, 'input_hash': 'd' * 64})
    with process_lock(pointer.with_suffix('.lock')):
        busy = executor.execute(candidate)
    assert busy['error_code'] == 'OCR_PURCHASE_ADMISSION_BUSY'
    assert not busy.get('call_budget')
    assert dispatches == []
    assert executor.resume(candidate)['status'] == 'succeeded'
    assert dispatches == [(candidate, key)]


def test_conflicting_tender_intent_never_dispatches(tmp_path):
    executor, key, dispatches = purchase_stub(tmp_path)
    previous = executor.create_run({})
    assert executor.execute(previous)['status'] == 'succeeded'
    pointer = tmp_path / 'ocr-purchases' / (key + '.json')
    record = executor.inspect(previous)['call_budget']['reservations'][0]
    atomic_json(pointer, {'schema_version': 'tender.ocr.purchase-intent.v1', 'purchase_key': key,
                          'run_id': previous, 'call_id': record['call_id'], 'input_hash': '0' * 64})

    next_run = executor.create_run({})
    blocked = executor.execute(next_run)
    assert blocked['error_code'] == 'OCR_PURCHASE_LEDGER_UNAVAILABLE'
    assert not blocked.get('call_budget')
    assert dispatches == [(previous, key)]
