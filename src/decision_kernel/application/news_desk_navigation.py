"""Read-only navigation over the original retained news windows and editions.

Paths nominate candidates; the original index and Brief readers own validation.
There is no latest pointer, new news pool, writer, provider call or scheduler.
"""
from __future__ import annotations

from copy import deepcopy
import re

from decision_kernel.application import brief_delivery, brief_discovery, news_rolling, news_rolling_index
from decision_kernel.application.news_rolling import clock, reference
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import RecordError, SHA, SourceRef, encoded, digest, require

SCHEMA = "news-desk-navigation-v1"
WINDOW_PREFIX = "source-views/news-roll-"
WINDOW_PATH = re.compile(r"source-views/news-roll-[a-z0-9][a-z0-9_-]*/[a-f0-9]{64}/manifest\.json\Z")
MAX_WINDOWS = 64
MAX_HISTORY_PAGE = 20
CURSOR_SCHEMA = "news-desk-history-cursor-v1"
HISTORY_SCOPE = "normal-news-host-windows-and-editions"


def plan_input(value, *, now):
    clock(now)
    require(isinstance(value, dict) and set(value) <= {"as_of", "snapshot", "history"},
            "NEWS_NAVIGATION_PLAN_INVALID")
    as_of = value.get("as_of", now)
    require(clock(as_of) <= clock(now), "NEWS_NAVIGATION_FUTURE_READ_CLOCK")
    history = value.get("history", {})
    require(isinstance(history, dict) and set(history) <= {"limit", "cursor"}, "NEWS_HISTORY_PLAN_INVALID")
    limit, cursor = history.get("limit", 8), history.get("cursor")
    require(type(limit) is int and 1 <= limit <= MAX_HISTORY_PAGE, "NEWS_HISTORY_PAGE_BOUND")
    snapshot = value.get("snapshot")
    if cursor is not None:
        require(isinstance(cursor, dict) and set(cursor) == {
            "schema", "repository", "snapshot", "scope", "inventory_sha256", "offset"}
            and cursor["schema"] == CURSOR_SCHEMA and cursor["scope"] == HISTORY_SCOPE
            and type(cursor["offset"]) is int and cursor["offset"] >= 0,
            "NEWS_HISTORY_CURSOR_INVALID")
        require(snapshot is None or snapshot == cursor["snapshot"], "NEWS_HISTORY_SNAPSHOT_CHANGED")
        snapshot = cursor["snapshot"]
    require(snapshot is None or isinstance(snapshot, str) and SHA.fullmatch(snapshot),
            "NEWS_HISTORY_SNAPSHOT_INVALID")
    return {"as_of": as_of, "snapshot": snapshot, "history": {"limit": limit, "cursor": deepcopy(cursor)}}


def _check_reader(reader, error=None):
    # The native host's original SourceReads owns its optional failure latch.
    check = getattr(reader, "raise_if_terminal", None)
    if check is not None:
        check()
    if error is not None and brief_discovery.operation_failure(error):
        raise error


def _window_candidate(reader, ref):
    """Classify original retained representations, without upgrading their use."""
    value = news_rolling._load(reader, ref)
    if isinstance(value, dict) and value.get("schema") == news_rolling.SCHEMA:
        news_rolling.verify_rolling(value)
        details = {"rolling_sha256": value["rolling_sha256"], "entry_count": len(value["entries"])}
    else:
        # Unknown schemas and damaged indexes remain errors, never legacy fallbacks.
        news_rolling_index.verify_index(value)
        details = {"index_sha256": value["index_sha256"], "entry_count": value["entry_count"]}
    return {"reference": ref, "as_of": value["as_of"], "window_start": value["window_start"],
            "snapshot_schema": value["schema"], **details}


def discover_windows(store, reader, *, snapshot):
    backend = store.backend
    backend.check_commit(snapshot)
    paths = sorted(p for p in backend.paths(snapshot, "source-views/") if WINDOW_PATH.fullmatch(p))
    result = {"scope": "normal-news-host-rolling-indexes", "prefix": WINDOW_PREFIX,
        "snapshot": snapshot, "total_candidates": len(paths), "scanned": 0,
        "valid_candidates": 0, "candidates": [], "failures": [], "complete": False,
        "reference": None, "selection": "greatest-original-window-as-of",
        "dependency_replay": "selected-window-only"}
    if len(paths) > MAX_WINDOWS:
        return {**result, "status": "not-included", "error": "NEWS_WINDOW_INVENTORY_BOUND"}
    for path in paths:
        ref = None
        try:
            ref = reference(SourceRef(backend.reference(snapshot, path)))
            result["candidates"].append(_window_candidate(reader, ref))
        except (RecordError, KeyError, TypeError) as error:
            _check_reader(reader, error)
            result["failures"].append({"reference": ref, "path": path,
                "status": "invalid-or-missing", "error": "NEWS_WINDOW_CANDIDATE_UNREADABLE"})
        result["scanned"] += 1
    candidates = result["candidates"]
    candidates.sort(key=lambda row: row["reference"]["manifest"]["path"])
    candidates.sort(key=lambda row: clock(row["as_of"]), reverse=True)
    result["valid_candidates"] = len(candidates)
    if result["failures"]:
        return {**result, "status": "partial", "error": "NEWS_WINDOW_DISCOVERY_INCOMPLETE"}
    if not candidates:
        return {**result, "status": "no-retained-window", "complete": True}
    chosen = candidates[0]
    return {**result, "status": "selected", "complete": True, "reference": chosen["reference"],
        "latest_as_of": chosen["as_of"], "snapshot_schema": chosen["snapshot_schema"],
        "same_as_of_count": sum(clock(row["as_of"]) == clock(chosen["as_of"]) for row in candidates),
        "tie_rule": "manifest-path-ascending-not-storage-order"}


def age(at, as_of, *, basis):
    if at is None:
        return {"at": None, "read_as_of": as_of, "age_seconds": None, "status": "unknown", "basis": basis}
    seconds = (clock(as_of) - clock(at)).total_seconds()
    return {"at": at, "read_as_of": as_of, "age_seconds": seconds,
        "status": "read-clock-before-original-time" if seconds < 0 else
                  "older-than-24h" if seconds > 86400 else "within-24h", "basis": basis}


def time_status(logical, document, *, as_of, rendered_at):
    captured = [row["observed_at"] for row in logical["source_outcomes"]
        if row["outcome"]["status"] == "captured" and row["clock_basis"] == "capture-receipt-clock"]
    legacy = [row["observed_at"] for row in logical["source_outcomes"]
        if row["outcome"]["status"] == "captured" and row["clock_basis"] == "legacy-observed-at-not-receipt-clock"]
    last_capture = max(captured, key=clock) if captured else None
    last_legacy = max(legacy, key=clock) if legacy else None
    return {"last_successful_capture_at": last_capture,
        "capture_clock_basis": "original-capture-receipt-clock" if captured else "not-established",
        "capture_age": age(last_capture, as_of, basis="original-successful-capture-receipt"),
        "latest_legacy_observation_at": last_legacy,
        "legacy_observation_age": age(last_legacy, as_of, basis="legacy-observation-not-capture-receipt"),
        "window_start": logical["window_start"], "window_as_of": logical["as_of"],
        "window_age": age(logical["as_of"], as_of, basis="original-observation-window"),
        "delivery_cutoff": document["plan"]["cutoff"] if document is not None else None,
        "delivery_age": age(document["plan"]["cutoff"] if document is not None else None,
                            as_of, basis="declared-delivery-cutoff-not-source-time"),
        "read_as_of": as_of, "rendered_at": rendered_at,
        "publication_claims": "original-per-row-raw-claim-and-precision-no-inferred-publication-time",
        "complete_news_coverage": False, "continuous_capture_verified": False}


def _history(windows, editions, plan, repository):
    rows = []
    for kind, collection, stamp in (("window", windows, "as_of"), ("edition", editions, "cutoff")):
        for item in collection["candidates"]:
            # Do not leak research case names or feedback from discovery cards.
            row = {"kind": kind, "status": "candidate-metadata-verified",
                "reference": deepcopy(item["reference"]), "original_time": item[stamp],
                "dependency_replay": "not-performed-for-history-row"}
            row["open_request"] = ({"operation": "read-desk", "plan": {"reference": item["reference"]}}
                if kind == "edition" else {"operation": "desk", "save": False, "plan": {
                    "window": item["reference"], "cutoff": item["as_of"], "kind": "adhoc", "attention": []}})
            if kind == "window" and item["snapshot_schema"] == news_rolling.SCHEMA:
                # Native desk/read require a compact index and exact batch replay.
                # The old offline reader validates inline bytes only; do not export
                # a host request which cannot consume this original representation.
                row.update(snapshot_schema=item["snapshot_schema"], open_request=None,
                           read_status="legacy-offline-reader-only")
            rows.append(row)
        rows.extend({"kind": kind, **deepcopy(failure), "original_time": None,
                     "open_request": None} for failure in collection["failures"])
    def path(row):
        return row.get("path") or (row["reference"].get("manifest") or row["reference"])["path"]
    rows.sort(key=lambda row: (row["kind"], path(row)))
    rows.sort(key=lambda row: clock(row["original_time"]) if row["original_time"] else clock("0002-01-01T00:00:00Z"), reverse=True)
    inventory_hash = digest(encoded({"snapshot": plan["snapshot"], "rows": rows}))
    history = plan["history"]
    cursor = history["cursor"]
    offset = cursor["offset"] if cursor is not None else 0
    if cursor is not None:
        require(cursor["repository"] == repository and cursor["inventory_sha256"] == inventory_hash,
                "NEWS_HISTORY_CURSOR_SCOPE_CHANGED")
    require(offset <= len(rows), "NEWS_HISTORY_OFFSET_OUTSIDE_INVENTORY")
    end = min(offset + history["limit"], len(rows))
    next_cursor = {"schema": CURSOR_SCHEMA, "repository": repository, "snapshot": plan["snapshot"],
        "scope": HISTORY_SCOPE, "inventory_sha256": inventory_hash, "offset": end} if end < len(rows) else None
    return {"scope": HISTORY_SCOPE, "repository": repository, "snapshot": plan["snapshot"],
        "total_candidates": windows["total_candidates"] + editions["total_candidates"],
        "scanned_candidates": windows["scanned"] + editions["scanned"], "listed_candidates": len(rows),
        "inventory_complete": windows["complete"] and editions["complete"],
        "invalid_candidates": len(windows["failures"]) + len(editions["failures"]),
        "offset": offset, "returned": end - offset, "limit": history["limit"],
        "coverage": "this-page-only", "inventory_sha256": inventory_hash,
        "items": rows[offset:end], "next_cursor": next_cursor}


def _same_window(left, right):
    # Both manifests are independently verified. A later snapshot can name the
    # same immutable path/digest without rewriting the edition's original Ref.
    return all(left["manifest"][key] == right["manifest"][key]
               for key in ("repository", "path", "sha256")) and left["selection"] == right["selection"]


def build_navigation(store, reader, value, *, now, authorize_document=None):
    plan = plan_input(value, now=now)
    cursor = plan["history"]["cursor"]
    require(cursor is None or cursor["repository"] == store.backend.repository,
            "NEWS_HISTORY_CURSOR_SCOPE_CHANGED")
    plan["snapshot"] = plan["snapshot"] or store.backend.head()
    windows = discover_windows(store, reader, snapshot=plan["snapshot"])
    editions = brief_discovery.discover_editions(store, snapshot=plan["snapshot"], as_of=plan["as_of"], strict=True)
    history = _history(windows, editions, plan, store.backend.repository)
    result = {"schema": SCHEMA, "repository": store.backend.repository, "snapshot": plan["snapshot"],
        "plan": plan, "read_as_of": plan["as_of"], "rendered_at": now,
        "latest_window": {k: deepcopy(v) for k, v in windows.items() if k != "candidates"},
        "latest_saved_edition": {k: deepcopy(v) for k, v in editions.items()
                                 if k not in {"candidates", "choices"}},
        "history": history, "display": {"status": "unavailable", "reference": None},
        "refresh_plan": {"history": {"limit": plan["history"]["limit"]}},
        "next_plan": {**deepcopy(plan), "history": {"limit": plan["history"]["limit"],
                     "cursor": history["next_cursor"]}} if history["next_cursor"] is not None else None,
        "source_http_requests": 0, "records_write": "not-requested", "research_executed": False,
        "notification_sent": False, "human_acceptance": "not-confirmed"}
    artifacts = ResearchArtifacts(store)
    saved = saved_logical = latest_logical = None

    def open_saved(ref):
        doc = brief_delivery._read_document(artifacts, ref)
        if authorize_document is not None:
            authorize_document(doc)
        found, logical = brief_delivery.open_edition(reader, artifacts, ref)
        _check_reader(reader)
        return found, logical

    if windows["reference"] is not None and windows["snapshot_schema"] == news_rolling.SCHEMA:
        result["latest_window"].update(status="selected-not-readable",
            dependency_replay="not-performed-for-legacy-materialized-window",
            error="NEWS_LEGACY_WINDOW_DESK_REPLAY_UNAVAILABLE")
    elif windows["reference"] is not None:
        try:
            latest_logical = news_rolling_index.restore_index(reader,
                news_rolling_index.open_index(reader, windows["reference"]))
            _check_reader(reader)
            result["latest_window"].update(dependency_replay="verified-exact-selected-batches",
                clocks=time_status(latest_logical, None, as_of=plan["as_of"], rendered_at=now))
        except (RecordError, KeyError, TypeError) as error:
            _check_reader(reader, error)
            result["latest_window"].update(status="selected-window-unavailable", error="NEWS_WINDOW_REPLAY_FAILED")
    if editions["reference"] is not None:
        try:
            saved, saved_logical = open_saved(editions["reference"])
            result["latest_saved_edition"].update(dependency_replay="verified-original-edition-and-dependencies",
                clocks=time_status(saved_logical, saved, as_of=plan["as_of"], rendered_at=now),
                open_request={"operation": "read-desk", "plan": {"reference": editions["reference"]}})
        except (RecordError, KeyError, TypeError) as error:
            _check_reader(reader, error)
            result["latest_saved_edition"].update(status="selected-edition-unavailable",
                error="NEWS_EDITION_PUBLICATION_NOT_CONFIRMED" if str(error).endswith("PUBLICATION_NOT_CONFIRMED")
                      else "NEWS_EDITION_REPLAY_FAILED")
    model = None
    ready = windows["complete"] and editions["complete"] \
        and (windows["reference"] is None or latest_logical is not None) \
        and (editions["reference"] is None or saved is not None)
    if ready:
        doc, logical, ref, status = saved, saved_logical, editions["reference"], "saved-edition"
        if latest_logical is not None and (saved_logical is None or
                clock(latest_logical["as_of"]) >= clock(saved_logical["as_of"])):
            matching = next((row for row in editions["candidates"]
                             if _same_window(row["window"], windows["reference"])), None)
            try:
                if matching is not None:
                    ref = matching["reference"]
                    doc, logical = ((saved, saved_logical) if ref == editions["reference"] else open_saved(ref))
                else:
                    doc, logical = brief_delivery.build_edition(reader, artifacts, {
                        "window": windows["reference"], "cutoff": latest_logical["as_of"], "kind": "adhoc", "attention": []})
                    ref, status = None, "retained-window-preview-not-saved"
                _check_reader(reader)
            except (RecordError, KeyError, TypeError) as error:
                _check_reader(reader, error)
                doc, logical = None, None
                result["display"]["error"] = "NEWS_SELECTED_DESK_UNAVAILABLE"
        if doc is not None:
            model = brief_delivery.page_model(doc, logical, ref)
            result["display"] = {"status": status, "reference": ref,
                "window_reference": doc["plan"]["window"], "edition_sha256": doc["edition_sha256"],
                "clocks": time_status(logical, doc, as_of=plan["as_of"], rendered_at=now)}
        elif windows["reference"] is None and editions["reference"] is None:
            result["display"]["status"] = "no-retained-desk"
    result["status"] = "readable" if model is not None else (
        "empty" if result["display"]["status"] == "no-retained-desk" else "partial")
    if model is not None:
        model["navigation"] = result
    return result, model
