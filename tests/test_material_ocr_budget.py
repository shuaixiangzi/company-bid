import copy

import pytest

from company_bid.material_ocr_budget import validate_pipeline_ocr_budget
from company_bid.pipeline import build_executor
from company_bid.provider import ProviderFailure


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
