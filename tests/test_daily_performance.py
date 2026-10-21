"""Performance observations preserve original history, budgets and live read exits."""
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
import json
import re

import pytest

from decision_kernel.application import versioned_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, digest, encoded
from decision_kernel.infrastructure.github_store import MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure import runtime_performance as perf
from decision_kernel.interfaces import daily_market_cli as daily_cli
from decision_kernel.interfaces import daily_discovery_cli as discovery_cli
from decision_kernel.interfaces import daily_reading_cli as reading_cli
from decision_kernel.interfaces import ftshare_collection_cli as ft_cli
from test_daily_master_refresh import Host as MasterHost
from test_daily_market import Host, REGISTRY
from test_daily_market_schedule import native_runtime
from test_daily_reading import AT, native_run
from test_ftshare_collection_runtime import SingleHost, SingleProvider, single_execute, runtime_input, config
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport


def embedded_model(path):
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>',
        path.read_text(), re.DOTALL)
    assert payload is not None
    return json.loads(payload.group(1))


def test_monotonic_diagnostics_leave_original_master_bytes_and_business_clock_calls_unchanged(monkeypatch):
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-20T12:00:00+00:00")
    def execute(observe):
        host = MasterHost(count=501)
        calls = []
        original = host.clock.monotonic
        def business_clock():
            calls.append(True)
            return original()
        if observe:
            with perf.performance_session(clock=lambda: 1234.5):
                results = [host.run(monotonic=business_clock) for _ in range(2)]
                diagnostic = perf.performance_receipt()
        else:
            results = [host.run(monotonic=business_clock) for _ in range(2)]
            diagnostic = None
        return host, results, len(calls), diagnostic
    original, old_results, old_calls, _ = execute(False)
    observed, results, calls, diagnostic = execute(True)
    assert results == old_results and calls == old_calls
    assert observed.backend.shared == original.backend.shared  # Includes every original retained byte.
    assert [row["source_requests"] for row in results] == [1, 1]
    assert diagnostic["counts"]["relay_requests"] == diagnostic["counts"]["relay_wire_attempts"] == 2
    unit_ids = [row["sequence"] for row in diagnostic["samples"] if row["phase"] == "master.unit"]
    wire = [row for row in diagnostic["samples"] if row["phase"] == "relay.http"]
    assert [row["unit_sequence"] for row in wire] == unit_ids
    assert all(row["outcome"] == "return" for row in wire)
    assert all("performance" not in row for row in results)


@pytest.mark.parametrize("failure_at", ["post", "readback"])
def test_unknown_master_reservation_preserves_source_zero_and_blocks_reentry(monkeypatch, failure_at):
    host = MasterHost()
    preflights = []
    original = host.backend.commit
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    def preflight(reader):
        preflights.append(True)
        if failure_at == "readback" and len(preflights) == 2:
            raise RecordError("DAILY_MARKET_STALE_CONTROL")
    if failure_at == "post":
        monkeypatch.setattr(host.backend, "commit", lost)
    with perf.performance_session(clock=lambda: 0):
        result = host.run(preflight=preflight)
        measured = perf.performance_receipt()
    assert result["status"] == "persistence-unconfirmed" and result["source_requests"] == 0
    assert measured["counts"]["relay_requests"] == measured["counts"]["relay_wire_attempts"] == 0
    assert "relay.http" in measured["unobserved_phases"]
    assert measured["phases"]["checkpoint.save"]["raised"] == 1
    if failure_at == "readback":
        assert measured["phases"]["checkpoint.readback"]["raised"] == 1
    monkeypatch.setattr(host.backend, "commit", original)
    assert host.checkpoint()["state"]["control"]["retry"]["phase"] == "in-flight"
    again = host.run(client_factory=lambda: pytest.fail("diagnostics reopened unknown reservation"))
    assert again["status"] == "persistence-unconfirmed" and again["source_requests"] == 0
    assert not host.opener.calls


@pytest.mark.parametrize("stop", [2, 3, 4])
def test_ft_unknown_reservation_capture_or_settlement_remains_nonresumable_with_diagnostics(stop):
    host = SingleHost()
    def lost(backend, files):
        if len(host.commits) == stop:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_commit = lost
    with perf.performance_session(clock=lambda: 0):
        first = single_execute(host, SingleProvider(host))
        observed = perf.performance_receipt()
    assert first["receipt"]["status"] == "persistence-unconfirmed"
    assert first["receipt"]["next_run_can_resume"] is False
    assert first["receipt"]["source_requests"] == (0 if stop == 2 else 1)
    assert "performance" not in first["receipt"]
    owner = "ft.reserve" if stop == 2 else "ft.capture_save" if stop == 3 else "ft.settle"
    assert observed["phases"][owner]["raised"] == 1
    # A function's return says nothing about its unknown write effects.
    assert observed["phases"]["ft.unit"]["returned"] == 1
    assert "not-write-effect" in observed["outcome_semantics"]
    host.after_commit = None
    before = host.read().backend.head()
    second = single_execute(host)
    assert second["receipt"]["source_requests"] == 0 and host.read().backend.head() == before


@pytest.mark.parametrize("failure", [None, "visibility", "reachability", "quota", "final-guard",
    "ft-read", "ft-read-exit"])
def test_daily_terminal_read_uses_one_native_snapshot_and_releases_only_after_exit(native, monkeypatch, failure):
    model, store = native
    host = Host(storage=(model.git, store, lambda: VersionedStore(copy(model.git))))
    api, fetches = transport(model), []
    backend = GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches))
    reader = VersionedStore(backend)
    trace, snapshots = [], []
    original_scope = GitMarketReader.market_read_scope
    @contextmanager
    def watched_scope(self):
        outer = not self._depth
        with original_scope(self) as value:
            yield value
        if outer:
            trace.append("exit")
    monkeypatch.setattr(GitMarketReader, "market_read_scope", watched_scope)
    resolve, read = daily_cli.daily_market_reference, daily_cli.read_ftshare_collection_version
    def resolved(grouped, *args, **kwargs):
        snapshots.append(grouped.backend._reader)
        trace.append("resolve")
        return resolve(grouped, *args, **kwargs)
    def reading(reference, registry, grouped, **kwargs):
        assert grouped.backend._reader is snapshots[0]
        trace.append("ft-read")
        result = read(reference, registry, grouped, **kwargs)
        if failure in {"visibility", "ft-read-exit"}:
            model.meta["private"] = False
        elif failure == "reachability":
            model.git._run("update-ref", "refs/heads/records", model.before_anchor, model.git.head())
        elif failure == "quota":
            api._requests = MAX_REQUESTS
        if failure in {"ft-read", "ft-read-exit"}:
            raise RecordError("FTSHARE_SYNTHETIC_VIEW_FAILED")
        return result
    def guard(current):
        assert current is api
        trace.append("guard")
        if failure == "final-guard" and "exit" in trace:
            raise RecordError("DAILY_MARKET_MAIN_CHANGED")
    monkeypatch.setattr(daily_cli, "daily_market_reference", resolved)
    monkeypatch.setattr(daily_cli, "read_ftshare_collection_version", reading)
    with perf.performance_session(clock=lambda: 0):
        result = host.run(operation="status", open_unit=lambda write: (store, lambda: reader, api), guard=guard)
        observed = perf.performance_receipt()
    assert len(fetches) == 1 and result["receipt"]["source_requests"] == 0 and model.writes == 0
    assert backend._depth == 0 and backend._reader is None
    assert all(not entry["destination"].exists() for entry in fetches)
    assert snapshots and not snapshots[0]._active and not snapshots[0]._memo
    assert observed["phases"]["snapshot.admit"]["entered"] == 1
    assert observed["phases"]["snapshot.exit"]["entered"] == 1
    if failure is None:
        assert trace[-2:] == ["exit", "guard"]
        assert result["receipt"]["status"] == "read" and result["receipt"]["health"]
        assert result["control"]["market_reference"] == host.approved["seed_reference"]
        assert api._requests == observed["counts"]["github_api_requests"] < MAX_REQUESTS
    elif failure == "ft-read":
        assert trace[-1] == "exit"  # No success-only final guard after a failed FT view.
        assert result["receipt"]["status"] == "reading-unavailable"
        assert result["receipt"]["reading_reason_code"] == "FTSHARE_SYNTHETIC_VIEW_FAILED"
        assert "health" not in result["receipt"]
        assert result["control"]["market_reference"] == host.approved["seed_reference"]
        assert observed["phases"]["snapshot.exit"]["returned"] == 1
    else:
        assert result["receipt"]["status"] == "reading-unavailable"
        assert "health" not in result["receipt"]
        assert result["control"]["market_reference"] is None and result["control"]["cycle_reference"] is None
        assert not result["control"]["calendar_references"] and not result["control"]["master_references"]
        assert not result["control"]["frontier_successor_references"]
        if failure == "final-guard":
            assert trace[-2:] == ["exit", "guard"]
            assert result["receipt"]["reading_reason_code"] == "DAILY_MARKET_MAIN_CHANGED"
        if failure == "ft-read-exit":
            assert result["receipt"]["reading_reason_code"] != "FTSHARE_SYNTHETIC_VIEW_FAILED"
            assert observed["phases"]["snapshot.exit"]["raised"] == 1


@pytest.mark.parametrize("source_has_performance", [False, True])
def test_reading_and_discovery_own_separate_diagnostics_and_do_not_rewrite_source(tmp_path, monkeypatch, source_has_performance):
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch, status="source-held")
    if source_has_performance:
        with perf.performance_session(clock=lambda: 0):
            value = perf.annotate_performance(json.loads((out / "result.json").read_bytes()))
        (out / "result.json").write_bytes(encoded(value))
    source_bytes = (out / "result.json").read_bytes()
    monkeypatch.setattr(discovery_cli, "_open_writer", lambda api: (object(), object()))
    assert discovery_cli.run_runtime(event, env, out, api=api, clock=lambda: AT) == 0
    discovery = json.loads((out / discovery_cli.OUTPUT).read_bytes())
    assert discovery["performance"]["schema"] == "daily-runtime-performance-v1"
    assert discovery["performance"]["phases"]["discovery.execute"]["returned"] == 1
    assert discovery["source_receipt_sha256"] == digest(source_bytes)
    assert reading_cli.run_runtime(event, env, out, api=api, clock=lambda: AT) == 0
    model = json.loads((out / "reading-room.json").read_bytes())
    assert model["performance"]["phases"]["reading.model"]["returned"] == 1
    assert "reading.render" in model["performance"]["unobserved_phases"]
    assert "performance" not in reading_cli.UPDATE_FIELDS
    assert "performance" not in model["daily_update"]["source_receipt"]
    assert model["daily_update"]["source_receipt_sha256"] == digest(source_bytes)
    assert (out / "result.json").read_bytes() == source_bytes
    assert embedded_model(out / "reading-room.html") == model
    assert (out / "reading-summary.md").read_text() == reading_cli.summary_markdown(model)


def test_daily_host_final_receipt_seals_completed_phases_without_copying_into_business_progress(tmp_path, monkeypatch):
    host = Host()
    event, env, api = native_runtime(tmp_path, monkeypatch, host.approved,
        invocation="status", admitted_at=host.now, actual_at=host.now)
    business = []
    def execute(**kwargs):
        result = host.run(operation="status", progress=kwargs["progress"])
        business.append(result)
        return result
    out = tmp_path / "daily"
    assert daily_cli.run_runtime(event, env, out, api=api, execute=execute) == 0
    selected = daily_cli.settings(event, env, host.approved)
    source = reading_cli.read_update_receipt(out, selected)
    assert source["performance"]["phases"]["daily.execute"]["returned"] == 1
    assert source["performance"]["phases"]["daily.read_scope"]["returned"] == 1
    assert source["source_requests"] == 0 and len(encoded(source)) <= 1024 * 1024
    assert "performance" not in business[0]["receipt"] and "performance" not in business[0]["control"]


def test_optional_reading_diagnostic_cannot_break_existing_html_capacity(tmp_path, monkeypatch):
    event, env, out, api, readers, calls = native_run(tmp_path, monkeypatch)
    selected = daily_cli.settings(event, env, reading_cli.policy(reading_cli._json(reading_cli.POLICY_FILE.read_bytes())))
    source = reading_cli.read_update_receipt(out, selected)
    approved = reading_cli.policy(reading_cli._json(reading_cli.POLICY_FILE.read_bytes()))
    base = reading_cli.build_reading(*readers, REGISTRY, approved, source, as_of=AT)
    base["synthetic_capacity_text"] = ""
    remaining = 8 * 1024 * 1024 - len(reading_cli.daily_page(base).encode())
    padding = "<" * ((remaining - 128) // 6)  # Each character is escaped to six bytes in the payload.
    perform = reading_cli.perform
    def large_model(*args, **kwargs):
        result = perform(*args, **kwargs)
        result["synthetic_capacity_text"] = padding
        return result
    monkeypatch.setattr(reading_cli, "perform", large_model)
    page, renders = reading_cli.daily_page, []
    def counted(model):
        renders.append("performance" in model)
        return page(model)
    monkeypatch.setattr(reading_cli, "daily_page", counted)
    calls.clear()
    assert reading_cli.run_runtime(event, env, out, api=api, clock=lambda: AT) == 0
    final = json.loads((out / "reading-room.json").read_bytes())
    assert renders == [True, False] and len(calls) == 2  # Resolver and read are not repeated.
    assert "performance" not in final and len(encoded(final)) < 6 * 1024 * 1024
    assert embedded_model(out / "reading-room.html") == final
    assert len((out / "reading-room.html").read_bytes()) <= 8 * 1024 * 1024
    assert (out / "reading-summary.md").read_text() == reading_cli.summary_markdown(final)


def test_standalone_ft_host_seals_one_bounded_top_level_diagnostic(tmp_path, monkeypatch):
    event, env = runtime_input(tmp_path)
    path = tmp_path / "trusted-ftshare.json"
    path.write_bytes(encoded(config()))
    monkeypatch.setattr(ft_cli, "CONFIG_FILE", path)
    business = {"status": "partial", "source_requests": 1}
    def execute(**kwargs):
        with perf.measure_phase("ft.unit"):
            perf.count_event("ft_requests")
            kwargs["progress"](business)
        return {"receipt": business, "reference": None}
    with perf.performance_session(clock=lambda: 0):
        perf.count_event("relay_requests")
        assert ft_cli.run_runtime(event, env, tmp_path / "ft", execute=execute) == 2
    saved = json.loads((tmp_path / "ft/result.json").read_bytes())
    assert saved["performance"]["counts"]["relay_requests"] == 1  # Nested host never resets the session.
    assert saved["performance"]["counts"]["ft_requests"] == 1
    assert saved["performance"]["phases"]["ft.unit"]["returned"] == 1
    assert len(encoded(saved)) <= 262144 and "performance" not in business
