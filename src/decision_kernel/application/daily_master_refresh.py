"""Daily current-L queries over the existing Relay checkpoint and source owners.

The daily cycle reserves each venue once. Original checkpoints alone retain
pages, attempts and waits; one invocation may issue at most one new HTTP call.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import re

from decision_kernel.application.market_collection_custody import (
    retain_market_checkpoint, restore_market_checkpoint,
)
from decision_kernel.application.market_collection_reading import (
    latest_market_reference, require_checkpoint_descendant,
)
from decision_kernel.application.market_custody import (
    fresh_market_reader, market_read_scope, restore_market_query, retain_market_query,
)
from decision_kernel.application.market_security_inputs import MASTER_FIELDS, inspect_security_master
from decision_kernel.application.relay_capture_checkpoint import (
    RELEASE_DECISION, CheckpointStateError, CheckpointWriteError, STOP_CLASSES, checkpoint_time,
    release_in_flight, validate_capture_checkpoint,
)
from decision_kernel.application.research_sources import capture_registered, source_inputs, validate_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy
from decision_kernel.infrastructure.runtime_performance import measure_phase, timed

POLICY = {"schema": "daily-master-refresh-policy-v1", "trigger": "every-new-completed-session",
    "venues": ["SSE", "SZSE"], "list_status": "L", "page_size": 500,
    "max_pages_per_venue": 20, "max_attempts_per_page": 4, "max_wait_seconds_per_page": 180}
RETRY = RetryPolicy(max_attempts=4, max_wait_seconds=180)


def policy(value):
    require(isinstance(value, dict) and encoded(value) == encoded(POLICY), "DAILY_MASTER_POLICY")
    return deepcopy(POLICY)


def _cycle(cycle):
    from decision_kernel.application.daily_market import MASTER_SCHEMAS
    require(isinstance(cycle, dict) and cycle.get("schema") in MASTER_SCHEMAS
        and isinstance(cycle.get("date"), str) and re.fullmatch(r"[0-9]{8}", cycle["date"]),
        "DAILY_MASTER_CYCLE")
    policy(cycle.get("master_refresh"))
    checkpoint_time(cycle.get("created_at"))


def checkpoint_id(cycle, venue):
    _cycle(cycle)
    require(isinstance(venue, str) and venue in POLICY["venues"], "DAILY_MASTER_VENUE")
    return "daily-master-" + cycle["date"] + "-" + venue.lower()


def master_plan(cycle, registry):
    _cycle(cycle)
    return validate_plan({"schema": "research-source-plan-v1", "case_id": "daily-master-" + cycle["date"],
        "requests": [{"stage": "baseline", "purpose": "Refresh complete current-L daily market references",
            "api": "stock_basic", "parameters": {"exchange": venue, "list_status": "L",
                "fields": ",".join(sorted(MASTER_FIELDS)), "limit": "500"}}
            for venue in POLICY["venues"]]}, registry)


def _use(cycle, venue, registry):
    checkpoint_id(cycle, venue)
    return master_plan(cycle, registry)["requests"][POLICY["venues"].index(venue)]


def _check_state(state, cycle, use, registry):
    state = validate_capture_checkpoint(state, use, registry, max_pages=20, policy=RETRY)
    born = checkpoint_time(cycle["created_at"])
    attempts = []
    for page in state["capture"]["pages"]:
        require(checkpoint_time(page["captured_at"]) >= born, "DAILY_MASTER_OLD_CAPTURE")
        attempts.extend(page["attempts"])
    attempts.extend((state["control"]["retry"] or {}).get("attempts", []))
    require(all(checkpoint_time(row[key]) >= born for row in attempts
        for key in ("requested_at", "received_at")), "DAILY_MASTER_OLD_CAPTURE")
    return state


def _restore_owned(reference, cycle, venue, registry, store):
    restored = restore_market_checkpoint(reference, registry, store)
    require(restored["checkpoint_id"] == checkpoint_id(cycle, venue), "DAILY_MASTER_CHECKPOINT_OWNER")
    _check_state(restored["state"], cycle, _use(cycle, venue, registry), registry)
    return restored


def _result(cycle, use, capture):
    return {"case_id": "daily-master-" + cycle["date"], "use": use,
        "status": "captured" if capture["complete"] else "capture-incomplete", "capture": capture}


def verify_completed_master(cycle, venue, registry, store):
    """Replay the exact complete checkpoint and its original normal query pages."""
    use = _use(cycle, venue, registry)
    row = cycle["masters"][venue]
    require(row["status"] == "complete" and type(row["reserved_queries"]) is int
        and row["reserved_queries"] == 1 and row["reference"] is not None
        and row["checkpoint_reference"] is not None and row["reason_code"] is None,
        "DAILY_MASTER_INCOMPLETE")
    with market_read_scope(store) as reader:
        saved = _restore_owned(row["checkpoint_reference"], cycle, venue, registry, reader)
        state = saved["state"]
        require(state["control"]["state"] == "complete" and state["capture"]["complete"],
            "DAILY_MASTER_INCOMPLETE")
        restored = restore_market_query(row["reference"], registry, reader)
        result = restored["result"]
        expected = _result(cycle, use, state["capture"])
        require(result["case_id"] == expected["case_id"] and result["use"] == use
            and restored["page_references"] == saved["page_references"]
            and encoded([asdict(source) for source in source_inputs(result, registry)])
                == encoded([asdict(source) for source in source_inputs(expected, registry)]),
            "DAILY_MASTER_QUERY_CHECKPOINT_CHANGED")
        inspect_security_master({"L": result}, registry=registry, exchange=venue)
    return restored


def _reason(error):
    value = error if isinstance(error, str) else error.args[0] if isinstance(error, RecordError) \
        and len(error.args) == 1 and type(error.args[0]) is str else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) \
        else "DAILY_MASTER_FAILED"


def release_master_venue(cycle, venue, registry, store, fresh_reader):
    """Make one unconfirmed venue reservation explicitly resumable.

    This is the exit `_stop` never had. An unconfirmed reservation used to
    freeze a venue permanently: every later invocation returned
    persistence-unconfirmed before it could issue anything, so no amount of new
    windows could advance it and the only recourse was editing records by hand.

    Releasing does not invent a response and does not reopen the request budget.
    It records that the reservation is abandoned and leaves the venue ready for
    its next page, with the unknown request still named in the checkpoint's own
    history.
    """
    owner, use = checkpoint_id(cycle, venue), _use(cycle, venue, registry)
    row = cycle["masters"][venue]
    require(cycle["status"] == "master-pending" and row["status"] == "in-flight"
        and type(row["reserved_queries"]) is int and row["reserved_queries"] == 1
        and row["reference"] is None and row["reason_code"] is None, "DAILY_MASTER_PHASE")
    with market_read_scope(store) as reader:
        newest = latest_market_reference(reader, registry, checkpoint_id=owner)
        require(newest is not None, "DAILY_MASTER_CHECKPOINT_REQUIRED")
        saved = _restore_owned(newest, cycle, venue, registry, reader)
        state = saved["state"]
        retry = state["control"]["retry"]
        require(isinstance(retry, dict) and retry.get("phase") == "in-flight",
            "DAILY_MASTER_NOT_INFLIGHT")
        released = release_in_flight(state)
        _check_state(released, cycle, use, registry)
        retained = retain_market_checkpoint(released, registry, store, checkpoint_id=owner,
            predecessor=newest, fresh_reader=fresh_reader,
            custody_authorized=True, public_rights_confirmed=False)
    return {"status": "released", "venue": venue,
        "prior_checkpoint_reference": newest, "checkpoint_reference": retained["reference"],
        "released_attempt": retry["reserved_attempt"], "recorded_attempts": len(retry["attempts"]),
        "page_number": state["control"]["page_number"], "decision": deepcopy(RELEASE_DECISION)}


def _unconfirmed(cycle, venue, registry, store):
    """Whether this venue's newest checkpoint still holds an unanswered request."""
    row = cycle["masters"][venue]
    if row["status"] != "in-flight" or row["reference"] is not None:
        return False
    with market_read_scope(store) as reader:
        newest = latest_market_reference(reader, registry, checkpoint_id=checkpoint_id(cycle, venue))
        if newest is None:
            return False
        retry = _restore_owned(newest, cycle, venue, registry, reader)["state"]["control"]["retry"]
    return isinstance(retry, dict) and retry.get("phase") == "in-flight"


def release_unconfirmed_masters(cycle, registry, store, fresh_reader):
    """Release every venue that actually holds an unanswered request.

    Selection comes from the observed checkpoint, not from an argument, so this
    cannot release a venue that has nothing to release and needs no extra
    dispatch input. Each release is reported separately.
    """
    return [release_master_venue(cycle, venue, registry, store, fresh_reader)
        for venue in POLICY["venues"] if _unconfirmed(cycle, venue, registry, store)]


def _count(state):
    if state is None:
        return 0
    pages, retry = state["capture"]["pages"], state["control"]["retry"]
    if retry and retry["phase"] == "in-flight":
        return None  # A durable reservation does not prove whether its HTTP was sent.
    total = sum(len(page["attempts"]) for page in pages)
    if retry and not (pages and retry["phase"] == "response" and retry["attempts"] == pages[-1]["attempts"]):
        total += len(retry["attempts"])
    require(total <= 80, "DAILY_MASTER_SOURCE_BUDGET")
    return total


def _stop(state):
    retry, capture = state["control"]["retry"], state["capture"]
    if state["control"]["state"] != "complete" and retry and (retry["phase"] == "in-flight"
            or retry["phase"] == "response" and state["control"]["state"] != "terminal"):
        return "persistence-unconfirmed", "RELAY_CHECKPOINT_INFLIGHT"
    failures = [capture.get("failure"), *((row.get("failure") for row in (retry or {}).get("attempts", [])))]
    if capture.get("error") == "RELAY_SECRET_REFLECTED" or any(
            (failure or {}).get("classification") in STOP_CLASSES for failure in failures):
        return "source-held", _reason(capture.get("error") or "RELAY_CHECKPOINT_GLOBAL_STOP")
    if state["control"]["state"] == "terminal":
        return "failed", _reason(capture.get("error") or "DAILY_MASTER_INCOMPLETE")
    return None


class _CheckedReads:
    """Check the daily frontier in the original fresh read scope.

    The initial read precedes checkpoint work. Original custody then opens its
    fresh scope after writing a checkpoint, so that admission is a readback
    check before HTTP, not a CAS condition on those checkpoint writes. A failed
    readback leaves the durable in-flight reservation for explicit reconciliation.
    The daily owner separately binds its full-query reservation to its snapshot.
    """
    def __init__(self, backend, preflight):
        self.backend, self.preflight = backend, preflight

    def __getattr__(self, key):
        return getattr(self.backend, key)

    @contextmanager
    def market_read_scope(self):
        with market_read_scope(VersionedStore(self.backend)) as reader:
            if self.preflight is not None:
                with measure_phase("master.preflight"):
                    self.preflight(reader)
            yield reader.backend


class _AdmissionStopped(RuntimeError):
    def __init__(self, error):
        super().__init__("DAILY_MASTER_ADMISSION_STOPPED")
        self.error = error


class _GuardedClient:
    """Keep the existing client and transport; stop before an unapproved HTTP."""
    def __init__(self, client, guard):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_guard", guard)

    def __getattr__(self, key):
        return getattr(self._client, key)

    def __setattr__(self, key, value):
        setattr(self._client, key, value)

    def request_registered(self, *args, **kwargs):
        try:
            self._guard()
            require(type(self._client.requests) is int and self._client.requests == 0,
                "DAILY_MASTER_WORK_UNIT_BUDGET")
        except Exception as error:
            raise _AdmissionStopped(error) from None
        return self._client.request_registered(*args, **kwargs)


@timed("master.unit")
def run_master_unit(cycle, venue, registry, store, fresh_reader, client_factory, clock,
                    monotonic, sleep, guard=None, preflight=None):
    """Continue one original page/attempt, or promote a complete saved checkpoint."""
    guard = guard or (lambda: None)
    require(callable(guard) and (preflight is None or callable(preflight)), "DAILY_MASTER_PREFLIGHT")
    report = {"status": "failed", "reason_code": None, "reference": None,
        "checkpoint_reference": None, "source_requests": 0, "page_count": 0,
        "cumulative_source_requests": 0}
    state = client = persistence_error = None
    previous_count = None  # Initial custody must prove absence or restore its recorded attempts.

    def fresh():
        original = fresh_market_reader(store, fresh_reader)
        return VersionedStore(_CheckedReads(original.backend, preflight))

    def emit(status, reason=None):
        count = 0 if client is None else client.requests
        require(type(count) is int and 0 <= count <= 1, "DAILY_MASTER_WORK_UNIT_BUDGET")
        cumulative = None if previous_count is None else previous_count + count
        require(cumulative is None or cumulative <= 80, "DAILY_MASTER_SOURCE_BUDGET")
        report.update(status=status, reason_code=reason, source_requests=count,
            page_count=0 if state is None else len(state["capture"]["pages"]),
            cumulative_source_requests=cumulative)
        return deepcopy(report)

    def save_checkpoint(value):
        nonlocal state, persistence_error
        try:
            with measure_phase("master.validate"):
                _check_state(value, cycle, use, registry)
            guard()
            saved = retain_market_checkpoint(value, registry, store, checkpoint_id=owner,
                predecessor=report["checkpoint_reference"], fresh_reader=fresh,
                custody_authorized=True, public_rights_confirmed=False)
            report["checkpoint_reference"], state = saved["reference"], saved["restored"]["state"]
        except Exception as error:
            persistence_error = error
            raise

    def wait(seconds):
        remaining = seconds
        while remaining:
            guard()
            part = min(60, remaining)
            with measure_phase("relay.wait"):
                sleep(part)
            remaining -= part

    try:
        owner, use = checkpoint_id(cycle, venue), _use(cycle, venue, registry)
        row = cycle["masters"][venue]
        require(cycle["status"] == "master-pending" and row["status"] == "in-flight"
            and type(row["reserved_queries"]) is int and row["reserved_queries"] == 1
            and row["reference"] is None and row["reason_code"] is None, "DAILY_MASTER_PHASE")
        require(checkpoint_time(clock()) >= checkpoint_time(cycle["created_at"]), "DAILY_MASTER_CLOCK")
        with measure_phase("master.initial_read"), market_read_scope(fresh()) as reader:
            newest = latest_market_reference(reader, registry, checkpoint_id=owner)
            require_checkpoint_descendant(newest, row["checkpoint_reference"], registry, reader)
            if newest is not None:
                saved = _restore_owned(newest, cycle, venue, registry, reader)
                report["checkpoint_reference"], state = saved["reference"], saved["state"]
        previous_count = _count(state)
        stopped = None if state is None else _stop(state)
        if stopped is not None:
            return emit(*stopped)
        if state is None or state["control"]["state"] != "complete":
            guard()
            client = (client_factory or (lambda: RelayHTTP(request_timeout_seconds=90)))()
            client.validate_credential()
            require(type(client.requests) is int and client.requests == 0, "DAILY_MASTER_FRESH_CLIENT")
            session = RetryingRelay(_GuardedClient(client, guard), RETRY, sleep=wait,
                clock=clock, monotonic=monotonic, deadline=monotonic() + 600)
            capture_registered(use, registry, session, max_pages=20, checkpoint=state,
                checkpoint_writer=save_checkpoint,
                pause_requested=lambda: "WORK_UNIT_COMPLETE" if client.requests >= 1 else None)
        require(state is not None, "DAILY_MASTER_CHECKPOINT_REQUIRED")
        stopped = _stop(state)
        if stopped is not None:
            return emit(*stopped)
        if state["control"]["state"] != "complete":
            return emit("partial", _reason(state["capture"].get("error") or "RELAY_WORK_UNIT_COMPLETE"))
        result = _result(cycle, use, state["capture"])
        inspect_security_master({"L": result}, registry=registry, exchange=venue)
        try:
            guard()
        except RecordError as error:
            if error.args == ("DAILY_MARKET_STORE_ADMISSION",):
                # No promotion write was attempted. The complete checkpoint is
                # already independently retained; the next unit needs no source.
                return emit("partial", "RELAY_STORAGE_BUDGET")
            raise
        try:
            with measure_phase("master.promote"):
                saved = retain_market_query(result, registry, store, fresh_reader=fresh,
                    checkpoint_reference=report["checkpoint_reference"],
                    custody_authorized=True, public_rights_confirmed=False)
        except Exception as error:
            persistence_error = error
            raise
        report["reference"] = saved["reference"]
        return emit("complete")
    except Exception as error:
        uncertain = persistence_error is not None or isinstance(error,
            (UnknownWrite, CheckpointWriteError, CheckpointStateError, _AdmissionStopped))
        original = persistence_error or (error.error if isinstance(error, _AdmissionStopped) else error)
        return emit("persistence-unconfirmed" if uncertain else "failed", _reason(original))
