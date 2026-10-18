"""Ordinary custody CLI and reading-room consumers of retained day collections.

Source responses are synthetic. Exact SourceRefs, query restoration and the
native Git subprocess are real; no live source or retention-rights claim.
"""
from dataclasses import asdict, replace
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_kernel.application import reading_market as consumer
from decision_kernel.application.market_collection import _slots, plan_collection, unit_request
from decision_kernel.application.market_collection_custody import retain_market_collection
from decision_kernel.application.market_collection_reading import read_collection_version
from decision_kernel.application.market_collection_runtime import health_receipt
from decision_kernel.application.market_custody import retain_market_query
from decision_kernel.application.market_relay_inputs import qualified_trade_calendar
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_sources import capture_registered
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, SourceInput, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.interfaces import market_custody_cli
from decision_kernel.interfaces.reading_page import render_page
from test_market_collection_inputs import CollectionOpener
from test_market_collection_sync import NOW, START, END, http_error, memory, plan, run
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_snapshot import native, local_fetch
from test_news_read_budget import transport


@pytest.fixture
def saved(monkeypatch):
    _, store, fresh = memory()
    original = run(plan(), store, fresh, CollectionOpener(start=START, end=END))
    monkeypatch.setattr(consumer, "datetime", SimpleNamespace(now=lambda zone: NOW))
    return store, fresh, original


def room(store, reference, **page):
    return ReadingRoom(store, market_registry=REGISTRY).build({"as_of": NOW.isoformat(),
        "market": {"reference": reference, **page}})


def seed_native_day(store, fresh, *, start=END, missing_slots=()):
    """Retain original query envelopes once; sync/control is exercised elsewhere."""
    p = plan_collection(collection_id="synthetic-native-consumer", generation="g1",
        start=start, end=END, calendar_end=END, registry=REGISTRY)
    opener = CollectionOpener(start=start, end=END)
    session = RetryingRelay(RelayHTTP(opener=opener, credential="synthetic-key-only"),
                            clock=lambda: NOW.isoformat())
    units, dates = {}, set()

    def retain(slot):
        use, max_pages = unit_request(p, slot, REGISTRY)
        capture = capture_registered(use, REGISTRY, session, max_pages=max_pages)
        assert capture["complete"]
        result = {"case_id": p["collection_id"], "use": use, "status": "captured", "capture": capture}
        kept = retain_market_query(result, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
        units[slot] = {"query": kept["reference"], "checkpoint": None,
                       "status": "complete", "reason_code": None}
        if slot.startswith("calendar:"):
            dates.update(d.replace("-", "") for d in qualified_trade_calendar(result,
                registry=REGISTRY, exchange=slot[9:], start=start, end=END))

    for slot in _slots(p):
        retain(slot)
    p["sessions"] = sorted(dates)
    for slot in _slots(p):
        if slot not in units:
            if slot in missing_slots:
                units[slot] = {"query": None, "checkpoint": None, "status": "pending", "reason_code": None}
            else:
                retain(slot)
    value = {"plan": p, "units": units, "last_qualified": None,
        "attempt": {"id": "synthetic-native-retention", "status": "partial" if missing_slots else "complete", "reason_code": None,
                    "started_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "source_requests": len(opener.calls)}}
    return retain_market_collection(value, REGISTRY, store, fresh_reader=fresh,
                                    custody_authorized=True)["reference"]


def test_room_uses_original_collection_reader_and_pins_pages_to_the_input_root(saved):
    store, fresh, original = saved
    head = store.backend.head()
    first = room(store, original["reference"], limit=1)["market"]
    expected = read_collection_version(original["reference"], REGISTRY, fresh(),
                                       read_as_of=NOW.isoformat(), limit=1)
    assert first["status"] == "readable" and encoded(first["view"]) == encoded(expected)
    assert first["partial"]  # The retained week does not create 20/60-session history.
    assert first["next_plan"]["reference"] == original["reference"]
    second = room(fresh(), original["reference"], offset=1, limit=1)["market"]
    for kind in ("raw", "qfq"):
        a, b = first["view"]["view"][kind], second["view"]["view"][kind]
        assert a["scope_count"] == b["scope_count"] == 4
        assert a["horizon_counts"] == b["horizon_counts"]
        assert a["source_version"] == b["source_version"]
        assert a["entries"][0]["entity_id"] != b["entries"][0]["entity_id"]
    assert first["view"]["view"]["view_version"] == second["view"]["view"]["view_version"]
    assert first["view"]["source_requests"] == second["view"]["source_requests"] == 0
    assert store.backend.head() == head


def test_room_unknown_master_scope_stays_unknown_and_keeps_other_venue_values(saved, monkeypatch):
    store, _, original = saved
    missing = original["collection"]["units"]["master:SZSE"]["query"]
    read = store.read_source

    def omit(reference):
        if encoded(asdict(reference)) == encoded(missing):
            raise MissingObject("SYNTHETIC_MISSING_MASTER")
        return read(reference)

    monkeypatch.setattr(store, "read_source", omit)
    result = room(store, original["reference"], entity_id="600000.SH")
    part, view = result["market"], result["market"]["view"]["view"]
    assert part["status"] == "readable" and part["partial"] and result["status"] == "partial"
    assert view["scope_count"] is None and view["known_scope_count"] == 2
    for kind in ("raw", "qfq"):
        assert view[kind]["scope_count"] is None and view[kind]["incomplete_count"] is None
        assert view[kind]["entries"][0]["horizons"][0]["status"] == "observed"
    page = render_page(result)
    assert '"scope_count":null' in page and "market-content" in page
    assert "innerHTML" not in page and "localStorage" not in page


def test_room_blocked_latest_attempt_keeps_previous_values_and_current_staleness_separate(monkeypatch):
    _, store, fresh = memory()
    old = run(plan(end="20261015", through="20261015"), store, fresh,
              CollectionOpener(start=START, end="20261015"))
    newer = CollectionOpener(start=START, end=END)

    def refuse_master(api, params):
        if api == "stock_basic":
            raise http_error(403, error="not_entitled")

    newer.before = refuse_master
    blocked = run(plan(generation="g2"), store, fresh, newer, previous=old["reference"])
    monkeypatch.setattr(consumer, "datetime", SimpleNamespace(now=lambda zone: NOW))
    result = room(fresh(), blocked["reference"], limit=1)
    part, restored = result["market"], result["market"]["view"]
    assert part["status"] == "readable" and part["partial"]
    assert restored["status"] == "restored" and restored["serving"] == "previous-complete"
    assert restored["latest_attempt"]["status"] == "blocked"
    assert restored["selected_reference"] == old["reference"]
    assert restored["reference"] == blocked["reference"] == part["next_plan"]["reference"]
    assert restored["selected_requested_end"] == "20261015" and restored["latest_requested_end"] == END
    assert restored["view"]["freshness"]["status"] == "stale"
    assert all(v["current_session_lag"] == 1 for v in restored["view"]["by_exchange"].values())
    previous = read_collection_version(old["reference"], REGISTRY, fresh(), limit=1)
    assert restored["view"]["raw"] == previous["view"]["raw"]
    assert restored["view"]["qfq"] == previous["view"]["qfq"]
    assert restored["view"]["endpoint_comparisons"] == previous["view"]["endpoint_comparisons"]
    page = render_page(result)
    assert '"serving":"previous-complete"' in page and '"status":"blocked"' in page
    assert '"status":"stale"' in page


def test_room_closed_interval_has_no_market_session_without_a_false_failure(monkeypatch):
    _, store, fresh = memory()
    start, end = "20261010", "20261011"
    saved = run(plan(start=start, end=end, through=end), store, fresh,
                CollectionOpener(start=start, end=end))
    monkeypatch.setattr(consumer, "datetime", SimpleNamespace(now=lambda zone: NOW))
    part = room(fresh(), saved["reference"])["market"]
    assert part["status"] == "readable" and not part["partial"]
    assert part["view"]["view"]["raw"]["status"] == "no-market-sessions"
    assert part["view"]["view"]["scope_count"] == 4
    assert part["view"]["view"]["freshness"]["status"] == "unknown"


def test_collection_owner_prefix_cannot_turn_a_counts_receipt_into_a_reading(saved):
    store = saved[0]
    forged = store.put_source("synthetic-prefix-only", SourceInput(
        "market-collection-" + "a" * 48, "content", "Synthetic counts", "synthetic:counts",
        '{"status":"checked","source_refs_created":false}'))
    with pytest.raises(RecordError, match="CUSTODY_SCHEMA"):
        consumer.read_market_reference(forged, REGISTRY, store)


def test_collection_storage_budget_failure_blocks_the_market_panel(saved, monkeypatch):
    store, _, original = saved
    missing = original["collection"]["units"][END + ":daily"]["query"]
    read, attempts = store.read_source, []

    def fail(reference):
        attempts.append(reference)
        if encoded(asdict(reference)) == encoded(missing):
            raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
        return read(reference)

    monkeypatch.setattr(store, "read_source", fail)
    result = room(store, original["reference"])
    assert result["market"]["status"] == "unavailable" and "view" not in result["market"]
    assert result["status"] == "partial"
    assert encoded(asdict(attempts[-1])) == encoded(missing)


def test_existing_custody_cli_reopens_native_collection_without_capture_files_or_source_key(tmp_path):
    path = tmp_path / "records.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    reference = seed_native_day(store, fresh)
    selected = tmp_path / "reference.json"
    selected.write_bytes(encoded(reference))
    env = {**os.environ, "PYTHONPATH": str(ROOT_DIR / "src")}
    for name in ("TUSHARE_PROXY_API_KEY", "DECISION_OS_STORE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    head = store.backend.head()
    command = [sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli", "read",
        "--path", str(path), "--registry", str(REGISTRY_PATH), "--input", str(selected), "--limit", "1"]
    first = subprocess.run(command, cwd=tmp_path, env=env, check=True, text=True,
                           capture_output=True, timeout=30)
    second = subprocess.run([*command, "--offset", "1"], cwd=tmp_path, env=env, check=True,
                            text=True, capture_output=True, timeout=30)
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    assert a["reference"] == b["reference"] == reference and a["source_requests"] == 0
    assert a["view"]["schema"] == "relay-ashare-collection-view-v1"
    assert a["view"]["freshness"]["read_as_of"] is not None
    assert a["view"]["scope_count"] == b["view"]["scope_count"] == 4
    assert a["view"]["raw"]["source_version"] == b["view"]["raw"]["source_version"]
    assert a["view"]["raw"]["entries"][0]["entity_id"] != b["view"]["raw"]["entries"][0]["entity_id"]
    assert "synthetic-key-only" not in first.stdout + first.stderr + second.stdout + second.stderr
    assert store.backend.head() == head


def test_partial_native_collection_cli_room_and_health_keep_endpoint_purpose_separate(tmp_path, monkeypatch):
    path = tmp_path / "endpoint-records.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    reference = seed_native_day(store, fresh, start="20261009", missing_slots=("20261013:daily", "20261013:adj_factor"))
    selected = tmp_path / "reference.json"
    selected.write_bytes(encoded(reference))
    head = store.backend.head()
    env = {**os.environ, "PYTHONPATH": str(ROOT_DIR / "src")}
    for name in ("TUSHARE_PROXY_API_KEY", "DECISION_OS_STORE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    command = [sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli", "read",
        "--path", str(path), "--registry", str(REGISTRY_PATH), "--input", str(selected), "--limit", "1"]
    completed = subprocess.run(command, cwd=tmp_path, env=env, check=False, text=True,
                               capture_output=True, timeout=30)
    restored = json.loads(completed.stdout)
    assert completed.returncode == 2 and restored["status"] == "partial-restoration"
    assert restored["serving"] == "current-partial" and restored["source_requests"] == 0
    assert restored["reference"] == restored["selected_reference"] == reference
    assert restored["unavailable_queries"] == 2 and restored["latest_attempt"]["status"] == "partial"
    view = restored["view"]
    for kind in ("raw", "qfq"):
        assert view[kind]["horizon_counts"][0]["observed_count"] == 0
        surface = view["endpoint_comparisons"][kind]
        assert surface["horizon_counts"][0]["observed_count"] == 4 and surface["scope_count"] == 4
        assert [w["observed_count"] for w in surface["horizon_counts"]] == [4, 0, 0]
        window = surface["entries"][0]["horizons"][0]
        assert window["status"] == "observed" and window["baseline_date"] == "2026-10-09"
        assert window["end_date"] == "2026-10-16" and window["change_pct"] == "0"
    # Pin the room clock to the actual CLI read so the full result is comparable.
    from datetime import datetime
    read_clock = datetime.fromisoformat(view["freshness"]["read_as_of"])
    monkeypatch.setattr(consumer, "datetime", SimpleNamespace(now=lambda zone: read_clock))
    result = room(fresh(), reference, limit=1)
    part = result["market"]
    assert encoded(part["view"]) == encoded(restored)
    assert part["status"] == "readable" and part["partial"] and result["status"] == "partial"
    next_part = room(fresh(), part["next_plan"]["reference"], offset=1, limit=1)["market"]
    assert next_part["view"]["view"]["view_version"] == view["view_version"]
    assert next_part["view"]["view"]["endpoint_comparisons"]["raw"]["entries"][0]["entity_id"] != view["endpoint_comparisons"]["raw"]["entries"][0]["entity_id"]
    receipt = health_receipt(restored)
    assert receipt["raw"]["horizon_counts"][0]["observed_count"] == 0
    assert receipt["endpoint_comparisons"]["raw"]["horizon_counts"][0]["observed_count"] == 4
    assert receipt["source_requests"] == 0 and not receipt["supplier_rows_in_receipt"]
    safe = encoded(receipt["endpoint_comparisons"]).decode()
    assert not any(value in safe for value in ("entries", "source_version", "SourceRef", "baseline_value", "600000.SH"))
    html = render_page(result)
    assert "完整逐日窗口" in html and "端点区间比较" in html and '"daily_path_certified":false' in html
    assert "innerHTML" not in html and "synthetic-key-only" not in completed.stdout + completed.stderr
    assert store.backend.head() == head


def test_ordinary_remote_cli_uses_one_original_api_and_one_native_market_snapshot(native, monkeypatch, tmp_path, capsys):
    model, store = native
    seed_api, seed_fetches = transport(model), []

    def fresh():
        return VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), seed_api,
                                              fetch=local_fetch(model, seed_fetches)))

    reference = seed_native_day(store, fresh)
    model.requests.clear()  # The ordinary read below owns its separate operation budget.
    binding, selected = tmp_path / "binding.json", tmp_path / "reference.json"
    binding.write_bytes(encoded(asdict(replace(model.binding, write_enabled=False))))
    selected.write_bytes(encoded(reference))
    api, fetches = transport(model), []
    monkeypatch.setattr(market_custody_cli.GitHubAPI, "from_environment", lambda: api)
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch",
                        local_fetch(model, fetches))
    monkeypatch.setattr(RelayHTTP, "__init__", lambda *a, **k: pytest.fail("ordinary reading must not load source credentials"))
    monkeypatch.setattr(RelayHTTP, "request_registered", lambda *a, **k: pytest.fail("ordinary reading must not call Relay"))
    head, writes = model.git.head(), model.writes
    status = market_custody_cli.main(["read", "--binding", str(binding), "--registry", str(REGISTRY_PATH),
                                     "--input", str(selected), "--limit", "1"])
    result = json.loads(capsys.readouterr().out)
    assert status == 0 and result["reference"] == reference and result["source_requests"] == 0
    assert len(fetches) == 1 and fetches[0]["api"] is api
    assert 0 < api._requests == len(model.requests) < 64
    assert all(not f["destination"].exists() for f in fetches)
    assert model.git.head() == head and model.writes == writes
