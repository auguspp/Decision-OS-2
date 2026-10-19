"""Synthetic qualified source paths and the real immutable artifact journal."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.market_discovery import MarketDiscovery, artifact_id, plan_input
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, SourceRef, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from test_market_custody import MemoryBackend, extra
from test_market_relay_inputs import REGISTRY

AS_OF = "2026-10-19T12:05:00+00:00"
DAYS = [(date(2026, 6, 1) + timedelta(days=n)).strftime("%Y%m%d") for n in range(100)]
CODE = "881001.TI"


def rising(n):
    return [100] * 21 + list(range(101, 101 + n - 21))


def group_payload(values, *, kind="sector", missing=(), name="合成分类", extra_code=True):
    code = CODE if kind == "sector" else "885001.TI"
    start, end = DAYS[0], DAYS[len(values) - 1]
    typ = "I" if kind == "sector" else "N"
    catalog = [[code, name, 1, "A", "20000101", typ]]
    if extra_code:
        catalog.append(["881002.TI" if kind == "sector" else "885002.TI", "合成缺数分类", 1, "A", None, typ])
    return {"schema": "relay-ths-group-reading-v1", "kind": kind, "start": start, "end": end,
        "exchange": "SSE", "reference_policy": "isolate-identified",
        "catalog_result": extra("ths_index", ["ts_code", "name", "count", "exchange", "list_date", "type"],
            catalog, {"exchange": "A", "type": typ}),
        "calendar_result": extra("trade_cal", ["exchange", "cal_date", "is_open"],
            [["SSE", day, "1"] for day in DAYS[:len(values)]],
            {"exchange": "SSE", "start_date": start, "end_date": end}),
        "index_results": {code: extra("ths_daily", ["ts_code", "trade_date", "close"],
            [[code, day, str(value)] for day, value in zip(DAYS, values) if day not in missing],
            {"ts_code": code, "start_date": start, "end_date": end})},
        "member_results": {}, "horizons": [5, 20]}


@pytest.fixture
def storage():
    backend = MemoryBackend()
    store = VersionedStore(backend)
    return store, lambda: VersionedStore(MemoryBackend(backend.shared))


def retain(storage, values, **kwargs):
    store, fresh = storage
    return retain_market_reading(group_payload(values, **kwargs), REGISTRY, store,
        fresh_reader=fresh, public_rights_confirmed=True)["reference"]


def request(reference, *, previous=None, kind="sector", **kwargs):
    return {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
        "surfaces": {kind: {"reference": reference}}, "previous": previous, **kwargs}


def row(document, kind="sector"):
    return document["surfaces"][kind]["states"][0]


def save_sequence(storage, values):
    service = MarketDiscovery(storage[0], REGISTRY)
    results = []
    for index, numbers in enumerate(values):
        ref = retain(storage, numbers)
        plan = request(ref, previous=results[-1]["reference"] if results else None)
        results.append(service.save("discovery-sequence-" + str(index), plan))
    return results


def put_document(store, document):
    """Rehashing JSON and Artifact bytes cannot substitute for source replay."""
    doc = deepcopy(document)
    doc["view_sha256"] = digest(encoded({k: v for k, v in doc.items() if k != "view_sha256"}))
    artifact = ArtifactInput.from_bytes(artifact_id(doc), "market-discovery.json",
                                       "application/json", encoded(doc))
    return ResearchArtifacts(store).put("tamper-" + digest(encoded(doc))[:32], artifact)


def test_qualified_source_path_and_missing_denominator_are_separate(storage):
    ref = retain(storage, rising(30))
    before = storage[0].backend.head()
    document = MarketDiscovery(storage[0], REGISTRY).build(request(ref))
    surface = document["surfaces"]["sector"]
    assert surface["status"] == "readable"
    assert surface["scope_count"] == 2 and surface["qualified_count"] == 1 and surface["unknown_count"] == 1
    assert surface["gaps"] == [{"entity_id": "ths-a-sector:881002.TI", "reason_code": "DISCOVERY_NOT_REQUESTED"}]
    assert row(document)["state"] == "accelerating" and row(document)["transition"] == "baseline"
    assert document["events"][row(document)["origin_event"]]["type"] == "baseline"
    assert document["events"][row(document)["origin_event"]]["evidence_mode"] == "retained-source-replay"
    assert document["events"][row(document)["origin_event"]]["timely_discovery_claim"] is False
    assert document["surfaces"]["stock"]["status"] == "not-supplied"
    assert document["source_requests"] == 0 and document["investment_authority"] == "none"
    assert storage[0].backend.head() == before


def test_save_entry_continuation_and_replay_preserve_original_event(storage):
    first, second, third = save_sequence(storage, [rising(25), rising(26), rising(27)])
    assert row(first["result"])["state"] == "not-entered"
    assert row(second["result"])["transition"] == "entered"
    original = row(second["result"])["origin_event"]
    assert row(third["result"])["origin_event"] == original
    assert row(third["result"])["transition"] == "continuing"
    assert third["result"]["events"][original] == second["result"]["events"][original]
    restored = MarketDiscovery(storage[1](), REGISTRY).read(third["reference"])
    assert restored["view_sha256"] == third["result"]["view_sha256"]
    assert restored["stored_at"] is None and restored["verification"] == "recomputed-from-retained-sources"
    assert restored["reference"] == third["reference"]


def test_same_save_replays_journal_same_session_has_no_duplicate_entry(storage):
    first, second = save_sequence(storage, [rising(25), rising(26)])
    service = MarketDiscovery(storage[0], REGISTRY)
    before = storage[0].backend.head()
    again = service.save("discovery-sequence-1", second["result"]["plan"])
    assert again["reference"] == second["reference"] and storage[0].backend.head() == before
    repeated = service.build({**second["result"]["plan"], "previous": second["reference"]})
    assert row(repeated)["transition"] == "unchanged-session"
    assert row(repeated)["origin_event"] == row(second["result"])["origin_event"]
    assert repeated["events"] == second["result"]["events"]


def test_exit_keeps_original_entry_proof_and_missing_data_does_not_exit(storage):
    first, second, third = save_sequence(storage, [rising(25), rising(26), rising(26) + [50]])
    assert row(third["result"])["state"] == "exited"
    assert row(third["result"])["origin_event"] == row(second["result"])["origin_event"]
    assert third["result"]["events"][row(third["result"])["last_event"]]["type"] == "exit"
    MarketDiscovery(storage[1](), REGISTRY).read(third["reference"])
    repeated = MarketDiscovery(storage[0], REGISTRY).save("exit-same-day", {
        **third["result"]["plan"], "previous": third["reference"], "as_of": "2026-10-19T12:06:00+00:00"})
    assert row(repeated["result"])["state"] == "exited"
    assert row(repeated["result"])["last_event"] == row(third["result"])["last_event"]
    assert repeated["result"]["events"] == third["result"]["events"]
    missing = retain(storage, rising(27), missing=[DAYS[26]])
    document = MarketDiscovery(storage[0], REGISTRY).build(request(missing, previous=second["reference"]))
    assert row(document)["state"] == "unknown" and row(document)["transition"] == "unknown"
    assert row(document)["origin_event"] == row(second["result"])["origin_event"]


@pytest.mark.parametrize("changes,expected", [
    ({"values": rising(29) + [170]}, "historical-prices-changed"),
    ({"values": rising(31), "name": "合成更名分类"}, "contract-changed"),
    ({"values": rising(32)}, "unobserved-session-gap"),
])
def test_same_day_correction_identity_change_and_gap_are_rebaselines(storage, changes, expected):
    first = save_sequence(storage, [rising(30)])[0]
    kwargs = dict(changes)
    ref = retain(storage, kwargs.pop("values"), **kwargs)
    result = MarketDiscovery(storage[0], REGISTRY).save("changed-baseline", request(ref, previous=first["reference"]))
    assert row(result["result"])["comparison"] == expected
    assert row(result["result"])["transition"] == "baseline"
    assert result["result"]["events"][row(result["result"])["origin_event"]]["type"] == "baseline"
    assert MarketDiscovery(storage[1](), REGISTRY).read(first["reference"])["view_sha256"] == first["result"]["view_sha256"]


def test_independent_sector_concept_failure_cannot_gate_valid_peer(storage):
    sector = retain(storage, rising(30))
    concept = retain(storage, rising(30), kind="concept")
    plan = request(sector)
    plan["surfaces"]["concept"] = {"reference": concept}
    both = MarketDiscovery(storage[0], REGISTRY).build(plan)
    assert both["surfaces"]["sector"]["qualified_count"] == both["surfaces"]["concept"]["qualified_count"] == 1
    plan["surfaces"]["sector"] = {"reference": concept}
    mixed = MarketDiscovery(storage[0], REGISTRY).build(plan)
    assert mixed["surfaces"]["sector"]["status"] == "unavailable"
    assert mixed["surfaces"]["concept"]["qualified_count"] == 1


@pytest.mark.parametrize("field,value", [("state", "exited"), ("metrics", {"change_20_pct": "999"}),
                                         ("name", "forged source name"), ("origin_event", "0" * 64)])
def test_rehashed_saved_state_tampering_is_rejected_by_source_replay(storage, field, value):
    saved = save_sequence(storage, [rising(30)])[0]
    doc = deepcopy(saved["result"])
    row(doc)[field] = value
    reference = put_document(storage[0], doc)
    with pytest.raises(RecordError, match="DISCOVERY_"):
        MarketDiscovery(storage[1](), REGISTRY).read(reference)


def test_rehashed_original_event_and_source_policy_tampering_are_rejected(storage):
    saved = save_sequence(storage, [rising(25), rising(26), rising(27)])[-1]
    doc = deepcopy(saved["result"])
    original = row(doc)["origin_event"]
    event = doc["events"].pop(original)
    event["metrics"]["change_20_pct"] = "999"
    event["event_id"] = digest(encoded({k: v for k, v in event.items() if k != "event_id"}))
    doc["events"][event["event_id"]] = event
    for item in doc["surfaces"]["sector"]["states"]:
        for key in ("origin_event", "last_event"):
            if item[key] == original:
                item[key] = event["event_id"]
    with pytest.raises(RecordError, match="DISCOVERY_EVENT_REPLAY_MISMATCH"):
        MarketDiscovery(storage[1](), REGISTRY).read(put_document(storage[0], doc))


@pytest.mark.parametrize("damage,reason", [
    ("removed", "DISCOVERY_STATE_EVENT_REQUIRED"),
    ("earlier-valid-baseline", "DISCOVERY_PREVIOUS_REPLAY_MISMATCH"),
])
def test_successor_cannot_launder_invalid_predecessor_original_events(storage, damage, reason):
    first, _, third = save_sequence(storage, [rising(25), rising(26), rising(27)])
    changed = deepcopy(third["result"])
    current = {surface["proof"] for surface in changed["surfaces"].values() if "proof" in surface}
    changed["proofs"] = {key: proof for key, proof in changed["proofs"].items() if key in current}
    if damage == "removed":
        row(changed)["origin_event"] = row(changed)["last_event"] = None
        changed["events"] = {}
    else:
        # Both links and every original proof are valid in isolation, but this
        # older baseline is not the entry carried by the predecessor's edge.
        row(changed)["origin_event"] = row(first["result"])["origin_event"]
        row(changed)["last_event"] = row(first["result"])["last_event"]
        changed["events"] = deepcopy(first["result"]["events"])
        changed["proofs"].update(deepcopy(first["result"]["proofs"]))
    corrupt = put_document(storage[0], changed)
    with pytest.raises(RecordError, match="DISCOVERY_"):
        MarketDiscovery(storage[1](), REGISTRY).read(corrupt)
    reference = retain(storage, rising(28))
    before = storage[0].backend.head()
    with pytest.raises(RecordError, match=reason):
        MarketDiscovery(storage[0], REGISTRY).save("child-after-damaged-events",
            request(reference, previous=asdict(corrupt)))
    assert storage[0].backend.head() == before


def test_predecessor_replay_remains_bounded_to_its_own_direct_edge(storage, monkeypatch):
    saved = save_sequence(storage, [rising(n) for n in range(25, 30)])
    source = retain(storage, rising(30))
    decoded, original = [], MarketDiscovery._decode
    def observed(self, reference):
        decoded.append(reference["path"])
        return original(self, reference)
    monkeypatch.setattr(MarketDiscovery, "_decode", observed)
    document = MarketDiscovery(storage[0], REGISTRY).build(request(source, previous=saved[-1]["reference"]))
    assert decoded == [saved[-1]["reference"]["path"], saved[-2]["reference"]["path"]]
    assert row(document)["origin_event"] == row(saved[1]["result"])["origin_event"]


def test_proof_source_loss_is_not_silently_replaced_by_current_source(storage):
    saved = save_sequence(storage, [rising(25), rising(26), rising(27)])[-1]
    origin = saved["result"]["events"][row(saved["result"])["origin_event"]]
    proof = saved["result"]["proofs"][origin["previous_proof"]]
    ref = SourceRef.from_dict(proof["plan"]["reference"])
    storage[0].backend.shared["missing"].add((ref.manifest.commit, ref.manifest.path))
    with pytest.raises(RecordError, match="DISCOVERY_PROOF_REPLAY_MISMATCH"):
        MarketDiscovery(storage[1](), REGISTRY).read(saved["reference"])


def test_input_only_accepts_reference_and_explicit_policy_not_caller_price_or_state(storage):
    ref = retain(storage, rising(30))
    for change in ({"policy": "magic-return"}, {"states": []}, {"as_of": "2026-10-19"}):
        with pytest.raises(RecordError):
            plan_input({**request(ref), **change})
    plan = request(ref)
    plan["surfaces"]["sector"]["prices"] = [100, 200]
    with pytest.raises(RecordError, match="DISCOVERY_SURFACE_PLAN_INVALID"):
        plan_input(plan)


def test_daily_stream_cannot_take_manual_predecessor(storage):
    saved = save_sequence(storage, [rising(30)])[0]
    plan = {**saved["result"]["plan"], "stream": "daily-market", "previous": saved["reference"]}
    with pytest.raises(RecordError, match="DISCOVERY_PREVIOUS_STREAM_CHANGED"):
        MarketDiscovery(storage[0], REGISTRY).build(plan)
    daily = MarketDiscovery(storage[0], REGISTRY).build({**plan, "previous": None})
    assert artifact_id(saved["result"]) != artifact_id(daily)


def test_artifact_capacity_refuses_whole_output_before_writing(storage, monkeypatch):
    ref = retain(storage, rising(30))
    before = storage[0].backend.head()
    monkeypatch.setattr("decision_kernel.application.market_discovery.MAX_ARTIFACT_BYTES", 1024)
    with pytest.raises(RecordError, match="DISCOVERY_ARTIFACT_BOUND"):
        MarketDiscovery(storage[0], REGISTRY).save("too-large", request(ref))
    assert storage[0].backend.head() == before


def test_submission_content_conflict_preserves_first_artifact(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    first = service.save("fixed-submission", request(retain(storage, rising(30))))
    newer = request(retain(storage, rising(31)), previous=first["reference"])
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        service.save("fixed-submission", newer)
    assert MarketDiscovery(storage[1](), REGISTRY).read(first["reference"])["view_sha256"] == first["result"]["view_sha256"]


def test_real_git_artifact_is_independently_replayed_after_original_inputs_are_gone(tmp_path):
    root = tmp_path / "records.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    storage = store, lambda: VersionedStore(GitStore(root))
    first, second, third = save_sequence(storage, [rising(25), rising(26), rising(27)])
    del first, second, store, storage
    restored = MarketDiscovery(VersionedStore(GitStore(root)), REGISTRY).read(third["reference"])
    assert restored["view_sha256"] == third["result"]["view_sha256"]
    assert restored["plan"]["previous"] is not None
    assert row(restored)["transition"] == "continuing"


def test_retained_ftshare_preserves_master_quarantine_and_is_independent_of_ths():
    from test_ftshare_collection import adopt_kernel, kernel_adoption_fixture

    fixture = kernel_adoption_fixture()
    retained = adopt_kernel(fixture)
    plan = {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
        "surfaces": {"stock": {"reference": retained["reference"], "adjustment": "raw"},
                     "sector": {"reference": retained["reference"]}}}
    before = fixture["store"].backend.head()
    output = MarketDiscovery(fixture["store"], REGISTRY).build(plan)
    stock = output["surfaces"]["stock"]
    assert stock["status"] == "readable"
    assert stock["scope_count"] == len(fixture["context"]["scope_symbols"])
    assert {r["entity_id"] for r in stock["states"]} == {"000001.SZ", "600000.SH"}
    assert any(r["reason_code"] == "DISCOVERY_REFERENCE_QUARANTINED" for r in stock["gaps"])
    assert output["surfaces"]["sector"]["status"] == "unavailable"
    assert all(source["wire_sha256"] for source in stock["sources"].values())
    assert fixture["store"].backend.head() == before and output["source_requests"] == 0
    saved = MarketDiscovery(fixture["store"], REGISTRY).save("ft-native-state", plan)
    restored = MarketDiscovery(fixture["fresh"](), REGISTRY).read(saved["reference"])
    assert restored["view_sha256"] == saved["result"]["view_sha256"]


def test_native_github_store_and_source_only_snapshot_share_budget_without_artifact_bypass(tmp_path):
    from copy import copy
    from dataclasses import replace
    from decision_kernel.infrastructure.github_store import GitHubStore
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader
    from decision_kernel.domain.research_artifacts import artifact_reference
    from test_github_store import GitAPI
    from test_market_snapshot import local_fetch
    from test_news_read_budget import transport

    model = GitAPI(tmp_path / "native.git")
    model.binding = replace(model.binding, branch="records")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository, model.git.binding = model.binding.repository, model.binding
    seed = VersionedStore(model.git)
    storage = seed, lambda: VersionedStore(copy(model.git))
    first = save_sequence(storage, [rising(25)])[0]
    next_source = retain(storage, rising(26))
    api, fetches = transport(model), []
    source_store = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    writer = VersionedStore(GitHubStore(model.binding, api))
    saved = MarketDiscovery(writer, REGISTRY, source_store=source_store).save(
        "native-entry", request(next_source, previous=first["reference"]))
    assert row(saved["result"])["transition"] == "entered"
    assert model.writes == 1 and len(fetches) == 2
    assert all(fetch["api"] is api for fetch in fetches)
    assert 0 < api._requests < 512
    # Passing the source-only reader is supported, but its artifact ACL remains
    # unchanged: artifact resolution uses the same native readonly binding/API.
    reopened = MarketDiscovery(source_store, REGISTRY).read(saved["reference"])
    assert reopened["view_sha256"] == saved["result"]["view_sha256"]
    assert len(fetches) == 3 and api._requests < 512
    with pytest.raises(RecordError, match="MARKET_SNAPSHOT_SOURCE_ONLY"):
        source_store.backend.resolve(artifact_reference(saved["reference"]))


def test_unavailable_current_catalog_cannot_claim_scope_departures(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    first = service.save("known-sector-scope", request(retain(storage, rising(30))))
    other_kind = retain(storage, rising(31), kind="concept")
    document = service.build(request(other_kind, previous=first["reference"]))
    surface = document["surfaces"]["sector"]
    assert surface["status"] == "unavailable" and surface["scope_count"] is None
    assert surface["departed_scope"] is None and surface["unknown_count"] is None
    assert MarketDiscovery(storage[1](), REGISTRY).read(first["reference"])["events"] == first["result"]["events"]



