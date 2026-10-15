"""Delivered single disclosure lookup; HTTP mechanics shared with financial batches."""
from __future__ import annotations

from datetime import datetime, timezone
from decision_kernel.domain.records import require
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT, SECRET_ENV, _NoRedirect
from decision_kernel.observations.relay_disclosure_date import (
    FIELDS, MAX_ROWS, normalize_disclosure_dates, ticker, report_period,
)

HOST = ROOT + "/disclosure_date"
MAX_BODY = 512 * 1024


class RelayDisclosureTransport(RelayHTTP):
    """Keep the existing single-operation contract and response receipt unchanged."""
    def __call__(self, ts_code: str, end_date: str, limit: int) -> dict:
        ticker(ts_code); report_period(end_date)
        require(type(limit) is int and 1 <= limit <= MAX_ROWS, "RELAY_ROW_BUDGET_INVALID")
        self.validate_credential()
        require(self.requests == 0, "RELAY_ONE_REQUEST_PER_OPERATION")
        return self.request("disclosure_date", {"ts_code": ts_code, "end_date": end_date,
                            "fields": ",".join(FIELDS), "limit": str(limit)},
                            max_bytes=MAX_BODY, agent="DecisionOS2-RelayDisclosureDate/0.1")


def fetch_disclosure_dates(*, ts_code: str, end_date: str, limit: int = MAX_ROWS, transport=None):
    ticker(ts_code); report_period(end_date)
    require(type(limit) is int and 1 <= limit <= MAX_ROWS, "RELAY_ROW_BUDGET_INVALID")
    fetch = transport if transport is not None else RelayDisclosureTransport()
    response = fetch(ts_code, end_date, limit)
    return normalize_disclosure_dates(ts_code=ts_code, end_date=end_date, raw=response,
        captured_at=datetime.now(timezone.utc).isoformat(), source_mode="live-relay", limit=limit,
        response_transport=getattr(fetch, "last_response_meta", None))
