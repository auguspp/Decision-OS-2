"""Explicit finite A-share work units; retained source budgets survive every unit."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from decision_kernel.application.market_collection import (
    CollectionSyncInterrupted, _partition_complete, collection_summary, sync_collection,
    validate_collection_plan,
)
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_collection_reading import (
    inspect_market_chain, market_cursor_capacity, read_collection_version,
)
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.domain.records import RecordError, digest, encoded, identifier, require
from decision_kernel.infrastructure.github_store import MAX_REQUESTS, GitHubRateLimits

MAX_WORK_UNITS = 32
SOURCE_ADMISSION_API_REQUESTS = 240


def health_receipt(reading):
    """Allowlist counts and qualified dates; never emit rows, names or SourceRefs."""
    view = reading["view"]
    return {"status": reading["status"], "serving": reading["serving"],
        "source_requests": reading["source_requests"],
        "unavailable_queries": reading["unavailable_queries"],
        "selected_requested_end": reading["selected_requested_end"],
        "latest_requested_end": reading["latest_requested_end"],
        "as_of": view["as_of"], "as_of_by_exchange": deepcopy(view["as_of_by_exchange"]),
        "freshness": deepcopy(view["freshness"]), "scope_count": view["scope_count"],
        "raw": {k: deepcopy(view["raw"][k]) for k in (
            "scope_count", "observed_count", "missing_count", "incomplete_count", "horizon_counts")},
        "qfq": {k: deepcopy(view["qfq"][k]) for k in (
            "scope_count", "observed_count", "missing_count", "incomplete_count", "horizon_counts")},
        "supplier_rows_in_receipt": False,
        "qualification": "restored-partition-and-window-counts-not-provider-uptime-certification"}


def run_collection(*, operation, plan, registry, open_unit, attempt_id, max_work_units=1,
                   access_confirmed=False, custody_authorized=False, public_rights_confirmed=False,
                   progress=None, now=None, capacity=market_capacity, sync_options=None):
    """A trusted host supplies one writer/API/fresh-reader factory per work unit.

    open_unit(write) returns (store, fresh_reader, api_or_None). All writes and
    independent readbacks in that work unit share that exact API object. Only a
    published finite unit permits the host to open the next unit's budget.
    Terminal source errors, unknown writes and no-progress never start a new unit.
    """
    require(operation in {"sync", "status", "read"}, "ASHARE_OPERATION")
    plan = validate_collection_plan(plan, registry)
    identifier(attempt_id)
    require(type(max_work_units) is int and 1 <= max_work_units <= MAX_WORK_UNITS,
            "ASHARE_WORK_UNIT_BOUND")
    require(operation == "sync" or max_work_units == 1, "ASHARE_READ_ONE_OPERATION")
    require(operation != "sync" or access_confirmed is True, "ASHARE_SOURCE_ACCESS_REQUIRED")
    require(callable(open_unit), "ASHARE_RUNTIME_CONFIGURATION")
    now = datetime.now(timezone.utc) if now is None else now
    receipt = {"schema": "ashare-data-runtime-v1", "operation": operation, "status": "not-started",
        "collection_id": plan["collection_id"], "generation": plan["generation"],
        "source_requests": 0, "work_units": [], "latest": None, "health": None,
        "capacity": None, "reason_code": None, "supplier_rows_in_receipt": False,
        "private_locations_in_receipt": False}
    reference, collection, reading = None, None, None
    options = {} if sync_options is None else dict(sync_options)

    def report():
        if progress is not None:
            progress(deepcopy(receipt))

    report()
    api = None
    for number in range(1, max_work_units + 1):
        previous_limits = getattr(api, "rate_limits", None)
        if isinstance(previous_limits, GitHubRateLimits) and not previous_limits.admits(MAX_REQUESTS):
            # Stop before constructing another API/writer. An explicitly bounded
            # unit does not renew the native repository's shared hourly quota.
            receipt.update(status="paused", reason_code="ASHARE_REMOTE_RATE_LIMIT",
                           store_rate_limits=previous_limits.receipt())
            report()
            break
        store, fresh, api = open_unit(operation == "sync")
        reader = fresh()
        with market_read_scope(reader) as grouped:
            chain = inspect_market_chain(grouped, registry, collection_id=plan["collection_id"])
            reference = chain["reference"]
            collection = None if reference is None else restore_market_collection(reference, registry, grouped)["collection"]
            receipt["capacity"] = capacity(grouped)
        receipt["cursor_capacity"] = market_cursor_capacity(node_count=chain["node_count"],
                                                           unique_body_bytes=chain["unique_body_bytes"])
        if collection is not None and collection["plan"]["generation"] == plan["generation"]:
            require({k: v for k, v in plan.items() if k != "sessions"} ==
                    {k: v for k, v in collection["plan"].items() if k != "sessions"},
                    "ASHARE_RESUME_PLAN_CHANGED")
        if operation != "sync":
            require(collection is not None, "ASHARE_COLLECTION_NOT_RETAINED")
            # Read/status select the explicitly named generation; older versions
            # remain readable by exact reference through the normal custody CLI.
            require(collection["plan"]["generation"] == plan["generation"], "ASHARE_GENERATION_NOT_CURRENT")
        if collection is not None:
            receipt["latest"] = collection_summary(collection, reference)
        if operation != "sync" or collection is not None and (
                collection["plan"]["generation"] == plan["generation"] and _partition_complete(collection)):
            reading = read_collection_version(reference, registry, reader, read_as_of=now.isoformat())
            receipt.update(status="read" if operation != "sync" else "already-complete",
                           health=health_receipt(reading))
            receipt["work_units"].append({"number": number, "operation": "read",
                "source_requests": 0, "store_api_requests": getattr(api, "_requests", None)})
            report()
            break
        if not receipt["capacity"]["admit_work_unit"]:
            receipt.update(status="blocked", reason_code="ASHARE_STORAGE_CAPACITY")
            report()
            break
        if not receipt["cursor_capacity"]["admit_work_unit"]:
            receipt.update(status="blocked", reason_code="ASHARE_CURSOR_CAPACITY")
            report()
            break
        before = None if collection is None else digest(encoded(collection["units"]))
        completed_requests = receipt["source_requests"]
        native_rate_pause = False

        def quota_available():
            limits = getattr(api, "rate_limits", None)
            return not isinstance(limits, GitHubRateLimits) or limits.admits(
                max(0, MAX_REQUESTS - api._requests))

        if not quota_available():
            receipt.update(status="paused", reason_code="ASHARE_REMOTE_RATE_LIMIT",
                           store_rate_limits=api.rate_limits.receipt())
            report()
            break

        def update(value):
            receipt["latest"] = value
            receipt["source_requests"] = completed_requests + value["latest_attempt"]["source_requests"]
            receipt["status"] = "in-progress-or-unconfirmed"
            report()

        # No API is recreated by this admission callback. The successful plan,
        # attempts, pages, pause and final collection must all fit this same unit.
        def pause():
            nonlocal native_rate_pause
            if not quota_available():
                native_rate_pause = True
                return "STORAGE_BUDGET"
            return "STORAGE_BUDGET" if api is not None and api._requests >= SOURCE_ADMISSION_API_REQUESTS else None

        try:
            result = sync_collection(plan=plan, registry=registry, store=store, fresh_reader=fresh,
                attempt_id=identifier(attempt_id + "-u" + str(number)), previous_ref=reference,
                access_confirmed=access_confirmed, custody_authorized=custody_authorized,
                public_rights_confirmed=public_rights_confirmed, max_queries=1,
                now=now, pause_requested=pause, progress=update, **options)
        except CollectionSyncInterrupted as exc:
            receipt.update(status="blocked", reason_code="ASHARE_COLLECTION_PERSISTENCE_UNCONFIRMED",
                           latest=exc.receipt)
            receipt["source_requests"] = completed_requests + exc.receipt["latest_attempt"]["source_requests"]
            receipt["work_units"].append({"number": number, "operation": "sync",
                "status": "persistence-unconfirmed", "source_requests": exc.receipt["latest_attempt"]["source_requests"],
                "store_api_requests": getattr(api, "_requests", None)})
            report()
            return {"receipt": receipt, "reference": exc.reference, "reading": None}
        reference, collection = result["reference"], result["collection"]
        summary = result["receipt"]
        receipt["latest"] = summary
        receipt["source_requests"] = completed_requests + summary["latest_attempt"]["source_requests"]
        receipt["work_units"].append({"number": number, "operation": "sync", "status": result["status"],
            "source_requests": summary["latest_attempt"]["source_requests"],
            "store_api_requests": getattr(api, "_requests", None)})
        if summary["latest_attempt"]["status"] == "blocked":
            receipt.update(status="blocked", reason_code=summary["latest_attempt"]["reason_code"])
        elif _partition_complete(collection):
            receipt["status"] = "complete"
            # This is the normal exact reader, within the same completed unit's
            # API budget, with no source credential or HTTP dependency.
            if (api is None or api._requests <= 448) and quota_available():
                reading = read_collection_version(reference, registry, fresh(), read_as_of=now.isoformat())
                receipt["health"] = health_receipt(reading)
                receipt["work_units"][-1]["store_api_requests"] = getattr(api, "_requests", None)
            else:
                receipt["reason_code"] = "ASHARE_NORMAL_READ_NEXT_OPERATION_REQUIRED"
        elif before == digest(encoded(collection["units"])) or summary["latest_attempt"]["reason_code"] == "ASHARE_NO_PROGRESS":
            receipt.update(status="blocked", reason_code="ASHARE_NO_PROGRESS")
        else:
            receipt["status"] = "partial"
        limits = getattr(api, "rate_limits", None)
        if isinstance(limits, GitHubRateLimits):
            receipt["store_rate_limits"] = limits.receipt()
        if native_rate_pause and receipt["status"] == "partial":
            receipt.update(status="paused", reason_code="ASHARE_REMOTE_RATE_LIMIT")
        report()
        if receipt["status"] in {"blocked", "complete", "paused"}:
            break
    return {"receipt": receipt, "reference": reference, "reading": reading if operation == "read" else None}
