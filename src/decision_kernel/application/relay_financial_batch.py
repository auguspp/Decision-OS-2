"""Stream capture or explicit saves; no second store and no fresh fetch during save."""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.domain.records import RecordError, UnknownWrite, identifier, require
from decision_kernel.observations.relay_financial import FinancialQuery, inspect_capture, source_for_page

STOP_SOURCE_ERRORS = {"RELAY_CREDENTIAL_REQUIRED", "RELAY_HTTP_401", "RELAY_HTTP_403", "RELAY_HTTP_429", "RELAY_SECRET_REFLECTED", "RELAY_RATE_LIMIT", "RELAY_AUTH_OR_ENTITLEMENT"}


def capture_batch(queries, loader, *, query_type=FinancialQuery, inspector=None):
    """Independent API errors are visible; shared authentication/rate refusal stops traffic."""
    inspector = inspect_capture if inspector is None else inspector
    queries = tuple(queries)
    require(bool(queries) and all(isinstance(q, query_type) for q in queries), "FINANCIAL_PLAN_INVALID")
    require(len({q.key for q in queries}) == len(queries), "FINANCIAL_DUPLICATE_QUERY")
    stopped = None
    for query in queries:
        if stopped:
            yield {"status": "not-attempted", "query": asdict(query), "error": stopped, "capture": None}
            continue
        capture = loader(query)
        require(isinstance(capture, dict) and capture.get("query") == asdict(query), "FINANCIAL_CAPTURE_SCOPE_MISMATCH")
        view = inspector(capture)
        error = capture.get("error")
        if error in STOP_SOURCE_ERRORS:
            stopped = error
        yield {"status": "captured" if view["request_complete"] else "capture-incomplete",
               "query": asdict(query), "error": error, "capture": capture}


def save_capture(capture, store_factory, batch_id, *, public_rights_confirmed=False, inspector=None, source_builder=None):
    """Each page is an existing atomic SourceInput submission, not a batch transaction.

    A new scoped remote API client per page avoids inheriting another page's runtime
    request counter. Server limits still apply. Saved receipts survive later failures.
    """
    inspector = inspect_capture if inspector is None else inspector
    source_builder = source_for_page if source_builder is None else source_builder
    identifier(batch_id)
    require(len(batch_id) <= 48, "FINANCIAL_BATCH_ID_TOO_LONG")
    require(public_rights_confirmed is True, "FINANCIAL_PUBLIC_RIGHTS_REQUIRED")
    view = inspector(capture)
    require(view["request_complete"], "FINANCIAL_INCOMPLETE_CAPTURE_CANNOT_SAVE")
    sources = [source_builder(capture, view, n) for n in range(len(view["pages"]))]
    saved = []
    for page, source in zip(view["pages"], sources):
        submission = batch_id + "-" + view["query_key"][:24] + "-" + str(page["offset"])
        identifier(submission)
        reference = None
        try:
            store = store_factory()
            reference = store.put_source(submission, source)
            readback = store.read_source(reference)
            require(readback["availability"] == "stored-representation" and readback["data"] == source.text,
                    "FINANCIAL_READBACK_MISMATCH")
            saved.append({"submission_id": submission, "reference": asdict(reference), "offset": page["offset"]})
        except RecordError as exc:
            return {"complete": False, "status": "saved-reference-unverified" if reference is not None else
                    "unknown-write" if isinstance(exc, UnknownWrite) else "save-failed",
                    "error": str(exc), "query_key": view["query_key"], "last_intent": submission,
                    "pending_reference": asdict(reference) if reference is not None else None,
                    "reconciliation_required": True, "saved": saved}
    return {"complete": True, "status": "saved", "query_key": view["query_key"], "saved": saved}
