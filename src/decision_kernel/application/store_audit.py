"""Fail-closed audit of visible source/research versions and their exact retained references."""
from __future__ import annotations

from dataclasses import asdict
import json

from decision_kernel.application.versioned_store import MAX_QUERY_VERSIONS, VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, Selection, SourceRef, require


def audit_store(store: VersionedStore, snapshot: str) -> dict:
    backend = store.backend
    backend.check_commit(snapshot)
    paths = backend.paths(snapshot)
    sources = [p for p in paths if p.startswith("source-views/") and p.endswith("/manifest.json")]
    revisions = [p for p in paths if p.startswith("research/cases/") and p.endswith("/record.json")]
    currents = [p for p in paths if p.startswith("research/cases/") and p.endswith("/current.json")]
    require(len(sources) + len(revisions) <= MAX_QUERY_VERSIONS, "AUDIT_VERSION_LIMIT")
    source_refs, revision_refs, current_refs = [], [], {}
    for path in sources:
        ref = backend.reference(backend.last_change(snapshot, path), path)
        try:
            meta = json.loads(backend.resolve(ref))
            custody = meta["input"]["custody"]
        except (ValueError, KeyError, TypeError):
            raise RecordError("SOURCE_MANIFEST_INVALID") from None
        source = SourceRef(ref, Selection("locator" if custody == "external" else "all"))
        store.read_source(source)
        # A damaged current copy must not hide behind an intact historical reference.
        store.read_source(SourceRef(backend.reference(snapshot, path), source.selection))
        source_refs.append(asdict(source))
    retained = 0
    for path in revisions:
        ref = backend.reference(backend.last_change(snapshot, path), path)
        record = store.read_revision(ref)["record"]["input"]
        store.read_revision(backend.reference(snapshot, path))
        for raw in record["sources"]:
            store.read_source(SourceRef.from_dict(raw))
            retained += 1
        if record.get("predecessor") is not None:
            try:
                previous = BlobRef(**record["predecessor"])
                previous_input = store.read_revision(previous)["record"]["input"]
            except (TypeError, KeyError):
                raise RecordError("INVALID_PREDECESSOR") from None
            require(previous_input["case_id"] == record["case_id"] and previous != ref,
                    "INVALID_PREDECESSOR")
        revision_refs.append(asdict(ref))
    for path in currents:
        case = path.split("/")[2]
        ref = store.current(case, snapshot=snapshot)
        require(ref is not None and ref.path in revisions, "INVALID_CURRENT_POINTER")
        current_refs[case] = asdict(ref)
    return {"schema": "store-reference-audit-v1", "snapshot": snapshot,
            "scope": "snapshot-visible-versions-and-retained-references",
            "source_versions": len(sources), "research_revisions": len(revisions),
            "current_cases": len(currents), "retained_source_uses": retained,
            "sources": source_refs, "revisions": revision_refs, "current": current_refs,
            "status": "verified", "originals_authenticated": False,
            "research_quality": "not-evaluated", "live_authorization": "not-certified-by-audit"}
