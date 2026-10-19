"""Private full-catalogue custody with the original native Git/API boundaries.

All provider and GitHub responses are synthetic. Native Git, SourceRef bytes,
the CAS writer and its 512-request transport remain the production code paths.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

import pytest

from decision_kernel.application import market_custody as custody
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from test_market_custody import MemoryBackend, raw_payload
from test_market_relay_inputs import REGISTRY, SyntheticOpener
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport


def synthetic_capture(api, fields, rows, parameters):
    request = {"api": api, "stage": "baseline", "purpose": "synthetic private custody test",
        "parameters": {**parameters, "fields": ",".join(fields), "limit": "500"}}
    return list(capture_plan({"schema": "research-source-plan-v1", "case_id": "synthetic-private-market",
        "requests": [request]}, REGISTRY,
        client=RelayHTTP(opener=SyntheticOpener(api, fields, rows), credential="synthetic-key-only"),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: "2026-10-19T12:00:00+00:00"))[0]


def full_catalogue_payload():
    """Two catalogue pages, one calendar and three 72-point index captures.

The 108-day calendar is expressly synthetic. The full 869 rows identify 781
codes and quarantine 88 duplicate identities, including every original row.
"""
    days = [date(2026, 7, 1) + timedelta(days=n) for n in range(108)]
    weekdays = [day for day in days if day.weekday() < 5]
    opened = set(weekdays[-72:])
    codes = [f"{881000 + n:06}.TI" for n in range(781)]
    rows = [[code, "合成分类", 1, "A", "20000101", "I"] for code in codes]
    rows += deepcopy(rows[:88])
    start, end = days[0].strftime("%Y%m%d"), days[-1].strftime("%Y%m%d")
    return {"schema": "relay-ths-group-reading-v1", "kind": "sector",
        "exchange": "SSE", "start": start, "end": end,
        "reference_policy": "isolate-identified", "horizons": [5, 20, 60],
        "catalog_result": synthetic_capture("ths_index",
            ["ts_code", "name", "count", "exchange", "list_date", "type"], rows,
            {"exchange": "A", "type": "I"}),
        "calendar_result": synthetic_capture("trade_cal", ["exchange", "cal_date", "is_open"],
            [["SSE", day.strftime("%Y%m%d"), int(day in opened)] for day in days],
            {"exchange": "SSE", "start_date": start, "end_date": end}),
        "member_results": {},
        "index_results": {code: synthetic_capture("ths_daily", ["ts_code", "trade_date", "close"],
            [[code, day.strftime("%Y%m%d"), str(100 + n)] for n, day in enumerate(sorted(opened))],
            {"ts_code": code, "start_date": start, "end_date": end}) for code in codes[100:103]}}


def public_origin_private(model):
    # Seed a synthetic public-origin marker, retaining it when native privacy is
    # private. This is the production binding policy, not a marker migration.
    model.binding = replace(model.binding, visibility="public", private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding = model.binding
    assert model.meta["private"] is True


@pytest.mark.parametrize("public_origin", [False, True], ids=["private", "public-origin-upgrade"])
def test_private_full_catalogue_custody_and_fresh_snapshot_readback_fit_original_budget(
        native, monkeypatch, record_property, public_origin):
    model, _ = native
    if public_origin:
        public_origin_private(model)
    payload = full_catalogue_payload()
    original = encoded(payload)
    api, fetches, readers = transport(model), [], []
    writer = VersionedStore(GitHubStore(model.binding, api))
    replays, restore_inputs = [], custody.restore_market_reading_inputs

    def remember_inputs(*args, **kwargs):
        restored = restore_inputs(*args, **kwargs)
        replays.append(restored["payload"])
        return restored

    monkeypatch.setattr(custody, "restore_market_reading_inputs", remember_inputs)

    def fresh():
        reader = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
            fetch=local_fetch(model, fetches)))
        readers.append(reader)
        return reader

    saved = custody.retain_market_reading(payload, REGISTRY, writer, fresh_reader=fresh,
        custody_authorized=True, public_rights_confirmed=False)
    assert saved["status"] == "stored-and-independently-restored"
    assert (saved["query_count"], saved["page_count"]) == (5, 6)
    assert encoded(payload) == original
    assert saved["restored"]["view"] == custody.read_market_payload(payload, REGISTRY)
    assert len(replays) == 1
    assert encoded(replays[0]) == encoded(custody.market_reading_projection(payload, REGISTRY))
    assert saved["restored"]["view"]["scope_count"] == 781
    assert saved["restored"]["view"]["catalogue"]["quality"] == {
        "source_row_count": 869, "scope_count": 781, "eligible_count": 693,
        "quarantined_count": 88, "quarantined_row_count": 176, "duplicate_group_count": 88}
    assert saved["restored"]["unavailable_queries"] == 0
    assert saved["restored"]["source_requests"] == 0
    assert model.writes == 12  # Six page versions, five queries and the complete reading root.
    assert MAX_REQUESTS == 512 and api._requests == len(model.requests) < 512
    record_property("native_api_requests", api._requests)
    assert len(readers) == 2 and readers[0].backend is not readers[1].backend
    assert all(reader.backend.api is api and not reader.backend.binding.write_enabled for reader in readers)
    assert len(fetches) == 6  # Five independent query snapshots, then one complete final snapshot.
    assert fetches[-1]["head"] == saved["reference"]["manifest"]["commit"]
    assert all(fetch["api"] is api and not fetch["destination"].exists() for fetch in fetches)


@pytest.mark.parametrize("refusal,reason", [
    ("missing-authorization", "MARKET_CUSTODY_AUTHORIZATION_REQUIRED"),
    ("new-api", "MARKET_CUSTODY_SHARED_OPERATION_BUDGET_REQUIRED"),
    ("writable-reader", "MARKET_CUSTODY_FRESH_READ_ONLY_REQUIRED"),
])
def test_private_whole_reading_admission_does_not_borrow_rights_or_reset_budget(native, refusal, reason):
    model, _ = native
    api = transport(model)
    writer = VersionedStore(GitHubStore(model.binding, api))
    before = api._requests
    def fresh():
        if refusal == "writable-reader":
            return VersionedStore(GitHubStore(model.binding, api))
        return VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False),
            transport(model) if refusal == "new-api" else api))
    with pytest.raises(RecordError, match=reason):
        custody.retain_market_reading(raw_payload(), REGISTRY, writer, fresh_reader=fresh,
            custody_authorized=refusal != "missing-authorization", public_rights_confirmed=False)
    assert model.writes == 0
    if refusal == "missing-authorization":
        assert api._requests == before


def test_public_target_still_requires_its_own_public_rights(native):
    model, _ = native
    model.binding = replace(model.binding, visibility="public")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta["private"] = False
    api = transport(model)
    writer = VersionedStore(GitHubStore(model.binding, api))
    with pytest.raises(RecordError, match="MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"):
        custody.retain_market_reading(raw_payload(), REGISTRY, writer,
            fresh_reader=lambda: pytest.fail("unauthorized custody must not open a reader"),
            custody_authorized=True, public_rights_confirmed=False)
    assert model.writes == 0


def test_final_independent_restore_cannot_release_after_native_private_reversal(native):
    model, _ = native
    api, fetches, readers, released = transport(model), [], [], []
    writer = VersionedStore(GitHubStore(model.binding, api))
    normal_fetch = local_fetch(model, fetches)

    def fresh():
        final = len(readers) == 1
        def fetch(binding, current_api, destination):
            normal_fetch(binding, current_api, destination)
            if final:
                model.meta["private"] = False
        reader = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
            fetch=fetch))
        readers.append(reader)
        return reader

    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        released.append(custody.retain_market_reading(raw_payload(), REGISTRY, writer,
            fresh_reader=fresh, custody_authorized=True, public_rights_confirmed=False))
    assert not released and model.writes == 5
    assert len(readers) == 2 and len(fetches) == 3
    assert all(reader.backend._depth == 0 and reader.backend._reader is None for reader in readers)
    assert all(not fetch["destination"].exists() for fetch in fetches)


def test_public_origin_private_reversal_before_final_reader_preserves_original_operation_authority(
        native, monkeypatch, record_property):
    model, _ = native
    public_origin_private(model)
    api, fetches, readers, released = transport(model), [], [], []
    writer = VersionedStore(GitHubStore(model.binding, api))
    assert writer.backend._observed_private_upgrade is True
    readbacks, ordinary_read = [], custody.read_stored_market

    def remember_readback(*args, **kwargs):
        restored = ordinary_read(*args, **kwargs)
        readbacks.append(restored)
        return restored

    monkeypatch.setattr(custody, "read_stored_market", remember_readback)

    def fresh():
        if readers:
            # The root write has completed while private. The new reader has
            # no memory of that observation and first sees the old public mode.
            assert model.writes == 5 and writer.backend._observed_private_upgrade is True
            model.meta["private"] = False
        reader = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
            fetch=local_fetch(model, fetches)))
        readers.append(reader)
        return reader

    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        released.append(custody.retain_market_reading(raw_payload(), REGISTRY, writer,
            fresh_reader=fresh, custody_authorized=True, public_rights_confirmed=False))
    assert not released and model.writes == 5
    # Failure is not a promise of no write: the independently restored root is
    # already in native Git and must remain available for later reconciliation.
    assert len(readbacks) == 1 and readbacks[0]["status"] == "restored"
    assert model.git.head() == readbacks[0]["reference"]["manifest"]["commit"]
    assert len(readers) == 2 and len(fetches) == 3
    assert all(reader.backend.api is api and reader.backend._depth == 0
               and reader.backend._reader is None for reader in readers)
    assert all(not fetch["destination"].exists() for fetch in fetches)
    assert api._requests == len(model.requests) < MAX_REQUESTS == 512
    record_property("native_api_requests", api._requests)


@pytest.mark.parametrize("reason", ["REMOTE_REQUEST_LIMIT", "REMOTE_NOT_AUTHORIZED", "REMOTE_RATE_LIMIT"])
def test_operation_failure_stops_before_following_query_instead_of_partial_success(monkeypatch, reason):
    backend = MemoryBackend()
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    saved = custody.retain_market_reading(raw_payload(), REGISTRY, store, fresh_reader=fresh,
        custody_authorized=True, public_rights_confirmed=False)
    calls = []
    def refused(reference, registry, reader):
        calls.append(reference)
        raise StoreUnavailable(reason)
    monkeypatch.setattr(custody, "restore_market_query", refused)
    with pytest.raises(StoreUnavailable, match=reason):
        custody.read_stored_market(saved["reference"], REGISTRY, fresh())
    assert len(calls) == 1
