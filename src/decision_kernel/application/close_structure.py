"""Close structure from exact retained market inputs, without new records or I/O.

MarketDiscovery's existing core owns source restoration, clocks, scope and
conventions. Its trading-path classification is not an admission condition for
this independent finite observation. No provider, fallback or artifact writer.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.market_discovery import (
    KINDS, MAX_SCOPE, MarketDiscovery, _clock, _reason, _surface_plan,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded, require
from decision_kernel.observations.close_structure import POLICY, dimensions, observe_close_structure
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.market_state import comparable

PLAN_SCHEMA = "close-structure-plan-v1"
SCHEMA = "close-structure-v1"
MAX_DISPLAY_BYTES = 768 * 1024
# These reasons belong exclusively to the old 20/5-session path policy. Prices
# may also survive source holds or failed clocks for diagnostics: every other
# reason remains disqualifying, even when a numerical series is present.
PATH_HISTORY_REASONS = frozenset({None, "DISCOVERY_INSUFFICIENT_CALENDAR_HISTORY",
    "DISCOVERY_MISSING_PRICE_WINDOW", "DISCOVERY_PERSISTENCE_GAP",
    "DISCOVERY_INSUFFICIENT_PERSISTENCE_HISTORY"})
LIMITS = ["CLOSE_ONLY_NOT_OHLC_AUCTION_MINUTE_CHAN_OR_WYCKOFF",
    "RIGHT_CLOSE_CONFIRMS_A_LOCAL_TURN_NOT_A_TRADE_OR_FUNDAMENTAL_JUDGMENT",
    "RETAINED_SOURCE_REPLAY_NOT_PROSPECTIVE_DISCOVERY_OR_POINT_IN_TIME_CERTIFICATION",
    "ORIGINAL_CATALOGUE_SCOPE_INCLUDES_MISSING_AND_QUARANTINED_IDENTITIES",
    "CURRENT_AND_PREVIOUS_SOURCE_VERSIONS_ARE_NEVER_STITCHED_OR_REWRITTEN"]


def fatal_read_error(error):
    """A runtime/access boundary is not an ordinary incomplete source window."""
    code = str(error)
    return isinstance(error, StoreUnavailable) or code in {
        "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
        "COMMIT_OUTSIDE_BINDING", "COMMIT_NOT_REACHABLE", "FOREIGN_REPOSITORY",
        "STORE_MARKER_MISMATCH", "STORE_BINDING_REQUIRED", "COMPLETE_SNAPSHOT_REQUIRED",
        "EXTERNAL_OBJECTS_FORBIDDEN", "DEDICATED_BARE_STORE_REQUIRED", "INVALID_SNAPSHOT",
        "INVALID_SNAPSHOT_TREE", "MARKET_CUSTODY_READ_SCOPE_REQUIRED",
        "MARKET_CUSTODY_READ_SCOPE_REPOSITORY_CHANGED", "DISCOVERY_SOURCE_STORE_CHANGED",
        "DISCOVERY_SHARED_API_BUDGET_REQUIRED"} or code.startswith((
            "REMOTE_", "MARKET_SNAPSHOT_", "SNAPSHOT_", "READING_SCOPE_", "SOURCE_SCAN_"))


class _SourceReads:
    """Read-only failure latch around the already pinned original backend.

    Nested market scopes keep this facade, including its failure latch, rather
    than recreating an unguarded VersionedStore. No new API or source budget.
    """
    def __init__(self, backend):
        self.owner, self.failure, self.active = backend, None, True
        self.repository = backend.repository
        self.api = getattr(backend, "api", None)
        self.binding = getattr(backend, "binding", None)

    def check(self):
        require(self.active, "READING_SCOPE_CLOSED")
        if self.failure is not None:
            raise self.failure

    @contextmanager
    def market_read_scope(self):
        self.check()
        yield self  # The enclosing original market_read_scope owns the pin and final check.
        self.check()

    def __getattr__(self, name):
        if name not in ("head", "check_commit", "read", "paths", "reference", "resolve", "last_change"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._read(name, *args, **kwargs)

    def _read(self, name, *args, **kwargs):
        self.check()
        try:
            return getattr(self.owner, name)(*args, **kwargs)
        except RecordError as error:
            if fatal_read_error(error):
                self.failure = error if isinstance(error, StoreUnavailable) else StoreUnavailable(str(error))
                raise self.failure from None
            raise

    def commit(self, files, *, expected):
        raise RecordError("READING_SCOPE_READ_ONLY")


def plan_input(value):
    require(isinstance(value, dict) and {"schema", "kind", "reference", "as_of"} <= set(value)
        <= {"schema", "kind", "reference", "as_of", "adjustment", "windows", "turn_sessions",
            "previous", "entity_id", "offset", "limit"}
        and value["schema"] == PLAN_SCHEMA, "STRUCTURE_PLAN_INVALID")
    kind = value["kind"]
    require(kind in KINDS, "STRUCTURE_KIND_INVALID")
    _clock(value["as_of"])
    surface = _surface_plan(kind, {k: value[k] for k in ("reference", "adjustment") if k in value})
    try:
        windows, turns = dimensions(value.get("windows", [5, 20, 60]), value.get("turn_sessions", 20))
    except MarketInputError as error:
        raise RecordError(str(error)) from None
    offset, limit, entity = value.get("offset", 0), value.get("limit", 20), value.get("entity_id")
    require(type(offset) is int and 0 <= offset <= MAX_SCOPE and type(limit) is int and 1 <= limit <= 50,
            "STRUCTURE_PAGE_INVALID")
    require(entity is None or isinstance(entity, str) and 0 < len(entity) <= 128
        and entity.isprintable() and entity.strip() == entity, "STRUCTURE_ENTITY_INVALID")
    previous = value.get("previous")
    if previous is not None:
        require(isinstance(previous, dict) and {"reference", "as_of"} <= set(previous)
            <= {"reference", "as_of", "adjustment"}, "STRUCTURE_PREVIOUS_INVALID")
        require(_clock(previous["as_of"]) <= _clock(value["as_of"]), "STRUCTURE_CLOCK_REVERSED")
        prior_surface = {k: previous[k] for k in ("reference", "adjustment") if k in previous}
        if kind == "stock":
            prior_surface.setdefault("adjustment", surface["adjustment"])
        previous = {**_surface_plan(kind, prior_surface), "as_of": previous["as_of"]}
    result = {"schema": PLAN_SCHEMA, "kind": kind, **surface, "as_of": value["as_of"],
        "windows": list(windows), "turn_sessions": turns, "previous": previous,
        "entity_id": entity, "offset": offset, "limit": limit}
    require(len(encoded(result)) <= 16384, "STRUCTURE_PLAN_BOUND")
    return result


def _qualification(core, entity):
    if core["status"] != "readable":
        return core.get("reason_code", "DISCOVERY_SOURCE_UNAVAILABLE")
    if core.get("source_hold"):
        return "DISCOVERY_SOURCE_HOLD"
    if entity is None:
        return "DISCOVERY_NOT_REQUESTED"
    reason = entity["observation"]["reason_code"]
    return None if reason in PATH_HISTORY_REASONS else reason


def _summary(core):
    return {k: deepcopy(core.get(k)) for k in ("status", "reason_code", "scope_count", "as_of",
        "as_of_by_exchange", "source_status", "source_hold", "scope_semantics",
        "universe_version", "adjustment", "source_reference")}


def _source_index(core):
    result = {}
    for value in core["sources"].values():
        ref = value.get("reference")
        if ref is not None:
            result.setdefault(digest(encoded(ref)), []).append(deepcopy(value))
    return result


def _history(core, current, previous_core, previous, prior_requested):
    if not prior_requested:
        return {"status": "baseline", "previous_source_reference": None,
                "semantics": "no-comparison-requested"}
    reason = _qualification(core, current)
    prior_reason = _qualification(previous_core, previous)
    if reason or prior_reason:
        status = "source-unavailable"
    else:
        status = comparable(current, previous)
    return {"status": status, "source_reason_code": reason, "previous_reason_code": prior_reason,
        "previous_as_of": None if previous is None else previous["as_of"],
        "previous_source_reference": None if previous is None else deepcopy(previous["source_reference"]),
        "semantics": "exact-source-pair-comparison-not-an-event-log-or-timely-discovery-claim"}


def _row(code, core, previous_core, plan, source_index):
    entity = core["entities"].get(code)
    reason = core["gaps"].get(code) or _qualification(core, entity)
    original_reason = None if entity is None else entity["observation"]["reason_code"]
    ref = None if entity is None else entity["source_reference"]
    before = None if previous_core is None else previous_core["entities"].get(code)
    row = {"entity_id": code, "name": code if entity is None else entity["name"],
        "as_of": core.get("as_of") if entity is None else entity["as_of"],
        "source_reference": deepcopy(ref),
        "source_qualification": {"status": "unavailable" if reason else "eligible",
            "reason_code": reason, "original_path_reason": original_reason},
        "source_provenance": [] if ref is None else source_index.get(digest(encoded(ref)), []),
        "history": _history(core, entity, previous_core, before, plan["previous"] is not None),
        "observation": None}
    if reason is None:
        stock = plan["kind"] == "stock"
        try:
            row["observation"] = observe_close_structure(entity_id=code,
                series=[{"trade_date": day, "close": price} for day, price in entity["prices"].items()],
                as_of=entity["as_of"], market_sessions=entity["sessions"],
                calendar_version=digest(encoded(entity["sessions"])),
                source_version=digest(encoded(row["source_provenance"])),
                market=("SSE" if code.endswith(".SH") else "SZSE") if stock else "THS-A",
                currency="CNY" if stock else "XXX",
                adjustment="qfq" if stock and core["adjustment"] == "forward" else "raw",
                windows=plan["windows"], turn_sessions=plan["turn_sessions"])
        except MarketInputError as error:
            # A structural input failure cannot become a valid empty observation.
            row["source_qualification"] = {"status": "unavailable", "reason_code": _reason(error),
                                            "original_path_reason": original_reason}
    row["status"] = "source-unavailable" if row["observation"] is None else row["observation"]["status"]
    return row


class CloseStructure:
    def __init__(self, store, registry=None):
        # The normal native room runs in its checked-out code tree, exactly as
        # read_market. A local CLI can explicitly select its trusted registry.
        registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
        self.discovery = MarketDiscovery(store, registry)

    def read(self, value):
        plan = plan_input(value)
        with market_read_scope(self.discovery.source_store) as reader:
            sources = _SourceReads(reader.backend)
            try:
                service = MarketDiscovery(self.discovery.store, self.discovery.registry,
                                          source_store=VersionedStore(sources))
                kind = plan["kind"]
                surface = {k: plan[k] for k in ("reference", "adjustment") if k in plan}
                _, core = service._core(kind, surface, plan["as_of"])
                sources.check()
                previous = None
                if plan["previous"] is not None:
                    prior = plan["previous"]
                    _, previous = service._core(kind, {k: prior[k] for k in ("reference", "adjustment")
                                                      if k in prior}, prior["as_of"])
                    sources.check()
                return self._project(plan, core, previous)
            finally:
                sources.active = False

    def _project(self, plan, core, previous):
        identities = sorted(set(core["entities"]) | set(core["gaps"]))
        if core["scope_count"] is not None:
            require(len(identities) == core["scope_count"], "STRUCTURE_SCOPE_MISMATCH")
        sources = _source_index(core)
        status_counts, reasons, histories = Counter(), Counter(), Counter()
        windows = {str(n): Counter() for n in plan["windows"]}
        eligible, all_digests, rows = 0, [], []
        selected = identities if plan["entity_id"] is None else [x for x in identities if x == plan["entity_id"]]
        page_ids = set(selected[plan["offset"]:plan["offset"] + plan["limit"]])
        for code in identities:
            row = _row(code, core, previous, plan, sources)
            all_digests.append(digest(encoded(row)))
            status_counts[row["status"]] += 1
            histories[row["history"]["status"]] += 1
            eligible += row["source_qualification"]["status"] == "eligible"
            if row["source_qualification"]["reason_code"]:
                reasons[row["source_qualification"]["reason_code"]] += 1
            observation = row["observation"]
            for n in plan["windows"]:
                item = None if observation is None else next(r for r in observation["ranges"] if r["sessions"] == n)
                windows[str(n)]["source-unavailable" if item is None else item["status"]] += 1
            if code in page_ids:
                rows.append(row)
        known = core["scope_count"] is not None
        scope = {**_summary(core), "source_eligible_count": eligible if known else None,
            "complete_count": status_counts["observed"] if known else None,
            "incomplete_count": core["scope_count"] - status_counts["observed"] if known else None,
            "status_counts": dict(status_counts), "source_reason_counts": dict(reasons),
            "window_status_counts": {n: dict(counts) for n, counts in windows.items()},
            "history_status_counts": dict(histories),
            "denominator_basis": "whole-original-source-scope-before-query-and-pagination"}
        prior = None
        if previous is not None:
            departed = (sorted((set(previous["entities"]) | set(previous["gaps"])) - set(identities))
                        if known and previous["scope_count"] is not None else None)
            prior = {**_summary(previous), "departed_scope_count": None if departed is None else len(departed),
                "departed_scope_display": [] if departed is None else departed[:50],
                "departed_scope_display_count": None if departed is None else min(50, len(departed))}
        complete = known and core["status"] == "readable" and scope["incomplete_count"] == 0
        if previous is not None:
            complete = complete and previous["status"] == "readable" and all(
                key in ("same-session", "consecutive-session") for key in histories)
        identity = {k: v for k, v in plan.items() if k not in ("entity_id", "offset", "limit")}
        full_digest = digest(encoded({"policy": POLICY, "plan": identity, "scope": scope,
                                      "previous": prior, "row_digests": all_digests}))
        end = plan["offset"] + len(rows)
        next_offset = end if end < len(selected) else None
        view = {"schema": SCHEMA, "policy": POLICY, "as_of": plan["as_of"], "kind": plan["kind"],
            "status": "unavailable" if not known else "readable" if complete else "partial",
            "scope": scope, "previous": prior, "rows": rows,
            "page": {"offset": plan["offset"], "limit": plan["limit"], "returned": len(rows),
                "selected_count": len(selected) if known else None, "next_offset": next_offset,
                "entity_id": plan["entity_id"], "order": "entity-id-including-source-failures"},
            "view_sha256": full_digest, "source_requests": 0, "writes": 0,
            "investment_authority": "none", "limitations": LIMITS,
            "clock_semantics": "declared-reading-clock-not-custody-or-first-observation-time"}
        result = {"reference": deepcopy(plan["reference"]), "plan": plan, "status": "readable",
            "view": view, "partial": not complete,
            "open_request": {"operation": "read-source", "payload": deepcopy(plan["reference"])},
            "next_plan": None if next_offset is None else {**deepcopy(plan), "offset": next_offset}}
        require(len(encoded(result)) <= MAX_DISPLAY_BYTES, "STRUCTURE_DISPLAY_BOUND")
        return result


def open_structure(store, value, *, registry=None):
    return CloseStructure(store, registry).read(value)
