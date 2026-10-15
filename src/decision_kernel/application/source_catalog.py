"""Bounded directory intake using the *existing* versioned source custody contract."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, identifier
from decision_kernel.observations.cninfo_catalog import Catalog, source_for


def catalog_view(catalog: Catalog) -> dict:
    """Explorable metadata only; no claims that the indexed PDFs were read."""
    return {
        "status": "complete-catalog" if catalog.total else "complete-empty-catalog",
        "source_mode": catalog.source_mode,
        "original_verified": False,
        "original_pdf_acquired": False,
        "stock_code": catalog.stock_code, "org_id": catalog.org_id,
        "start_date": catalog.start_date, "end_date": catalog.end_date,
        "captured_at": catalog.captured_at, "total": catalog.total,
        "page_count": max(1, (catalog.total + catalog.page_size - 1) // catalog.page_size),
        "items": [asdict(x) for x in catalog.announcements],
    }


def save_catalog(store: VersionedStore, catalog: Catalog, batch_id: str) -> dict:
    """Save each authorized locator under a replayable intent; never hide partial writes.

    A network/permission failure may happen after earlier complete atomic source writes.
    Those original source refs must be returned, never discarded or labelled full success.
    """
    identifier(batch_id)
    if len(batch_id) > 55:
        raise RecordError("CNINFO_BATCH_ID_TOO_LONG")
    results: list[dict] = []
    for item in catalog.announcements:
        ident = sha256((catalog.stock_code + ":" + item.announcement_id).encode("ascii")).hexdigest()[:24]
        submission_id = batch_id + "-" + ident
        try:
            source = source_for(item, catalog)
            ref = store.put_source(submission_id, source)
            verified = store.read_source(ref)
            if verified["data"] is not None or verified["availability"] != "external-only":
                raise RecordError("CNINFO_DIRECTORY_NOT_LOCATOR_ONLY")
            results.append({"announcement_id": item.announcement_id,
                            "submission_id": submission_id, "reference": asdict(ref),
                            "availability": "external-only"})
        except RecordError as exc:
            return {"status": "unknown-write" if "UNCONFIRMED" in str(exc) or "RECONCILIATION" in str(exc)
                    else "partial-failure", "error": str(exc), "saved": results,
                    "saved_count": len(results), "catalog_count": catalog.total,
                    "last_intent": submission_id, "complete": False}
    return {"status": "saved-catalog" if catalog.total else "saved-empty-catalog",
            "saved": results, "saved_count": len(results),
            "catalog_count": catalog.total, "complete": True}
