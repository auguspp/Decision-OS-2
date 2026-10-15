"""Source catalog consumer uses the existing VersionedStore and precise SourceRef."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, identifier
from decision_kernel.observations.broker_report_catalog import BrokerCatalog, source_for

# A single GitHubAPI instance has a strict finite read/write request budget.
# Do not begin a known-unfinishable 200-item remote submission batch.
MAX_SAVE_REPORTS = 12


def catalog_view(catalog: BrokerCatalog, *, keyword: str = "") -> dict:
    if not isinstance(keyword,str) or len(keyword.encode("utf-8"))>256:
        raise RecordError("BROKER_QUERY_INVALID")
    query=keyword.casefold()
    results=[x for x in catalog.reports if x.type_status!="COLUMN_CONFLICT"
             and (not query or query in " ".join(z or "" for z in
                (x.title,x.institution,x.industry,x.publication_date,x.identity_value)).casefold())]
    return {"status":"provider-pages-read" if catalog.provider_rows else "provider-empty-result",
            "coverage": catalog.coverage,"captured_at":catalog.captured_at,
            "source_mode":catalog.source_mode,"original_verified":False,
            "report_pdf_read":False,"scope":asdict(catalog.scope),
            "provider_rows":catalog.provider_rows,"provider_pages":catalog.provider_pages,
            "duplicate_rows":catalog.duplicate_rows,"versions":len(catalog.reports),
            "unique_reports":len({row.report_key for row in catalog.reports}),
            "duplicate_or_variant_rows":catalog.provider_rows-len({row.report_key for row in catalog.reports}),
            "window_completeness":"NOT_ESTABLISHED",
            "excluded_type_conflict":sum(z.type_status=="COLUMN_CONFLICT" for z in catalog.reports),
            "matched":len(results),"items":[asdict(v) for v in results]}


def save_catalog(store:VersionedStore, catalog:BrokerCatalog, batch_id:str)->dict:
    """One atomic submission per locator; retain original receipts on any later failure."""
    identifier(batch_id)
    if len(batch_id)>55:
        raise RecordError("BROKER_BATCH_ID_TOO_LONG")
    if any(x.type_status=="COLUMN_CONFLICT" for x in catalog.reports):
        raise RecordError("BROKER_CONFLICTING_TYPE_CANNOT_SAVE")
    if len(catalog.reports)>MAX_SAVE_REPORTS:
        raise RecordError("BROKER_SAVE_BATCH_BUDGET_EXCEEDED")
    saved=[]
    for row in catalog.reports:
        suffix=sha256((row.report_key+row.version_hash).encode("ascii")).hexdigest()[:24]
        submission=batch_id+"-"+suffix
        ref = None
        try:
            ref=store.put_source(submission,source_for(row,catalog))
            verified=store.read_source(ref)
            if verified["data"] is not None or verified["availability"]!="external-only":
                raise RecordError("BROKER_BODY_IN_INDEX")
            saved.append({"report_key":row.report_key,"version":row.version_hash,
                          "submission_id":submission,"reference":asdict(ref),"availability":"external-only"})
        except RecordError as exc:
            code=str(exc)
            return {"complete":False,"status":"readback-unconfirmed" if ref is not None else
                    "unknown-write" if isinstance(exc, UnknownWrite) or "UNCONFIRMED" in code
                    or "RECONCILIATION" in code else "partial-failure",
                    "pending_reference":asdict(ref) if ref is not None else None,
                    "error":code,"last_intent":submission,"saved":saved,"saved_count":len(saved),
                    "catalog_versions":len(catalog.reports)}
    return {"complete":True,"status":"saved-bounded-index" if saved else "saved-empty-window",
            "saved":saved,"saved_count":len(saved),"catalog_versions":len(catalog.reports)}
