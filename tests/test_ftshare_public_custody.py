"""Normal public FT custody through real Git/API; all source responses are synthetic.

The public path resumes an already retained repaired daily cycle. It does not
reopen historical adoption, change either request budget, or contact a source.
"""
from collections import Counter
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.ftshare_collection import (
    adopt_ftshare_kernel, latest_ftshare_reference, restore_ftshare_collection,
    retain_ftshare_collection,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, encoded
from decision_kernel.infrastructure import runtime_publication as publication
from decision_kernel.infrastructure.github_store import GitHubAPI, MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces import daily_market_cli as cli
from test_daily_market import Host, QuotaClock, quota_headers
from test_ftshare_collection_runtime import NativeAPI
from test_github_transport import Response
from test_market_relay_inputs import REGISTRY
from test_market_snapshot import local_fetch, native
from test_runtime_publication import binding, context, grant


def _no_reader():
    pytest.fail("A refused custody grant must not open a reader or retain a root")


def _gate_store(bound):
    # These small denial tests isolate the authorization boundary. The complete
    # allowed path below uses GitHubStore, real Git and the bounded wire client.
    return VersionedStore(SimpleNamespace(binding=bound, repository=bound.repository,
        live_visibility=lambda: "public", commit=_no_reader))


@pytest.mark.parametrize("mode", ["absent", "revoked", "missing-sources"])
def test_caller_publication_flag_cannot_replace_deployment_sources_grant(grant, mode):
    doc, path = grant
    if mode == "absent":
        path.unlink()
    else:
        if mode == "revoked":
            doc["enabled"] = False
        else:
            doc["scopes"].remove("sources")
        path.write_text(json.dumps(doc))
    with pytest.raises(RecordError, match="^FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED$"):
        retain_ftshare_collection({}, REGISTRY, _gate_store(binding()), fresh_reader=_no_reader,
            custody_authorized=True, public_rights_confirmed=True)


@pytest.mark.parametrize("change", [
    {"repository": "someone/other-store"}, {"repository_id": 2},
    {"branch": "records/other"}, {"anchor": "b" * 40}, {"store_id": "other-store"},
    {"visibility": "private", "private_upgrade_allowed": False},
])
def test_caller_publication_flag_cannot_authorize_another_public_binding(grant, change):
    bound = replace(binding(), **change)
    with pytest.raises(RecordError, match="^FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED$"):
        retain_ftshare_collection({}, REGISTRY, _gate_store(bound), fresh_reader=_no_reader,
            custody_authorized=True, public_rights_confirmed=True)


@pytest.mark.parametrize("public_rights,reason", [
    (False, "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"),
    (True, "FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED"),
])
def test_deployment_publication_does_not_replace_original_custody(grant, public_rights, reason):
    with pytest.raises(RecordError, match="^" + reason + "$"):
        retain_ftshare_collection({}, REGISTRY, _gate_store(binding()), fresh_reader=_no_reader,
            custody_authorized=False, public_rights_confirmed=public_rights)


@pytest.mark.parametrize("public_rights", [False, True])
def test_normal_publication_never_reopens_historical_kernel_adoption(grant, public_rights):
    assert publication.publication_basis(binding(), "sources") == grant[0]["authorization_ref"]
    with pytest.raises(RecordError, match="^FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED$"):
        adopt_ftshare_kernel(b"", {}, {}, REGISTRY, _gate_store(binding()),
            collection_id="ftshare-cn-eod", generation="g20261016",
            created_at="2026-10-19T12:02:00+00:00", fresh_reader=_no_reader,
            custody_authorized=True, public_rights_confirmed=public_rights)


def test_public_repaired_daily_resumes_twelve_prices_and_independently_restores_six_stocks(
        native, grant, monkeypatch, record_property):
    model, _ = native
    model.binding = replace(model.binding, repository=publication.REPOSITORY,
        repository_id=publication.REPOSITORY_ID, store_id=publication.STORE_ID,
        visibility="public", private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding, model.git.repository = model.binding, model.binding.repository
    model.meta.update(id=publication.REPOSITORY_ID, full_name=publication.REPOSITORY, private=True)
    # Fixture-only prehistory was admitted while the public-origin repository
    # was private, including historical adoption. The native public host below
    # reads the same original marker and never substitutes private visibility.
    model.git.live_visibility = lambda: "private" if model.meta["private"] else "public"
    # A synthetic Git graph cannot have the production anchor's SHA. Bind the
    # real policy parser to its actual original commit, not a mocked grant result.
    monkeypatch.setattr(publication, "ANCHOR", anchor)
    doc, path = grant
    doc["anchor"] = anchor
    path.write_text(json.dumps(doc))
    local_fresh = lambda: VersionedStore(copy(model.git))
    host = Host(storage=(model.git, VersionedStore(model.git), local_fresh), count=7)
    host.approved.update(symbols=sorted(host.fixture["context"]["eligible_symbols"])[:6],
        max_price_requests=12)
    assert len(host.approved["symbols"]) == 6
    assert len(host.fixture["context"]["eligible_symbols"]) > 6

    host.calendar_error = URLError("synthetic interrupted calendar response")
    failed = host.run()
    assert failed["receipt"]["reason_code"] == "RELAY_HTTP_UNAVAILABLE", failed
    original = daily.latest_cycle(local_fresh(), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])
    assert original["cycle"]["revision"] == 2
    original_body = local_fresh().read_source(SourceRef.from_dict(original["reference"]))["data"]
    repair = {"schema": "daily-calendar-repair-v1", "original_reference": original["reference"],
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6284820778",
        "venue": "SSE", "additional_requests": 1}
    host.calendar_error = None
    host.now = "2026-10-19T12:11:00+00:00"
    def pending_prices(**_):
        return {"receipt": {"status": "paused", "reason_code": "SYNTHETIC_PRICE_PHASE_PENDING",
                            "source_requests": 0}}
    paused = host.run(calendar_repair=repair, execute_prices=pending_prices)
    assert paused["receipt"]["cycle_status"] == "market-pending", paused
    pending = daily.latest_cycle(local_fresh(), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])
    cycle = pending["cycle"]
    assert cycle["revision"] == 7 and cycle["price_end"] == "20261019"
    assert {venue: row["reserved_requests"] for venue, row in cycle["calendars"].items()} == {
        "SSE": 2, "SZSE": 1}
    assert sum(client.requests for client in host.calendar_clients) == 3 and not host.price_clients
    assert latest_ftshare_reference(local_fresh(), REGISTRY,
        collection_id="ftshare-cn-eod", generation=cycle["market_generation"]) is None
    calendar_refs = {venue: row["reference"] for venue, row in cycle["calendars"].items()}
    marker = model.git.read(model.git.head(), "store.json")

    model.meta["private"] = False
    admitted = host.now = "2026-10-20T08:40:00+00:00"
    clock = QuotaClock(datetime.fromisoformat(admitted).timestamp())
    _, env, event = context("recover-late")
    selected = cli.settings(event, env, host.approved)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert publication.native_publication_basis(selected) == doc["authorization_ref"]
    assert publication.publication_basis(model.binding, "sources") == doc["authorization_ref"]
    native_model = NativeAPI()
    native_model.run.update(path=cli.WORKFLOW, name=cli.TITLE, created_at=admitted)
    native_model.ci.update(total_count=1)
    native_model.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": publication.REPOSITORY_ID}, head_repository={"id": publication.REPOSITORY_ID})
    counts, apis, fetches, trace = {}, [], [], []
    immutable, blob_sizes = {}, {}
    immutable_path = re.compile(re.escape(cli.PREFIX) +
        r"/git/(?:(?:commits|blobs)/[a-f0-9]{40}|trees/[a-f0-9]{40}(?:\?recursive=1)?)")

    class Wire:
        # Same native 1000/core+GraphQL model as test_daily_market_quota: the
        # real GitHubAPI owns per-unit counting and shared quota observations.
        def open(self, request, timeout):
            assert timeout == 30
            window = int(clock.at // 3600)
            totals = counts.setdefault(window, Counter())
            resource = "graphql" if request.method == "POST" else "core"
            totals[resource] += 1
            path = request.full_url.removeprefix("https://api.github.com")
            trace.append((request.method, path))
            headers = quota_headers(resource=resource, remaining=max(0, 1000 - totals[resource]),
                reset=(window + 1) * 3600)
            if totals[resource] > 1000:
                raise HTTPError(request.full_url, 429, "synthetic quota exhausted", headers, None)
            payload = json.loads(request.data) if request.data else None
            cacheable = request.method == "GET" and immutable_path.fullmatch(path) is not None
            if cacheable and path in immutable:
                model.requests.append((request.method, path, deepcopy(payload)))
                return Response(immutable[path], request.full_url, headers=headers)
            if path.endswith("/git/ref/heads/main"):
                value = deepcopy(native_model.main)
            elif path.endswith("/actions/runs/8001"):
                value = deepcopy(native_model.run)
            elif "/actions/workflows/ci.yml/runs?" in path:
                value = deepcopy(native_model.ci)
            elif cacheable and path.startswith(cli.PREFIX + "/git/trees/"):
                model.requests.append((request.method, path, deepcopy(payload)))
                sha, rows = path.rsplit("/", 1)[1].split("?", 1)[0], []
                for raw in model.git._run("ls-tree", "-r", "-z", sha).stdout.split(b"\0"):
                    if not raw:
                        continue
                    meta, name = raw.split(b"\t")
                    mode, kind, blob = meta.decode().split()
                    if blob not in blob_sizes:
                        blob_sizes[blob] = int(model.git._run("cat-file", "-s", blob).stdout)
                    rows.append(dict(path=name.decode(), mode=mode, type=kind,
                        sha=blob, size=blob_sizes[blob]))
                value = dict(sha=sha, truncated=False, tree=rows)
            else:
                value = model.request(request.method, path, payload)
            raw = encoded(value)
            if cacheable:
                immutable[path] = raw
            return Response(raw, request.full_url, headers=headers)

    class BoundedAPI(GitHubAPI):
        def __init__(self, token):
            super().__init__(token)
            self._opener = Wire()
            apis.append(self)

    def sleep(seconds):
        clock.sleep(seconds)
        host.now = datetime.fromtimestamp(clock.at, timezone.utc).isoformat()

    monkeypatch.setattr(cli, "GitHubAPI", BoundedAPI)
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation:
        replace(model.binding, write_enabled=operation == "sync"))
    monkeypatch.setattr(cli, "GitMarketReader", lambda bound, api:
        GitMarketReader(bound, api, fetch=local_fetch(model, fetches)))
    opened = cli.remote_units("synthetic-public-daily-only", selected, clock=lambda: clock.at,
        monotonic=lambda: clock.elapsed, sleep=sleep)
    def no_calendar():
        pytest.fail("The retained complete calendars must not acquire another source request")
    options = dict(open_unit=opened, admission_at=admitted, allow_late=True,
        guard=lambda api: cli.check_private(api, selected), capacity=cli.market_capacity,
        clock=lambda: host.now, monotonic=lambda: clock.elapsed, sleep=sleep,
        calendar_client_factory=no_calendar)
    result = host.run(**options)
    receipt = result["receipt"]
    assert receipt["status"] == "complete", result
    assert receipt["cycle_status"] == "complete" and receipt["cycle_date"] == "20261019"
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"],
            receipt["source_requests"]) == (0, 12, 12)
    assert receipt["calendar_reserved_requests"] == receipt["calendar_source_budget"] == 3
    assert receipt["price_source_budget"] == 12
    assert len(host.price_clients) == 1 and host.price_clients[0].requests == 12
    assert sum(client.requests for client in host.calendar_clients) == 3
    assert all(unit["status"] == "complete" and unit["source_requests"] == 1
        for unit in receipt["price_execution"]["work_units"])
    assert len(receipt["price_execution"]["work_units"]) == 12

    # A fresh native, readonly backend restores the actual final root, all
    # source bodies and ordinary price-window calculation without the writer.
    writes = model.writes
    _, fresh, read_api = opened(False)
    reader = fresh()
    with market_read_scope(reader) as grouped:
        final = daily.latest_cycle(grouped, registry=REGISTRY,
            protected_symbols=host.approved["symbols"])
        resolved = daily.daily_market_reference(grouped, REGISTRY, host.approved)
        retained = restore_ftshare_collection(resolved["reference"], REGISTRY, grouped)["collection"]
        assert grouped.read_source(SourceRef.from_dict(original["reference"]))["data"] == original_body
        assert final["cycle"]["calendars"] == pending["cycle"]["calendars"]
        assert final["cycle"]["calendar_repair"] == repair
        assert resolved["frontier_reason_code"] is None
        assert final["cycle"]["market_reference"] == resolved["reference"]
        assert retained["plan"]["base_reference"] == cycle["base_market_reference"]
        assert retained["plan"]["calendar_references"] == calendar_refs
        assert retained["plan"]["source_budget"] == retained["reserved_requests"] == 12
        assert retained["source_hold"] is None and len(retained["units"]) == 12
        assert all(unit["status"] == "complete" and unit["request_count"] == 1
            for unit in retained["units"].values())
        for symbol in host.approved["symbols"]:
            restored = read_ftshare_collection_version(resolved["reference"], REGISTRY, grouped,
                entity_id=symbol, limit=1, read_as_of=host.now)
            assert restored["status"] == "restored" and restored["unavailable_queries"] == 0
            assert restored["source_requests"] == 0 and restored["ftshare_response_bodies_restored"]
            assert restored["selected_requested_end"] == "20261019"
            assert restored["serving"] == "current-partial"
            view = restored["view"]
            assert view["scope_count"] == len(host.fixture["context"]["scope_symbols"])
            assert view["freshness"]["read_as_of"] == host.now
            assert view["freshness"]["calendar_through"] == "2026-10-19"
            assert view["freshness"]["completion_policy"] == "ashare-eod-1835-v1"
            for kind in ("raw", "qfq"):
                assert view[kind]["observed_count"] == 6
                assert view[kind]["missing_count"] == view["scope_count"] - 6
                row = view[kind]["entries"][0]
                assert row["entity_id"] == symbol and row["as_of"] == "2026-10-19"
                assert row["status"] == "observed" and row["input_status"] == "supplied"
                assert [window["sessions"] for window in row["horizons"]] == [5, 20, 60]
                assert all(window["status"] == "observed" and window["end_date"] == "2026-10-19"
                    for window in row["horizons"])
                assert all(window["status"] == "observed" and window["end_date"] == "2026-10-19"
                    for window in view["endpoint_comparisons"][kind]["entries"][0]["horizons"])
    assert reader.backend.binding.write_enabled is False and reader.backend.api is read_api
    assert model.writes == writes
    assert model.git.read(model.git.head(), "store.json") == marker
    before = model.git.head()
    repeated = host.run(**options)
    assert repeated["receipt"]["status"] == "already-complete", repeated
    assert repeated["receipt"]["source_requests"] == 0 and model.git.head() == before
    assert model.writes == writes
    assert len(host.price_clients) == 1 and host.price_clients[0].requests == 12
    assert len(trace) == sum(api._requests for api in apis)
    assert max(api._requests for api in apis) <= MAX_REQUESTS
    assert all(api.rate_limits is apis[0].rate_limits for api in apis)
    assert all(total["core"] <= 1000 and total["graphql"] <= 1000 for total in counts.values())
    assert all(not call["destination"].exists() for call in fetches)
    record_property("public_daily_custody", json.dumps({"calendar_source_requests": 0,
        "calendar_cumulative_requests": 3, "price_source_requests": 12,
        "independently_restored_stocks": 6, "original_failed_cycle_preserved": True,
        "live_source_requests": 0, "live_store_writes": 0,
        "max_unit_api_requests": max(api._requests for api in apis),
        "native_quota_windows": len(counts)}))
