"""Stateful market observations over exact existing source and artifact records.

Source adapters own qualification; VersionedStore owns writes/CAS. This module
adds a finite, replayable close-path policy and immutable original event proofs.
It neither acquires sources nor creates a second store or research authority.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from decision_kernel.application.market_custody import (
    authorize_market_custody, market_read_scope, restore_market_reading_inputs,
)
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    RecordError, Selection, SourceRef, StoreUnavailable, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import (
    ArtifactInput, MAX_ARTIFACT_BYTES, artifact_reference,
)
from decision_kernel.observations.market_daily import MarketInputError, _price
from decision_kernel.observations.market_state import (
    ACTIVE, POLICY, event_for, observe_path, transition,
)

PLAN_SCHEMA = "market-discovery-plan-v1"
SCHEMA = "market-discovery-v1"
PREFIX = "market-discovery-"
KINDS = ("sector", "concept", "stock")
MAX_SCOPE = 10000
MAX_SESSIONS = 512
MAX_PROOFS = 64
MAX_EVENTS = 20000
LIMITS = ["EXPLICIT_ABSOLUTE_CLOSE_PATH_POLICY_NOT_KERNEL_TOP_DECILE_OR_TURNOVER_POLICY",
    "NO_EXCESS_RANK_BREADTH_FUNDAMENTAL_OR_INVESTMENT_INFERENCE",
    "BASELINE_HISTORY_IS_REPLAY_NOT_PROSPECTIVE_FIRST_DISCOVERY",
    "DECLARED_SOURCE_SCOPE_AND_MISSING_IDENTITIES_ARE_NOT_FULL_MARKET_COVERAGE",
    "SOURCE_CLOCKS_ARE_NOT_PROVIDER_UPTIME_OR_POINT_IN_TIME_CERTIFICATION"]


def _clock(value):
    require(isinstance(value, str) and 1 <= len(value) <= 64 and "T" in value,
            "DISCOVERY_CLOCK_INVALID")
    try:
        clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError("DISCOVERY_CLOCK_INVALID") from None
    require(clock.utcoffset() is not None, "DISCOVERY_CLOCK_INVALID")
    return clock


def _source(value):
    ref = SourceRef.from_dict(value)
    require(ref.selection == Selection(), "DISCOVERY_WHOLE_SOURCE_REQUIRED")
    return asdict(ref)


def _surface_plan(kind, value):
    keys = {"reference", "adjustment"} if kind == "stock" else {"reference"}
    require(isinstance(value, dict) and set(value) <= keys and "reference" in value,
            "DISCOVERY_SURFACE_PLAN_INVALID")
    result = {"reference": _source(value["reference"])}
    if kind == "stock":
        adjustment = value.get("adjustment", "forward")
        require(adjustment in ("forward", "raw"), "DISCOVERY_ADJUSTMENT_INVALID")
        result["adjustment"] = adjustment
    return result


def plan_input(value):
    """Only exact retained input references, a policy and observation clock."""
    require(isinstance(value, dict) and {"schema", "as_of", "surfaces"} <= set(value)
        <= {"schema", "as_of", "surfaces", "policy", "previous", "stream"}
        and value["schema"] == PLAN_SCHEMA, "DISCOVERY_PLAN_INVALID")
    _clock(value["as_of"])
    require(value.get("policy", POLICY) == POLICY, "DISCOVERY_POLICY_INVALID")
    stream = identifier(value.get("stream", "manual"))
    surfaces = value["surfaces"]
    require(isinstance(surfaces, dict) and surfaces and set(surfaces) <= set(KINDS),
            "DISCOVERY_SURFACES_INVALID")
    previous = value.get("previous")
    if previous is not None:
        previous = asdict(artifact_reference(previous))
        require(previous["path"].startswith("research/artifacts/" + PREFIX),
                "DISCOVERY_PREVIOUS_KIND")
    result = {"schema": PLAN_SCHEMA, "policy": POLICY, "stream": stream, "as_of": value["as_of"],
        "surfaces": {kind: _surface_plan(kind, surfaces[kind]) for kind in KINDS if kind in surfaces},
        "previous": previous}
    require(len(encoded(result)) <= 16384, "DISCOVERY_PLAN_BOUND")
    return result


def _reason(exc):
    value = str(exc)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else "DISCOVERY_SOURCE_UNAVAILABLE"


def _number(value):
    number = _price(value)
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _entity(*, code, name, rows, sessions, reference, contract, source_version,
            market, currency, adjustment, reason=None):
    require(isinstance(sessions, list) and 0 < len(sessions) <= MAX_SESSIONS,
            "DISCOVERY_SESSION_BOUND")
    as_of = sessions[-1]
    try:
        observation = observe_path(entity_id=code, series=rows, market_sessions=sessions,
            as_of=as_of, calendar_version=digest(encoded(sessions)), source_version=source_version,
            market=market, currency=currency, adjustment=adjustment)
    except (RecordError, MarketInputError) as exc:
        reason = reason or _reason(exc)
        observation = observe_path(entity_id=code, series=[], market_sessions=sessions,
            as_of=as_of, calendar_version=digest(encoded(sessions)), source_version=source_version,
            market=market, currency=currency, adjustment=adjustment)
        rows = []
    if reason is not None:
        observation.update(state="unknown", reason_code=reason)
    return {"entity_id": code, "name": name, "as_of": as_of, "contract": contract,
        "sessions": sessions, "prices": {r["trade_date"]: _number(r["close"]) for r in rows
            if sessions[0] <= r["trade_date"] <= as_of},
        "source_reference": reference, "observation": observation}


def _ft_inputs(plan, as_of, registry, store):
    from decision_kernel.application.ftshare_adoption import ADOPTION_PLAN_SCHEMA
    from decision_kernel.application.ftshare_collection import (
        SINGLE_PLAN_SCHEMA, _capture_completion, restore_ftshare_batch, restore_ftshare_collection,
    )
    from decision_kernel.application.ftshare_single import restore_single
    from decision_kernel.application.market_completion import completed_close_clock
    from decision_kernel.application.market_security_inputs import _within_listing

    require(SourceRef.from_dict(plan["reference"]).manifest.path.startswith(
        "source-views/ftshare-collection-"), "DISCOVERY_FTSHARE_ROOT_REQUIRED")
    restored = restore_ftshare_collection(plan["reference"], registry, store)
    collection, context = restored["collection"], restored["context"]
    original = collection["plan"]
    master = {code: row for venue in context["masters"].values()
              for code, row in venue["securities"].items()}
    for family in ("master_results", "calendar_results"):
        for result in context[family].values():
            for page in result["capture"]["pages"]:
                require(_clock(page["captured_at"]) <= _clock(as_of),
                        "DISCOVERY_CONTEXT_AFTER_OBSERVATION")
    require(set(master) == set(context["scope_symbols"]) and len(master) <= MAX_SCOPE,
            "DISCOVERY_SCOPE_INVALID")
    universe = digest(encoded(master))
    adjustment = "forward" if plan["adjustment"] == "forward" else "none"
    convention = "qfq" if adjustment == "forward" else "raw"
    single = original["schema"] in (ADOPTION_PLAN_SCHEMA, SINGLE_PLAN_SCHEMA)
    loader = restore_single if single else restore_ftshare_batch
    inputs, gaps, sources, requested = {}, {}, {}, set()
    hold = collection.get("source_hold")
    for slot, request in sorted(original["requests"].items()):
        params = request["parameters"]
        if params["adjust_kind"] != adjustment:
            continue
        codes = [params["symbol"]] if single else params["symbols"]
        require(not requested.intersection(codes) and set(codes) <= set(master),
                "DISCOVERY_DUPLICATE_SERIES")
        requested.update(codes)
        unit = collection["units"][slot]
        capture, reason = None, "DISCOVERY_SOURCE_HOLD" if hold else None
        if unit["status"] != "complete":
            reason = reason or unit.get("reason_code") or "DISCOVERY_PRICE_NOT_RETAINED"
        else:
            try:
                capture = loader(unit["reference"], store, request=request)
                _capture_completion(capture, original.get("completion_policy"))
                require(_clock(capture["acquired_at"]) <= _clock(as_of),
                        "DISCOVERY_SOURCE_AFTER_OBSERVATION")
            except StoreUnavailable:
                raise
            except (RecordError, MarketInputError) as exc:
                reason = reason or _reason(exc)
                capture = None
        sources[slot] = {"reference": deepcopy(unit.get("reference")),
            "status": "restored" if capture is not None else "unavailable",
            "reason_code": reason, "request": deepcopy(request),
            "wire_sha256": None if capture is None else capture["wire_sha256"],
            "acquired_at": None if capture is None else capture["acquired_at"]}
        for code in codes:
            row = master[code]
            venue = "SSE" if code.endswith(".SH") else "SZSE"
            sessions = context["sessions"][venue]
            if not sessions:
                gaps[code] = "DISCOVERY_NO_COMPLETED_SESSION"
                continue
            local_reason = reason
            if row is None:
                local_reason = "DISCOVERY_REFERENCE_QUARANTINED"
            elif not _within_listing(sessions[-1], row):
                local_reason = "DISCOVERY_OUTSIDE_LISTING"
            try:
                completed_close_clock(as_of, sessions[-1], original.get("completion_policy"))
            except RecordError as exc:
                local_reason = local_reason or _reason(exc)
            prices = [] if capture is None or row is None else [
                {"trade_date": value["trade_date"], "close": value["close"]}
                for value in capture["normalized"]["series"].get(code, [])
                if _within_listing(value["trade_date"], row)]
            inputs[code] = _entity(code=code, name=code if row is None else row["name"], rows=prices,
                sessions=sessions, reference=deepcopy(unit.get("reference")),
                contract=digest(encoded({"policy": POLICY, "kind": "stock", "universe": universe,
                    "convention": convention, "market": venue, "currency": "CNY"})),
                source_version=digest(encoded(sources[slot])), market=venue, currency="CNY",
                adjustment=convention, reason=local_reason)
    for code, row in sorted(master.items()):
        if code not in inputs:
            gaps.setdefault(code, "DISCOVERY_REFERENCE_QUARANTINED" if row is None else "DISCOVERY_NOT_REQUESTED")
    cutoffs = {venue: days[-1] if days else None for venue, days in context["sessions"].items()}
    return {"kind": "stock", "source_reference": plan["reference"], "scope_count": len(master),
        "universe_version": universe, "as_of": next(iter(set(cutoffs.values())))
            if len(set(cutoffs.values())) == 1 else None,
        "as_of_by_exchange": cutoffs, "entities": inputs, "gaps": gaps,
        "sources": sources, "source_status": "source-hold" if hold else "retained-partial-scope",
        "scope_semantics": "all-source-master-identities-including-quarantine",
        "adjustment": plan["adjustment"], "source_hold": deepcopy(hold)}


def _group_inputs(kind, plan, as_of, registry, store):
    from decision_kernel.application.market_group_inputs import (
        _levels, inspect_ths_catalog, qualified_ths_members,
    )
    from decision_kernel.application.market_relay_inputs import (
        _calendar, _lineage, _range, _view,
    )
    from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY, completed_close_clock

    restored = restore_market_reading_inputs(plan["reference"], registry, store)
    payload = restored["payload"]
    require(payload.get("schema") == "relay-ths-group-reading-v1" and payload.get("kind") == kind,
            "DISCOVERY_THS_ROOT_REQUIRED")
    catalog = inspect_ths_catalog(payload["catalog_result"], registry=registry, kind=kind)
    groups = catalog["groups"]
    require(len(groups) <= MAX_SCOPE, "DISCOVERY_SCOPE_INVALID")
    identities = {catalog["namespace"] + ":" + code: row for code, row in groups.items()}
    # The observation policy does not depend on reported member counts; names,
    # actual code/type/list date still belong to the comparison contract.
    universe = digest(encoded({code: None if row is None else {k: v for k, v in row.items()
        if k != "reported_member_count"} for code, row in identities.items()}))
    lo, hi = _range(payload["start"], payload["end"])
    calendar = _view(payload["calendar_result"], registry, "trade_cal",
        {"exchange": payload["exchange"], "start_date": payload["start"], "end_date": payload["end"]},
        {"exchange", "cal_date", "is_open"})
    for lineage in (catalog["lineage"], _lineage(calendar)):
        for page in lineage["pages"]:
            require(_clock(page["captured_at"]) <= _clock(as_of),
                    "DISCOVERY_CONTEXT_AFTER_OBSERVATION")
    sessions = _calendar(calendar, payload["exchange"], lo, hi)
    require(sessions and sessions[-1] == hi, "DISCOVERY_END_NOT_MARKET_SESSION")
    completed_close_clock(as_of, hi, EOD_COMPLETION_POLICY)
    require(isinstance(payload["index_results"], dict) and set(payload["index_results"]) <= set(groups)
        and isinstance(payload["member_results"], dict) and set(payload["member_results"]) <= set(groups),
        "DISCOVERY_UNDECLARED_GROUP")
    inputs, gaps, sources, members = {}, {}, {}, {}
    for code, row in sorted(groups.items()):
        key = catalog["namespace"] + ":" + code
        if code not in payload["index_results"]:
            gaps[key] = "DISCOVERY_REFERENCE_QUARANTINED" if row is None else "DISCOVERY_NOT_REQUESTED"
            continue
        prices, reason, lineage = [], None, None
        if row is None:
            reason = "DISCOVERY_REFERENCE_QUARANTINED"
        else:
            try:
                prices, lineage = _levels(payload["index_results"][code], registry, code,
                    payload["start"], payload["end"], set(sessions))
                for page in lineage["pages"]:
                    require(_clock(page["captured_at"]) <= _clock(as_of),
                            "DISCOVERY_SOURCE_AFTER_OBSERVATION")
                    completed_close_clock(page["captured_at"], hi, EOD_COMPLETION_POLICY)
            except (RecordError, MarketInputError) as exc:
                reason = _reason(exc)
        # Original query refs, not the displayed index code, identify evidence.
        source = next((item["reference"] for item in restored["sources"]
            if item["path"] == ["index_results", code]), None)
        sources[key] = {"reference": source, "lineage": lineage,
            "status": "restored" if reason is None else "unavailable", "reason_code": reason}
        inputs[key] = _entity(code=key, name=key if row is None else row["name"], rows=prices,
            sessions=sessions, reference=source,
            contract=digest(encoded({"policy": POLICY, "kind": kind, "universe": universe,
                "market": "THS-A", "calendar_exchange": payload["exchange"], "convention": "raw-index-points"})),
            source_version=digest(encoded(sources[key])), market="THS-A", currency="XXX",
            adjustment="raw", reason=reason)
    # Members are explanatory literal relationships, never admission conditions.
    for code, result in sorted(payload["member_results"].items()):
        key = catalog["namespace"] + ":" + code
        try:
            member = qualified_ths_members(result, registry=registry, ts_code=code)
            members[key] = {"status": "checked", "members": member["members"],
                "effective_at": None, "semantics": "reported-ids-not-historical-or-economic-exposure"}
        except (RecordError, MarketInputError) as exc:
            members[key] = {"status": "unavailable", "reason_code": _reason(exc)}
    return {"kind": kind, "source_reference": plan["reference"], "scope_count": len(groups),
        "universe_version": universe, "as_of": hi, "entities": inputs, "gaps": gaps,
        "sources": sources, "source_status": "retained-classification-slice",
        "scope_semantics": "all-ths-a-" + kind + "-catalogue-identities-including-quarantine",
        "members": members, "benchmark_policy": "absolute-policy-no-benchmark-comparison",
        "adjustment": "raw-index-points", "source_hold": None}


def _proof_descriptor(kind, plan, as_of):
    return {"kind": kind, "plan": deepcopy(plan), "as_of": as_of}


def artifact_id(result):
    """A stable artifact family; source revisions remain content-addressed versions."""
    identity = {"policy": POLICY, "stream": result["plan"]["stream"],
        "surfaces": {kind: value.get("adjustment", "raw-index-points")
        for kind, value in result["plan"]["surfaces"].items()}}
    return PREFIX + digest(encoded(identity))[:24]


def _sealed(value):
    return {**value, "view_sha256": digest(encoded(value))}


class MarketDiscovery:
    """One explicit build/save/read, using the caller's original bound store."""

    def __init__(self, store, registry=None, *, source_store=None):
        require(isinstance(store, VersionedStore), "DISCOVERY_STORE_REQUIRED")
        self.store = store
        from decision_kernel.infrastructure.github_store import GitHubStore
        from decision_kernel.infrastructure.market_snapshot import GitMarketReader

        # GitMarketReader intentionally cannot read research/artifacts. Keep its
        # source-only boundary; artifact reads use the original native binding
        # and the SAME operation API budget, never a second credential/client.
        if isinstance(store.backend, GitMarketReader):
            self.store = VersionedStore(GitHubStore(store.backend.binding, store.backend.api))
            source_store = store if source_store is None else source_store
        elif source_store is None and isinstance(store.backend, GitHubStore):
            source_store = VersionedStore(GitMarketReader(
                replace(store.backend.binding, write_enabled=False), store.backend.api))
        self.source_store = self.store if source_store is None else source_store
        require(isinstance(self.source_store, VersionedStore)
            and self.source_store.backend.repository == self.store.backend.repository,
            "DISCOVERY_SOURCE_STORE_CHANGED")
        api = getattr(self.store.backend, "api", None)
        if api is not None:
            require(getattr(self.source_store.backend, "api", None) is api,
                    "DISCOVERY_SHARED_API_BUDGET_REQUIRED")
        self.registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
        require(isinstance(self.registry, RelayRegistry), "DISCOVERY_REGISTRY_REQUIRED")
        self._cores = {}

    def _core(self, kind, plan, as_of):
        descriptor = _proof_descriptor(kind, plan, as_of)
        key = digest(encoded(descriptor))
        if key not in self._cores:
            require(len(self._cores) < MAX_PROOFS, "DISCOVERY_PROOF_BOUND")
            require(SourceRef.from_dict(plan["reference"]).manifest.repository == self.store.backend.repository,
                    "DISCOVERY_FOREIGN_SOURCE")
            try:
                value = (_ft_inputs(plan, as_of, self.registry, self.source_store) if kind == "stock"
                         else _group_inputs(kind, plan, as_of, self.registry, self.source_store))
                value["status"] = "readable"
            except StoreUnavailable:
                raise
            except (RecordError, MarketInputError) as exc:
                value = {"kind": kind, "source_reference": plan["reference"],
                    "status": "unavailable", "reason_code": _reason(exc), "as_of": None,
                    "scope_count": None, "universe_version": None, "entities": {}, "gaps": {},
                    "source_status": "unavailable", "sources": {}}
            self._cores[key] = value
        return key, self._cores[key]

    def _proof(self, kind, plan, as_of):
        key, core = self._core(kind, plan, as_of)
        return key, {**_proof_descriptor(kind, plan, as_of), "view_sha256": digest(encoded(core))}, core

    def _decode(self, reference):
        ref = artifact_reference(reference)
        require(ref.repository == self.store.backend.repository
            and ref.path.startswith("research/artifacts/" + PREFIX), "DISCOVERY_ARTIFACT_REFERENCE")
        restored = ResearchArtifacts(self.store).read(ref)
        meta = restored["metadata"]
        require(meta["media_type"] == "application/json" and meta["name"] == "market-discovery.json",
                "DISCOVERY_ARTIFACT_KIND")
        raw = base64.b64decode(restored["data_base64"], validate=True)
        try:
            value = _json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise RecordError("DISCOVERY_DOCUMENT_INVALID") from None
        require(isinstance(value, dict) and encoded(value) == raw, "DISCOVERY_DOCUMENT_CANONICAL")
        fields = {"schema", "policy", "as_of", "plan", "surfaces", "proofs", "events",
            "source_requests", "investment_authority", "limitations", "clock_semantics", "view_sha256"}
        require(set(value) == fields and value["schema"] == SCHEMA and value["policy"] == POLICY
            and value["source_requests"] == 0 and type(value["source_requests"]) is int
            and value["investment_authority"] == "none" and value["limitations"] == LIMITS,
            "DISCOVERY_DOCUMENT_INVALID")
        require(value["view_sha256"] == digest(encoded({k: v for k, v in value.items() if k != "view_sha256"})),
                "DISCOVERY_DOCUMENT_DIGEST")
        require(encoded(plan_input(value["plan"])) == encoded(value["plan"])
            and value["as_of"] == value["plan"]["as_of"]
            and meta["artifact_id"] == artifact_id(value), "DISCOVERY_DOCUMENT_PLAN")
        require(isinstance(value["surfaces"], dict) and set(value["surfaces"]) == set(KINDS),
                "DISCOVERY_DOCUMENT_SURFACES")
        return value

    def _verify_proofs(self, document):
        proofs, events = document["proofs"], document["events"]
        require(isinstance(proofs, dict) and len(proofs) <= MAX_PROOFS
            and isinstance(events, dict) and len(events) <= MAX_EVENTS, "DISCOVERY_PROOF_BOUND")
        cores = {}
        for key, proof in proofs.items():
            require(isinstance(proof, dict) and set(proof) == {"kind", "plan", "as_of", "view_sha256"}
                and proof["kind"] in KINDS, "DISCOVERY_PROOF_INVALID")
            kind, plan, at = proof["kind"], proof["plan"], proof["as_of"]
            require(encoded(_surface_plan(kind, plan)) == encoded(plan), "DISCOVERY_PROOF_PLAN")
            require(_clock(at) <= _clock(document["as_of"]), "DISCOVERY_FUTURE_PROOF")
            actual_key, actual = self._core(kind, plan, at)
            require(key == actual_key and proof["view_sha256"] == digest(encoded(actual)),
                    "DISCOVERY_PROOF_REPLAY_MISMATCH")
            cores[key] = actual
        for key, event in events.items():
            require(isinstance(event, dict) and isinstance(event.get("current_proof"), str)
                and event["current_proof"] in proofs
                and (event.get("previous_proof") is None or
                     isinstance(event["previous_proof"], str) and event["previous_proof"] in proofs),
                "DISCOVERY_EVENT_PROOF_REQUIRED")
            now, before = event["current_proof"], event["previous_proof"]
            code, kind = event.get("entity_id"), event.get("kind")
            require(isinstance(code, str) and kind in KINDS and proofs[now]["kind"] == kind
                and (before is None or proofs[before]["kind"] == kind)
                and code in cores[now]["entities"], "DISCOVERY_EVENT_ENTITY")
            previous = None if before is None else cores[before]["entities"].get(code)
            expected = event_for(kind=kind, entity_id=code, current=cores[now]["entities"][code],
                previous=previous, current_proof=now, previous_proof=before)
            require(expected is not None and expected == event and key == expected["event_id"],
                    "DISCOVERY_EVENT_REPLAY_MISMATCH")
        return cores

    def _verify_materialized(self, document):
        """Validate prior state and standalone original proofs without chain recursion.

        Earlier original events are assertions only after their exact source pair
        recomputes them. Previous display prose cannot supply a state or price.
        Full replay of the current document additionally checks its direct edge.
        """
        cores = self._verify_proofs(document)
        for kind, surface in document["surfaces"].items():
            require(isinstance(surface, dict) and isinstance(surface.get("states"), list),
                    "DISCOVERY_STATES_INVALID")
            if kind not in document["plan"]["surfaces"]:
                require(surface["status"] == "not-supplied" and surface["states"] == [],
                        "DISCOVERY_UNSUPPLIED_STATES")
                continue
            key, core = self._core(kind, document["plan"]["surfaces"][kind], document["as_of"])
            require(key in cores and surface.get("proof") == key
                and surface.get("scope_count") == core["scope_count"]
                and surface.get("universe_version") == core["universe_version"],
                "DISCOVERY_SURFACE_REPLAY_MISMATCH")
            seen = set()
            for row in surface["states"]:
                require(isinstance(row, dict), "DISCOVERY_STATE_INVALID")
                code = row.get("entity_id")
                require(isinstance(code, str) and code not in seen, "DISCOVERY_DUPLICATE_STATE")
                seen.add(code)
                original = core["entities"].get(code)
                if original is None:
                    require(code in core["gaps"] and row.get("state") == "unknown"
                        and row.get("metrics") is None, "DISCOVERY_STATE_WITHOUT_SOURCE")
                else:
                    observation = original["observation"]
                    require(row.get("metrics") == observation["metrics"]
                        and row.get("name") == original["name"]
                        and row.get("as_of") == original["as_of"]
                        and row.get("reason_code") == observation["reason_code"],
                        "DISCOVERY_STATE_REPLAY_MISMATCH")
                    require(row.get("state") == observation["state"] or
                        row.get("state") == "exited" and observation["state"] == "not-entered",
                        "DISCOVERY_STATE_REPLAY_MISMATCH")
                origin, last = row.get("origin_event"), row.get("last_event")
                require((origin is None) == (last is None)
                    and (row.get("state") == "unknown" or origin is not None)
                    and (original is not None or origin is not None),
                    "DISCOVERY_STATE_EVENT_REQUIRED")
                for field in ("origin_event", "last_event"):
                    event_id = row.get(field)
                    if event_id is None:
                        continue
                    require(isinstance(event_id, str), "DISCOVERY_STATE_EVENT_MISMATCH")
                    event = document["events"].get(event_id)
                    require(event is not None and event["entity_id"] == code and event["kind"] == kind
                        and (row.get("as_of") is None or event["at"] <= row["as_of"]),
                        "DISCOVERY_STATE_EVENT_MISMATCH")
                    if field == "origin_event":
                        require(event["type"] in ("baseline", "entry"), "DISCOVERY_ORIGIN_EVENT_TYPE")
                if origin is not None:
                    require(document["events"][origin]["at"] <= document["events"][last]["at"],
                            "DISCOVERY_EVENT_CLOCK_REVERSED")
                if row.get("state") == "exited":
                    event = document["events"].get(row.get("last_event"), {})
                    event_core = cores.get(event.get("current_proof"), {}).get("entities", {}).get(code)
                    require(event.get("type") == "exit" and original is not None and event_core is not None
                        and transition(original, event_core)["comparison"] == "same-session",
                            "DISCOVERY_EXIT_PROOF_REQUIRED")
            require(set(core["entities"]) <= seen, "DISCOVERY_STATES_DROPPED")
        return cores

    def _compose(self, plan, previous):
        proofs = {} if previous is None else deepcopy(previous["proofs"])
        events = {} if previous is None else deepcopy(previous["events"])
        surfaces = {}
        for kind in KINDS:
            if kind not in plan["surfaces"]:
                surfaces[kind] = {"status": "not-supplied", "scope_count": None,
                    "qualified_count": None, "unknown_count": None, "as_of": None,
                    "states": [], "gaps": [], "source_reference": None, "universe_version": None}
                continue
            key, proof, core = self._proof(kind, plan["surfaces"][kind], plan["as_of"])
            proofs[key] = proof
            prior_surface = None if previous is None else previous["surfaces"][kind]
            prior_plan = None if previous is None else previous["plan"]["surfaces"].get(kind)
            prior_key, prior_core = (None, None) if prior_plan is None else self._core(
                kind, prior_plan, previous["as_of"])
            before_rows = {} if prior_surface is None else {
                row["entity_id"]: row for row in prior_surface["states"]}
            states = []
            for code, current in sorted(core["entities"].items()):
                before = None if prior_core is None else prior_core["entities"].get(code)
                changed = transition(current, before)
                earlier = before_rows.get(code, {})
                origin, last = earlier.get("origin_event"), earlier.get("last_event")
                if changed["comparison"] == "same-session" and earlier.get("state") == "exited":
                    changed["state"] = "exited"
                event = event_for(kind=kind, entity_id=code, current=current, previous=before,
                    current_proof=key, previous_proof=prior_key)
                if event is not None:
                    events[event["event_id"]] = event
                    last = event["event_id"]
                    if event["type"] in ("baseline", "entry"):
                        origin = event["event_id"]
                row = {"entity_id": code, "name": current["name"], "as_of": current["as_of"],
                    "state": changed["state"], "transition": changed["transition"],
                    "comparison": changed["comparison"],
                    "reason_code": current["observation"]["reason_code"],
                    "metrics": deepcopy(current["observation"]["metrics"]),
                    "origin_event": origin, "last_event": last,
                    "source_reference": deepcopy(current["source_reference"])}
                states.append(row)
            gaps = deepcopy(core["gaps"])
            # A previously observed security becoming unrequested keeps its old
            # event, while the present state is unknown rather than an exit.
            for code in sorted(set(gaps) & set(before_rows)):
                before = before_rows[code]
                if before.get("origin_event") is None and before.get("last_event") is None:
                    continue
                states.append({"entity_id": code, "name": before["name"], "as_of": core["as_of"],
                    "state": "unknown", "transition": "unknown", "comparison": "input-not-requested",
                    "reason_code": gaps.pop(code), "metrics": None,
                    "origin_event": before.get("origin_event"), "last_event": before.get("last_event"),
                    "source_reference": None})
            states.sort(key=lambda row: row["entity_id"])
            qualified = sum(row["state"] != "unknown" for row in states)
            surface = {k: deepcopy(v) for k, v in core.items() if k not in ("entities", "gaps")}
            surface.update(proof=key, states=states,
                gaps=[{"entity_id": code, "reason_code": reason} for code, reason in sorted(gaps.items())],
                qualified_count=qualified if core["scope_count"] is not None else None,
                unknown_count=core["scope_count"] - qualified if core["scope_count"] is not None else None,
                active_count=sum(row["state"] in ACTIVE for row in states),
                changed_count=sum(row["transition"] in ("entered", "acceleration-entered", "weakened", "exited")
                                  for row in states),
                departed_scope=sorted(set(before_rows) - set(core["entities"]) - set(core["gaps"]))
                    if core["scope_count"] is not None else None)
            surfaces[kind] = surface
        # Original entry and most recent event remain independently replayable.
        # Older snapshots still hold superseded events; no history is overwritten.
        used_events = {row[field] for surface in surfaces.values() for row in surface["states"]
                       for field in ("origin_event", "last_event") if row[field] is not None}
        events = {key: events[key] for key in sorted(used_events)}
        used_proofs = {surface["proof"] for surface in surfaces.values() if "proof" in surface}
        used_proofs.update(event[field] for event in events.values()
                           for field in ("current_proof", "previous_proof") if event[field] is not None)
        proofs = {key: proofs[key] for key in sorted(used_proofs)}
        require(len(proofs) <= MAX_PROOFS and len(events) <= MAX_EVENTS, "DISCOVERY_PROOF_BOUND")
        output = _sealed({"schema": SCHEMA, "policy": POLICY, "as_of": plan["as_of"],
            "plan": plan, "surfaces": surfaces, "proofs": proofs, "events": events,
            "source_requests": 0, "investment_authority": "none", "limitations": LIMITS,
            "clock_semantics": "declared-observation-clock-not-native-save-time"})
        require(len(encoded(output)) <= MAX_ARTIFACT_BYTES, "DISCOVERY_ARTIFACT_BOUND")
        return _json(encoded(output))

    def _previous(self, plan):
        previous = None if plan["previous"] is None else self._decode(plan["previous"])
        if previous is not None:
            require(previous["plan"]["stream"] == plan["stream"], "DISCOVERY_PREVIOUS_STREAM_CHANGED")
            require(_clock(previous["as_of"]) <= _clock(plan["as_of"]), "DISCOVERY_CLOCK_REVERSED")
            self._verify_materialized(previous)
        return previous

    def _build(self, plan):
        previous = self._previous(plan)
        if previous is not None:
            # Check the predecessor's own direct edge before adopting any of
            # its carried event links. This deliberately loads only one more
            # Artifact, rather than recursively walking every daily ancestor.
            prior = self._previous(previous["plan"])
            require(previous == self._compose(previous["plan"], prior),
                    "DISCOVERY_PREVIOUS_REPLAY_MISMATCH")
        return self._compose(plan, previous)

    def build(self, plan):
        """Read-only state calculation; no source acquisition or hidden save."""
        checked = plan_input(plan)
        self._cores = {}
        with market_read_scope(self.source_store) as reader:
            service = MarketDiscovery(self.store, self.registry, source_store=reader)
            return service._build(checked)

    def save(self, submission_id, plan, *, before_new_write=None):
        """Save through the existing artifact journal, then a fresh source replay."""
        result = self.build(plan)
        visibility = authorize_market_custody(self.store, custody_authorized=True)
        require(visibility in ("private", "synthetic"), "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
        artifact = ArtifactInput.from_bytes(artifact_id(result), "market-discovery.json",
                                           "application/json", encoded(result))
        ref = ResearchArtifacts(self.store).put(submission_id, artifact,
                                                before_new_write=before_new_write)
        reopened = MarketDiscovery(VersionedStore(self.store.backend), self.registry,
            source_store=self.source_store).read(ref)
        require(reopened["view_sha256"] == result["view_sha256"], "DISCOVERY_SAVE_READBACK_MISMATCH")
        return {"status": "stored-and-verified", "reference": asdict(ref), "result": result,
            "stored_at": None, "verification": "recomputed-from-retained-sources"}

    def read(self, reference):
        """Exact artifact bytes, original event proofs and the direct state edge."""
        self._cores = {}
        with market_read_scope(self.source_store) as reader:
            service = MarketDiscovery(self.store, self.registry, source_store=reader)
            document = service._decode(reference)
            service._verify_materialized(document)
            expected = service._build(plan_input(document["plan"]))
            require(document == expected, "DISCOVERY_REPLAY_MISMATCH")
            return {**document, "reference": asdict(artifact_reference(reference)),
                "stored_at": None, "verification": "recomputed-from-retained-sources"}
