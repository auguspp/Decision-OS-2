"""Public Discovery uses native custody and the existing Artifact CAS.

Only remote responses and source inputs are synthetic. Real Git owns retained
bytes, event references and compare-and-swap; the real policy parser decides
publication, and the ordinary GitHubAPI retains its 512-request limit.
"""
from copy import copy
from dataclasses import replace
import json

import pytest

from decision_kernel.application import daily_discovery as daily
from decision_kernel.application.market_discovery import MarketDiscovery
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure import runtime_publication as publication
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from test_daily_discovery import source_count
from test_daily_market import Host, NOW
from test_market_discovery import REGISTRY, request, retain, rising, row
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport
from test_runtime_publication import grant


@pytest.fixture
def public_native(native, grant, monkeypatch):
    model, _ = native
    model.binding = replace(model.binding, repository=publication.REPOSITORY,
        repository_id=publication.REPOSITORY_ID, store_id=publication.STORE_ID,
        visibility="public", private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding, model.git.repository = model.binding, model.binding.repository
    model.meta.update(id=publication.REPOSITORY_ID, full_name=publication.REPOSITORY, private=True)
    model.git.live_visibility = lambda: "private" if model.meta["private"] else "public"
    # Bind the unchanged policy parser to this fixture's actual original Git
    # anchor. A synthetic history cannot reproduce the production commit SHA.
    monkeypatch.setattr(publication, "ANCHOR", anchor)
    document, path = grant
    document["anchor"] = anchor
    path.write_text(json.dumps(document))
    fresh = lambda: VersionedStore(copy(model.git))
    return model, VersionedStore(model.git), fresh, document, path


def opened(model):
    api, fetches = transport(model), []
    store = VersionedStore(GitHubStore(model.binding, api))
    sources = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    return store, sources, api, fetches


def no_post(model, head, callbacks):
    assert model.git.head() == head and model.writes == 0 and callbacks == []
    assert all(method != "POST" for method, _, _ in model.requests)


def test_public_discovery_saves_original_events_and_replays_with_independent_readers(public_native):
    model, local, fresh, document, _ = public_native
    first_source = retain((local, fresh), rising(30))
    second_source = retain((local, fresh), rising(31))
    original_marker = model.git.read(model.binding.anchor, "store.json")
    model.meta["private"] = False
    assert all(publication.publication_basis(model.binding, scope) == document["authorization_ref"]
               for scope in ("sources", "market"))
    store, sources, api, fetches = opened(model)
    callbacks = []
    before = model.git.head()
    service = MarketDiscovery(store, REGISTRY, source_store=sources)
    first = service.save("public-discovery-first", request(first_source),
        before_new_write=callbacks.append)
    second = service.save("public-discovery-second",
        request(second_source, previous=first["reference"]), before_new_write=callbacks.append)
    assert first["status"] == second["status"] == "stored-and-verified"
    assert callbacks == [before, first["reference"]["commit"]] and model.writes == 2
    posts = [payload["variables"]["input"] for method, _, payload in model.requests if method == "POST"]
    assert [item["expectedHeadOid"] for item in posts] == callbacks
    assert row(first["result"])["origin_event"] == row(second["result"])["origin_event"]
    assert first["result"]["source_requests"] == second["result"]["source_requests"] == 0
    assert MAX_REQUESTS == 512 and api._requests == len(model.requests) < MAX_REQUESTS
    assert fetches and all(not item["destination"].exists() for item in fetches)

    # New backends have neither the writer's cached bytes nor source snapshot.
    writes, head, prior_requests = model.writes, model.git.head(), len(model.requests)
    reader, input_reader, read_api, read_fetches = opened(model)
    restored = MarketDiscovery(reader, REGISTRY, source_store=input_reader).read(second["reference"])
    assert restored["verification"] == "recomputed-from-retained-sources"
    for field in ("plan", "surfaces", "proofs", "events", "view_sha256"):
        assert restored[field] == second["result"][field]
    origin = row(restored)["origin_event"]
    assert restored["events"][origin] == first["result"]["events"][origin]
    assert restored["plan"]["previous"] == first["reference"]
    assert encoded(restored["plan"]["surfaces"]["sector"]["reference"]) == encoded(second_source)
    assert model.git.read(model.binding.anchor, "store.json") == original_marker
    assert model.writes == writes and model.git.head() == head
    assert read_api._requests == len(model.requests) - prior_requests < MAX_REQUESTS
    assert len(read_fetches) == 1 and not read_fetches[0]["destination"].exists()


@pytest.mark.parametrize("mode,reason", [
    ("absent", "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"),
    ("disabled", "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"),
    ("missing-sources", "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"),
    ("missing-market", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED"),
])
def test_public_discovery_refuses_missing_scope_before_artifact_or_callback(public_native, mode, reason):
    model, local, fresh, document, path = public_native
    reference = retain((local, fresh), rising(30))
    if mode == "absent":
        path.unlink()
    else:
        if mode == "disabled":
            document["enabled"] = False
        else:
            document["scopes"].remove(mode.removeprefix("missing-"))
        path.write_text(json.dumps(document))
    model.meta["private"] = False
    store, sources, api, _ = opened(model)
    head, callbacks = model.git.head(), []
    with pytest.raises(RecordError, match="^" + reason + "$"):
        MarketDiscovery(store, REGISTRY, source_store=sources).save("refused-public-discovery",
            request(reference), before_new_write=callbacks.append)
    no_post(model, head, callbacks)
    assert api._requests == len(model.requests) < MAX_REQUESTS


def test_public_grant_does_not_bypass_same_backend_private_reversal(public_native):
    model, local, fresh, _, _ = public_native
    reference = retain((local, fresh), rising(30))
    # The writer genuinely observes private native metadata before it reverses.
    store, sources, api, _ = opened(model)
    assert store.backend.live_visibility() == "private"
    model.meta["private"] = False
    head, callbacks = model.git.head(), []
    with pytest.raises(RecordError, match="^PRIVATE_UPGRADE_REVERSED$"):
        MarketDiscovery(store, REGISTRY, source_store=sources).save("reversed-public-discovery",
            request(reference), before_new_write=callbacks.append)
    no_post(model, head, callbacks)
    assert api._requests == len(model.requests) < MAX_REQUESTS


@pytest.mark.parametrize("race", [False, True], ids=["stored", "inventory-head-changed"])
def test_public_daily_discovery_keeps_native_inventory_cas_and_original_source_budget(
        public_native, monkeypatch, race):
    model, local, fresh, _, _ = public_native
    host = Host(storage=(model.git, local, fresh))
    result = host.run()
    assert result["receipt"]["status"] == "complete"
    source_requests = source_count(host)
    market_reference = result["control"]["market_reference"]
    model.meta["private"] = False
    store, sources, api, fetches = opened(model)
    guard_calls, winners = [], []
    if race:
        original = MarketDiscovery.save
        def intervening(self, submission_id, plan, **kwargs):
            # Move the real records head after daily inventory, before the
            # unchanged owner callback at ResearchArtifacts.put's CAS base.
            winners.append(model.git.commit({"submissions/concurrent-note.json": b"{}\n"},
                expected=model.git.head()))
            return original(self, submission_id, plan, **kwargs)
        monkeypatch.setattr(MarketDiscovery, "save", intervening)
    arguments = dict(as_of=NOW, submission_id="public-daily-state",
        expected_reference=market_reference, guard=lambda: guard_calls.append(model.git.head()))
    if race:
        with pytest.raises(RecordError, match="^DAILY_DISCOVERY_HEAD_CHANGED$"):
            daily.save_daily_discovery(store, sources, REGISTRY, host.approved,
                result["receipt"], **arguments)
        assert len(winners) == 1
        no_post(model, winners[0], guard_calls)
    else:
        saved = daily.save_daily_discovery(store, sources, REGISTRY, host.approved,
            result["receipt"], **arguments)
        assert saved["status"] == "stored-and-verified" and saved["records_writes"] == model.writes == 1
        assert saved["market_reference"] == market_reference and saved["source_requests"] == 0
        posts = [payload["variables"]["input"] for method, _, payload in model.requests if method == "POST"]
        assert guard_calls == [saved["inventory"]["snapshot"]]
        assert posts[0]["expectedHeadOid"] == guard_calls[0]
        restored = MarketDiscovery(fresh(), REGISTRY).read(saved["reference"])
        assert restored["verification"] == "recomputed-from-retained-sources"
        assert restored["plan"]["stream"] == "daily-market"
        assert encoded(restored["plan"]["surfaces"]["stock"]["reference"]) == encoded(market_reference)
        assert restored["surfaces"]["stock"]["scope_count"] == 5
    assert source_count(host) == source_requests
    assert MAX_REQUESTS == 512 and api._requests == len(model.requests) < MAX_REQUESTS
    assert fetches and all(not item["destination"].exists() for item in fetches)
