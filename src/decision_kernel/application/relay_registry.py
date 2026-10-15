"""Offline index over an archived manual, not a live capability or authority registry."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
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
            require(data['schema'] == 'relay-functional-registry-v1'
                    and data['basis'] == 'archived-manual-not-live-capabilities', 'RELAY_REGISTRY_SCHEMA')
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
            # This version intentionally cannot assert live enabled, even if someone edits the index.
            require(data['live_catalogue']['status'] == 'not-obtained'
                    and data['live_catalogue']['enabled_count'] is None, 'RELAY_REGISTRY_LIVE_UNSUPPORTED')
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError('RELAY_REGISTRY_INVALID') from None
        self.data, self.rows, self.groups, self.assigned = data, rows, groups, assigned

    def lookup(self, api: str) -> dict:
        require(isinstance(api, str) and api in self.rows, 'RELAY_REGISTRY_NOT_FOUND')
        group = self.groups[self.assigned[api]]
        evidence = self.data['business_evidence']
        sample = evidence['api_results'].get(api)
        return deepcopy({**self.rows[api], 'group': group['id'], 'group_title': group['title'],
            'suggested_consumers': group['suggested_consumers'],
            'classification_basis': self.data['classification_basis'],
            'interpretation': group['interpretation'],
            'manual_sha256': self.data['manual']['sha256'],
            'live_enabled': None, 'live_methods': None,
            'required': None, 'required_any': None, 'probe_example': None,
            'machine_contract_status': 'not-obtained-use-manual-notes-with-qualification',
            'shared_relay_client_admits_api': api in FINANCIAL_APIS | {'disclosure_date'},
            'scoped_business_evidence': None if sample is None else {
                'run_id': evidence['run_id'], 'scope': evidence['scope'], **sample,
                'qualification': evidence['qualification']},
            'execution_authority': 'not-granted-by-registry',
            'private_or_write_scope': api.startswith('p_') or self.rows[api]['method'] != 'GET'})

    def select(self, *, group: str | None = None, module: str | None = None) -> list[dict]:
        require(group is None or group in self.groups, 'RELAY_REGISTRY_GROUP_NOT_FOUND')
        require(module is None or module in {'#7', '#10', '#11', '#12', '#13'}, 'RELAY_REGISTRY_MODULE')
        return [self.lookup(api) for api in sorted(self.rows)
                if (group is None or self.assigned[api] == group)
                and (module is None or module in self.groups[self.assigned[api]]['suggested_consumers'])]
