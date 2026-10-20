"""Original SourceRef custody, independent recovery and bounded malformed roots."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import auction_minute_custody as custody
from decision_kernel.application.auction_minute_inputs import minute_view
from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market, market_reading_projection,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES, RecordError, SourceRef, encoded
from test_market_custody import MemoryBackend
from test_auction_minute_inputs import REGISTRY, seed_payload, minute_payload, replace_body
from test_global_room import source_query


def saved_fixture(*, seed=None, payload_change=None):
    backend = MemoryBackend()
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    seed = seed_payload() if seed is None else seed
    auction = retain_market_reading(seed, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    payload = minute_payload(read_market_payload(seed, REGISTRY), auction["reference"])
    if payload_change is not None:
        payload_change(payload)
    saved = retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    return store, fresh, auction, payload, saved


def root_document(store, reference):
    return json.loads(store.read_source(SourceRef.from_dict(reference))["data"])


def test_full_standard_sources_fresh_readback_replay_and_original_bytes():
    store, fresh, auction, payload, saved = saved_fixture()
    root = root_document(store, saved["reference"])
    original = payload["records"][0]["body"]
    assert root["reading"]["records"][0]["body"] == {"source": 0}
    source = fresh().read_source(SourceRef.from_dict(root["sources"][0]["reference"]))
    assert source["data"].encode() == original.encode() and source["manifest"]["input"]["custody"] == "provided"
    assert source["manifest"]["input"]["acquired_at"] == payload["records"][0]["received_at"]
    result = read_stored_market(saved["reference"], REGISTRY, fresh())
    seed = read_stored_market(auction["reference"], REGISTRY, fresh())["view"]
    assert encoded(result["view"]) == encoded(minute_view(payload, seed))
    assert result["source_requests"] == 0 and not result["original_http_restored"]
    assert result["original_bodies_restored"] and result["view"]["cohort_denominator"] == 9
    assert market_reading_projection(payload, REGISTRY) == payload
    before = store.backend.head()
    replay = retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert replay["reference"] == saved["reference"] and store.backend.head() == before


def test_request_clock_and_root_bind_identical_bodies_separately_without_revising_old_root():
    store, fresh, _, payload, first = saved_fixture()
    changed = deepcopy(payload)
    changed["records"][0]["received_at"] = "2026-10-20T01:41:02+00:00"
    second = retain_market_reading(changed, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    one, two = root_document(store, first["reference"]), root_document(store, second["reference"])
    assert one["sources"][0]["reference"] != two["sources"][0]["reference"]
    assert one["reading"]["records"][0]["sha256"] == two["reading"]["records"][0]["sha256"]
    assert read_stored_market(first["reference"], REGISTRY, fresh()) == first["restored"]


def test_missing_minute_original_preserves_full_seed_groups_and_missing_seed_is_unknown():
    store, fresh, auction, _, saved = saved_fixture()
    root = root_document(store, saved["reference"])
    source = SourceRef.from_dict(root["sources"][0]["reference"])
    store.backend.shared["missing"].add((source.manifest.commit, source.manifest.path))
    result = read_stored_market(saved["reference"], REGISTRY, fresh())
    assert result["status"] == "partial-restoration" and result["view"]["cohort_denominator"] == 9
    assert result["view"]["observed_0940"] == 0 and result["view"]["groups"] == {
        key: {"denominator": value["denominator"], "observed_0940": 0}
        for key, value in saved["restored"]["view"]["groups"].items()}
    pool, _ = source_query(store, auction["reference"], api="limit_list_d")
    store.backend.shared["missing"].add((pool.manifest.commit, pool.manifest.path))
    result = read_stored_market(saved["reference"], REGISTRY, fresh())
    assert result["status"] == "partial-restoration" and result["view"] is None
    assert result["sources"][-1]["path"] == "auction"


@pytest.mark.parametrize("mutation", ["cohort", "source-hash", "foreign", "selection", "clock", "request"])
def test_resealed_bad_recipe_cannot_admit_tampered_seed_request_original_or_scope(mutation):
    store, fresh, _, _, saved = saved_fixture()
    root = root_document(store, saved["reference"])
    if mutation == "cohort":
        root["plan"]["cohort_version"] = "0" * 64
    elif mutation == "source-hash":
        root["sources"][0]["source_sha256"] = "0" * 64
    elif mutation == "foreign":
        root["sources"][0]["reference"]["manifest"]["repository"] = "synthetic-git:other"
    elif mutation == "selection":
        root["sources"][0]["reference"]["selection"] = {"kind": "lines", "start": 1, "end": 1, "columns": []}
    elif mutation == "clock":
        root["reading"]["records"][0]["received_at"] = "2026-10-20T01:41:02+00:00"
    else:
        root["reading"]["records"][0]["request"]["parameters"]["symbols"].reverse()
    changed = store.put_source("minute-bad-" + mutation, custody._root_source(root))
    if mutation in ("foreign", "selection"):
        with pytest.raises(RecordError):
            read_stored_market(changed, REGISTRY, fresh())
    else:
        result = read_stored_market(changed, REGISTRY, fresh())
        assert result["status"] == "partial-restoration"
        if result["view"] is not None:
            assert result["view"]["observed_0940"] == 0


def test_whole_encoded_source_and_total_capacity_preflight_precede_every_new_write():
    store, fresh, auction, payload, _ = saved_fixture()
    record = payload["records"][0]
    # The raw string is below the old body allowance, but its standard source
    # envelope escapes every quote and cannot fit the unchanged 2 MiB limit.
    raw = '"' * (MAX_BYTES // 2)
    assert len(raw.encode()) < MAX_BYTES
    with pytest.raises(RecordError, match="SOURCE_TOO_LARGE"):
        custody._body_source(payload, record, raw)
    body = json.loads(record["body"])
    body["padding"] = "a" * (MAX_BYTES - 10000)
    replace_body(payload, body)
    before = store.backend.head()
    # Increase only supplied padding to leave the encoded input just in range;
    # the complete root plus original must still be preflighted before writes.
    body["padding"] += "a" * (MAX_BYTES - len(encoded(payload)) - 1)
    replace_body(payload, body)
    assert len(encoded(payload)) <= MAX_BYTES
    with pytest.raises(RecordError, match="MINUTE_CUSTODY_(TOTAL|INPUT)_BOUND"):
        retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert store.backend.head() == before


def test_no_permission_wrong_seed_and_incomplete_plan_never_write():
    store, fresh, auction, payload, _ = saved_fixture()
    before = store.backend.head()
    with pytest.raises(RecordError, match="MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"):
        retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh)
    bad = deepcopy(payload)
    bad["records"][0]["request"]["parameters"]["symbols"].reverse()
    with pytest.raises(RecordError, match="MINUTE_ORIGINAL_COHORT_REQUEST_ORDER"):
        retain_market_reading(bad, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    bad = deepcopy(payload)
    bad["auction_reference"]["manifest"]["repository"] = "synthetic-git:other"
    with pytest.raises(RecordError, match="FOREIGN_REPOSITORY"):
        retain_market_reading(bad, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert store.backend.head() == before


def test_declared_aggregate_overflow_in_resealed_root_rejects_before_any_dependency(monkeypatch):
    store, fresh, _, _, saved = saved_fixture()
    root = root_document(store, saved["reference"])
    root["reading"]["records"][0]["bytes"] = MAX_BYTES
    reference = store.put_source("minute-declared-total-overflow", custody._root_source(root))
    actual, reads = VersionedStore.read_source, []
    def read(owner, ref):
        reads.append(ref)
        return actual(owner, ref)
    monkeypatch.setattr(VersionedStore, "read_source", read)
    with pytest.raises(RecordError, match="MINUTE_CUSTODY_TOTAL_BOUND"):
        read_stored_market(reference, REGISTRY, fresh())
    assert reads == [reference]
