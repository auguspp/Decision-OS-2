"""Synthetic weekly prices through real Git custody; not live ministry data."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Inexact, ROUND_DOWN, localcontext
from pathlib import Path
import csv
import io
import json
import os
import subprocess
import sys

import pytest

from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.livestock_weekly import (
    FAMILY, REPORT_COLUMNS, _check, _measure, _reported, _transition, read_livestock_weekly,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Entity, RecordError, Selection, SourceInput, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces.industry_commands import checked_payload, perform

ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = tuple(Entity('synthetic-' + name, 'commodity', 'Synthetic ' + name,
                       attributes=(('observation_family', FAMILY),))
                 for name in ('egg-national', 'egg-regional', 'chick'))
DAYS = ('2026-09-10', '2026-09-17', '2026-09-24')


def retain(store):
    refs = []
    for i, day in enumerate(DAYS):
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, (*COLUMNS, *REPORT_COLUMNS), lineterminator='\n')
        writer.writeheader()
        for n, subject in enumerate(SUBJECTS):
            row = dict(zip(COLUMNS, (subject.entity_id, 'survey-price', 'synthetic-average',
                'synthetic-selected-provinces' if n == 1 else 'synthetic-national', 'CNY',
                'CNY/bird' if n == 2 else 'CNY/kg', 'survey', 'spot', 'not-applicable',
                'not-separately-disclosed', 'weekly', 'price-level', 'collection-day', day, day,
                str(10 + i))))
            writer.writerow({**row, 'reported_previous': str(9 + i),
                'reported_year_ago': str((10 + i) / 2), 'reported_wow_pct': ('11.1', '10.0', '9.1')[i],
                'reported_yoy_pct': '100.0'})
        refs.append(store.put_source('weekly-' + str(i), SourceInput('synthetic-weekly', 'dataset',
            'Synthetic sample-day prices', 'synthetic:weekly', output.getvalue(), entities=SUBJECTS,
            acquired_at='2026-10-17', published_at=day, report_period=day)))
    packet = {'schema': 'industry-reading-v1', 'prepared_at': '2026-10-17T08:00:00+08:00',
              'as_of': '2026-10-17T08:00:00+08:00', 'coverage_note': 'Synthetic three different populations/units',
              'use': 'Synthetic price and publisher comparison tests, no issuer role', 'relations': [],
              'series': [{'series_id': subject.entity_id, 'subject': asdict(subject), 'observations': [
                  asdict(SourceRef(ref.manifest, Selection('rows', n, n, COLUMNS))) for ref in refs]}
                  for n, subject in enumerate(SUBJECTS, 1)]}
    root = store.put_source('weekly-packet', SourceInput('synthetic-weekly-packet', 'content',
        'Synthetic weekly packet', 'synthetic:packet', encoded(packet).decode()))
    return root, refs


@pytest.fixture(scope='module')
def saved(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp('weekly') / 'records.git'))
    root, refs = retain(store)
    return store, root, refs


@pytest.fixture(scope='module')
def reading(saved):
    store, root, _ = saved
    return read_livestock_weekly(root, store)


def test_end_to_end_exact_versions_and_no_inferred_roles(saved, reading):
    store, root, _ = saved
    assert reading['retained'] == read_industry(root, store)
    assert reading['schema'] == 'industry-livestock-weekly-v1' and reading['status'] == 'read'
    assert reading['scope']['price_points'] == 9
    assert reading['scope']['publisher_checks'] == {
        'matches-displayed-rounding': 18, 'rounding-difference': 0, 'unavailable': 0}
    assert not reading['relations'] and reading['company_benefit'] == 'not-assessed'
    assert reading['writes'] == reading['source_requests'] == 0
    assert reading['entries'][2]['points'][0]['unit'] == 'CNY/bird'
    assert reading['entries'][1]['points'][0]['region'] != reading['entries'][0]['points'][0]['region']
    assert reading['entries'][0]['latest_change']['published_previous_matches_prior_observation'] is True
    assert reading['entries'][0]['latest_change']['elapsed_days'] == 7


@pytest.mark.parametrize('field,value,code', [
    ('unit','CNY/tonne','LIVESTOCK_METRIC_OR_UNIT'), ('metric','futures-close','LIVESTOCK_METRIC_OR_UNIT'),
    ('price_kind','quote','LIVESTOCK_PRICE_BASIS'), ('market','futures','LIVESTOCK_PRICE_BASIS'),
    ('currency','USD','LIVESTOCK_PRICE_BASIS'), ('frequency','daily','LIVESTOCK_PRICE_BASIS'),
    ('assessment','period-average','LIVESTOCK_PRICE_BASIS'), ('period_kind','ytd-flow','LIVESTOCK_PRICE_BASIS'),
    ('period_start','2026-09-01','LIVESTOCK_COLLECTION_DAY'),
    ('region','unknown','LIVESTOCK_SCOPE_UNKNOWN'), ('value',None,'LIVESTOCK_PRICE_MISSING'),
    ('value','0','LIVESTOCK_NONPOSITIVE_PRICE'), ('value','-1','LIVESTOCK_NONPOSITIVE_PRICE'),
    ('value','NaN','INDUSTRY_DECIMAL'), ('value',True,'INDUSTRY_DECIMAL'),
])
def test_bad_current_point_not_zero(reading, field, value, code):
    point = deepcopy(reading['entries'][0]['points'][0]); point[field] = value
    out = _measure(point)
    assert out['status'] == 'unavailable' and out['value'] is None and out['reason_code'] == code


@pytest.mark.parametrize('days', [1, 6, 8, 14, -7])
def test_gap_not_weekly_growth(reading, days):
    from datetime import date, timedelta
    a, b = deepcopy(reading['entries'][0]['points'][:2])
    b['period_start'] = b['period_end'] = (date.fromisoformat(a['period_end']) + timedelta(days=days)).isoformat()
    out = _transition(a, b)
    assert out['reason_code'] == 'LIVESTOCK_NOT_ADJACENT_WEEK'
    assert out['delta'] is out['percent_change'] is None


@pytest.mark.parametrize('field,value', [('unit','CNY/bird'), ('region','another-region'),
                                        ('specification','another-grade'), ('tax_basis','different-tax')])
def test_basis_change_not_converted_or_spliced(reading, field, value):
    a, b = deepcopy(reading['entries'][0]['points'][:2]); b[field] = value
    assert _transition(a, b)['reason_code'] == 'INDUSTRY_BASIS_CHANGED'


def test_reported_base_is_not_backfill_or_restatement(reading):
    a, b = deepcopy(reading['entries'][0]['points'][:2]); b['reported']['cells']['reported_previous'] = '8'
    out = _transition(a, b)
    assert out['delta'] == '1' and out['published_previous_matches_prior_observation'] is False
    assert a['value'] == '10'


def test_reported_rounding_difference_is_diagnostic_not_price_failure(reading):
    point = deepcopy(reading['entries'][0]['points'][0]); point['reported']['cells']['reported_wow_pct'] = '11.2'
    out = _check(point, 'reported_previous', 'reported_wow_pct')
    assert out['status'] == 'rounding-difference' and out['rounded_pct'] == '11.1'
    assert point['status'] == 'read' and out['publisher_unrounded_inputs_verified'] is False


@pytest.mark.parametrize('field', REPORT_COLUMNS)
def test_reported_null_never_filled(reading, field):
    point = deepcopy(reading['entries'][0]['points'][0]); point['reported']['cells'][field] = None
    base, growth = (REPORT_COLUMNS[0], REPORT_COLUMNS[2]) if field in (REPORT_COLUMNS[0], REPORT_COLUMNS[2]) else (REPORT_COLUMNS[1], REPORT_COLUMNS[3])
    assert _check(point, base, growth)['status'] == 'unavailable'
    assert point['reported']['cells'][field] is None


def test_same_day_revision_and_regressed_publication(reading):
    a, b = deepcopy(reading['entries'][0]['points'][:2])
    b['period_start'] = b['period_end'] = a['period_end']
    assert _transition(a, b)['status'] == 'same-period-revision'
    b['source']['published_at'] = '2026-09-01'
    assert _transition(a, b)['reason_code'] == 'LIVESTOCK_RELEASE_REGRESSED'


def test_decimal_environment_independent(reading):
    point = reading['entries'][0]['points'][0]
    expected = _check(point, 'reported_previous', 'reported_wow_pct')
    with localcontext() as context:
        context.prec = 2; context.rounding = ROUND_DOWN; context.traps[Inexact] = True
        assert _check(point, 'reported_previous', 'reported_wow_pct') == expected


def test_auxiliary_missing_keeps_current_prices(saved):
    store, root, _ = saved
    class Reader:
        backend = store.backend
        def read_source(self, ref):
            if set(REPORT_COLUMNS) <= set(ref.selection.columns):
                raise RecordError('COLUMN_MISSING')
            return store.read_source(ref)
    out = read_livestock_weekly(root, Reader())
    assert out['status'] == 'partial' and out['scope']['unavailable_price_points'] == 0
    assert out['scope']['publisher_checks']['unavailable'] == 18
    assert out['entries'][0]['latest_change']['delta'] == '1'


def test_optional_rows_must_match_original_core(saved, reading):
    store, _, _ = saved
    class Reader:
        def read_source(self, ref):
            view = store.read_source(ref); view['data'][0]['value'] = '999'
            return view
    out = _reported(reading['entries'][0]['points'][0], Reader())
    assert out['status'] == 'unavailable' and out['cells'] is None


def test_selector_keeps_whole_denominator_and_version(saved, reading):
    store, root, _ = saved
    one = read_livestock_weekly(root, store, subject_id=SUBJECTS[1].entity_id)
    empty = read_livestock_weekly(root, store, company_id='unknown-company')
    assert len(one['entries']) == 1 and not empty['entries']
    for out in (one, empty):
        assert out['scope'] == reading['scope'] and out['view_version'] == reading['view_version']


def test_original_host_uses_same_result_without_writes(saved, reading):
    store, root, _ = saved; before = store.backend.head()
    payload = {'reference': asdict(root), 'view': 'livestock-weekly', 'public_sources_custody_confirmed': True}
    assert perform(store, payload) == reading
    assert store.backend.head() == before
    with pytest.raises(RecordError, match='PUBLIC_SOURCES'):
        perform(store, {**payload, 'public_sources_custody_confirmed': False})
    with pytest.raises(RecordError, match='ONE_QUERY_DIRECTION'):
        checked_payload({**payload, 'subject_id': 'x', 'company_id': 'y'})


def test_scope_exit_revocation_does_not_return_partial_result(saved):
    from contextlib import contextmanager
    store, root, _ = saved
    class Backend:
        @contextmanager
        def source_read_scope(self, commit):
            yield store.backend
            raise RecordError('SYNTHETIC_ACCESS_REVOKED')
    wrapper = VersionedStore(Backend())
    with pytest.raises(RecordError, match='ACCESS_REVOKED'):
        read_livestock_weekly(root, wrapper)


def test_cli_in_fresh_process_matches_original_host(saved, reading, tmp_path):
    store, root, _ = saved
    ref = tmp_path / 'ref.json'; ref.write_bytes(encoded(asdict(root)))
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT / 'src'), 'LANG': 'C.UTF-8'}
    out = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.industry_cli',
        '--path', str(store.backend.root), '--ref', str(ref), '--livestock-weekly'],
        cwd=tmp_path, env=env, capture_output=True, timeout=30, check=True)
    assert json.loads(out.stdout) == json.loads(encoded(reading))
