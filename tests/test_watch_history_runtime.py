"""Actual research host and original bounded API over native segmented history.

Only remote wire responses and the transport into a temporary native mirror are
synthetic. The existing API, writer, complete Git graph and installed CLI remain.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import UnknownWrite
from decision_kernel.domain.watch_history import HISTORY_VERSION
from decision_kernel.infrastructure import github_store
from decision_kernel.interfaces import research_runtime, watch_activity_commands
from test_conditional_odds import authorized, price_payload
from test_market_snapshot import native, local_fetch
from test_news_read_budget import transport
from test_research_runtime import CODE, inputs
from test_watch_activity import change, query
from test_watch_history import at, build_prefix, setup_watch


def hosted(native, monkeypatch, *, count=128, versioned=True):
    model, _ = native
    # The original host config describes a public-origin store with the existing
    # explicit one-way private upgrade. Keep the actual repository private.
    model.binding = replace(model.binding, visibility="public", write_enabled=False,
                            private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding = model.binding
    store = VersionedStore(model.git)
    data = build_prefix(store, setup_watch(store), count=count, versioned=versioned)
    fetches = []
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch",
                        local_fetch(model, fetches))
    return model, store, data, fetches


def command(model, output, operation, payload, submission="", expected=None):
    api, start = transport(model), len(model.requests)
    values = inputs(operation, payload, submission,
                    model.git.head() if expected is None and operation in research_runtime.WRITE_OPERATIONS
                    else expected or "")
    result = research_runtime.run_command(model.binding, api=api, inputs=values, code_sha=CODE,
                                         output=output, run_id="synthetic-history", attempt="1")
    assert 0 < api._requests == len(model.requests) - start < github_store.MAX_REQUESTS == 512
    return result, api._requests


def test_native_host_128_boundary_and_independent_segment_reader_keep_one_api_budget(native, monkeypatch, tmp_path):
    model, store, data, fetches = hosted(native, monkeypatch)
    request = query(store, data["watch_ref"], data["refs"][-1], at(128), price_payload())
    saved, save_requests = command(model, tmp_path / "saved", "save-odds-watch-activity-check",
                                   authorized("check", request), "native-boundary")
    assert saved["status"] == "completed" and saved["write"] == "published" and model.writes == 1
    assert saved["result"]["readback"]["status"] == "verified"
    event_ref = saved["result"]["reference"]
    snapshot = model.git.head()
    read, read_requests = command(model, tmp_path / "event", "read-odds-watch-activity", {"reference": event_ref})
    value = read["result"]
    assert value["summary"]["events"] == value["history_validation"]["verified_events"] == 129
    assert value["event"]["result"]["request"]["previous_check"] == data["refs"][-1]
    assert value["event"]["result"]["status"] == "continued-trigger"
    seal = value["sealed_segments"][0]["reference"]
    segment, segment_requests = command(model, tmp_path / "segment", "read-odds-watch-activity-segment", {"reference": seal})
    assert segment["result"]["segment"]["events"] == data["refs"]
    assert len(segment["result"]["history"]) == 128
    assert segment["result"]["history_validation"]["verified_events"] == 129
    assert segment["result"]["publication_event_ref"] == event_ref
    assert model.writes == 1 and model.git.head() == snapshot
    assert fetches and all(not row["destination"].exists() for row in fetches)
    assert all(row["api"]._requests <= 512 for row in fetches)
    assert max(save_requests, read_requests, segment_requests) < 512


def test_host_lost_migration_receipt_recovers_original_seal_without_writing(native, monkeypatch, tmp_path):
    model, _, data, fetches = hosted(native, monkeypatch, count=4, versioned=False)
    payload = authorized("change", {**change(data["watch_ref"], data["refs"][-1], "migrate", at(4)),
                                    "history_version": HISTORY_VERSION})
    original = model.git.head()
    def lose(_):
        raise UnknownWrite("SIMULATED_HISTORY_RECEIPT_LOST")
    model.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        command(model, tmp_path / "lost", "change-odds-watch-activity", payload, "adopt-original", original)
    model.after_write = None
    snapshot, writes = model.git.head(), model.writes
    restored, _ = command(model, tmp_path / "restored", "reconcile-odds-watch-activity",
                           payload, "adopt-original", original)
    assert restored["status"] == "completed" and restored["write"] == "reconciled-without-write"
    assert restored["result"]["reference"]["commit"] == snapshot
    assert restored["result"]["readback"]["status"] == "verified"
    assert model.writes == writes == 1 and model.git.head() == snapshot
    assert all(not row["destination"].exists() for row in fetches)
    report = json.loads((tmp_path / "lost/report.json").read_text())
    assert report["status"] == "unknown-write"


def exercise_installed_history(call, watch_ref, paused_ref):
    """Use the one already installed wheel and its existing command bridge."""
    migration = {**change(watch_ref, paused_ref, "migrate", datetime.now(timezone.utc).isoformat()),
                 "history_version": HISTORY_VERSION}
    saved = call("change-odds-watch-activity", authorized("change", migration), "installed-history-migrate")
    assert saved["readback"]["status"] == "verified" and saved["state"] == "paused"
    view = call("read-odds-watch-activity", saved["reference"], reference=True)
    assert view["history_validation"]["summary_trusted"] is False
    assert len(view["history"]) == 1 and view["sealed_segments"][0]["kind"] == "legacy-v1"
    seal = view["sealed_segments"][0]["reference"]
    original = call("read-odds-watch-activity-segment", seal, reference=True)
    assert original["publication_event_ref"] == saved["reference"]
    assert original["segment"]["events"][-1] == paused_ref
    assert original["summary"]["events"] == len(original["history"])
    assert original["history_validation"]["verified_events"] == view["summary"]["events"]
    assert "read-odds-watch-activity-segment" in watch_activity_commands.READS
    return saved["reference"]
