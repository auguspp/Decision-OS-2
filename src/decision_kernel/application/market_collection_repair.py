"""One explicit repair of the four original zero-page cn-eod terminals.

The immutable collection grant owns an additional finite allowance. Checkpoint
v1 still owns every request, reservation and retry; its terminal rule is never
relaxed. No ordinary sync, new generation or alternate capture ID grants work.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
import time

from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, digest, encoded, require, source_time

REQUEST_SCHEMA = "ashare-terminal-repair-request-v1"
GRANT_SCHEMA = "ashare-terminal-repair-grant-v1"
REPAIR_REASON = "EXPLICIT_REPAIR_AFTER_VERIFIED_TRANSPORT_FIX"
REPAIR_SLOTS = ("20261016:daily", "20261016:adj_factor", "20261015:daily", "20261015:adj_factor")
REPAIR_DAYS = {"20261015", "20261016"}
LIMITS = {"slots": 4, "max_pages_per_slot": 2, "max_attempts_per_page": 4,
    "max_wait_seconds_per_page": 180, "max_new_requests": 32,
    "original_requests": 16, "max_combined_requests": 48}
STATES = ("pending", "in-progress", "complete", "paused", "failed", "blocked", "unknown-write")


def _sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "ASHARE_REPAIR_CODE_SHA")
    return value


def _fixed_plan(plan):
    require(all(plan.get(key) == value for key, value in {
        "collection_id": "cn-eod", "generation": "g20261016", "start": "20260701",
        "end": "20261016", "calendar_end": "20261016", "refresh_dates": []}.items())
        and REPAIR_DAYS <= set(plan.get("sessions", [])), "ASHARE_REPAIR_FIXED_SCOPE")


def validate_grant_request(document):
    require(isinstance(document, dict) and set(document) == {
        "schema", "base_reference", "terminal_references", "transport_evidence"}
        and document["schema"] == REQUEST_SCHEMA, "ASHARE_REPAIR_REQUEST")
    SourceRef.from_dict(document["base_reference"])
    terminals = document["terminal_references"]
    require(isinstance(terminals, dict) and set(terminals) == set(REPAIR_SLOTS), "ASHARE_REPAIR_TARGETS")
    for reference in terminals.values():
        SourceRef.from_dict(reference)
    evidence = document["transport_evidence"]
    require(isinstance(evidence, dict) and set(evidence) == {"daily", "adj_factor"}, "ASHARE_REPAIR_EVIDENCE")
    for api, entry in evidence.items():
        require(isinstance(entry, dict) and set(entry) == {"code_sha", "run_id", "run_attempt",
            "collection_reference", "slot", "checkpoint_reference", "query_reference"}
            and entry["slot"] == "20261014:" + api, "ASHARE_REPAIR_EVIDENCE")
        _sha(entry["code_sha"])
        require(all(isinstance(entry[key], str) and re.fullmatch(r"[1-9][0-9]*", entry[key])
            for key in ("run_id", "run_attempt")), "ASHARE_REPAIR_EVIDENCE_RUN")
        for key in ("collection_reference", "checkpoint_reference", "query_reference"):
            SourceRef.from_dict(entry[key])
    require(evidence["daily"]["code_sha"] == evidence["adj_factor"]["code_sha"],
            "ASHARE_REPAIR_EVIDENCE_CODE_CHANGED")
    return json.loads(encoded(document))


def _grant_id(grant):
    return "ashare-repair-" + digest(encoded({k: v for k, v in grant.items() if k != "grant_id"}))[:64]


def validate_native_context(context, request, execution_code_sha):
    require(isinstance(context, dict) and set(context) == {"execution_code_sha", "transport_runs"}
        and context["execution_code_sha"] == _sha(execution_code_sha), "ASHARE_REPAIR_NATIVE_EXECUTION_CHANGED")
    runs = context["transport_runs"]
    require(isinstance(runs, dict) and set(runs) == {"daily", "adj_factor"}, "ASHARE_REPAIR_NATIVE_RUNS")
    for api, run in runs.items():
        evidence = request["transport_evidence"][api]
        require(isinstance(run, dict) and set(run) == {"run_id", "run_attempt", "started_at", "completed_at"}
            and run["run_id"] == evidence["run_id"] and run["run_attempt"] == evidence["run_attempt"],
            "ASHARE_REPAIR_NATIVE_RUNS")
        for key in ("started_at", "completed_at"):
            source_time(run[key])
            require(isinstance(run[key], str) and "T" in run[key], "ASHARE_REPAIR_NATIVE_RUN_CLOCK")
        require(datetime.fromisoformat(run["started_at"]) <= datetime.fromisoformat(run["completed_at"]),
            "ASHARE_REPAIR_NATIVE_RUN_CLOCK")
    return deepcopy(context)


def repair_checkpoint_id(slot):
    require(slot in REPAIR_SLOTS, "ASHARE_REPAIR_TARGETS")
    # A locator for this one episode, never an alternative source of allowance.
    return "ashare-repair-" + digest(encoded({"collection_id": "cn-eod", "episode": "original-four-terminals",
        "generation": "g20261016", "slot": slot}))[:64]


def validate_repair_shape(collection, registry):
    repair = collection.get("repair")
    require(isinstance(repair, dict) and set(repair) == {"grant", "units"}, "ASHARE_REPAIR_SHAPE")
    grant = repair["grant"]
    require(isinstance(grant, dict) and set(grant) == {"schema", "grant_id", "request", "original_units",
        "issued_at", "limits", "reason", "execution_code_sha", "transport_runs"} and grant["schema"] == GRANT_SCHEMA,
        "ASHARE_REPAIR_GRANT")
    validate_grant_request(grant["request"])
    _sha(grant["execution_code_sha"])
    validate_native_context({"execution_code_sha": grant["execution_code_sha"],
        "transport_runs": grant["transport_runs"]}, grant["request"], grant["execution_code_sha"])
    source_time(grant["issued_at"])
    require(isinstance(grant["issued_at"], str) and "T" in grant["issued_at"]
        and encoded(grant["limits"]) == encoded(LIMITS) and grant["reason"] == REPAIR_REASON
        and grant["grant_id"] == _grant_id(grant), "ASHARE_REPAIR_GRANT_CHANGED")
    originals = grant["original_units"]
    require(isinstance(originals, dict) and set(originals) == set(REPAIR_SLOTS), "ASHARE_REPAIR_ORIGINALS")
    for slot, unit in originals.items():
        require(isinstance(unit, dict) and set(unit) == {"query", "checkpoint", "status", "reason_code"}
            and unit["query"] is None and unit["status"] == "failed"
            and unit["checkpoint"] == grant["request"]["terminal_references"][slot]
            and isinstance(unit["reason_code"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", unit["reason_code"]), "ASHARE_REPAIR_ORIGINALS")
        if slot in collection["units"]:
            require(collection["units"][slot] == unit, "ASHARE_REPAIR_ORIGINAL_CHANGED")
    p = collection["plan"]
    require(p["collection_id"] == "cn-eod" and not REPAIR_DAYS.intersection(p["refresh_dates"]),
            "ASHARE_REPAIR_SCOPE_CHANGED")
    if p["generation"] == "g20261016":
        _fixed_plan(p)
    units = repair["units"]
    require(isinstance(units, dict) and set(units) == set(REPAIR_SLOTS), "ASHARE_REPAIR_UNITS")
    for unit in units.values():
        require(isinstance(unit, dict) and set(unit) == {"query", "checkpoint", "status", "reason_code", "reserved_requests"}
            and unit["status"] in STATES and type(unit["reserved_requests"]) is int
            and 0 <= unit["reserved_requests"] <= 8, "ASHARE_REPAIR_UNIT")
        require(unit["reason_code"] is None or isinstance(unit["reason_code"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", unit["reason_code"]), "ASHARE_REPAIR_UNIT")
        for key in ("query", "checkpoint"):
            if unit[key] is not None:
                SourceRef.from_dict(unit[key])
        require((unit["query"] is not None) == (unit["status"] == "complete"), "ASHARE_REPAIR_UNIT")
        if unit["checkpoint"] is None:
            require(unit["status"] in {"pending", "in-progress", "blocked", "unknown-write"}, "ASHARE_REPAIR_UNIT")
            require(unit["reserved_requests"] == 0 or unit["status"] == "unknown-write", "ASHARE_REPAIR_ALLOWANCE_CHANGED")
        if unit["status"] == "pending":
            require(unit["checkpoint"] is None and unit["reserved_requests"] == 0 and unit["reason_code"] is None,
                "ASHARE_REPAIR_UNIT")
    require(sum(u["reserved_requests"] for u in units.values()) <= 32, "ASHARE_REPAIR_ALLOWANCE_CHANGED")


def repair_finished(collection):
    return "repair" in collection and all(u["status"] in {"complete", "failed"}
        for u in collection["repair"]["units"].values())


def repair_summary(collection):
    repair = collection.get("repair")
    counts = {state: 0 if repair is None else sum(u["status"] == state for u in repair["units"].values())
        for state in STATES}
    status = ("not-granted" if repair is None else "repair-complete" if counts["complete"] == 4 else
        "repair-exhausted" if repair_finished(collection) else "repair-pending" if counts["pending"] == 4 else "in-progress")
    return {"status": status, "counts": counts, "reserved_requests": 0 if repair is None else
        sum(u["reserved_requests"] for u in repair["units"].values()),
        "max_new_requests": 32, "original_requests": 16, "max_combined_requests": 48,
        "has_grant": repair is not None}


def validate_repair_transition(previous, current, registry, *, predecessor):
    """Thin collection-edge semantics; no I/O and no recursive restoration."""
    before = None if previous is None else previous.get("repair")
    after = current.get("repair")
    require(before is None or after is not None, "ASHARE_REPAIR_GRANT_REMOVED")
    if after is None:
        return
    validate_repair_shape(current, registry)
    if before is None:
        require(previous is not None and encoded(predecessor) == encoded(after["grant"]["request"]["base_reference"]),
            "ASHARE_REPAIR_BASE_CHANGED")
        _fixed_plan(previous["plan"])
        require(current["plan"] == previous["plan"] and current["units"] == previous["units"]
            and current["last_qualified"] == previous["last_qualified"]
            and all(u == {"query": None, "checkpoint": None, "status": "pending", "reason_code": None,
                "reserved_requests": 0} for u in after["units"].values()), "ASHARE_REPAIR_ACTIVATION_CHANGED")
        require(current["attempt"].get("repair_execution_code_sha") == after["grant"]["execution_code_sha"],
            "ASHARE_REPAIR_EXECUTION_CODE_REQUIRED")
        return
    require(before["grant"] == after["grant"], "ASHARE_REPAIR_GRANT_CHANGED")
    changed = before["units"] != after["units"]
    if previous["plan"]["generation"] != current["plan"]["generation"]:
        require(repair_finished(previous) and before == after, "ASHARE_REPAIR_GENERATION_RESET")
    if changed:
        _fixed_plan(current["plan"])
        _sha(current["attempt"].get("repair_execution_code_sha"))
    for slot in REPAIR_SLOTS:
        old, new = before["units"][slot], after["units"][slot]
        require(new["reserved_requests"] >= old["reserved_requests"], "ASHARE_REPAIR_ALLOWANCE_RESET")
        require(old["checkpoint"] is None or new["checkpoint"] is not None, "ASHARE_REPAIR_CHECKPOINT_RESET")
        if old["status"] in {"complete", "failed"}:
            require(new == old, "ASHARE_REPAIR_TERMINAL_RESTART")
        if old["status"] in {"blocked", "unknown-write"}:
            require(new == old, "ASHARE_REPAIR_RECONCILIATION_REQUIRED")


def checkpoint_reserved_requests(state):
    """Charge each page attempt once, including an unresolved reservation."""
    pages, control = state["capture"]["pages"], state["control"]
    count = sum(len(page["attempts"]) for page in pages)
    retry = control["retry"]
    if retry is not None:
        number = control["page_number"]
        if number <= len(pages):
            require(retry["attempts"] == pages[number - 1]["attempts"], "ASHARE_REPAIR_ATTEMPT_DUPLICATION")
        else:
            count += len(retry["attempts"]) + (1 if retry["phase"] == "in-flight" else 0)
    require(count <= 8, "ASHARE_REPAIR_ALLOWANCE_CHANGED")
    return count


def _collection_ancestor(candidate, ancestor, registry, reader):
    from decision_kernel.application.market_collection_custody import _collection_manifest
    from decision_kernel.application.market_collection_reading import MAX_CURSOR_BYTES, MAX_CURSOR_VERSIONS, same_reference
    cursor, seen, total = candidate, set(), 0
    for _ in range(MAX_CURSOR_VERSIONS):
        if same_reference(cursor, ancestor):
            return
        identity = encoded(cursor)
        require(identity not in seen, "ASHARE_REPAIR_ROOT_CYCLE")
        seen.add(identity)
        _, body = _collection_manifest(cursor, registry, reader)
        total += len(encoded(body))
        require(total <= MAX_CURSOR_BYTES, "ASHARE_REPAIR_ROOT_BYTE_BOUND")
        cursor = body["predecessor"]
        require(cursor is not None, "ASHARE_REPAIR_EVIDENCE_NOT_ANCESTOR")
    raise RecordError("ASHARE_REPAIR_ROOT_BOUND")


def _checkpoint_lineage(reference, registry, reader, *, slot, known=None):
    """Validate the single repair chain, including every original transition."""
    from decision_kernel.application.market_collection_custody import _checkpoint_manifest, restore_market_checkpoint
    from decision_kernel.application.market_collection_reading import MAX_CURSOR_BYTES, MAX_CURSOR_VERSIONS, same_reference
    from decision_kernel.application.relay_capture_checkpoint import validate_capture_transition
    from decision_kernel.infrastructure.relay_retry import RetryPolicy
    restored = restore_market_checkpoint(reference, registry, reader)
    _, latest_manifest = _checkpoint_manifest(reference, registry, reader)
    require(restored["checkpoint_id"] == repair_checkpoint_id(slot), "ASHARE_REPAIR_CHECKPOINT_OWNER")
    current, cursor, seen = restored["state"], restored["predecessor"], set()
    original = deepcopy(current)
    total = len(encoded(current))
    found = known is None or same_reference(reference, known)
    for _ in range(MAX_CURSOR_VERSIONS):
        if cursor is None:
            retry = current["control"]["retry"]
            require(not current["capture"]["pages"] and current["control"]["page_number"] == 1
                and (current["control"]["state"] in {"ready", "paused"} and retry is None
                    or current["control"]["state"] == "executing" and retry is not None
                    and retry["phase"] == "in-flight" and not retry["attempts"]
                    and retry["reserved_attempt"] == 1), "ASHARE_REPAIR_CHECKPOINT_GENESIS")
            require(found, "ASHARE_REPAIR_CHECKPOINT_ROLLBACK")
            return restored
        identity = encoded(cursor)
        require(identity not in seen, "ASHARE_REPAIR_CHECKPOINT_CYCLE")
        seen.add(identity)
        found = found or same_reference(cursor, known)
        _, body = _checkpoint_manifest(cursor, registry, reader)
        total += len(encoded(body))
        require(total <= MAX_CURSOR_BYTES, "ASHARE_REPAIR_CHECKPOINT_BYTE_BOUND")
        require(body["checkpoint_id"] == restored["checkpoint_id"], "ASHARE_REPAIR_CHECKPOINT_OWNER")
        require(body["pages"] == latest_manifest["pages"][:len(body["pages"])],
                "ASHARE_REPAIR_CHECKPOINT_PAGE_REFERENCE_CHANGED")
        pages = deepcopy(original["capture"]["pages"][:len(body["pages"])])
        require(len(pages) == len(body["pages"]) and all(page["attempts"] == entry["attempts"]
            for page, entry in zip(pages, body["pages"])), "ASHARE_REPAIR_CHECKPOINT_HISTORY")
        previous = {"schema": body["state_schema"], "capture": {**body["capture"], "pages": pages},
            "control": body["control"]}
        require(digest(encoded(previous)) == body["state_sha256"], "ASHARE_REPAIR_CHECKPOINT_HISTORY")
        validate_capture_transition(previous, current, current["control"]["request"], registry,
            max_pages=2, policy=RetryPolicy())
        current, cursor = previous, body["predecessor"]
    raise RecordError("ASHARE_REPAIR_CHECKPOINT_BOUND")


def validate_repair_records(collection, registry, reader, *, reference=None, activation=False, chain=None, previous=None):
    """Full dependency proof inside one independently fresh, bounded read scope.

    The collection scanner only calls thin shape/edge validation. This function
    may call that scanner, but never calls restore_market_collection recursively.
    """
    from decision_kernel.application.market_collection import _qualified, _reason, checkpoint_id, unit_request
    from decision_kernel.application.market_collection_custody import _collection_manifest, restore_market_checkpoint
    from decision_kernel.application.market_collection_reading import inspect_market_chain, same_reference
    from decision_kernel.application.market_custody import restore_market_query
    from decision_kernel.observations.market_daily import MarketInputError
    repair, grant = collection["repair"], collection["repair"]["grant"]
    request = grant["request"]
    validate_repair_shape(collection, registry)
    _, base = _collection_manifest(request["base_reference"], registry, reader)
    origin = base["collection"]
    require("repair" not in origin, "ASHARE_REPAIR_SECOND_GRANT")
    _fixed_plan(origin["plan"])
    require({slot: origin["units"][slot] for slot in REPAIR_SLOTS} == grant["original_units"],
            "ASHARE_REPAIR_ORIGINAL_CHANGED")
    require(datetime.fromisoformat(grant["issued_at"]) >= datetime.fromisoformat(origin["attempt"]["updated_at"]),
            "ASHARE_REPAIR_CLOCK")
    chain = chain or inspect_market_chain(reader, registry, collection_id="cn-eod")
    if activation:
        require(same_reference(chain["reference"], request["base_reference"]), "ASHARE_REPAIR_BASE_NOT_TIP")
    elif reference is not None:
        _collection_ancestor(reference, request["base_reference"], registry, reader)
    for slot in REPAIR_SLOTS:
        retained = restore_market_checkpoint(request["terminal_references"][slot], registry, reader)
        state = retained["state"]
        use, pages = unit_request(origin["plan"], slot, registry)
        retry = state["control"]["retry"]
        require(retained["checkpoint_id"] == checkpoint_id(origin["plan"], slot)
            and state["control"]["request"] == use and state["control"]["max_pages"] == pages == 2
            and state["control"]["state"] == "terminal" and not state["capture"]["pages"]
            and retry is not None and len(retry["attempts"]) == 4
            and sum(a["completed_wait_seconds"] for a in retry["attempts"]) == 90
            and all((a.get("failure") or {}).get("classification") == "timeout" for a in retry["attempts"]),
            "ASHARE_REPAIR_ORIGINAL_NOT_EXHAUSTED")
        if activation:
            original_chain = inspect_market_chain(reader, registry, checkpoint_id=retained["checkpoint_id"])
            require(same_reference(original_chain["reference"], request["terminal_references"][slot]),
                "ASHARE_REPAIR_ORIGINAL_NOT_TIP")
            require(inspect_market_chain(reader, registry, checkpoint_id=repair_checkpoint_id(slot))["reference"] is None,
                "ASHARE_REPAIR_CHECKPOINT_ALREADY_EXISTS")
    for api, evidence in request["transport_evidence"].items():
        _collection_ancestor(request["base_reference"], evidence["collection_reference"], registry, reader)
        _, retained_root = _collection_manifest(evidence["collection_reference"], registry, reader)
        proof = retained_root["collection"]
        _fixed_plan(proof["plan"])
        require(re.fullmatch(r"run-" + evidence["run_id"] + "-" + evidence["run_attempt"] + r"-u[1-9][0-9]*",
            proof["attempt"]["id"]) is not None, "ASHARE_REPAIR_EVIDENCE_RUN_CHANGED")
        unit = proof["units"].get(evidence["slot"])
        require(unit is not None and unit["status"] == "complete"
            and unit["query"] == evidence["query_reference"]
            and unit["checkpoint"] == evidence["checkpoint_reference"], "ASHARE_REPAIR_EVIDENCE_UNIT_CHANGED")
        require(retained_root["predecessor"] is not None, "ASHARE_REPAIR_EVIDENCE_FIRST_COMPLETION")
        _, before_root = _collection_manifest(retained_root["predecessor"], registry, reader)
        before_unit = before_root["collection"]["units"].get(evidence["slot"])
        require(before_unit is not None and before_unit["status"] != "complete",
                "ASHARE_REPAIR_EVIDENCE_FIRST_COMPLETION")
        checkpoint = restore_market_checkpoint(evidence["checkpoint_reference"], registry, reader)
        query = restore_market_query(evidence["query_reference"], registry, reader)
        use, pages = unit_request(proof["plan"], evidence["slot"], registry)
        require(checkpoint["checkpoint_id"] == checkpoint_id(proof["plan"], evidence["slot"])
            and checkpoint["state"]["control"]["state"] == "complete"
            and checkpoint["state"]["control"]["request"] == use
            and checkpoint["state"]["control"]["max_pages"] == pages == 2
            and checkpoint["page_references"] == query["page_references"], "ASHARE_REPAIR_EVIDENCE_INCOMPLETE")
        _qualified(query["result"], use, proof["plan"], registry)
        prior_pages = [] if before_unit["checkpoint"] is None else restore_market_checkpoint(
            before_unit["checkpoint"], registry, reader)["page_references"]
        require(checkpoint["page_references"][:len(prior_pages)] == prior_pages
            and len(prior_pages) < len(checkpoint["page_references"]), "ASHARE_REPAIR_EVIDENCE_NO_NEW_SUCCESS")
        window = grant["transport_runs"][api]
        start, end = datetime.fromisoformat(window["started_at"]), datetime.fromisoformat(window["completed_at"])
        timestamps = [proof["attempt"]["started_at"], proof["attempt"]["updated_at"]]
        for page in checkpoint["state"]["capture"]["pages"][len(prior_pages):]:
            success = page["attempts"][-1]
            timestamps.extend((success["requested_at"], success["received_at"]))
        require(all(start <= datetime.fromisoformat(value) <= end for value in timestamps),
                "ASHARE_REPAIR_EVIDENCE_OUTSIDE_RUN")
    for slot, unit in repair["units"].items():
        if unit["checkpoint"] is None:
            require(unit["reserved_requests"] == 0, "ASHARE_REPAIR_ALLOWANCE_CHANGED")
            continue
        known = None if previous is None or "repair" not in previous else previous["repair"]["units"][slot]["checkpoint"]
        checkpoint = _checkpoint_lineage(unit["checkpoint"], registry, reader, slot=slot, known=known)
        checkpoint_chain = inspect_market_chain(reader, registry, checkpoint_id=repair_checkpoint_id(slot))
        require(checkpoint_chain["reference"] is not None, "ASHARE_REPAIR_CHECKPOINT_ROLLBACK")
        if not same_reference(checkpoint_chain["reference"], unit["checkpoint"]):
            require(unit["status"] not in {"complete", "failed"}, "ASHARE_REPAIR_TERMINAL_RESTART")
            _checkpoint_lineage(checkpoint_chain["reference"], registry, reader, slot=slot, known=unit["checkpoint"])
        state = checkpoint["state"]
        use, pages = unit_request(origin["plan"], slot, registry)
        require(state["control"]["request"] == use and state["control"]["max_pages"] == pages == 2
            and unit["reserved_requests"] == checkpoint_reserved_requests(state), "ASHARE_REPAIR_ALLOWANCE_CHANGED")
        if unit["status"] == "complete":
            query = restore_market_query(unit["query"], registry, reader)
            require(state["control"]["state"] == "complete" and query["page_references"] == checkpoint["page_references"],
                "ASHARE_REPAIR_QUERY_UNQUALIFIED")
            _qualified(query["result"], use, origin["plan"], registry)
        elif unit["status"] == "failed":
            require(state["control"]["state"] in {"terminal", "complete"}, "ASHARE_REPAIR_FALSE_TERMINAL")
            if state["control"]["state"] == "complete":
                try:
                    _qualified({"case_id": "cn-eod", "use": use, "status": "captured", "capture": state["capture"]},
                        use, origin["plan"], registry)
                except (RecordError, MarketInputError) as exc:
                    require(unit["reason_code"] == _reason(exc), "ASHARE_REPAIR_TERMINAL_REASON_CHANGED")
                else:
                    raise RecordError("ASHARE_REPAIR_FALSE_TERMINAL")
            else:
                require(unit["reason_code"] == _reason(state["capture"]["error"]),
                        "ASHARE_REPAIR_TERMINAL_REASON_CHANGED")
        elif unit["status"] == "paused":
            require(state["control"]["state"] == "paused", "ASHARE_REPAIR_FALSE_PAUSE")
    return chain


def repair_collection(*, plan, registry, store, fresh_reader, attempt_id, previous_ref=None,
                      grant_request=None, verify_evidence=None, execution_code_sha=None,
                      custody_authorized=False, public_rights_confirmed=False,
                      access_confirmed=False, max_queries=1, client=None, now=None, clock=None,
                      monotonic=time.monotonic, sleep=time.sleep, pause_requested=None, progress=None):
    """Activate once, or consume one bounded part of that same retained grant.

    Activation publishes and independently restores only the grant (zero source
    requests). Continuations reconcile every repair checkpoint before source
    admission, then use the original Relay, checkpoint and query custody code.
    The native callback and execution SHA come from the trusted runtime, never
    from the grant request JSON. The callback is invoked once per activation.
    """
    from decision_kernel.application.market_collection import (
        CollectionSyncInterrupted, MAX_QUERIES_PER_WORK_UNIT, WORK_UNIT_SECONDS, _PersistenceStopped,
        _date, _new_collection, _partition_complete, _qualified, _reason, collection_summary,
        completed_through, unit_request, validate_collection, validate_collection_plan,
    )
    from decision_kernel.application.market_collection_custody import (
        retain_market_checkpoint, retain_market_collection, restore_market_collection,
    )
    from decision_kernel.application.market_collection_reading import (
        inspect_market_chain, market_cursor_capacity, same_reference,
    )
    from decision_kernel.application.market_custody import (
        authorize_market_custody, fresh_market_reader, market_read_scope, retain_market_query,
    )
    from decision_kernel.application.relay_capture_checkpoint import CheckpointStateError, CheckpointWriteError
    from decision_kernel.application.research_sources import capture_registered
    from decision_kernel.infrastructure.relay_http import RelayHTTP
    from decision_kernel.infrastructure.relay_retry import RetryingRelay
    from decision_kernel.observations.market_daily import MarketInputError

    require(access_confirmed is True, "ASHARE_SOURCE_ACCESS_REQUIRED")
    require(type(max_queries) is int and 1 <= max_queries <= MAX_QUERIES_PER_WORK_UNIT,
            "ASHARE_WORK_UNIT_QUERY_BOUND")
    execution_code_sha = _sha(execution_code_sha)
    plan = validate_collection_plan(plan, registry)
    now = datetime.now(timezone.utc) if now is None else now
    require(_date(plan["calendar_end"]) <= _date(completed_through(now)), "ASHARE_COMPLETED_DAYS_ONLY")
    visibility = authorize_market_custody(store, custody_authorized=custody_authorized,
        public_rights_confirmed=public_rights_confirmed)
    require(visibility in {"private", "synthetic"}, "ASHARE_REPAIR_PRIVATE_CUSTODY_REQUIRED")
    require(previous_ref is not None, "ASHARE_REPAIR_BASE_REQUIRED")
    previous_ref = asdict(previous_ref) if isinstance(previous_ref, SourceRef) else previous_ref
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    at = clock(); source_time(at)
    with market_read_scope(fresh_market_reader(store, fresh_reader)) as reader:
        previous = restore_market_collection(previous_ref, registry, reader)["collection"]
        chain = inspect_market_chain(reader, registry, collection_id=plan["collection_id"])
        require(same_reference(chain["reference"], previous_ref), "ASHARE_COLLECTION_STALE_REFERENCE")
        collection = _new_collection(plan, previous, previous_ref, attempt_id, at, registry)
        _fixed_plan(collection["plan"])
    collection["attempt"]["repair_execution_code_sha"] = execution_code_sha
    permission = {"custody_authorized": custody_authorized, "public_rights_confirmed": public_rights_confirmed}
    reference = previous_ref
    capacity = market_cursor_capacity(node_count=chain["node_count"], unique_body_bytes=chain["unique_body_bytes"])
    source_client, source_before = client, (client.requests if client is not None else 0)
    selected_slot, persistence_error = None, None
    execution_error_persisted = True

    def emit():
        collection["attempt"]["updated_at"] = clock()
        if source_client is not None:
            collection["attempt"]["source_requests"] = source_client.requests - source_before
        receipt = collection_summary(collection, reference)
        receipt["cursor_capacity_before_work_unit"] = deepcopy(capacity)
        if progress is not None:
            progress(deepcopy(receipt))
        return receipt

    def result(*, granted=False):
        receipt = emit()
        receipt["execution_error_persisted"] = execution_error_persisted
        if granted:
            receipt["repair_operation"] = "repair-granted"
        state = repair_summary(collection)["status"]
        return {"status": state if state in {"repair-complete", "repair-exhausted"} else "partial",
            "reference": reference, "collection": deepcopy(collection), "receipt": receipt}

    def save(action, *args, **kwargs):
        nonlocal persistence_error
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            persistence_error = exc
            raise _PersistenceStopped("ASHARE_PERSISTENCE_UNCONFIRMED") from exc

    def persist(*, verifier=None):
        nonlocal reference
        emit()
        validate_collection(collection, registry)
        saved = save(retain_market_collection, collection, registry, store, fresh_reader=fresh_reader,
            predecessor=reference, verify_evidence=verifier, **permission)
        reference = saved["reference"]
        emit()

    if grant_request is not None:
        require("repair" not in previous, "ASHARE_REPAIR_SECOND_GRANT")
        request = validate_grant_request(grant_request)
        require(same_reference(request["base_reference"], previous_ref), "ASHARE_REPAIR_BASE_CHANGED")
        require(callable(verify_evidence), "ASHARE_REPAIR_NATIVE_EVIDENCE_REQUIRED")
        require(capacity["admit_work_unit"], "ASHARE_CURSOR_CAPACITY")
        # Only a successful trusted native callback fills this local metadata
        # context. It cannot be supplied through the serialized request.
        native = validate_native_context(verify_evidence(deepcopy(request)), request, execution_code_sha)
        grant = {"schema": GRANT_SCHEMA, "request": request,
            "original_units": {slot: deepcopy(previous["units"][slot]) for slot in REPAIR_SLOTS},
            "issued_at": at, "limits": deepcopy(LIMITS), "reason": REPAIR_REASON,
            "execution_code_sha": execution_code_sha, "transport_runs": native["transport_runs"]}
        grant["grant_id"] = _grant_id(grant)
        collection["repair"] = {"grant": grant, "units": {slot: {"query": None, "checkpoint": None,
            "status": "pending", "reason_code": None, "reserved_requests": 0} for slot in REPAIR_SLOTS}}
        collection["attempt"].update(status="partial", reason_code=None)

        def verified_context(document):
            require(document == request, "ASHARE_REPAIR_NATIVE_REQUEST_CHANGED")
            return deepcopy(native)

        try:
            persist(verifier=verified_context)
        except _PersistenceStopped as exc:
            collection["attempt"].update(status="blocked", reason_code="ASHARE_PERSISTENCE_UNCONFIRMED")
            receipt = emit(); receipt["persistence_error"] = _reason(persistence_error or exc)
            raise CollectionSyncInterrupted(receipt, reference) from exc
        return result(granted=True)

    require("repair" in collection, "ASHARE_REPAIR_GRANT_REQUIRED")
    if repair_finished(collection):
        collection["attempt"].update(status="partial", reason_code=None)
        return result()
    require(capacity["admit_work_unit"], "ASHARE_CURSOR_CAPACITY")
    states = {}
    try:
        # Record the actual execution code for this finite continuation first.
        persist()
        collection["attempt"].update(status="in-progress", reason_code=None)
        with market_read_scope(fresh_market_reader(store, fresh_reader)) as reader:
            # Scan all four owners before any source request. An unpublished
            # in-flight checkpoint on a later slot blocks this whole operation.
            for slot in REPAIR_SLOTS:
                selected_slot = slot
                unit = collection["repair"]["units"][slot]
                require(unit["status"] not in {"blocked", "unknown-write"}, "ASHARE_REPAIR_RECONCILIATION_REQUIRED")
                checkpoint_chain = inspect_market_chain(reader, registry, checkpoint_id=repair_checkpoint_id(slot))
                latest = checkpoint_chain["reference"]
                if latest is None:
                    require(unit["checkpoint"] is None, "ASHARE_REPAIR_CHECKPOINT_ROLLBACK")
                    states[slot] = None
                    continue
                restored = _checkpoint_lineage(latest, registry, reader, slot=slot, known=unit["checkpoint"])
                state = restored["state"]
                use, pages = unit_request(collection["plan"], slot, registry)
                require(state["control"]["request"] == use and state["control"]["max_pages"] == pages == 2,
                        "ASHARE_REPAIR_CHECKPOINT_SCOPE")
                count = checkpoint_reserved_requests(state)
                require(count >= unit["reserved_requests"], "ASHARE_REPAIR_ALLOWANCE_RESET")
                if unit["status"] in {"complete", "failed"}:
                    require(same_reference(latest, unit["checkpoint"]), "ASHARE_REPAIR_TERMINAL_RESTART")
                else:
                    unit.update(checkpoint=restored["reference"], reserved_requests=count,
                        status="in-progress", reason_code=None)
                states[slot] = state
                retry = state["control"]["retry"]
                frozen = state["control"]["state"] in {"complete", "terminal"}
                require(frozen or not retry or retry["phase"] not in {"in-flight", "response"},
                        "RELAY_CHECKPOINT_INFLIGHT")
                failure = state["capture"].get("failure") or (
                    (retry["attempts"][-1].get("failure") or {}) if retry and retry["attempts"] else {})
                require(failure.get("classification") not in {"auth-or-entitlement", "rate-limit"}
                    and state["capture"].get("error") != "RELAY_SECRET_REFLECTED", "RELAY_CHECKPOINT_GLOBAL_STOP")
        selected_slot = None
        session, queries = None, 0
        for slot in REPAIR_SLOTS:
            unit = collection["repair"]["units"][slot]
            if unit["status"] in {"complete", "failed"}:
                continue
            if queries >= max_queries:
                break
            selected_slot = slot
            use, pages = unit_request(collection["plan"], slot, registry)
            state, cp_ref = states[slot], unit["checkpoint"]
            unit.update(status="in-progress", reason_code=None)

            def checkpoint_writer(value):
                nonlocal state, cp_ref
                count = checkpoint_reserved_requests(value)
                require(count >= unit["reserved_requests"] and count <= 8
                    and sum(v["reserved_requests"] for key, v in collection["repair"]["units"].items()
                        if key != slot) + count <= 32, "ASHARE_REPAIR_ALLOWANCE_CHANGED")
                # Even an unknown write reserves its attempted admission. The
                # next operation can only reconcile this same immutable chain.
                unit["reserved_requests"] = count
                saved = save(retain_market_checkpoint, value, registry, store,
                    checkpoint_id=repair_checkpoint_id(slot), fresh_reader=fresh_reader,
                    predecessor=cp_ref, **permission)
                cp_ref, state = saved["reference"], deepcopy(saved["restored"]["state"])
                unit["checkpoint"] = cp_ref
                emit()

            frozen = state is not None and state["control"]["state"] in {"complete", "terminal"}
            if session is None and not frozen:
                source_client = source_client or RelayHTTP(request_timeout_seconds=90)
                source_before = source_client.requests
                source_client.validate_credential()
                session = RetryingRelay(source_client, sleep=sleep, monotonic=monotonic,
                    clock=clock, deadline=monotonic() + WORK_UNIT_SECONDS)
            queries += 1
            capture = deepcopy(state["capture"]) if frozen else capture_registered(use, registry, session,
                max_pages=pages, checkpoint=state, checkpoint_writer=checkpoint_writer,
                pause_requested=pause_requested)
            query = {"case_id": collection["plan"]["collection_id"], "use": use,
                "status": "captured" if capture["complete"] else "capture-incomplete", "capture": capture}
            if capture["complete"]:
                try:
                    _qualified(query, use, collection["plan"], registry)
                except (RecordError, MarketInputError) as exc:
                    unit.update(status="failed", reason_code=_reason(exc))
                else:
                    saved = save(retain_market_query, query, registry, store, fresh_reader=fresh_reader,
                        checkpoint_reference=cp_ref, **permission)
                    unit.update(query=json.loads(encoded(saved["reference"])), status="complete", reason_code=None)
            else:
                reason = _reason(capture.get("error") or "ASHARE_QUERY_INCOMPLETE")
                paused = state["control"]["state"] == "paused" or reason in {
                    "RELAY_RUN_DEADLINE", "RELAY_WORK_UNIT_COMPLETE", "RELAY_STORAGE_BUDGET"}
                global_stop = (capture.get("failure") or {}).get("classification") in {
                    "auth-or-entitlement", "rate-limit"} or reason == "RELAY_SECRET_REFLECTED"
                unit.update(status="blocked" if global_stop or "INFLIGHT" in reason else "paused" if paused else "failed",
                    reason_code=reason)
                if paused or global_stop:
                    collection["attempt"].update(status="blocked" if global_stop else "partial", reason_code=reason)
                    emit()
                    break
            emit()
        if collection["attempt"]["status"] != "blocked":
            collection["attempt"].update(status="complete" if _partition_complete(collection) else "partial")
            if queries == 0 and not repair_finished(collection):
                collection["attempt"]["reason_code"] = "ASHARE_NO_PROGRESS"
        persist()
    except Exception as exc:
        if isinstance(exc, (CheckpointWriteError, _PersistenceStopped)):
            if selected_slot is not None:
                collection["repair"]["units"][selected_slot].update(query=None, status="unknown-write",
                    reason_code="ASHARE_PERSISTENCE_UNCONFIRMED")
            collection["attempt"].update(status="blocked", reason_code="ASHARE_PERSISTENCE_UNCONFIRMED")
            receipt = emit(); receipt["persistence_error"] = _reason(persistence_error or exc)
            raise CollectionSyncInterrupted(receipt, reference) from exc
        if isinstance(exc, StoreUnavailable) or _reason(exc) in {
            "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "STORE_MARKER_MISMATCH",
            "COMMIT_OUTSIDE_BINDING", "REMOTE_READ_ONLY"}:
            raise
        if isinstance(exc, (RecordError, MarketInputError, CheckpointStateError)) and selected_slot is not None:
            reason = _reason(exc)
            unit = collection["repair"]["units"][selected_slot]
            if unit["status"] not in {"complete", "failed"}:
                unit.update(query=None, status="blocked", reason_code=reason)
            collection["attempt"].update(status="blocked", reason_code=reason)
            execution_error_persisted = False
            emit()
        else:
            raise
    return result()
