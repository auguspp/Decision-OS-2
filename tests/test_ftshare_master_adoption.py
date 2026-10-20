"""Exact current-L adoption over original FTShare custody; all sources synthetic."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from decision_kernel.application.ftshare_collection import (
    begin_ftshare_slot, resolve_ftshare_expansion_frontier, restore_ftshare_collection,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_custody import market_read_scope, retain_market_query
from decision_kernel.application.market_security_inputs import MASTER_FIELDS
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.github_store import MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces import ftshare_collection_cli as cli
from test_ftshare_collection import (
    _new_calendars, _retain, adopt_kernel, kernel_adoption_fixture, plan_single, settle_single,
)
from test_ftshare_collection_runtime import SingleHost, SingleProvider, single_execute
from test_market_collection_inputs import CollectionOpener
from test_market_relay_inputs import REGISTRY
from test_market_session_coverage import capture as relay_capture
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport

STAMP = "2026-10-19T12:02:10+00:00"
CREATED = "2026-10-19T12:03:00+00:00"


@pytest.fixture
def adopted():
    fixture = kernel_adoption_fixture()
    fixture["adopted"] = adopt_kernel(fixture)
    return fixture


def new_masters(fixture, *, stamp=STAMP, edit=None, extra=None, pages=20):
    """Retain every original page through the production query custody owner."""
    opener = CollectionOpener(count=4)
    for venue in ("SSE", "SZSE"):
        for row in opener.rows["stock_basic", venue]:
            row["name"] = "CURRENT_SOURCE_NAME"
        opener.rows["stock_basic", venue][-1]["name"] = ""
    if edit is not None:
        edit(opener.rows)
    if extra is not None:
        for venue in ("SSE", "SZSE"):
            if "ts_code" in extra:
                opener.rows["stock_basic", venue] = [row for row in opener.rows["stock_basic", venue]
                    if row["ts_code"] == extra["ts_code"]]
            if "list_status" in extra:
                for row in opener.rows["stock_basic", venue]:
                    row.update(list_status=extra["list_status"], delist_date="20261001")
    references, results = {}, {}
    for venue in ("SSE", "SZSE"):
        result = relay_capture(opener, "stock_basic", {"exchange": venue, "list_status": "L",
            **(extra or {})}, MASTER_FIELDS, limit=2, pages=pages)
        for number, page in enumerate(result["capture"]["pages"]):
            page["captured_at"] = (datetime.fromisoformat(stamp) + timedelta(seconds=number)).isoformat()
        retained = retain_market_query(result, REGISTRY, fixture["store"], fresh_reader=fixture["fresh"],
            custody_authorized=True)
        references[venue] = json.loads(encoded(retained["reference"]))
        results[venue] = result
    return references, results


def master_plan(fixture, references, **options):
    return plan_single(fixture, created_at=CREATED,
        targets=[{"symbol": "600002.SH", "adjust_kind": "none"}], master_references=references, **options)


def test_native_new_masters_rebuild_scope_preserve_legacy_and_read_two_generations_with_bounded_api(native):
    model, store = native
    fresh = lambda: VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False),
        transport(model), fetch=local_fetch(model, [])))
    f = kernel_adoption_fixture(storage=(model.git, store, fresh))
    f["adopted"] = adopt_kernel(f)
    original = restore_ftshare_collection(f["adopted"]["reference"], REGISTRY, fresh())
    original_bytes = encoded(original["collection"])
    refs, results = new_masters(f)
    collection = master_plan(f, refs)
    assert collection["plan"]["master_references"] == refs
    assert collection["plan"]["legacy_reference"] == f["legacy_reference"]
    assert len(collection["plan"]["symbols"]) == 6
    assert collection["plan"]["eligible_symbols"] == ["000001.SZ", "600000.SH", "600001.SH", "600002.SH"]
    assert collection["plan"]["quarantined_symbols"] == ["300001.SZ", "600003.SH"]
    assert collection["reserved_requests"] == 0 and collection["plan"]["source_budget"] == 1
    for slot, request in original["collection"]["plan"]["requests"].items():
        params = request["parameters"]
        owner = params["adjust_kind"] + ":" + params["symbol"]
        assert collection["plan"]["requests"][owner] == request
        assert collection["units"][owner] == {**original["collection"]["units"][slot], "request_count": 0}
    saved = _retain(collection, store, fresh)
    collection, saved = settle_single(f, collection, saved)
    successor = plan_single(f, base=saved["reference"], generation="g20261016-second",
        created_at=CREATED, targets=[{"symbol": "600001.SH", "adjust_kind": "none"}])
    assert successor["plan"]["master_references"] == refs
    second = _retain(successor, store, fresh)
    successor, second = settle_single(f, successor, second)

    api, fetches = transport(model), []
    first_request = len(model.requests)
    reader = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    before = model.git.head()
    with market_read_scope(reader) as grouped:
        reading = read_ftshare_collection_version(second["reference"], REGISTRY, grouped, read_as_of=CREATED)
        rebuilt = restore_ftshare_collection(second["reference"], REGISTRY, grouped)
        historical = restore_ftshare_collection(f["adopted"]["reference"], REGISTRY, grouped)
        legacy = restore_market_collection(f["legacy_reference"], REGISTRY, grouped)
    assert reading["source_requests"] == reading["unavailable_queries"] == 0
    assert reading["view"]["scope_count"] == 6 and reading["view"]["freshness"]["status"] == "unknown"
    assert reading["view"]["source_lineage"]["masters"] == refs
    assert reading["view"]["master"]["SSE"]["quality"] == {
        "source_row_count": 4, "scope_count": 4, "eligible_count": 3,
        "quarantined_count": 1, "quarantined_row_count": 1, "duplicate_group_count": 0}
    for venue in ("SSE", "SZSE"):
        expected = [page["captured_at"] for page in results[venue]["capture"]["pages"]]
        assert reading["view"]["master"][venue]["capture_times"] == {"L": expected}
        assert encoded(inspect_result(rebuilt["context"]["master_results"][venue], REGISTRY)) == encoded(
            inspect_result(results[venue], REGISTRY))
    assert rebuilt["context"]["masters"]["SSE"]["securities"]["600002.SH"]["name"] == "CURRENT_SOURCE_NAME"
    assert encoded(historical["collection"]) == original_bytes
    assert historical["context"]["master_references"] == original["context"]["master_references"]
    assert legacy["collection"] == f["old"]
    assert model.git.head() == before
    # Native Git verifies old and new query pages locally in one existing scope.
    # Keep the original hard admission and per-unit bounds; no new API allowance.
    assert 0 < api._requests == len(model.requests) - first_request < 240 < MAX_REQUESTS == 512
    assert len(fetches) == 1 and not fetches[0]["destination"].exists()
    assert all(method == "GET" for method, _, _ in model.requests)


@pytest.mark.parametrize("change", ["missing", "quarantined", "duplicate"])
def test_every_prior_owner_must_remain_eligible_before_any_new_plan(adopted, change):
    def edit(rows):
        entries = rows["stock_basic", "SSE"]
        if change == "missing":
            entries.pop(0)
        elif change == "quarantined":
            entries[0]["name"] = ""
        else:
            entries.append(deepcopy(entries[0]))
    refs, _ = new_masters(adopted, edit=edit)
    before = adopted["backend"].head()
    with pytest.raises(RecordError, match="FTSHARE_SINGLE_MASTER_OWNER_UNQUALIFIED"):
        master_plan(adopted, refs)
    assert adopted["backend"].head() == before
    assert restore_ftshare_collection(adopted["adopted"]["reference"], REGISTRY,
        adopted["fresh"]())["collection"]["reserved_requests"] == 0


def test_failed_owner_survives_refresh_and_cannot_be_removed_even_if_targeted(adopted):
    collection = plan_single(adopted)
    saved = _retain(collection, adopted["store"], adopted["fresh"])
    collection, saved = settle_single(adopted, collection, saved, reason_code="FTSHARE_HTTP_UNAVAILABLE")
    refs, _ = new_masters(adopted)
    next_plan = master_plan(adopted, refs, base=saved["reference"], generation="g20261016-next")
    assert next_plan["units"]["forward:600000.SH"] == {
        "status": "failed", "reason_code": "FTSHARE_HTTP_UNAVAILABLE", "request_count": 0, "reference": None}
    def quarantine(rows):
        rows["stock_basic", "SSE"][0]["name"] = ""
    refused, _ = new_masters(adopted, stamp="2026-10-19T12:02:20+00:00", edit=quarantine)
    calendars = _new_calendars(adopted["store"], adopted["fresh"])
    with pytest.raises(RecordError, match="FTSHARE_SINGLE_MASTER_OWNER_UNQUALIFIED"):
        plan_single(adopted, base=saved["reference"], generation="g20261016-replaced",
            created_at=CREATED, end="20261017", calendar_references=calendars,
            master_references=refused, targets=[{"symbol": "600000.SH", "adjust_kind": "forward"}])


@pytest.mark.parametrize("stamp", ["2026-10-16T00:00:00+00:00", "2026-10-19T12:03:01+00:00"])
def test_capture_pages_must_not_precede_original_or_postdate_generation(adopted, stamp):
    refs, _ = new_masters(adopted, stamp=stamp)
    before = adopted["backend"].head()
    with pytest.raises(RecordError, match="FTSHARE_SINGLE_MASTER_CLOCK"):
        master_plan(adopted, refs)
    assert adopted["backend"].head() == before


@pytest.mark.parametrize("change", ["one-venue", "wrong-venue", "filtered", "non-L", "incomplete"])
def test_master_override_requires_two_exact_complete_current_l_snapshots(adopted, change):
    before = adopted["backend"].head()
    with pytest.raises(ValueError, match="MASTER_REFERENCES|QUERY_SCOPE|CAPTURE_INCOMPLETE"):
        refs, _ = new_masters(adopted,
            extra={"ts_code": "600000.SH"} if change == "filtered" else
                {"list_status": "D"} if change == "non-L" else None,
            pages=1 if change == "incomplete" else 20)
        if change == "one-venue":
            refs.pop("SZSE")
        elif change == "wrong-venue":
            refs["SSE"] = deepcopy(refs["SZSE"])
        before = adopted["backend"].head()
        master_plan(adopted, refs)
    assert adopted["backend"].head() == before


def test_new_master_refs_are_frozen_in_retained_plan_and_replayed_exactly(adopted):
    refs, _ = new_masters(adopted)
    collection = master_plan(adopted, refs)
    saved = _retain(collection, adopted["store"], adopted["fresh"])
    head = adopted["backend"].head()
    replay = _retain(collection, adopted["store"], adopted["fresh"])
    assert replay["reference"] == saved["reference"] and adopted["backend"].head() == head
    started = begin_ftshare_slot(collection, collection["plan"]["new_slots"][0], at=CREATED)
    started["plan"]["master_references"] = deepcopy(adopted["context"]["master_references"])
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_PLAN_CHANGED"):
        _retain(started, adopted["store"], adopted["fresh"], saved["reference"])
    assert adopted["backend"].head() == head


def test_master_change_is_an_explicit_frontier_refusal_without_returning_an_older_root(adopted):
    refs, _ = new_masters(adopted)
    collection = master_plan(adopted, refs)
    saved = _retain(collection, adopted["store"], adopted["fresh"])
    collection, saved = settle_single(adopted, collection, saved)
    frontier = resolve_ftshare_expansion_frontier(adopted["adopted"]["reference"], REGISTRY, adopted["fresh"]())
    assert frontier["reference"] == frontier["blocking_reference"] == saved["reference"]
    assert frontier["reason_code"] == "FTSHARE_EXPANSION_MASTER_CHANGED"
    assert frontier["restored"]["context"]["master_references"] == refs
    with pytest.raises(RecordError, match="FTSHARE_SINGLE_BASE_ALREADY_HAS_SUCCESSOR"):
        plan_single(adopted, generation="g20261016-older", created_at=CREATED)


def test_cli_passes_master_refs_before_source_and_resume_cannot_switch_them():
    host = SingleHost()
    refs, _ = new_masters(host.fixture)
    host.config["kernel_sync"].update(targets=[{"symbol": "600001.SH", "adjust_kind": "none"},
        {"symbol": "600002.SH", "adjust_kind": "none"}], source_budget=2)
    provider = SingleProvider(host)
    result = single_execute(host, provider, master_references=refs)
    assert result["receipt"]["status"] == "partial", result["receipt"]
    assert result["receipt"]["source_requests"] == provider.requests == 1
    assert host.latest()["collection"]["plan"]["master_references"] == refs
    before = host.read().backend.head()
    rejected = single_execute(host, master_references=host.fixture["context"]["master_references"])
    assert rejected["receipt"]["status"] == "blocked"
    assert rejected["receipt"]["reason_code"] == "FTSHARE_RUNTIME_RESUME_MASTER_CHANGED"
    assert rejected["receipt"]["source_requests"] == 0
    assert host.read().backend.head() == before
    resumed = single_execute(host, SingleProvider(host), master_references=refs)
    assert resumed["receipt"]["status"] == "complete" and resumed["receipt"]["source_requests"] == 1
    assert resumed["receipt"]["reserved_requests"] == 2
    before = host.read().backend.head()
    replay = single_execute(host, master_references=refs)
    assert replay["receipt"]["status"] == "already-complete" and replay["receipt"]["source_requests"] == 0
    assert replay["reference"] == resumed["reference"] and host.read().backend.head() == before


@pytest.mark.parametrize("operation,single", [("ftshare-status", True), ("ftshare-adopt-kernel", False),
    ("ftshare-sync", False)])
def test_master_overrides_are_limited_to_single_sync_before_store_or_client(operation, single):
    with pytest.raises(RecordError, match="FTSHARE_RUNTIME_MASTER_OVERRIDE_REQUIRES_SINGLE_SYNC"):
        cli.run_collection(operation=operation, config={"kernel_sync": {} if single else None},
            registry=REGISTRY, master_references={}, access_confirmed=True, custody_authorized=True,
            open_unit=lambda _: pytest.fail("inadmissible override must not open a Store"),
            client_factory=lambda: pytest.fail("inadmissible override must not create a source client"))
