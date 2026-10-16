"""Operating-cycle contracts using original Git/SourceRef/industry inputs; synthetic values."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.industry_operations import read_industry_operations
from decision_kernel.application.operating_cycle import METRICS, operating_cycle
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Entity, MissingObject, RecordError, Selection, SourceInput, SourceRef, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from test_github_store import GitAPI

ROOT = Path(__file__).resolve().parents[1]
VALUES = {
    'revenue': ('100', '120'), 'cost': ('80', '102'), 'profit': ('10', '6'),
    'revenue_yoy': ('10', '20'), 'cost_yoy': ('9', '22'), 'profit_yoy': ('12', '-5'),
    'receivables': ('20', '28'), 'inventory': ('10', '15'),
    'receivables_yoy': ('15', '25'), 'inventory_yoy': ('11', '30'),
    'receivables_days': ('30', '33'), 'inventory_days': ('10', '11'),
}


def retain(store, *, populations=None, changes=None, months=('2026-07-31', '2026-08-31')):
    populations = populations or {'synthetic-industry': list(METRICS)}
    entities = [Entity(sid, 'industry', sid) for sid in populations]
    sources, records = [], []
    for slot, end in enumerate(months):
        rows = []
        for sid, metrics in populations.items():
            for metric in metrics:
                unit, kind, assessment = METRICS[metric]
                row = dict(zip(COLUMNS, [sid, metric, 'synthetic-comparable-population',
                    'CN-synthetic', 'CNY', unit, 'not-applicable', 'not-applicable',
                    'not-applicable', 'reported-accounting', 'monthly', kind, assessment,
                    end[:4] + '-01-01' if kind == 'ytd-flow' or assessment == 'ytd-comparable-yoy' else end,
                    end, VALUES[metric][min(slot, 1)]]))
                if changes:
                    row.update(changes.get((slot, metric), {}))
                rows.append(row)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
        source = SourceInput('synthetic-operating', 'dataset', 'Synthetic operating values',
            'synthetic:nbs-operating', stream.getvalue(), entities=tuple(entities),
            published_at='2026-09-28T09:30:00+08:00', acquired_at='2026-10-16')
        sources.append(store.put_source('synthetic-data-' + str(slot), source))
        records.append(source)
    series, index = [], 0
    for entity in entities:
        for metric in populations[entity.entity_id]:
            index += 1
            series.append({'series_id': entity.entity_id + '-' + metric,
                'subject': asdict(entity), 'observations': [asdict(SourceRef(ref.manifest,
                    Selection('rows', index, index, COLUMNS))) for ref in sources]})
    packet = dict(schema='industry-reading-v1', prepared_at='2026-10-16T15:00:00+08:00',
        as_of='2026-10-16T15:00:00+08:00', coverage_note='Synthetic declared populations only',
        use='Synthetic operating-cycle read, not investment research', series=series, relations=[])
    root = store.put_source('synthetic-packet', SourceInput('synthetic-operating-packet', 'content',
        'Synthetic packet', 'synthetic:operating-packet', encoded(packet).decode()))
    return root, sources, records, packet


@pytest.fixture(scope='module')
def original(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp('operations') / 'records.git'))
    root, _, _, _ = retain(store)
    return read_industry(root, store)


def find(reading, name):
    return next(s for s in reading['series'] if s['series_id'].endswith('-' + name))


def point(reading, name, index=1):
    return find(reading, name)['observations'][index]


def derived(reading):
    return operating_cycle(reading)['entries'][0]


def calculation(reading, name, slot=1):
    return derived(reading)['snapshots'][slot]['calculations'][name]


def test_arithmetic_amounts_rates_and_stocks_are_separate(original):
    out = operating_cycle(original)
    entry = out['entries'][0]
    values = entry['snapshots'][1]['calculations']
    assert values['profit_margin_pct']['value'] == '5.00'
    assert values['cost_per_100_revenue']['value'] == '85.00'
    assert values['receivables_plus_inventory']['value'] == '43'
    assert values['profit_less_revenue_growth_pp']['value'] == '-25'
    assert values['inventory_less_revenue_growth_pp']['value'] == '10'
    assert len(entry['snapshots'][1]['questions']) == 3
    for metric in ('revenue', 'cost', 'profit'):
        change = entry['metrics'][metric]['changes'][0]
        assert change['delta'] is None and change['reason_code'] == 'OPERATING_CUMULATIVE_NOT_MONTHLY'
    assert entry['metrics']['receivables']['changes'][0]['delta'] == '8'
    assert entry['metrics']['receivables']['changes'][0]['percent_change'] == '40.0'
    assert entry['metrics']['profit_yoy']['changes'][0]['delta'] == '-17'
    assert entry['metrics']['profit_yoy']['changes'][0]['unit'] == 'percentage-point'
    assert entry['metrics']['receivables_days']['changes'][0]['delta'] == '3'
    assert out['source_requests'] == out['writes'] == 0 and out['research_executed'] is False
    assert out['retained'] == original and out['status'] == 'read'
    assert 'COMPLETE_NET_WORKING_CAPITAL' in ' '.join(out['limitations'])


def test_calculations_bind_every_operand_to_exact_retained_row(original):
    value = calculation(original, 'profit_margin_pct')
    assert value['sources'] == [point(original, name)['reference'] for name in ('profit', 'revenue')]
    assert value['sources'][0]['selection'] != value['sources'][1]['selection']
    assert value['sources'][0]['manifest'] == value['sources'][1]['manifest']


@pytest.mark.parametrize('metric,value,code', [
    ('revenue', None, 'OPERATING_VALUE_MISSING'), ('revenue', '-1', 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS'),
    ('cost', '-1', 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS'), ('receivables', '-1', 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS'),
    ('inventory', '-1', 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS'), ('receivables_days', '-1', 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS'),
    ('profit', 'bad', 'INDUSTRY_DECIMAL'), ('profit_yoy', 'NaN', 'INDUSTRY_DECIMAL'),
])
def test_bad_values_are_local_and_not_zero(original, metric, value, code):
    reading = deepcopy(original); point(reading, metric)['value'] = value
    result = operating_cycle(reading)
    assert result['status'] == 'partial'
    bad = result['entries'][0]['metrics'][metric]['points'][1]
    assert bad['reason_code'] == code and bad['value'] is None
    assert result['scope']['unavailable_metric_points_or_series'] == 1
    assert result['retained'] == reading


@pytest.mark.parametrize('metric,value', [('profit', '-5'), ('profit', '0'), ('profit_yoy', '-150'),
    ('revenue_yoy', '0'), ('cost', '0'), ('receivables', '0'), ('inventory_days', '0')])
def test_signed_profit_growth_and_real_zero_allowed(original, metric, value):
    reading = deepcopy(original); point(reading, metric)['value'] = value
    assert derived(reading)['metrics'][metric]['points'][1]['value'] == value


def test_zero_denominator_is_unknown_not_infinity(original):
    reading = deepcopy(original); point(reading, 'revenue')['value'] = '0'
    value = calculation(reading, 'profit_margin_pct')
    assert value['value'] is None and value['reason_code'] == 'OPERATING_NONPOSITIVE_DENOMINATOR'
    assert calculation(reading, 'receivables_plus_inventory')['value'] == '43'
    point(reading, 'inventory', 0)['value'] = '0'
    change = derived(reading)['metrics']['inventory']['changes'][0]
    assert change['delta'] == '15' and change['percent_change'] is None


@pytest.mark.parametrize('field,value,code', [
    ('unit', 'CNY', 'OPERATING_METRIC_BASIS'), ('period_kind', 'stock', 'OPERATING_METRIC_BASIS'),
    ('assessment', 'period-average', 'OPERATING_METRIC_BASIS'),
    ('currency', 'USD', 'OPERATING_NONMARKET_BASIS'), ('frequency', 'daily', 'OPERATING_NONMARKET_BASIS'),
    ('market', 'spot', 'OPERATING_NONMARKET_BASIS'), ('price_kind', 'quote', 'OPERATING_NONMARKET_BASIS'),
    ('specification', 'unknown', 'OPERATING_SCOPE_UNKNOWN'), ('region', 'unspecified', 'OPERATING_SCOPE_UNKNOWN'),
    ('period_end', '2026-08-30', 'OPERATING_MONTH_END'),
    ('period_start', '2026-08-01', 'OPERATING_PERIOD_BASIS'),
])
def test_metric_basis_is_not_inferred_from_name(original, field, value, code):
    reading = deepcopy(original); point(reading, 'revenue')[field] = value
    out = derived(reading)['metrics']['revenue']['points'][1]
    assert out['status'] == 'unavailable' and out['reason_code'] == code


@pytest.mark.parametrize('field,value', [('specification', 'different-population'), ('region', 'other'),
                                       ('tax_basis', 'different-accounting')])
def test_arithmetic_never_joins_different_populations(original, field, value):
    reading = deepcopy(original); point(reading, 'profit')[field] = value
    assert calculation(reading, 'profit_margin_pct')['reason_code'] == 'OPERATING_DIFFERENT_POPULATIONS_OR_PERIODS'
    assert calculation(reading, 'cost_per_100_revenue')['status'] == 'calculated'


def test_different_release_versions_never_silently_joined(original):
    reading = deepcopy(original); point(reading, 'profit')['reference']['manifest']['commit'] = 'c' * 40
    assert calculation(reading, 'profit_margin_pct')['reason_code'] == 'OPERATING_DIFFERENT_SOURCE_VERSIONS'


def test_mismatched_slots_not_sorted_to_make_a_ratio(original):
    reading = deepcopy(original)
    find(reading, 'profit')['observations'].reverse()
    assert all(s['calculations']['profit_margin_pct']['status'] == 'unavailable'
               for s in derived(reading)['snapshots'])


def test_missing_optional_series_not_dropped_or_filled(original):
    reading = deepcopy(original)
    reading['series'] = [s for s in reading['series'] if not s['series_id'].endswith('-cost')]
    out = derived(reading)
    assert 'cost' in out['not_supplied_metrics']
    assert out['snapshots'][1]['calculations']['cost_per_100_revenue']['reason_code'] == 'OPERATING_METRIC_NOT_SUPPLIED'
    assert out['snapshots'][1]['calculations']['profit_margin_pct']['value'] == '5.00'


def test_invalid_series_and_duplicate_metric_not_silently_selected(original):
    reading = deepcopy(original)
    reading['series'][0]['observations'] = [{'status': 'unavailable', 'reason_code': 'OBJECT_MISSING'}] * 2
    assert operating_cycle(reading)['unclassified_series'] == [
        {'series_id': reading['series'][0]['series_id'], 'reason_code': 'OPERATING_UNCLASSIFIED_SERIES'}]
    reading = deepcopy(original); reading['series'].append(deepcopy(reading['series'][0]))
    with pytest.raises(RecordError, match='DUPLICATE'): operating_cycle(reading)
    reading = deepcopy(original); reading['query']['subject_id'] = 'filtered'
    with pytest.raises(RecordError, match='FULL_INPUT'): operating_cycle(reading)


@pytest.mark.parametrize('metric', ['inventory', 'profit_yoy', 'receivables_days'])
def test_gap_month_not_bridged(original, metric):
    reading = deepcopy(original)
    p = point(reading, metric); p['period_end'] = '2026-10-31'
    if metric != 'profit_yoy': p['period_start'] = '2026-10-31'
    assert derived(reading)['metrics'][metric]['changes'][0]['reason_code'] == 'OPERATING_NONADJACENT_MONTHS'


def test_year_reset_and_release_regression_explicit(original):
    reading = deepcopy(original)
    for i, end in enumerate(('2025-12-31', '2026-01-31')):
        p = point(reading, 'profit_yoy', i); p['period_end'] = end; p['period_start'] = end[:4] + '-01-01'
    assert derived(reading)['metrics']['profit_yoy']['changes'][0]['reason_code'] == 'OPERATING_CUMULATIVE_YEAR_RESET'
    reading = deepcopy(original)
    point(reading, 'inventory', 0)['source']['published_at'] = '2026-09-28T10:00:00+08:00'
    assert derived(reading)['metrics']['inventory']['changes'][0]['reason_code'] == 'OPERATING_RELEASE_REGRESSED'


def test_source_identity_change_does_not_become_growth(original):
    reading = deepcopy(original); point(reading, 'inventory')['source']['origin'] = 'synthetic:another'
    assert derived(reading)['metrics']['inventory']['changes'][0]['reason_code'] == 'OPERATING_BASIS_OR_SOURCE_CHANGED'


def test_same_period_revision_and_unchanged_not_monthly_change(original):
    for value, status in [('100', 'unchanged'), ('110', 'same-period-revision')]:
        reading = deepcopy(original)
        second = point(reading, 'revenue'); second.update(period_end='2026-07-31', value=value)
        change = derived(reading)['metrics']['revenue']['changes'][0]
        assert change['status'] == status and change['meaning'] == 'release-revision-not-period-flow'


def test_decimal_determinism_and_no_mutation(original):
    initial = encoded(original); expected = operating_cycle(original)
    with localcontext() as ctx:
        ctx.prec = 1; ctx.rounding = ROUND_DOWN; ctx.traps[Inexact] = True
        assert operating_cycle(original) == expected
    assert encoded(original) == initial
    expected['limitations'].clear()
    assert operating_cycle(original)['limitations']


def test_filter_preserves_whole_version_and_scope(original):
    whole = operating_cycle(original)
    assert operating_cycle(original, subject_id='synthetic-industry')['view_version'] == whole['view_version']
    missing = operating_cycle(original, subject_id='not-covered')
    assert missing['entries'] == [] and missing['scope'] == whole['scope']
    assert missing['view_version'] == whole['view_version']


def test_real_local_store_unchanged_and_old_reader_still_available(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / 'records.git'))
    ref, _, _, _ = retain(store, changes={(1, 'profit'): {'value': ''}})
    before = store.backend.head()
    old = read_industry(ref, store)
    new = read_industry_operations(ref, VersionedStore(GitStore(tmp_path / 'records.git')))
    assert new['retained'] == old and new['status'] == 'partial'
    assert new['entries'][0]['snapshots'][1]['calculations']['cost_per_100_revenue']['value'] == '85.00'
    assert store.backend.head() == before
    with pytest.raises(RecordError, match='COMPANY_QUERY'): read_industry_operations(ref, store, company_id='company')


def test_native_scoped_git_reader_90_points_no_budget_reset_or_post(tmp_path):
    api = GitAPI(tmp_path / 'remote.git')
    full = list(METRICS)
    populations = {'total': full, **{sid: full[:6] for sid in ('mining', 'manufacturing', 'utilities')}}
    writer = VersionedStore(GitHubStore(api.binding, api))
    ref, _, _, _ = retain(writer, populations=populations, months=('2026-06-30', '2026-07-31', '2026-08-31'))
    api.requests.clear(); posts = api.writes
    reader = VersionedStore(GitHubStore(replace(api.binding, write_enabled=False), api))
    out = read_industry_operations(ref, reader)
    assert out['scope'] == {'subjects': 4, 'series': 30, 'unavailable_metric_points_or_series': 0}
    assert sum(len(m['points']) for e in out['entries'] for m in e['metrics'].values()) == 90
    assert len(api.requests) < 160 and not any(r[0] != 'GET' for r in api.requests)
    assert api.writes == posts
    assert all(len(e['snapshots']) == 3 for e in out['entries'])


def test_access_revocation_during_scope_prevents_return(tmp_path, monkeypatch):
    from decision_kernel.application import industry_operations as module
    api = GitAPI(tmp_path / 'remote.git')
    ref, _, _, _ = retain(VersionedStore(GitHubStore(api.binding, api)))
    reader = VersionedStore(GitHubStore(replace(api.binding, write_enabled=False), api))
    calculate = module.operating_cycle
    def revoke(value, **kwargs):
        result = calculate(value, **kwargs)
        api.meta['private'] = False
        return result
    monkeypatch.setattr(module, 'operating_cycle', revoke)
    with pytest.raises(RecordError, match='BINDING_MISMATCH'):
        module.read_industry_operations(ref, reader)


def test_native_scopes_closed_and_failed_dependency_preserved(tmp_path):
    api = GitAPI(tmp_path / 'remote.git')
    ref, _, _, _ = retain(VersionedStore(GitHubStore(api.binding, api)))
    backend = GitHubStore(replace(api.binding, write_enabled=False), api)
    native = backend.source_read_scope; opened = []
    @contextmanager
    def tracked(snapshot):
        with native(snapshot) as scoped:
            opened.append(scoped)
            yield scoped
    backend.source_read_scope = tracked
    read_industry_operations(ref, VersionedStore(backend))
    assert len(opened) == 3 and all(not s.active for s in opened)
    for scope in opened:
        with pytest.raises(RecordError, match='CLOSED'): scope.check_commit(scope.snapshot)


def test_original_cli_new_process_only_saved_ref(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / 'records.git'))
    ref, _, _, _ = retain(store)
    ref_file = tmp_path / 'reference.json'; ref_file.write_bytes(encoded(asdict(ref)))
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('TOKEN', 'KEY', 'SECRET'))}
    env['PYTHONPATH'] = str(ROOT / 'src')
    args = [sys.executable, '-m', 'decision_kernel.interfaces.industry_cli', '--path', str(tmp_path / 'records.git'),
            '--ref', str(ref_file)]
    for extra, schema in [([], 'industry-reading-result-v1'), (['--operating-cycle'], 'industry-operating-cycle-v1')]:
        run = subprocess.run([*args, *extra], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout)['schema'] == schema
    run = subprocess.run([*args, '--operating-cycle', '--company-id', 'company'], cwd=tmp_path,
                         env=env, capture_output=True, text=True, timeout=45)
    assert run.returncode == 2 and 'COMPANY_QUERY' in run.stderr


def test_one_lost_source_version_preserves_other_period(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / 'records.git'))
    ref, sources, _, _ = retain(store)
    original_resolve = store.backend.resolve
    def missing(blob):
        if blob.path == sources[1].manifest.path:
            raise MissingObject('OBJECT_MISSING')
        return original_resolve(blob)
    monkeypatch.setattr(store.backend, 'resolve', missing)
    out = read_industry_operations(ref, store)
    assert out['status'] == 'partial' and out['retained']['scope']['unavailable_inputs'] == 12
    slots = out['entries'][0]['snapshots']
    assert slots[0]['calculations']['profit_margin_pct']['value'] == '10.0'
    assert slots[1]['calculations']['profit_margin_pct']['status'] == 'unavailable'
    assert all(m['changes'][0]['delta'] is None for m in out['entries'][0]['metrics'].values())
