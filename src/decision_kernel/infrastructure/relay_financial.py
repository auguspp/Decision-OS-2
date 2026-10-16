"""Capture explicit financial queries; page within a query, never silently switch APIs."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.observations.relay_financial import CAPTURE_SCHEMA, FinancialQuery, normalize_page


def capture_query(query: FinancialQuery, *, transport=None, page_size=100, max_pages=100, clock=None,
                  query_type=FinancialQuery, schema=CAPTURE_SCHEMA, normalizer=None, failure_details=False):
    """The max_pages guard is caller-controlled run safety, not a consumption quota.

    Offset compatibility is validated by the returned pages. Unsupported parameters,
    repeated pages and source failure remain failed captures, never complete tables.
    """
    normalizer = normalize_page if normalizer is None else normalizer
    require(isinstance(query, query_type), "FINANCIAL_QUERY_INVALID")
    query.parameters(page_size=page_size)
    require(type(max_pages) is int and max_pages > 0, "FINANCIAL_PAGE_BOUND_INVALID")
    client = transport if transport is not None else RelayHTTP()
    now = clock or (lambda: datetime.now(timezone.utc).isoformat())
    capture = {"schema": schema, "query": asdict(query), "page_size": page_size,
               "source_mode": "live-relay", "pages": [], "error": None}
    offset, seen, captured_bytes, declared_rows = 0, set(), 0, 0
    for _ in range(max_pages):
        try:
            body = client.request(query.api, query.parameters(offset=offset, page_size=page_size))
            if isinstance(body, dict) and body.get("error") in ("rate_limited", "ip_rate_limited"):
                raise RecordError("RELAY_RATE_LIMIT")
            if isinstance(body, dict) and body.get("error") in ("unauthorized", "forbidden"):
                raise RecordError("RELAY_AUTH_OR_ENTITLEMENT")
            captured_bytes += len(encoded(body))
            require(captured_bytes <= 8 * 1024 * 1024, "FINANCIAL_CAPTURE_MEMORY_LIMIT")
            stamp = now()
            page = normalizer(query, body, captured_at=stamp, page_size=page_size, offset=offset)
            fingerprint = digest(encoded(body["data"]))
            require(not page["row_count"] or fingerprint not in seen, "FINANCIAL_PAGINATION_STALLED")
            seen.add(fingerprint)
            capture["pages"].append({"offset": offset, "captured_at": stamp, "body": body,
                                      "transport": getattr(client, "last_response_meta", None)})
            offset += page["row_count"]
            declared_rows = max(declared_rows, page["reported_count"] or 0)
            if page["row_count"] < page_size:
                if declared_rows > offset:
                    capture["error"] = "FINANCIAL_COUNT_SUGGESTS_TRUNCATION"
                return capture
        except RecordError as exc:
            capture["error"] = str(exc)
            if failure_details:
                capture["failure"] = getattr(client, "last_error_meta", None)
            return capture
    capture["error"] = "FINANCIAL_PAGE_BOUND_REACHED"
    return capture
