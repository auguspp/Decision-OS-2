"""Shared fixed-host Relay HTTP, extracted from the delivered disclosure transport.

No provider routing, retry, scheduling, cost quota, or untrusted URL selection.
"""
from __future__ import annotations
from decision_kernel.infrastructure.runtime_performance import count_event, measure_phase

from hashlib import sha256
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_failure import http_failure, transport_failure, retry_after, rate_limit_headers

ROOT = "https://pcd.mobcvb.cn/tushare/pro"
CAPABILITIES = "https://pcd.mobcvb.cn/tushare/capabilities"
SECRET_ENV = "TUSHARE_PROXY_API_KEY"
FINANCIAL_APIS = frozenset({"income", "balancesheet", "cashflow", "fina_indicator",
                            "forecast", "express", "fina_audit", "fina_mainbz"})
PARTICIPANT_APIS = frozenset({"report_rc", "research_report", "stk_surv"})
READ_APIS = FINANCIAL_APIS | PARTICIPANT_APIS | {"disclosure_date"}
MAX_BODY = 4 * 1024 * 1024  # Response safety, not a purchased-use allowance.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_TIMEOUT_SECONDS = 90


def validate_request_timeout_seconds(value):
    require(type(value) is int and DEFAULT_REQUEST_TIMEOUT_SECONDS <= value <= MAX_REQUEST_TIMEOUT_SECONDS,
            "RELAY_REQUEST_TIMEOUT_INVALID")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        return None


class RelayHTTP:
    """Only reviewed read APIs. Each call is one attempt; shared by batch and legacy API."""

    def __init__(self, *, opener=None, credential=None,
                 request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS):
        self._request_timeout_seconds = validate_request_timeout_seconds(request_timeout_seconds)
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._credential = credential if credential is not None else os.environ.get(SECRET_ENV, "")
        self.requests = 0
        self.last_response_meta = None
        self.last_response_bytes = None
        self.last_error_meta = None
        self.last_retry_after = ("missing", None)
        self.last_header_diagnostics = None

    @property
    def request_timeout_seconds(self):
        """Socket operation timeout, not a total HTTP wall-clock deadline."""
        return self._request_timeout_seconds

    def validate_credential(self):
        key = self._credential
        require(isinstance(key, str) and 8 <= len(key) <= 4096 and key.isascii()
                and all(33 <= ord(c) < 127 for c in key), "RELAY_CREDENTIAL_REQUIRED")
        return key

    def request(self, api: str, parameters: dict[str, str], *, max_bytes=MAX_BODY,
                agent="DecisionOS2-RelayFinancial/0.1") -> dict:
        require(isinstance(api, str) and api in READ_APIS, "RELAY_API_NOT_ALLOWED")
        required = {"ts_code", "fields", "limit", "end_date" if api == "disclosure_date" else "period"}
        optional = set() if api == "disclosure_date" else {"offset"}
        if api in {"income", "balancesheet", "cashflow"}:
            optional.add("report_type")
        if api == "fina_mainbz":
            optional.add("type")
        if api in PARTICIPANT_APIS:
            from decision_kernel.observations.relay_participants import validate_request
            validate_request(api, parameters)
            required = {"ts_code", "fields", "limit", "report_date" if api == "report_rc" else "trade_date"}
            optional = {"offset"}
        require(isinstance(parameters, dict) and required <= parameters.keys() <= required | optional,
                "RELAY_PARAMETERS_NOT_ALLOWED")
        require(all(isinstance(k, str) and isinstance(v, str) and 0 < len(v) <= 8192
                    and not any(ord(c) < 32 or ord(c) == 127 for c in v)
                    for k, v in parameters.items()), "RELAY_PARAMETER_INVALID")
        exact = ROOT + "/" + api + "?" + urlencode(parameters)
        return self._get_json(exact, max_bytes=max_bytes, agent=agent)

    def request_registered(self, registry, api, parameters):
        """Explicit business use of a trusted catalogue; not arbitrary host or method routing."""
        from decision_kernel.application.research_sources import parameters_for
        checked, _ = parameters_for(registry, api, parameters)
        return self._get_json(ROOT + "/" + api + "?" + urlencode(checked),
                              max_bytes=MAX_BODY, agent="DecisionOS2-ResearchSources/0.1")

    def capabilities(self, api: str | None = None) -> dict:
        """Read service metadata only; never invoke an API named in the response."""
        require(api is None or isinstance(api, str) and
                re.fullmatch(r"[a-z][a-z0-9_]{0,63}", api), "RELAY_CAPABILITY_NAME_INVALID")
        exact = CAPABILITIES + ("/" + api if api is not None else "")
        return self._get_json(exact, max_bytes=MAX_BODY, agent="DecisionOS2-Capabilities/0.1")

    def _get_json(self, exact, *, max_bytes, agent):
        # Only fixed-URL public methods above dispatch here.
        require(type(max_bytes) is int and 1 <= max_bytes <= MAX_BODY, "RELAY_RESPONSE_BOUND_INVALID")
        self.last_response_meta = None
        self.last_response_bytes = None
        self.last_error_meta = None
        self.last_retry_after = ("missing", None)
        self.last_header_diagnostics = None
        key = self.validate_credential()
        self.requests += 1
        count_event("relay_requests")
        request = Request(exact, method="GET", headers={"X-API-Key": key,
                          "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": agent})
        observed_response = None
        try:
            with measure_phase("relay.http"):
                count_event("relay_wire_attempts")
                with self._opener.open(request, timeout=self.request_timeout_seconds) as resp:
                    require(resp.status == 200 and resp.geturl() == exact, "RELAY_HTTP_RESPONSE_INVALID")
                    # Headers are already observed even if the body later stalls.
                    # Preserve their original wait instruction without inventing a
                    # successful response body or changing the transport category.
                    def safe_header(name):
                        value = resp.headers.get(name)
                        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) and key not in value else None
                    received_at = datetime.now(timezone.utc)
                    wait_metadata = retry_after(resp.headers, key, received_at)
                    self.last_header_diagnostics = {
                        'retry_after': {'state': wait_metadata[0], 'seconds': wait_metadata[1]},
                        **rate_limit_headers(resp.headers, key)}
                    observed_response = {
                        'http_status': 200, 'received_at': received_at.isoformat(),
                        'retry_after_state': wait_metadata[0], 'retry_after_seconds': wait_metadata[1],
                        'x_request_id': safe_header('X-Request-ID'), 'x_cache': safe_header('X-Cache')}
                    require(resp.headers.get("Content-Encoding", "identity").lower() in ("", "identity"),
                            "RELAY_CONTENT_ENCODING_INVALID")
                    mime = resp.headers.get("Content-Type", "application/json").split(";")[0].strip().lower()
                    require(mime in ("application/json", "text/json", "text/plain"), "RELAY_CONTENT_TYPE_INVALID")
                    length = resp.headers.get("Content-Length")
                    require(length is None or isinstance(length, str) and len(length) <= 12 and length.isascii()
                            and length.isdigit() and int(length) <= max_bytes, "RELAY_RESPONSE_BYTES_INVALID")
                    with measure_phase("relay.body"):
                        raw = resp.read(max_bytes + 1)
                    require(0 < len(raw) <= max_bytes and (length is None or len(raw) == int(length)),
                            "RELAY_RESPONSE_BYTES_INVALID")
                    meta = {"http_status": 200, "x_request_id": observed_response['x_request_id'],
                            "x_cache": observed_response['x_cache'], "wire_sha256": sha256(raw).hexdigest()}
        except HTTPError as exc:
            self.last_error_meta = http_failure(exc, key)
            self.last_header_diagnostics = {
                'retry_after': {'state': self.last_error_meta['retry_after_state'],
                                'seconds': self.last_error_meta['retry_after_seconds']},
                **rate_limit_headers(exc.headers or {}, key)}
            raise RecordError("RELAY_HTTP_" + str(exc.code)) from None
        except (URLError, OSError, TimeoutError, HTTPException) as exc:
            self.last_error_meta = transport_failure(exc)
            if observed_response is not None:
                self.last_error_meta.update(observed_response, error_body_state='unreadable')
            raise RecordError("RELAY_HTTP_UNAVAILABLE") from None
        with measure_phase("relay.json"):
            require(key.encode("ascii") not in raw, "RELAY_SECRET_REFLECTED")
            try:
                def unique(pairs):
                    out = {}
                    for k, v in pairs:
                        if k in out:
                            raise ValueError("duplicate")
                        out[k] = v
                    return out
                result = json.loads(raw, object_pairs_hook=unique,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                decoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (UnicodeError, ValueError, RecursionError):
                raise RecordError("RELAY_JSON_INVALID") from None
            require(key not in decoded, "RELAY_SECRET_REFLECTED")
            # Scan decoded strings too: JSON may escape quote/backslash characters in
            # a reflected credential, so serialised substring checks alone are not enough.
            pending = [result]
            while pending:
                value = pending.pop()
                if isinstance(value, str):
                    require(key not in value, "RELAY_SECRET_REFLECTED")
                elif isinstance(value, dict):
                    pending.extend(value.keys()); pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        self.last_response_meta = meta
        self.last_response_bytes = raw
        self.last_retry_after = wait_metadata
        return result
