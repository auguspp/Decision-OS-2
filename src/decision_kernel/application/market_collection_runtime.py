"""Explicit finite A-share work units; retained source budgets survive every unit."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from decision_kernel.application.market_collection import (
    CollectionSyncInterrupted, _partition_complete, collection_progress_digest, collection_summary, sync_collection,
    validate_collection_plan,
)
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_collection_reading import (
    inspect_market_chain, market_cursor_capacity, read_collection_version,
)
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.domain.records import RecordError, identifier, require
from decision_kernel.infrastructure.github_store import MAX_REQUESTS, GitHubRateLimits, GitHubStore

MAX_WORK_UNITS = 32
SOURCE_ADMISSION_API_REQUESTS = 240
# The reviewed 90-second transport is evidence for this one fixed repair
# episode. It is not the SHA of a later workflow executing that repair.
REPAIR_TRANSPORT_CODE_SHA = "8a620022439acbfa76440ad5a1a4fc1945f61ebe"


def verify_native_repair_evidence(document, *, store, api, execution_code_sha):
    """Authenticate the core's independently restored exact query evidence.

    This uses the current work unit's existing API and quota. A partial source
    run legitimately concludes failure; query completeness belongs to the
    original checkpoints, not to an aggregate workflow success badge.
    """
    backend = store.backend
    require(isinstance(backend, GitHubStore) and backend.api is api
            and backend.binding.repository == "auguspp/Decision-OS-2"
            and backend.binding.repository_id == 1420041744
            and backend.binding.branch == "records"
            and backend.live_visibility() == "private", "ASHARE_REPAIR_NATIVE_STORE_REQUIRED")
    require(isinstance(execution_code_sha, str)
            and re.fullmatch(r"[a-f0-9]{40}", execution_code_sha),
            "ASHARE_REPAIR_EXECUTION_CODE_REQUIRED")
    evidence = document.get("transport_evidence") if isinstance(document, dict) else None
    require(isinstance(evidence, dict) and set(evidence) == {"daily", "adj_factor"},
            "ASHARE_REPAIR_TRANSPORT_EVIDENCE")
    prefix = "/repos/" + backend.binding.repository
    runs, windows = {}, {}
    for api_name, item in evidence.items():
        require(isinstance(item, dict) and item.get("code_sha") == REPAIR_TRANSPORT_CODE_SHA
                and all(isinstance(item.get(k), str) and re.fullmatch(r"[1-9][0-9]{0,14}", item[k])
                        for k in ("run_id", "run_attempt")), "ASHARE_REPAIR_TRANSPORT_IDENTITY")
        run_id = item["run_id"]
        if run_id not in runs:
            # The latest native run view must still describe this exact attempt;
            # an old attempt cannot be substituted after a later rerun.
            runs[run_id] = api.request("GET", prefix + "/actions/runs/" + run_id)
        native = runs[run_id]
        require(isinstance(native, dict) and type(native.get("id")) is int and native["id"] == int(run_id)
                and type(native.get("run_attempt")) is int
                and native["run_attempt"] == int(item["run_attempt"])
                and native.get("event") == "workflow_dispatch" and native.get("head_branch") == "main"
                and native.get("name") == "decision-os-ashare-data"
                and native.get("path") == ".github/workflows/ashare-data.yml"
                and native.get("head_sha") == item["code_sha"]
                and native.get("status") == "completed"
                and native.get("conclusion") in {"success", "failure"},
                "ASHARE_REPAIR_NATIVE_RUN_MISMATCH")
        require(all(isinstance(native.get(k), dict)
                    and native[k].get("full_name") == backend.binding.repository
                    and type(native[k].get("id")) is int
                    and native[k]["id"] == backend.binding.repository_id
                    for k in ("repository", "head_repository")),
                "ASHARE_REPAIR_NATIVE_REPOSITORY_MISMATCH")
        stamps = [native.get("run_started_at"), native.get("updated_at")]
        require(all(isinstance(stamp, str) and 0 < len(stamp) <= 80 and "T" in stamp for stamp in stamps),
                "ASHARE_REPAIR_NATIVE_RUN_TIME")
        try:
            started, completed = [datetime.fromisoformat(stamp) for stamp in stamps]
        except ValueError as exc:
            raise RecordError("ASHARE_REPAIR_NATIVE_RUN_TIME") from exc
        require(started.utcoffset() is not None and completed.utcoffset() is not None
                and started <= completed, "ASHARE_REPAIR_NATIVE_RUN_TIME")
        # A completed run's native updated_at is the upper bound of this
        # evidence window; it is not claimed to be an exact source/step end.
        windows[api_name] = {"run_id": item["run_id"], "run_attempt": item["run_attempt"],
                            "started_at": stamps[0], "completed_at": stamps[1]}
    # Reuse the store's immutable native ancestry proof, without pretending that
    # a records anchor authenticates engineering code or weakening its checks.
    backend._ancestor(REPAIR_TRANSPORT_CODE_SHA, execution_code_sha)
    return {"execution_code_sha": execution_code_sha, "transport_runs": windows}


def health_receipt(reading):
    """Allowlist counts and qualified dates; never emit rows, names or SourceRefs."""
    view = reading["view"]
    result = {"status": reading["status"], "serving": reading["serving"],
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
    if "endpoint_comparisons" in view:
        endpoints = view["endpoint_comparisons"]
        result["endpoint_comparisons"] = {kind: {
            **{key: endpoints[kind][key] for key in (
                "scope_count", "observed_count", "missing_count", "incomplete_count")},
            "horizon_counts": [{key: window[key] for key in (
                "sessions", "scope_count", "observed_count", "missing_count")}
                for window in endpoints[kind]["horizon_counts"]]} for kind in ("raw", "qfq")}
    return result


def run_collection(*, operation, plan, registry, open_unit, attempt_id, max_work_units=1,
                   access_confirmed=False, custody_authorized=False, public_rights_confirmed=False,
                   progress=None, now=None, capacity=market_capacity, sync_options=None,
                   repair_request=None, execution_code_sha=None):
    """A trusted host supplies one writer/API/fresh-reader factory per work unit.

    open_unit(write) returns (store, fresh_reader, api_or_None). All writes and
    independent readbacks in that work unit share that exact API object. Only a
    published finite unit permits the host to open the next unit's budget.
    Terminal source errors, unknown writes and no-progress never start a new unit.
    """
    require(operation in {"sync", "repair", "status", "read"}, "ASHARE_OPERATION")
    writing = operation in {"sync", "repair"}
    require(operation == "repair" or repair_request is None, "ASHARE_REPAIR_EXPLICIT_OPERATION_REQUIRED")
    if operation == "repair":
        from decision_kernel.application.market_collection_repair import (
            repair_collection, repair_finished, repair_summary,
        )
        require(isinstance(execution_code_sha, str) and re.fullmatch(r"[a-f0-9]{40}", execution_code_sha),
                "ASHARE_REPAIR_EXECUTION_CODE_REQUIRED")
    plan = validate_collection_plan(plan, registry)
    identifier(attempt_id)
    require(type(max_work_units) is int and 1 <= max_work_units <= MAX_WORK_UNITS,
            "ASHARE_WORK_UNIT_BOUND")
    require(writing or max_work_units == 1, "ASHARE_READ_ONE_OPERATION")
    require(not writing or access_confirmed is True, "ASHARE_SOURCE_ACCESS_REQUIRED")
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
        store, fresh, api = open_unit(writing)
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
        if operation == "repair" and repair_request is None and repair_finished(collection):
            receipt["repair"] = repair_summary(collection)
            receipt["status"] = receipt["repair"]["status"]
            receipt["reason_code"] = "ASHARE_REPAIR_EXHAUSTED" if receipt["status"] == "repair-exhausted" else None
            receipt["work_units"].append({"number": number, "operation": "read",
                "source_requests": 0, "store_api_requests": getattr(api, "_requests", None)})
            report()
            break
        if not writing or operation == "sync" and collection is not None and (
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
        before = None if collection is None else collection_progress_digest(collection)
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
            execute = repair_collection if operation == "repair" else sync_collection
            extra = {} if operation != "repair" else {
                "grant_request": repair_request,
                "execution_code_sha": execution_code_sha,
                "verify_evidence": lambda document: verify_native_repair_evidence(
                    document, store=store, api=api, execution_code_sha=execution_code_sha),
            }
            result = execute(plan=plan, registry=registry, store=store, fresh_reader=fresh,
                attempt_id=identifier(attempt_id + "-u" + str(number)), previous_ref=reference,
                access_confirmed=access_confirmed, custody_authorized=custody_authorized,
                public_rights_confirmed=public_rights_confirmed, max_queries=1,
                now=now, pause_requested=pause, progress=update, **extra, **options)
        except CollectionSyncInterrupted as exc:
            receipt.update(status="blocked", reason_code="ASHARE_COLLECTION_PERSISTENCE_UNCONFIRMED",
                           latest=exc.receipt)
            receipt["source_requests"] = completed_requests + exc.receipt["latest_attempt"]["source_requests"]
            receipt["work_units"].append({"number": number, "operation": operation,
                "status": "persistence-unconfirmed", "source_requests": exc.receipt["latest_attempt"]["source_requests"],
                "store_api_requests": getattr(api, "_requests", None)})
            report()
            return {"receipt": receipt, "reference": exc.reference, "reading": None}
        reference, collection = result["reference"], result["collection"]
        if operation == "repair":
            # Only a successfully published first unit consumes the request.
            # Every following unit resumes the immutable retained grant.
            repair_request = None
            receipt["repair"] = repair_summary(collection)
        summary = result["receipt"]
        receipt["latest"] = summary
        receipt["source_requests"] = completed_requests + summary["latest_attempt"]["source_requests"]
        receipt["work_units"].append({"number": number, "operation": operation, "status": result["status"],
            "source_requests": summary["latest_attempt"]["source_requests"],
            "store_api_requests": getattr(api, "_requests", None)})
        if summary["latest_attempt"]["status"] == "blocked":
            receipt.update(status="blocked", reason_code=summary["latest_attempt"]["reason_code"])
        elif operation == "repair" and repair_finished(collection):
            receipt["status"] = receipt["repair"]["status"]
            receipt["reason_code"] = "ASHARE_REPAIR_EXHAUSTED" if receipt["status"] == "repair-exhausted" else None
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
        elif before == collection_progress_digest(collection) or summary["latest_attempt"]["reason_code"] == "ASHARE_NO_PROGRESS":
            receipt.update(status="blocked", reason_code="ASHARE_NO_PROGRESS")
        else:
            receipt["status"] = "partial"
        limits = getattr(api, "rate_limits", None)
        if isinstance(limits, GitHubRateLimits):
            receipt["store_rate_limits"] = limits.receipt()
        if native_rate_pause and receipt["status"] == "partial":
            receipt.update(status="paused", reason_code="ASHARE_REMOTE_RATE_LIMIT")
        report()
        if receipt["status"] in {"blocked", "complete", "paused", "repair-complete", "repair-exhausted"}:
            break
    return {"receipt": receipt, "reference": reference, "reading": reading if operation == "read" else None}
