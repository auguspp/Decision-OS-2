"""Safe Relay failure facts and finite metadata retry decisions; never echoes error prose.

Uses the vendor manual section 6/9/11 and the legacy Relay error categories.
The existing business transport remains single-attempt. No alternate hosts or keys.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, IncompleteRead
import json
import math
import re
import socket
import ssl
from urllib.error import HTTPError, URLError

ERROR_BYTES = 16 * 1024
KNOWN_ERRORS = frozenset({
    "invalid_params", "unauthorized", "forbidden", "unknown_api", "method_not_allowed",
    "rate_limited", "ip_rate_limited", "data_source_unavailable", "minute_data_pending",
    "upstream_timeout", "citydata_timeout", "upstream_pool_exhausted",
})


def _header(headers, name, key):
    value = headers.get(name)
    return value if isinstance(value, str) and key not in value and re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", value) else None


def retry_after(headers, key: str, now: datetime) -> tuple[str, int | None]:
    """Missing/invalid is not zero. Preserve long waits, never clamp them down."""
    values = headers.get_all("Retry-After") if hasattr(headers, "get_all") else None
    if values is not None and len(values) != 1:
        return "invalid", None
    raw = headers.get("Retry-After")
    if raw is None:
        return "missing", None
    if not isinstance(raw, str) or len(raw) > 128 or any(ord(c) < 32 or ord(c) > 126 for c in raw):
        return "invalid", None
    if key in raw:
        return "withheld", None
    raw = raw.strip()
    if re.fullmatch(r"[0-9]{1,12}", raw):
        return "valid", int(raw)
    try:
        target = parsedate_to_datetime(raw)
        if target.utcoffset() is None:
            return "invalid", None
        return "valid", max(0, math.ceil((target - now).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return "invalid", None


def _error_code(raw: bytes, key: str) -> tuple[str, str | None]:
    if not raw:
        return "empty", None
    if len(raw) > ERROR_BYTES:
        return "oversized", None
    if key.encode("ascii") in raw:
        return "withheld", None
    try:
        def unique(pairs):
            out = {}
            for name, value in pairs:
                if name in out:
                    raise ValueError("duplicate")
                out[name] = value
            return out
        body = json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # Inspect decoded strings, but retain no arbitrary message, token or body.
        pending = [body]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and key in item:
                return "withheld", None
            if isinstance(item, dict):
                pending.extend(item.keys()); pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        code = body.get("error") if isinstance(body, dict) else None
        return "parsed", code if isinstance(code, str) and code in KNOWN_ERRORS else None
    except (UnicodeError, ValueError, RecursionError):
        return "invalid-json", None


def http_failure(exc: HTTPError, key: str, *, now: datetime | None = None) -> dict:
    """Read a bounded error envelope, retain only reviewed codes and safe headers."""
    now = now or datetime.now(timezone.utc)
    headers = exc.headers or {}
    state, wait = retry_after(headers, key, now)
    body_state, code = "unreadable", None
    try:
        if headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            body_state = "unsupported-encoding"
        else:
            body_state, code = _error_code(exc.read(ERROR_BYTES + 1), key)
    except (OSError, HTTPException):
        pass
    finally:
        exc.close()
    status = exc.code
    if status in (401, 403) or code in {"unauthorized", "forbidden"}:
        category = "auth-or-entitlement"
    elif status == 429 or code in {"rate_limited", "ip_rate_limited"}:
        category = "rate-limit"
    elif code == "data_source_unavailable":
        category = "source-unavailable"
    elif status in (400, 404, 405) or code in {"invalid_params", "unknown_api", "method_not_allowed"}:
        category = "request-rejected"
    elif code in {"minute_data_pending", "upstream_timeout", "citydata_timeout", "upstream_pool_exhausted"}:
        category = "temporary-upstream"
    elif status in (502, 504):
        category = "temporary-gateway"
    else:
        category = "http-unclassified"  # Bare 503 is not automatically a transient timeout.
    return {"http_status": status, "classification": category, "business_error": code,
            "error_body_state": body_state, "retry_after_state": state,
            "retry_after_seconds": wait, "received_at": now.isoformat(),
            "x_request_id": _header(headers, "X-Request-ID", key),
            "x_cache": _header(headers, "X-Cache", key)}


def transport_failure(exc: BaseException) -> dict:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        category = "tls-certificate"
    elif isinstance(reason, ssl.SSLError):
        category = "tls"
    elif isinstance(reason, socket.gaierror):
        category = "dns-temporary" if reason.errno == socket.EAI_AGAIN else "dns"
    elif isinstance(reason, TimeoutError):
        category = "timeout"
    elif isinstance(reason, IncompleteRead):
        category = "incomplete-read"
    elif isinstance(reason, ConnectionError):
        category = "connection"
    elif isinstance(reason, HTTPException):
        category = "http-protocol"
    else:
        category = "transport-unclassified"
    return {"http_status": None, "classification": category,
            "retry_after_state": "missing", "retry_after_seconds": None}


def metadata_retry_delay(failure: dict | None, attempt: int) -> int | None:
    """Read-only metadata policy. Callers separately enforce attempt and total-wait bounds."""
    if not isinstance(failure, dict):
        return None
    category, state = failure.get("classification"), failure.get("retry_after_state")
    if category not in {"rate-limit", "temporary-upstream", "temporary-gateway", "timeout",
                        "connection", "dns-temporary", "incomplete-read"}:
        return None
    if state in {"invalid", "withheld"} or category == "rate-limit" and state != "valid":
        return None
    wait = failure.get("retry_after_seconds")
    if state == "valid" and (type(wait) is not int or wait < 0):
        return None
    return max(2 ** (attempt - 1), wait or 0)
