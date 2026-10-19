"""Daily state chain on the original Artifact store; no source acquisition.

The complete bounded inventory chooses one connected daily-market chain.
A damaged candidate, missing predecessor or fork cannot select older green
state. Original source proof replay remains with MarketDiscovery.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.daily_market import daily_market_reference
from decision_kernel.application.market_discovery import (
    MarketDiscovery, PLAN_SCHEMA, PREFIX, _clock, _reason, plan_input,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    RecordError, SHA, SourceRef, StoreUnavailable, require,
)
from decision_kernel.domain.research_artifacts import ARTIFACT_PATH, artifact_reference
from decision_kernel.infrastructure.github_reading import reading_scope

STREAM = "daily-market"
MAX_ARTIFACTS = 128
SAVE_STATUSES = frozenset({"complete", "already-complete", "no-new-session"})


def _artifact_key(reference):
    ref = artifact_reference(reference)
    return ref.repository, ref.path, ref.sha256


def same_source_version(left, right):
    """Different reachable commits can refer to the same immutable source bytes."""
    a, b = SourceRef.from_dict(left), SourceRef.from_dict(right)
    return (a.manifest.repository, a.manifest.path, a.manifest.sha256, a.selection) == (
        b.manifest.repository, b.manifest.path, b.manifest.sha256, b.selection)


def discover_latest(store, *, registry=None, as_of, snapshot=None):
    """Inspect all 128 nominated Artifact headers or explicitly refuse selection.

    Artifact path names only nominate candidates. Canonical content, schema,
    policy and plan must match. The selected document is replayed by its reader;
    this inventory never claims that header hashing verifies source evidence.
    """
    _clock(as_of)
    # One original native admission snapshot, immutable caches and API budget
    # cover the full inventory. The existing scope rechecks native privacy and
    # reachability before releasing any selected result.
    with reading_scope(store.backend) as backend:
        return _discover_latest(VersionedStore(backend), registry=registry,
                                as_of=as_of, snapshot=snapshot)


def _discover_latest(store, *, registry, as_of, snapshot):
    snapshot = store.backend.head() if snapshot is None else snapshot
    require(isinstance(snapshot, str) and SHA.fullmatch(snapshot) is not None,
            "DAILY_DISCOVERY_SNAPSHOT_INVALID")
    backend = store.backend
    backend.check_commit(snapshot)
    paths = sorted(path for path in backend.paths(snapshot, "research/artifacts/")
                   if path.startswith("research/artifacts/" + PREFIX) and ARTIFACT_PATH.fullmatch(path))
    result = {"schema": "daily-discovery-inventory-v1", "stream": STREAM,
        "snapshot": snapshot, "repository": backend.repository,
        "total_candidates": len(paths), "scanned": 0, "stream_candidates": 0,
        "complete": False, "reference": None, "failures": [], "source_requests": 0,
        "selection": "single-complete-connected-chain", "persistence": "not-written"}
    if len(paths) > MAX_ARTIFACTS:
        return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_INVENTORY_BOUND"}
    decoder, candidates = MarketDiscovery(store, registry), {}
    for path in paths:
        try:
            reference = asdict(backend.reference(snapshot, path))
            document = decoder._decode(reference)
            if document["plan"]["stream"] == STREAM:
                candidates[_artifact_key(reference)] = {
                    "reference": reference, "as_of": document["as_of"], "plan": document["plan"]}
        except StoreUnavailable:
            # A global transport/rate/budget failure cannot be converted into
            # candidate damage and followed by more requests on the same API.
            raise
        except (RecordError, KeyError, TypeError, ValueError) as exc:
            result["failures"].append({"path": path, "reason_code": _reason(exc)})
        result["scanned"] += 1
    result["stream_candidates"] = len(candidates)
    if result["failures"]:
        # An unreadable header could belong to this stream and be newest.
        return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_INCOMPLETE"}
    if not candidates:
        return {**result, "complete": True, "status": "no-saved-state"}
    children, roots = {}, []
    for key, candidate in candidates.items():
        previous = candidate["plan"]["previous"]
        if previous is None:
            roots.append(key)
            continue
        parent = _artifact_key(previous)
        if parent not in candidates or parent == key:
            return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_PREDECESSOR_MISSING"}
        if _clock(candidate["as_of"]) < _clock(candidates[parent]["as_of"]):
            return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_CLOCK_REVERSED"}
        children.setdefault(parent, []).append(key)
    leaves = set(candidates) - set(children)
    if len(roots) != 1 or len(leaves) != 1 or any(len(rows) != 1 for rows in children.values()):
        return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_HISTORY_FORK"}
    visited, cursor = set(), roots[0]
    while cursor not in visited:
        visited.add(cursor)
        following = children.get(cursor)
        if not following:
            break
        cursor = following[0]
    if visited != set(candidates):
        return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_HISTORY_DISCONNECTED"}
    chosen = candidates[next(iter(leaves))]
    if _clock(chosen["as_of"]) > _clock(as_of):
        return {**result, "status": "unavailable", "reason_code": "DAILY_DISCOVERY_FUTURE_STATE"}
    return {**result, "status": "selected", "complete": True,
            "reference": chosen["reference"], "latest_as_of": chosen["as_of"]}


def _latest(store, source_store, registry, as_of):
    inventory = discover_latest(store, registry=registry, as_of=as_of)
    require(inventory["complete"], inventory.get("reason_code", "DAILY_DISCOVERY_INCOMPLETE"))
    service = MarketDiscovery(store, registry, source_store=source_store)
    saved = service.read(inventory["reference"]) if inventory["reference"] is not None else None
    return inventory, service, saved


def _current_plan(reference, saved, as_of):
    # Unchanged THS references retain their original actual data dates. A daily
    # stock update never implies another industry/concept request or refresh.
    surfaces = {} if saved is None else deepcopy(saved["plan"]["surfaces"])
    surfaces["stock"] = {"reference": deepcopy(reference),
                         "adjustment": surfaces.get("stock", {}).get("adjustment", "forward")}
    return plan_input({"schema": PLAN_SCHEMA, "stream": STREAM, "as_of": as_of,
        "surfaces": surfaces, "previous": None if saved is None else saved["reference"]})


def reading_discovery(store, source_store, registry, reference, *, as_of):
    """Return one exact saved selector or an explicit unsaved current preview."""
    inventory, service, saved = _latest(store, source_store, registry, as_of)
    base = {"schema": "daily-discovery-resolution-v1", "stream": STREAM,
        "inventory": inventory, "saved_reference": None if saved is None else saved["reference"],
        "saved_as_of": None if saved is None else saved["as_of"], "source_requests": 0,
        "records_write": "not-requested", "current_market_confirmed": reference is not None}
    prior_stock = None if saved is None else saved["plan"]["surfaces"].get("stock")
    if saved is not None and (reference is None or prior_stock is not None and
                             same_source_version(prior_stock["reference"], reference)):
        return {**base, "status": "saved", "persistence": "saved-exact-version",
            "panel": {"reference": saved["reference"], "kind": "stock", "limit": 20}}
    if reference is None:
        return {**base, "status": "unavailable", "persistence": "not-written", "panel": None,
                "reason_code": "DAILY_DISCOVERY_CURRENT_MARKET_UNAVAILABLE"}
    plan = _current_plan(reference, saved, as_of)
    # The ReadingRoom owns the actual source-qualified preview build. The
    # selector alone never labels that pending calculation as healthy.
    return {**base, "status": "preview", "persistence": "not-written",
            "panel": {"build": plan, "kind": "stock", "limit": 20}}


def save_daily_discovery(store, source_store, registry, approved, receipt, *,
                         as_of, submission_id, expected_reference=None, guard=None):
    """Write only after an eligible actual FT version has qualified and replayed."""
    _clock(as_of)
    base = {"schema": "daily-discovery-runtime-v1", "stream": STREAM, "as_of": as_of,
        "update_status": receipt.get("status"), "source_requests": 0, "records_writes": 0,
        "reference": None, "persistence": "not-written", "reason_code": None}
    if receipt.get("operation") != "update" or receipt.get("status") not in SAVE_STATUSES:
        return {**base, "status": "not-updated", "reason_code": "DAILY_DISCOVERY_UPDATE_NOT_QUALIFIED"}
    require(receipt.get("reading_reason_code") is None, "DAILY_DISCOVERY_UPDATE_READING_FAILED")
    resolved = daily_market_reference(source_store, registry, approved)
    reference = resolved["reference"]
    if expected_reference is not None:
        require(same_source_version(reference, expected_reference), "DAILY_DISCOVERY_RUNTIME_SOURCE_CHANGED")
    cycle = resolved["cycle"]
    require(cycle is not None and cycle["status"] in SAVE_STATUSES,
            "DAILY_DISCOVERY_CYCLE_NOT_COMPLETE")
    require(receipt.get("cycle_date") == cycle["date"] and receipt.get("cycle_status") == cycle["status"],
            "DAILY_DISCOVERY_RUNTIME_CYCLE_CHANGED")
    inventory, service, saved = _latest(store, source_store, registry, as_of)
    plan = _current_plan(reference, saved, as_of)
    preview = service.build(plan)
    stock = preview["surfaces"]["stock"]
    require(stock["status"] == "readable" and not stock.get("source_hold")
        and bool(stock.get("sources")) and all(row["status"] == "restored" and row["reason_code"] is None
            for row in stock["sources"].values())
        and bool(stock.get("as_of_by_exchange")) and all(stock["as_of_by_exchange"].values()),
        "DAILY_DISCOVERY_CURRENT_FTSHARE_UNAVAILABLE")
    expected_end = receipt.get("expected_completed_session")
    if expected_end is not None:
        require(isinstance(expected_end, str) and all(
            day.replace("-", "") == expected_end for day in stock["as_of_by_exchange"].values()),
            "DAILY_DISCOVERY_RUNTIME_SESSION_CHANGED")
    prior_stock = None if saved is None else saved["plan"]["surfaces"].get("stock")
    if prior_stock is not None and same_source_version(prior_stock["reference"], reference):
        return {**base, "status": "already-recorded", "reference": saved["reference"],
            "persistence": "saved-exact-version", "market_reference": reference,
            "actual_dates": stock["as_of_by_exchange"], "inventory": inventory}
    def before_write(head):
        # No new index/lock. Ordinary Artifact CAS provides the writer boundary;
        # a concurrent records change aborts this finite admission instead of
        # publishing a second leaf against a stale inventory.
        require(head == inventory["snapshot"], "DAILY_DISCOVERY_HEAD_CHANGED")
        if guard is not None:
            guard()
    result = service.save(submission_id, plan, before_new_write=before_write)
    return {**base, "status": "stored-and-verified", "records_writes": 1,
        "reference": result["reference"], "persistence": "saved-exact-version",
        "market_reference": reference, "actual_dates": stock["as_of_by_exchange"],
        "view_sha256": result["result"]["view_sha256"], "inventory": inventory}

