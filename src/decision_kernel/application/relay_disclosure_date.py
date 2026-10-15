"""Exactly one reviewed Relay schedule excerpt on the existing versioned store."""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, identifier
from decision_kernel.observations.relay_disclosure_date import ScheduleSnapshot, source_for


def save_schedule(store: VersionedStore, snapshot: ScheduleSnapshot, submission_id: str, *,
                  public_rights_confirmed: bool = False) -> dict:
    """Keep a known reference if the additional read fails; never resubmit automatically."""
    identifier(submission_id)
    if public_rights_confirmed is not True:
        raise RecordError("RELAY_PUBLICATION_RIGHTS_REQUIRED")
    source = source_for(snapshot)
    try:
        receipt = store.put_source(submission_id, source)
    except RecordError as exc:
        return {"complete": False, "status": "unknown-write" if isinstance(exc, UnknownWrite)
                else "submission-unconfirmed", "error": str(exc), "submission_id": submission_id,
                "reference": None, "reconcile_original_intent": True}
    try:
        saved = store.read_source(receipt)
        if saved["data"] != source.text or saved["original_verified"] is not False:
            raise RecordError("RELAY_SCHEDULE_READBACK_INVALID")
    except RecordError as exc:
        return {"complete": False, "status": "saved-reference-unverified", "error": str(exc),
                "submission_id": submission_id, "reference": asdict(receipt)}
    return {"status": "saved-derived-schedule-not-original-notice", "complete": True,
            "submission_id": submission_id, "reference": asdict(receipt),
            "original_pdf_read": False, "observed_count": snapshot.observed_count}
