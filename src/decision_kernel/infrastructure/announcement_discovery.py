"""Finite public directory queries on the existing CNINFO transport; no PDF download."""
from datetime import datetime, timezone

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.cninfo_catalog import ANNOUNCEMENTS, OfficialCatalogTransport
from decision_kernel.observations.announcement_discovery import TERMS, MAX_PAGES, assemble, normalize_query, query_form, window


def capture(*, start, end, transport_factory=OfficialCatalogTransport, clock=None):
    window(start, end)  # Validate the entire finite plan before any request.
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    queries, halted, attempted = [], False, 0
    for term in TERMS:
        pages, error = [], None
        if halted:
            error = "NOT_ATTEMPTED_AFTER_SOURCE_REFUSAL"
        else:
            transport = transport_factory()  # Each declared query has <=4 pages; total <=28 calls.
            for number in range(1, MAX_PAGES + 1):
                try:
                    attempted += 1
                    page = transport(ANNOUNCEMENTS, query_form(term, start, end, number))
                    normalized = normalize_query(term, pages + [page], start=start, end=end)
                    pages.append(page)
                    if normalized["complete_query"] or not page.get("announcements"):
                        break
                except RecordError as exc:
                    error = str(exc)
                    # A refusal affects this whole provider, not only one keyword.
                    halted = error in ("CNINFO_HTTP_401", "CNINFO_HTTP_403", "CNINFO_HTTP_429")
                    break
        queries.append(normalize_query(term, pages, start=start, end=end, error=error))
    return {**assemble(queries, start=start, end=end, acquired_at=clock()),
            "source_hold": halted, "requests_attempted": attempted}
