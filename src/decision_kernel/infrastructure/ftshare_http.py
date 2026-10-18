"""Fixed-origin, one-attempt FTShare daily transport; no provider routing."""
from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.application.ftshare_daily import batch_request, _bounds, _symbol
from decision_kernel.domain.records import RecordError, digest, require
from decision_kernel.infrastructure.relay_failure import retry_after, rate_limit_headers, transport_failure

ROOT = 'https://market.ft.tech/gateway'
SECRET_ENV = 'FTSHARE_API_KEY'
MAX_BODY = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        return None


class FTShareHTTP:
    """Only explicit read endpoints; a socket timeout is not a wall-clock SLA."""

    def __init__(self, *, credential=None, opener=None, request_timeout_seconds=90):
        require(type(request_timeout_seconds) is int and 1 <= request_timeout_seconds <= 90,
                'FTSHARE_TIMEOUT_INVALID')
        self._credential = os.environ.get(SECRET_ENV, '') if credential is None else credential
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self.request_timeout_seconds = request_timeout_seconds
        self.requests = 0
        self.last_response_bytes = None
        self.last_response_meta = None
        self.last_error_meta = None
        self.last_retry_after = ('missing', None)

    def validate_credential(self):
        key = self._credential
        require(isinstance(key, str) and 8 <= len(key) <= 4096 and key.isascii()
                and all(33 <= ord(c) < 127 for c in key), 'FTSHARE_CREDENTIAL_REQUIRED')
        return key

    def candlesticks_batch(self, symbols, *, since: int, until: int, adjust_kind='none'):
        request = batch_request(symbols=symbols, since=since, until=until, adjust_kind=adjust_kind)
        params = request['parameters']
        return self._get('/api/v2/market/data/stock-candlesticks/batch', {
            'symbols': params['symbols'], 'interval_unit': 'day', 'adjust_kind': adjust_kind,
            'since_ts_millis': since, 'until_ts_millis': until})

    def stock_adjust_factor(self, symbol, *, since: int, until: int, page=1, page_size=1000):
        _symbol(symbol)
        lo, hi = _bounds(since, until)
        require(type(page) is int and page == 1 and type(page_size) is int
                and 1 <= page_size <= 1000, 'FTSHARE_FACTOR_PAGE_BOUND')
        return self._get('/api/v1/market/data/stock-adjust-factor', {
            'symbol': symbol, 'start_date': lo.date().isoformat(), 'end_date': hi.date().isoformat(),
            'page': page, 'page_size': page_size})

    def stock_list(self):
        return self._get('/api/v1/market/data/stock-list', {})

    def _headers(self, headers, key, status):
        now = datetime.now(timezone.utc)
        self.last_retry_after = retry_after(headers, key, now)
        def safe(name):
            value = headers.get(name)
            return value if isinstance(value, str) and key not in value and re.fullmatch(
                r'[A-Za-z0-9._-]{1,128}', value) else None
        return {'http_status': status, 'received_at': now.isoformat(),
            'retry_after_state': self.last_retry_after[0],
            'retry_after_seconds': self.last_retry_after[1],
            'rate_limits': rate_limit_headers(headers, key),
            'x_request_id': safe('X-Request-ID'), 'x_cache': safe('X-Cache')}

    def _get(self, path, parameters):
        require(path in ('/api/v2/market/data/stock-candlesticks/batch',
            '/api/v1/market/data/stock-adjust-factor', '/api/v1/market/data/stock-list'),
            'FTSHARE_ENDPOINT_NOT_ALLOWED')
        exact = ROOT + path + ('?' + urlencode(parameters, doseq=True) if parameters else '')
        self.last_response_bytes = self.last_response_meta = self.last_error_meta = None
        self.last_retry_after = ('missing', None)
        key = self.validate_credential()
        request = Request(exact, method='GET', headers={'FTSHARE_API_KEY': key,
            'X-Client-Name': 'ft-claw',
            'Accept': 'application/json', 'Content-Type': 'application/json',
            'Accept-Encoding': 'identity', 'User-Agent': 'DecisionOS2-FTShareDaily/0.1'})
        self.requests += 1
        observed = None
        try:
            with self._opener.open(request, timeout=self.request_timeout_seconds) as response:
                require(response.status == 200 and response.geturl() == exact,
                        'FTSHARE_HTTP_RESPONSE_INVALID')
                observed = self._headers(response.headers, key, 200)
                require(response.headers.get('Content-Encoding', 'identity').lower() in ('', 'identity'),
                        'FTSHARE_CONTENT_ENCODING_INVALID')
                mime = response.headers.get('Content-Type', 'application/json').split(';')[0].strip().lower()
                require(mime in ('application/json', 'text/json', 'text/plain'), 'FTSHARE_CONTENT_TYPE_INVALID')
                length = response.headers.get('Content-Length')
                require(length is None or isinstance(length, str) and re.fullmatch(r'[0-9]{1,10}', length)
                        and int(length) <= MAX_BODY, 'FTSHARE_RESPONSE_BYTES_INVALID')
                raw = response.read(MAX_BODY + 1)
                require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY
                        and (length is None or len(raw) == int(length)), 'FTSHARE_RESPONSE_BYTES_INVALID')
        except HTTPError as exc:
            try:
                status = exc.code
                self.last_error_meta = self._headers(exc.headers or {}, key, status)
                self.last_error_meta['classification'] = ('auth-or-entitlement' if status in (401, 403)
                    else 'rate-limit' if status == 429 else 'http-error')
            finally:
                exc.close()  # No arbitrary supplier error text is retained or logged.
            raise RecordError('FTSHARE_HTTP_' + str(status)) from None
        except (URLError, OSError, TimeoutError, HTTPException) as exc:
            self.last_error_meta = {**transport_failure(exc), **(observed or {})}
            raise RecordError('FTSHARE_HTTP_UNAVAILABLE') from None
        except RecordError:
            self.last_error_meta = observed
            raise
        require(key.encode('ascii') not in raw, 'FTSHARE_SECRET_REFLECTED')
        try:
            def unique(pairs):
                out = {}
                for name, value in pairs:
                    if name in out:
                        raise ValueError('duplicate')
                    out[name] = value
                return out
            result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_float=str,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError, RecursionError):
            raise RecordError('FTSHARE_JSON_INVALID') from None
        pending = [result]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                require(key not in item, 'FTSHARE_SECRET_REFLECTED')
            elif isinstance(item, dict):
                pending.extend(item.keys()); pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        self.last_response_meta = {**observed, 'wire_sha256': digest(raw), 'wire_bytes': len(raw)}
        self.last_response_bytes = raw
        return result
