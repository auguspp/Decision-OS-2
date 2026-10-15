"""Offline manual and captured-service catalogue. No request or execution authority."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from datetime import datetime
from pathlib import Path
import re

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_http import FINANCIAL_APIS

MAX_FILE = 2 * 1024 * 1024
ROW = re.compile(r'^\| `([a-z0-9_]+)` \| (GET|POST|DELETE) \| ([^|]+)\|')


def _read(path: Path) -> bytes:
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE + 1)
    require(len(raw) <= MAX_FILE, 'RELAY_REGISTRY_FILE_TOO_LARGE')
    return raw


def _json(raw: bytes):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, 'RELAY_REGISTRY_DUPLICATE_KEY')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


class RelayRegistry:
    """A repository-supplied index and its byte-bound manual. This class never uses a key."""

    def __init__(self, path: Path):
        try:
            path = Path(path).resolve()
            data = _json(_read(path))
            require((data['schema'], data['basis']) in {
                        ('relay-functional-registry-v1', 'archived-manual-not-live-capabilities'),
                        ('relay-functional-registry-v2', 'archived-manual-and-captured-capabilities')}, 'RELAY_REGISTRY_SCHEMA')
            rel = Path(data['manual']['path'])
            require(not rel.is_absolute() and '..' not in rel.parts, 'RELAY_REGISTRY_PATH')
            manual = (path.parent / rel).resolve()
            require(manual.is_relative_to(path.parent), 'RELAY_REGISTRY_PATH')
            raw = _read(manual)
            require(sha256(raw).hexdigest() == data['manual']['sha256'], 'RELAY_REGISTRY_MANUAL_CHANGED')
            rows = {}
            for number, text in enumerate(raw.decode('utf-8').splitlines(), 1):
                match = ROW.match(text)
                if not match:
                    continue
                name, method, status = match.groups()
                require(name not in rows, 'RELAY_REGISTRY_DUPLICATE_API')
                cells = [cell.strip() for cell in text.strip().strip('|').split('|')]
                # Preserve broken tables verbatim. Never shift cells to guess a missing provider.
                regular = len(cells) == 5
                rows[name] = {'api': name, 'method': method, 'manual_status': status.strip(),
                    'manual_enabled': {'可用': True, '未启用': False}.get(status.strip()),
                    'provider_note': cells[3] if regular else None,
                    'parameter_note': cells[4] if regular else None,
                    'table_shape': 'five-cells' if regular else 'ambiguous-see-original',
                    'source_line': number, 'raw_row': text}
            counts = Counter(row['manual_status'] for row in rows.values())
            require(len(rows) == data['manual']['observed_rows']
                    and counts['可用'] == data['manual']['observed_available_rows']
                    and counts['未启用'] == data['manual']['observed_disabled_rows'], 'RELAY_REGISTRY_COUNTS')
            groups, assigned = {}, {}
            for group in data['groups']:
                require(isinstance(group['id'], str) and group['id'] not in groups, 'RELAY_REGISTRY_GROUP')
                require(isinstance(group['apis'], list) and group['apis'], 'RELAY_REGISTRY_GROUP')
                groups[group['id']] = group
                for api in group['apis']:
                    require(isinstance(api, str) and api in rows and api not in assigned, 'RELAY_REGISTRY_ASSIGNMENT')
                    assigned[api] = group['id']
            require(set(assigned) == set(rows), 'RELAY_REGISTRY_COVERAGE')
            live, snapshot = {}, None
            if data['schema'] == 'relay-functional-registry-v1':
                require(data['live_catalogue']['status'] == 'not-obtained'
                        and data['live_catalogue']['enabled_count'] is None, 'RELAY_REGISTRY_LIVE_UNSUPPORTED')
            else:
                live, snapshot = _snapshot(path.parent, data['live_catalogue'])
                overrides = data.get('live_group_overrides', {})
                require(isinstance(overrides, dict), 'RELAY_REGISTRY_ASSIGNMENT')
                for api, group_id in overrides.items():
                    require(api in live and group_id in groups and live[api].get('description'),
                            'RELAY_REGISTRY_ASSIGNMENT')
                    assigned[api] = group_id
                for api in live:
                    assigned.setdefault(api, 'needs-review')
                require('needs-review' in groups, 'RELAY_REGISTRY_GROUP')
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError('RELAY_REGISTRY_INVALID') from None
        self.data, self.rows, self.groups, self.assigned = data, rows, groups, assigned
        self.live, self.snapshot = live, snapshot

    def lookup(self, api: str) -> dict:
        require(isinstance(api, str) and api in self.rows.keys() | self.live.keys(), 'RELAY_REGISTRY_NOT_FOUND')
        group = self.groups[self.assigned[api]]
        evidence = self.data['business_evidence']
        sample = evidence['api_results'].get(api)
        manual = self.rows.get(api, {'api': api, 'method': None, 'manual_status': None,
            'manual_enabled': None, 'provider_note': None, 'parameter_note': None,
            'table_shape': 'not-in-archived-manual', 'source_line': None, 'raw_row': None})
        service = self.live.get(api)
        fields = ('enabled', 'methods', 'required', 'required_any', 'probe_example')
        return deepcopy({**manual, 'manual_present': api in self.rows,
            'group': group['id'], 'group_title': group['title'],
            'suggested_consumers': group['suggested_consumers'],
            'classification_basis': self.data['classification_basis'],
            'interpretation': group['interpretation'],
            'manual_sha256': self.data['manual']['sha256'],
            'live_enabled': service.get('enabled') if service is not None else None,
            'live_methods': service.get('methods') if service is not None else None,
            'required': service.get('required') if service is not None else None,
            'required_any': service.get('required_any') if service is not None else None,
            'probe_example': service.get('probe_example') if service is not None else None,
            'machine_contract_status': ('captured-service-declaration-not-business-validation' if service is not None
                                        else 'not-obtained-use-manual-notes-with-qualification'),
            'machine_field_presence': {f: ('missing' if service is None or f not in service
                                           else 'null' if service[f] is None else 'value') for f in fields},
            'service_metadata': service,
            'snapshot': self.snapshot,
            'status_disagrees_with_manual': (manual['manual_enabled'] != service.get('enabled')
                if manual['manual_enabled'] is not None and service is not None
                and type(service.get('enabled')) is bool else None),
            'shared_relay_client_admits_api': api in FINANCIAL_APIS | {'disclosure_date'},
            'scoped_business_evidence': None if sample is None else {
                'run_id': evidence['run_id'], 'scope': evidence['scope'], **sample,
                'qualification': evidence['qualification']},
            'execution_authority': 'not-granted-by-registry',
            'private_or_write_scope': api.startswith('p_') or
                any(m != 'GET' for m in (service.get('methods') or []) if isinstance(m, str))
                if service is not None else api.startswith('p_') or manual['method'] != 'GET'})

    def select(self, *, group: str | None = None, module: str | None = None, enabled: bool | None = None) -> list[dict]:
        require(group is None or group in self.groups, 'RELAY_REGISTRY_GROUP_NOT_FOUND')
        require(module is None or module in {'#7', '#10', '#11', '#12', '#13'}, 'RELAY_REGISTRY_MODULE')
        require(enabled is None or type(enabled) is bool, 'RELAY_REGISTRY_ENABLED_FILTER')
        return [self.lookup(api) for api in sorted(self.rows.keys() | self.live.keys())
                if (group is None or self.assigned[api] == group)
                and (module is None or module in self.groups[self.assigned[api]]['suggested_consumers'])
                and (enabled is None or self.live.get(api, {}).get('enabled') is enabled)]


    def summary(self) -> dict:
        names = self.rows.keys() | self.live.keys()
        return {'registered': len(names), 'manual_count': len(self.rows),
                'captured_service_count': len(self.live),
                'enabled_at_capture': sum(v.get('enabled') is True for v in self.live.values()),
                'disabled_at_capture': sum(v.get('enabled') is False for v in self.live.values()),
                'unknown_at_capture': sum(type(v.get('enabled')) is not bool for v in self.live.values()),
                'added_vs_manual': sorted(self.live.keys() - self.rows.keys()),
                'not_in_snapshot': sorted(self.rows.keys() - self.live.keys()) if self.snapshot else None,
                'status_differences': [api for api in sorted(names)
                                       if self.lookup(api)['status_disagrees_with_manual']],
                'groups': dict(Counter(self.assigned[api] for api in names)),
                'snapshot': deepcopy(self.snapshot), 'business_validation': 'not-performed-by-catalogue'}

    def check_parameters(self, api: str, parameters: dict, *, method: str = 'GET') -> dict:
        """Check only the captured gateway requirements; never issue or authorize a request."""
        item = self.lookup(api)
        require(isinstance(parameters, dict) and all(isinstance(k, str) for k in parameters),
                'RELAY_REGISTRY_PARAMETERS')
        require(isinstance(method, str), 'RELAY_REGISTRY_METHOD')
        live = item['service_metadata']
        reasons, missing, alternatives = [], [], []
        if item['private_or_write_scope'] or method != 'GET':
            reasons.append('private-or-write-scope-not-admitted')
        if live is None:
            reasons.append('machine-contract-not-obtained')
        else:
            if live.get('enabled') is not True:
                reasons.append('disabled-or-unknown-at-capture')
            if method not in (live.get('methods') or []):
                reasons.append('method-not-declared')
            def supplied(key):
                value = parameters.get(key)
                return value is not None and value != '' and (not isinstance(value, str) or bool(value.strip()))
            if live.get('required') is None or live.get('required_any') is None:
                reasons.append('parameter-contract-incomplete')
            else:
                missing = [key for key in live['required'] if not supplied(key)]
                alternatives = live['required_any']
                if missing:
                    reasons.append('required-parameters-missing')
                if alternatives and not any(all(supplied(key) for key in group) for group in alternatives):
                    reasons.append('required-alternative-unsatisfied')
        return {'api': api, 'gateway_requirements_satisfied': not reasons, 'reasons': reasons,
                'missing_required': missing, 'required_alternatives': deepcopy(alternatives),
                'snapshot': deepcopy(self.snapshot), 'dispatch_performed': False,
                'execution_authority': 'not-granted-by-registry',
                'qualification': 'gateway-required-fields-only-not-upstream-fields-values-coverage-or-permission'}


def _bound_path(root: Path, value: str) -> Path:
    require(isinstance(value, str), 'RELAY_REGISTRY_PATH')
    rel = Path(value)
    require(not rel.is_absolute() and '..' not in rel.parts, 'RELAY_REGISTRY_PATH')
    full = (root / rel).resolve()
    require(full.is_relative_to(root), 'RELAY_REGISTRY_PATH')
    return full


def _snapshot(root: Path, spec: dict) -> tuple[dict, dict]:
    require(spec['status'] == 'captured' and spec['encoding'] == 'utf-8' and
            all(type(spec[k]) is int and spec[k] >= 0 for k in
                ('bytes', 'count', 'enabled_count', 'disabled_count')), 'RELAY_SNAPSHOT_SPEC')
    raw = _read(_bound_path(root, spec['path']))
    require(len(raw) <= MAX_FILE and len(raw) == spec['bytes'] and
            sha256(raw).hexdigest() == spec['sha256'], 'RELAY_SNAPSHOT_RAW_HASH')
    receipt_raw = _read(_bound_path(root, spec['receipt_path']))
    require(sha256(receipt_raw).hexdigest() == spec['receipt_sha256'], 'RELAY_SNAPSHOT_RECEIPT_HASH')
    receipt, body = _json(receipt_raw), _json(raw)
    require(receipt['status'] == 'captured' and receipt['business_requests'] == 0 and
            receipt['sha256'] == spec['sha256'] and receipt['bytes'] == spec['bytes'] and
            receipt['transport']['http_status'] == 200 and
            receipt['transport']['wire_sha256'] == spec['sha256'] and
            all(receipt[k] == spec[k] for k in ('run_id', 'run_attempt', 'code_sha', 'endpoint')) and
            receipt['received_at'] == spec['captured_at'] and
            spec['endpoint'] == 'https://pcd.mobcvb.cn/tushare/capabilities', 'RELAY_SNAPSHOT_IDENTITY')
    received = datetime.fromisoformat(spec['captured_at'].replace('Z', '+00:00'))
    requested = datetime.fromisoformat(receipt['requested_at'].replace('Z', '+00:00'))
    require(received.utcoffset() is not None and requested.utcoffset() is not None and received >= requested,
            'RELAY_SNAPSHOT_CLOCK')
    require(re.fullmatch(r'[a-f0-9]{40}', spec['code_sha']) and
            re.fullmatch(r'[1-9][0-9]*', spec['run_id']) and re.fullmatch(r'[1-9][0-9]*', spec['run_attempt']),
            'RELAY_SNAPSHOT_IDENTITY')
    items = body['interfaces']
    require(isinstance(items, list) and 0 < len(items) <= 5000 and type(body['count']) is int
            and len(items) == body['count'] == spec['count'], 'RELAY_SNAPSHOT_COUNT')
    parsed = {}
    for item in items:
        require(isinstance(item, dict) and isinstance(item.get('name'), str) and
                re.fullmatch(r'[a-z][a-z0-9_]{0,63}', item['name']) and item['name'] not in parsed,
                'RELAY_SNAPSHOT_NAME')
        require(item.get('enabled') is None or type(item['enabled']) is bool, 'RELAY_SNAPSHOT_ENABLED')
        for field in ('required', 'methods'):
            values = item.get(field)
            require(values is None or isinstance(values, list) and
                    all(isinstance(s, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', s) for s in values)
                    and len(values) == len(set(values)), 'RELAY_SNAPSHOT_FIELDS')
        groups = item.get('required_any')
        require(groups is None or isinstance(groups, list) and all(
            isinstance(g, list) and g and len(g) == len(set(g)) and all(
                isinstance(s, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', s) for s in g)
            for g in groups), 'RELAY_SNAPSHOT_FIELDS')
        parsed[item['name']] = item  # All supplier fields, including unknown/null fields, stay intact.
    require(sum(i.get('enabled') is True for i in items) == spec['enabled_count'] and
            sum(i.get('enabled') is False for i in items) == spec['disabled_count'], 'RELAY_SNAPSHOT_COUNT')
    return parsed, {k: spec[k] for k in ('captured_at', 'sha256', 'code_sha', 'run_id', 'run_attempt', 'endpoint')}
