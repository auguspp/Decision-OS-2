"""A narrow check mode on the original Relay host, with no supplier-row artifact."""
from pathlib import Path

from decision_kernel.application.pmi_reference_check import checked_plan, check_reference
from decision_kernel.application.industry_pmi import pmi_source_plan
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore

FAMILY = 'industry-pmi'


def settings(data, sha):
    operation = data.get('operation', 'plan')
    access = data.get('access_confirmed') is True or (type(data.get('access_confirmed')) is str and data['access_confirmed'] == 'true')
    require(operation in ('plan', 'check'), 'PMI_CHECK_OPERATION')
    require(operation != 'check' or access, 'PMI_CHECK_ACCESS_REQUIRED')
    declaration = data.get('public_rights_confirmed')
    require(declaration is None or declaration is False or (type(declaration) is str and declaration == 'false'),
            'PMI_CHECK_NO_SOURCE_EXPORT')
    require(not any(data.get(k, '') for k in ('codes', 'periods', 'dates', 'apis'))
            and str(data.get('page_size', '100')) == str(data.get('max_pages', '100')) == '100',
            'PMI_CHECK_UNUSED_SELECTOR')
    raw = data.get('source_plan')
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= 60000, 'PMI_CHECK_PLAN')
    plan = checked_plan(_json(raw.encode()))
    return {'operation': operation, 'code_sha': sha, 'family': FAMILY, 'source_plan': plan,
            'access_confirmed': access, 'public_rights_confirmed': False}


def _live(plan, registry, progress):
    binding = GitHubBinding.from_dict(_json(Path('config/store-binding.json').read_bytes()))
    require(binding.visibility == 'public' and not binding.write_enabled, 'PMI_CHECK_READ_ONLY_BINDING')
    store = VersionedStore(GitHubStore(binding, GitHubAPI.from_environment()))
    return check_reference(plan, registry, store, access_confirmed=True, progress=progress)


def execute(stage, selected, out):
    registry = RelayRegistry(Path('docs/sources/relay-registry.json'))
    plan = checked_plan(selected['source_plan'])
    generated = pmi_source_plan(registry, case_id='industry-pmi-reference-check',
        start_month=plan['start_month'], end_month=plan['end_month'])
    request_path, plan_path, result_path = (out / name for name in ('request.json', 'plan.json', 'result.json'))
    if stage == 'prepare':
        out.mkdir(parents=False, exist_ok=False)
        request_path.write_bytes(encoded(selected))
        plan_path.write_bytes(encoded(generated))
        result_path.write_bytes(encoded({'status': 'planned-not-captured', 'code_sha': selected['code_sha'],
                                       'records_written': False, 'supplier_rows_retained': False}))
        return 0
    require(stage == 'check' and selected['operation'] == 'check' and selected['access_confirmed'] is True,
            'PMI_CHECK_ACCESS_REQUIRED')
    require(_json(request_path.read_bytes()) == selected and _json(plan_path.read_bytes()) == generated,
            'PMI_CHECK_PLAN_CHANGED')
    require(_json(result_path.read_bytes())['status'] == 'planned-not-captured', 'PMI_CHECK_ALREADY_STARTED')
    def progress(result):
        result_path.write_bytes(encoded({**result, 'code_sha': selected['code_sha']}))
    progress({'status': 'in-progress', 'records_written': False, 'supplier_rows_retained': False})
    try:
        result = _live(plan, registry, progress)
    except (RecordError, OSError, ValueError, KeyError, TypeError):
        result = {'status': 'check-unavailable', 'error': 'PMI_CHECK_HOST_UNAVAILABLE',
                  'records_written': False, 'supplier_rows_retained': False}
    progress(result)
    return 0 if result['status'] == 'matched' else 2
