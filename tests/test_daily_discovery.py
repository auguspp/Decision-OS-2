"""The actual daily FT custody plus finite immutable discovery chain failures."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from decision_kernel.application import daily_discovery as daily
from decision_kernel.application.market_discovery import MarketDiscovery, artifact_id
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.interfaces import daily_discovery_cli as cli, daily_reading_cli
from test_daily_market import Host, NOW
from test_daily_reading import native_run
from test_market_discovery import REGISTRY, request, retain, rising, row, storage, put_document
from test_market_snapshot import native


def source_count(host):
    return sum(client.requests for client in host.calendar_clients + host.price_clients)


def save_host(host, result, *, name="daily-observation", at=NOW):
    return daily.save_daily_discovery(host.fixture["store"], host.fixture["fresh"](), REGISTRY,
        host.approved, result["receipt"], as_of=at, submission_id=name,
        expected_reference=result["control"]["market_reference"])


def test_actual_daily_ft_update_saves_state_then_same_ft_version_is_read_only():
    host = Host()
    result = host.run()
    assert result["receipt"]["status"] == "complete"
    count = source_count(host)
    saved = save_host(host, result)
    assert saved["status"] == "stored-and-verified" and saved["records_writes"] == 1
    assert saved["source_requests"] == 0 and source_count(host) == count
    restored = MarketDiscovery(host.fixture["fresh"](), REGISTRY).read(saved["reference"])
    assert restored["plan"]["stream"] == "daily-market"
    assert restored["surfaces"]["stock"]["scope_count"] == 5  # Complete retained master includes two quarantines.
    assert restored["surfaces"]["sector"]["status"] == "not-supplied"
    head = host.fixture["store"].backend.head()
    repeated = save_host(host, result, name="another-run", at="2026-10-19T12:20:00+00:00")
    assert repeated["status"] == "already-recorded" and repeated["records_writes"] == 0
    assert repeated["reference"]["path"] == saved["reference"]["path"]
    assert host.fixture["store"].backend.head() == head and source_count(host) == count
    selected = daily.reading_discovery(host.fixture["store"], host.fixture["fresh"](), REGISTRY,
        result["control"]["market_reference"], as_of=NOW)
    assert selected["status"] == "saved" and "reference" in selected["panel"]


def test_daily_update_carries_exact_industry_concept_dates_and_original_events():
    host = Host()
    state_storage = host.fixture["store"], host.fixture["fresh"]
    sector, concept = retain(state_storage, rising(30)), retain(state_storage, rising(30), kind="concept")
    plan = request(sector, stream="daily-market")
    plan["surfaces"].update(concept={"reference": concept},
        stock={"reference": host.approved["seed_reference"], "adjustment": "forward"})
    seed = MarketDiscovery(state_storage[0], REGISTRY).save("seed-three-surfaces", plan)
    result = host.run()
    saved = save_host(host, result)
    doc = MarketDiscovery(state_storage[1](), REGISTRY).read(saved["reference"])
    for kind, reference in (("sector", sector), ("concept", concept)):
        assert encoded(doc["plan"]["surfaces"][kind]["reference"]) == encoded(reference)
        assert doc["surfaces"][kind]["as_of"] == seed["result"]["surfaces"][kind]["as_of"]
        assert row(doc, kind)["origin_event"] == row(seed["result"], kind)["origin_event"]
        assert row(doc, kind)["transition"] == "unchanged-session"
    assert doc["surfaces"]["stock"]["as_of"] == "2026-10-19"


def test_failed_source_cycle_preserves_saved_state_and_current_unknown_preview():
    host = Host()
    first = save_host(host, host.run())
    host.now = "2026-10-20T12:10:00+00:00"
    host.price_error = RecordError("FTSHARE_HTTP_403")
    failed = host.run()
    assert failed["receipt"]["status"] == "failed" and failed["receipt"]["reason_code"] == "FTSHARE_HTTP_403"
    before = host.fixture["store"].backend.head()
    count = source_count(host)
    skipped = save_host(host, failed, name="held-update", at=host.now)
    assert skipped["status"] == "not-updated" and skipped["records_writes"] == 0
    assert host.fixture["store"].backend.head() == before and source_count(host) == count
    reading = daily.reading_discovery(host.fixture["store"], host.fixture["fresh"](), REGISTRY,
        failed["control"]["market_reference"], as_of=host.now)
    assert reading["status"] == "preview" and reading["persistence"] == "not-written"
    assert reading["saved_reference"]["path"] == first["reference"]["path"]
    doc = MarketDiscovery(host.fixture["fresh"](), REGISTRY).build(reading["panel"]["build"])
    assert doc["surfaces"]["stock"]["source_status"] == "source-hold"
    assert all(item["state"] == "unknown" for item in doc["surfaces"]["stock"]["states"])


def test_success_receipt_cannot_replace_failed_actual_ft_input(monkeypatch):
    host = Host()
    result = host.run()
    wrong = retain((host.fixture["store"], host.fixture["fresh"]), rising(30))
    original = daily.daily_market_reference
    def unrelated(*args):
        value = original(*args)
        return {**value, "reference": wrong}
    monkeypatch.setattr(daily, "daily_market_reference", unrelated)
    head = host.fixture["store"].backend.head()
    with pytest.raises(RecordError, match="CURRENT_FTSHARE_UNAVAILABLE"):
        daily.save_daily_discovery(host.fixture["store"], host.fixture["fresh"](), REGISTRY,
            host.approved, result["receipt"], as_of=NOW, submission_id="forged-green")
    assert host.fixture["store"].backend.head() == head


def test_changed_runtime_cycle_or_source_rejects_before_state_write():
    host = Host()
    result = host.run()
    before = host.fixture["store"].backend.head()
    altered = deepcopy(result)
    altered["receipt"]["cycle_date"] = "20261018"
    with pytest.raises(RecordError, match="RUNTIME_CYCLE_CHANGED"):
        save_host(host, altered)
    altered = deepcopy(result)
    altered["control"]["market_reference"] = host.approved["seed_reference"]
    with pytest.raises(RecordError, match="RUNTIME_SOURCE_CHANGED"):
        save_host(host, altered)
    assert host.fixture["store"].backend.head() == before


def test_read_only_status_never_advances_state_even_with_a_good_current_source():
    host = Host()
    result = host.run()
    read = deepcopy(result)
    read["receipt"].update(operation="status", status="read")
    before = host.fixture["store"].backend.head()
    assert save_host(host, read)["status"] == "not-updated"
    preview = daily.reading_discovery(host.fixture["store"], host.fixture["fresh"](), REGISTRY,
        result["control"]["market_reference"], as_of=NOW)
    assert preview["status"] == "preview" and preview["panel"]["build"]["previous"] is None
    assert host.fixture["store"].backend.head() == before


def test_daily_chain_is_separate_from_manual_artifacts_and_rejects_multiple_roots(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    reference = retain(storage, rising(30))
    service.save("manual-root", request(reference))
    first = service.save("daily-root", request(reference, stream="daily-market"))
    selected = daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert selected["status"] == "selected" and selected["stream_candidates"] == 1
    assert selected["reference"]["path"] == first["reference"]["path"]
    service.save("second-daily-root", request(reference, stream="daily-market",
        as_of="2026-10-19T12:06:00+00:00"))
    failed = daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert failed["status"] == "unavailable" and failed["reason_code"] == "DAILY_DISCOVERY_HISTORY_FORK"
    with pytest.raises(RecordError, match="HISTORY_FORK"):
        daily.reading_discovery(storage[0], storage[1](), REGISTRY, None, as_of=NOW)


def test_child_fork_and_missing_predecessor_never_choose_newest_good_date(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    first = service.save("root", request(retain(storage, rising(30)), stream="daily-market"))
    reference = retain(storage, rising(31))
    service.save("child-a", request(reference, previous=first["reference"], stream="daily-market"))
    service.save("child-b", request(reference, previous=first["reference"], stream="daily-market",
        as_of="2026-10-19T12:06:00+00:00"))
    assert daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)["reason_code"] == "DAILY_DISCOVERY_HISTORY_FORK"
    del storage[0].backend.shared["trees"][storage[0].backend.head()][first["reference"]["path"]]
    assert daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)["reason_code"] == "DAILY_DISCOVERY_PREDECESSOR_MISSING"


def test_damaged_candidate_cannot_silently_disappear_as_another_stream(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    ref = retain(storage, rising(30))
    service.save("daily-root", request(ref, stream="daily-market"))
    manual = service.save("manual-root", request(ref))
    head = storage[0].backend.head()
    path = manual["reference"]["path"].rsplit("/", 1)[0] + "/content.bin"
    storage[0].backend.shared["trees"][head][path] = b"bad bytes"
    result = daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert result["reason_code"] == "DAILY_DISCOVERY_INCOMPLETE" and result["reference"] is None
    assert len(result["failures"]) == 1


@pytest.mark.parametrize("reason", ["REMOTE_REQUEST_LIMIT", "REMOTE_RATE_LIMIT"])
def test_global_inventory_failure_stops_at_first_candidate_without_fallback(storage, monkeypatch, reason):
    service = MarketDiscovery(storage[0], REGISTRY)
    ref = retain(storage, rising(30))
    service.save("daily-root", request(ref, stream="daily-market"))
    service.save("manual-root", request(ref))
    attempted = []
    def unavailable(self, reference):
        attempted.append(reference["path"])
        raise StoreUnavailable(reason)
    monkeypatch.setattr(MarketDiscovery, "_decode", unavailable)
    before = storage[0].backend.head()
    with pytest.raises(StoreUnavailable, match=reason):
        daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert len(attempted) == 1 and storage[0].backend.head() == before


def test_selected_rehashed_state_still_requires_original_source_replay(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    document = service.build(request(retain(storage, rising(30)), stream="daily-market"))
    row(document)["metrics"]["change_20_pct"] = "999"
    put_document(storage[0], document)
    assert daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)["status"] == "selected"
    with pytest.raises(RecordError, match="REPLAY_MISMATCH"):
        daily.reading_discovery(storage[0], storage[1](), REGISTRY, None, as_of=NOW)


def test_whole_inventory_bound_is_checked_before_any_candidate_can_be_selected(storage):
    backend = storage[0].backend
    tree = backend.shared["trees"][backend.head()]
    for index in range(129):
        tree[f"research/artifacts/market-discovery-{index:03d}/" + "a" * 64 + "/manifest.json"] = b"unread"
    result = daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert daily.MAX_ARTIFACTS == 128 and result["total_candidates"] == 129 and result["scanned"] == 0
    assert result["reason_code"] == "DAILY_DISCOVERY_INVENTORY_BOUND" and result["reference"] is None


def test_concurrent_records_change_aborts_state_at_existing_artifact_cas(monkeypatch):
    host = Host()
    result = host.run()
    original = MarketDiscovery.save
    changed = []
    def intervening(self, submission_id, plan, **kwargs):
        artifact = ArtifactInput.from_bytes("another-research-note", "note.txt", "text/plain", b"concurrent note")
        changed.append(ResearchArtifacts(self.store).put("concurrent-note", artifact))
        return original(self, submission_id, plan, **kwargs)
    monkeypatch.setattr(MarketDiscovery, "save", intervening)
    with pytest.raises(RecordError, match="HEAD_CHANGED"):
        save_host(host, result)
    assert len(changed) == 1
    assert daily.discover_latest(host.fixture["store"], registry=REGISTRY, as_of=NOW)["status"] == "no-saved-state"


def test_real_daily_reading_exports_exact_saved_state_and_actual_three_scope_counts():
    host = Host()
    result = host.run()
    saved = save_host(host, result)
    model = daily_reading_cli.build_reading(host.fixture["store"], host.fixture["fresh"](),
        REGISTRY, host.approved, result["receipt"], as_of=NOW)
    assert model["discovery"]["status"] == "readable", model["discovery"]
    assert model["discovery"]["reference"]["path"] == saved["reference"]["path"]
    assert set(model["discovery"]["view"]["surfaces"]) == {"sector", "concept", "stock"}
    summary = daily_reading_cli.summary_markdown(model)
    assert "观察状态：读取已存原始版本" in summary and "行业观察：尚无已存输入" in summary
    assert "独立个股观察实际截止" in summary


def test_workflow_state_write_stays_inside_existing_daily_writer_without_source_credentials():
    workflow = yaml.load(Path(".github/workflows/daily-market.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {} and workflow["concurrency"]["group"] == "decision-os-relay-source"
    assert not any(cli.__name__ in step.get("run", "") for step in workflow["jobs"]["status"]["steps"])
    job = workflow["jobs"]["daily"]
    steps = job["steps"]
    indices = {name: next(i for i, step in enumerate(steps) if module in step.get("run", ""))
        for name, module in (("source", "daily_market_cli runtime"), ("state", cli.__name__),
                             ("reading", "daily_reading_cli"), ("release", "daily_market_cli release"))}
    assert list(indices.values()) == sorted(indices.values())
    assert job["permissions"] == {"contents": "write", "actions": "read"}
    step = steps[indices["state"]]
    assert step["if"] == "always()" and step["env"] == {"GITHUB_TOKEN": "$" + "{{ github.token }}"}
    assert "secrets." not in step["run"]


def test_native_state_host_retains_identity_and_source_receipt_and_calls_no_provider(tmp_path, monkeypatch):
    event, env, out, api, readers, calls = native_run(tmp_path, monkeypatch, status="source-held")
    monkeypatch.setattr(cli, "_open_writer", lambda current: readers)
    original = (out / "result.json").read_bytes()
    assert cli.run_runtime(event, env, out, api=api, clock=lambda: NOW) == 0
    saved = cli._json((out / cli.OUTPUT).read_bytes())
    assert saved["status"] == "not-updated" and saved["records_writes"] == 0
    assert saved["source_receipt_sha256"] == digest(original)
    assert saved["run_id"] == 8001 and saved["source_requests"] == 0
    assert (out / "result.json").read_bytes() == original and not calls
    assert api.calls.count("/repos/auguspp/Decision-OS-2") == 2


def test_native_state_host_rejects_copied_receipt_before_any_read_or_write(tmp_path, monkeypatch):
    event, env, out, api, _, calls = native_run(tmp_path, monkeypatch, run_id=9999)
    with pytest.raises(RecordError, match="RUNTIME_MISMATCH"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: NOW)
    assert not api.calls and not calls and not (out / cli.OUTPUT).exists()


def test_native_state_output_is_withheld_after_private_visibility_changes(tmp_path, monkeypatch):
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch, status="source-held")
    def changed(*args):
        api.meta["private"] = False
        return object(), object()
    monkeypatch.setattr(cli, "_open_writer", changed)
    with pytest.raises(RecordError, match="PRIVATE_REPOSITORY"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: NOW)
    assert not (out / cli.OUTPUT).exists()


def test_native_writer_and_source_reader_share_original_binding_anchor_api(monkeypatch):
    opened = []
    def backend(binding, api):
        opened.append((binding, api))
        return object()
    monkeypatch.setattr(cli, "GitHubStore", backend)
    monkeypatch.setattr(cli, "GitMarketReader", backend)
    api = object()
    cli._open_writer(api)
    assert len(opened) == 2 and opened[0][0].write_enabled is True and opened[1][0].write_enabled is False
    assert opened[0][0].anchor == opened[1][0].anchor and opened[0][0].repository == opened[1][0].repository
    assert all(item[1] is api for item in opened)


def test_status_operation_cannot_invoke_native_state_writer(tmp_path, monkeypatch):
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch)
    event["inputs"]["operation"] = "status"
    with pytest.raises(RecordError, match="WRITE_OPERATION_REQUIRED"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: NOW)
    assert not api.calls and not (out / cli.OUTPUT).exists()


def test_native_daily_write_inventory_and_exact_read_keep_one_api_budget(native):
    from copy import copy
    from dataclasses import replace
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader
    from test_market_snapshot import local_fetch
    from test_news_read_budget import transport

    model, seed = native
    fresh = lambda: VersionedStore(copy(model.git))
    host = Host(storage=(model.git, seed, fresh))
    result = host.run()
    count = source_count(host)
    api, fetches = transport(model), []
    writer = VersionedStore(GitHubStore(model.binding, api))
    sources = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    saved = daily.save_daily_discovery(writer, sources, REGISTRY, host.approved, result["receipt"],
        as_of=NOW, submission_id="native-daily-state",
        expected_reference=result["control"]["market_reference"])
    assert saved["status"] == "stored-and-verified" and model.writes == 1
    selected = daily.reading_discovery(writer, sources, REGISTRY,
        result["control"]["market_reference"], as_of=NOW)
    assert selected["status"] == "saved" and selected["saved_reference"]["path"] == saved["reference"]["path"]
    assert 1 < len(fetches) <= 6 and all(item["api"] is api for item in fetches)
    assert source_count(host) == count and api._requests == len(model.requests) < 512


def test_native_full_128_candidate_inventory_and_selected_replay_keep_original_budget(native):
    from copy import copy
    from dataclasses import replace
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader
    from test_market_snapshot import local_fetch
    from test_news_read_budget import transport

    model, store = native
    reference = retain((store, lambda: VersionedStore(copy(model.git))), rising(30))
    baseline = MarketDiscovery(store, REGISTRY).build(request(reference))
    selected = None
    # Actual original Artifact manifests/journal on native Git, not a fake
    # GitHubStore, constant-return resolver or enlarged cache/request budget.
    for index in range(128):
        document = deepcopy(baseline)
        document["plan"]["stream"] = "daily-market" if index == 0 else f"manual-{index}"
        document["view_sha256"] = digest(encoded({k: v for k, v in document.items() if k != "view_sha256"}))
        artifact = ArtifactInput.from_bytes(artifact_id(document), "market-discovery.json",
                                           "application/json", encoded(document))
        retained = ResearchArtifacts(store).put(f"native-inventory-{index}", artifact)
        if index == 0:
            selected = retained
    head = model.git.head()
    model.requests.clear()
    api, fetches = transport(model), []
    reader = VersionedStore(GitHubStore(model.binding, api))
    inventory = daily.discover_latest(reader, registry=REGISTRY, as_of=NOW)
    assert inventory["complete"] and inventory["status"] == "selected"
    assert inventory["total_candidates"] == inventory["scanned"] == 128
    assert inventory["stream_candidates"] == 1 and inventory["failures"] == []
    assert inventory["reference"]["path"] == selected.path and inventory["snapshot"] == head
    # Original backend initialization, scope admission and final scope release.
    assert sum(path == f"/repos/{model.binding.repository}" for _, path, _ in model.requests) == 3
    sources = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    restored = MarketDiscovery(reader, REGISTRY, source_store=sources).read(inventory["reference"])
    assert restored["verification"] == "recomputed-from-retained-sources"
    assert row(restored)["origin_event"] == row(baseline)["origin_event"]
    assert MAX_REQUESTS == 512 and api._requests == len(model.requests) < 512
    assert model.git.head() == head and model.writes == 0 and len(fetches) == 1
    assert all(item["api"] is api for item in fetches)


def test_native_inventory_withholds_selection_after_final_private_visibility_change(native, monkeypatch):
    from copy import copy
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    from test_news_read_budget import transport

    model, store = native
    ref = retain((store, lambda: VersionedStore(copy(model.git))), rising(30))
    MarketDiscovery(store, REGISTRY).save("private-daily-root", request(ref, stream="daily-market"))
    original = MarketDiscovery._decode
    def changed(self, reference):
        document = original(self, reference)
        model.meta["private"] = False
        return document
    monkeypatch.setattr(MarketDiscovery, "_decode", changed)
    reader = VersionedStore(GitHubStore(model.binding, transport(model)))
    with pytest.raises(RecordError, match="PRIVATE"):
        daily.discover_latest(reader, registry=REGISTRY, as_of=NOW)
    assert model.writes == 0


def test_daily_inventory_rejects_future_declared_saved_clock(storage):
    service = MarketDiscovery(storage[0], REGISTRY)
    service.save("future-state", request(retain(storage, rising(30)), stream="daily-market",
        as_of="2026-10-20T12:05:00+00:00"))
    found = daily.discover_latest(storage[0], registry=REGISTRY, as_of=NOW)
    assert found["reference"] is None and found["reason_code"] == "DAILY_DISCOVERY_FUTURE_STATE"


def test_native_success_rechecks_eligibility_immediately_before_existing_cas(tmp_path, monkeypatch):
    from decision_kernel.infrastructure.github_store import GitHubRateLimits
    from decision_kernel.interfaces.daily_market_cli import settings

    host = Host()
    result = host.run()
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch, status="complete")
    approved = tmp_path / "daily-policy.json"
    approved.write_bytes(encoded(host.approved))
    monkeypatch.setattr(cli, "POLICY_FILE", approved)
    selected = settings(event, env, host.approved)
    (out / "result.json").write_bytes(encoded({**result["receipt"], **selected}))
    (out / "control.json").write_bytes(encoded(result["control"]))
    api._requests = 4
    api.rate_limits = GitHubRateLimits()
    api.rate_limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-remaining": "900",
        "x-ratelimit-limit": "1000", "x-ratelimit-reset": "9999999999"})
    monkeypatch.setattr(cli, "_open_writer", lambda current: (host.fixture["store"], host.fixture["fresh"]()))
    assert cli.run_runtime(event, env, out, api=api, clock=lambda: NOW) == 0
    saved = cli._json((out / cli.OUTPUT).read_bytes())
    assert saved["status"] == "stored-and-verified" and saved["records_writes"] == 1
    assert api.calls.count("/repos/auguspp/Decision-OS-2") == 3
    assert MarketDiscovery(host.fixture["fresh"](), REGISTRY).read(saved["reference"])["plan"]["stream"] == "daily-market"


