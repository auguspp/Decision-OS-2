"""Finite Watch stages through the original host, API budget and installed CLI."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, UnknownWrite
from decision_kernel.infrastructure import github_store
from decision_kernel.interfaces import research_runtime, watch_execution_commands
from test_conditional_odds import authorized, price_payload, spec_payload
from test_market_snapshot import native, local_fetch
from test_news_read_budget import transport
from test_research_runtime import CODE, inputs
from test_watch_activity import apply, change
from test_watch_conditions import evidence_condition, numeric, v2_input, watch_v2
from test_watch_execution import Clock, delivery_change, plan_payload, query


@pytest.fixture
def execution_host(native, monkeypatch):
    model, _ = native
    model.binding = replace(model.binding, visibility="public", write_enabled=False, private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding = model.binding
    store = VersionedStore(model.git)
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
    source = store.put_source("source", SourceInput("source", "content", "Synthetic", "synthetic:execution", "Synthetic research"))
    research = store.submit_revision("research", RevisionInput("case", "r1", "full", "Synthetic research", "Synthetic", "synthetic", (source,)))
    spec = ConditionalOdds(store).freeze(authorized("spec", spec_payload(asdict(research))), submission_id="spec")["reference"]
    watch = OddsWatch(store).freeze(authorized("watch", watch_v2(spec, conditions=[numeric("price")])), submission_id="watch")["reference"]
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "register")
    apply(activity, change(watch, registered, "resume", "2026-01-03T04:01:00Z"), "resume")
    clock = Clock()
    class RuntimeClock:
        @staticmethod
        def now(tz):
            return clock()
    monkeypatch.setattr("decision_kernel.application.watch_execution.datetime", RuntimeClock)
    monkeypatch.setattr("decision_kernel.application.watch_delivery.datetime", RuntimeClock)
    fetches = []
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch", local_fetch(model, fetches))
    return model, store, watch, clock, fetches


def command(model, out, operation, payload, submission="", expected=None):
    api, start = transport(model), len(model.requests)
    values = inputs(operation, payload, submission,
                    model.git.head() if expected is None and operation in research_runtime.WRITE_OPERATIONS
                    else expected or "")
    report = research_runtime.run_command(model.binding, api=api, inputs=values, code_sha=CODE,
                                         output=out, run_id="synthetic-execution", attempt="1")
    assert 0 < api._requests == len(model.requests) - start < github_store.MAX_REQUESTS == 512
    return report


def reserve(model, watch, tmp_path):
    plan = command(model, tmp_path / "plan", "freeze-odds-watch-execution", authorized("plan", plan_payload(watch)), "plan")
    assert plan["result"]["readback"]["status"] == "verified"
    p = plan["result"]["reference"]
    q = query(p, price=v2_input(price_payload()))
    preview = command(model, tmp_path / "preview", "preview-odds-watch-execution", q)
    assert preview["write"] == "not-requested" and preview["result"]["next_request"]["stage"] == "reserve"
    assert model.writes == 1
    keys = preview["result"]["submission_ids"]
    slot = command(model, tmp_path / "slot", "reserve-odds-watch-execution", authorized("execution", q), keys["reserve"])
    assert slot["result"]["readback"]["status"] == "verified"
    return slot["result"]["reference"], keys


def test_original_native_host_each_stage_and_pending_notice_have_fresh_bounded_receipts(execution_host, tmp_path):
    model, store, watch, clock, fetches = execution_host
    original_research = store.current("case")
    slot, keys = reserve(model, watch, tmp_path)
    assert model.writes == 2
    clock.set("2026-01-03T04:02:09Z")
    checked = command(model, tmp_path / "check", "save-odds-watch-execution-check", {"slot_ref": slot}, keys["check"])
    assert checked["result"]["readback"]["status"] == "verified" and model.writes == 3
    done = command(model, tmp_path / "finish", "finish-odds-watch-execution", {"slot_ref": slot}, keys["finish"])
    assert done["result"]["readback"]["status"] == "verified" and model.writes == 4
    result = done["result"]
    assert result["record"]["checked_at"] == "2026-01-03T04:02:09Z"
    assert len(result["record"]["notices"]) == 1
    notice = result["record"]["notices"][0]
    assert notice["state"] == "pending-transport-selection" and notice["destination"] is None
    read = command(model, tmp_path / "result", "read-odds-watch-execution-result", {"reference": result["reference"]})
    assert read["result"]["record"] == result["record"] and model.writes == 4
    clock.set("2026-01-03T04:03:00Z")
    payload = authorized("change", delivery_change(result["reference"], notice["notice_id"]))
    attempt = command(model, tmp_path / "attempt", "change-odds-watch-delivery", payload, "delivery-attempt")
    assert attempt["result"]["readback"]["status"] == "verified" and model.writes == 5
    delivery = command(model, tmp_path / "delivery", "read-odds-watch-delivery", {"reference": attempt["result"]["reference"]})
    assert delivery["result"]["event"]["state"]["status"] == "attempt-reserved-no-sender"
    assert delivery["result"]["effects"]["notification_sent"] is False
    assert model.writes == 5 and store.current("case") == original_research
    assert fetches and all(not row["destination"].exists() for row in fetches)


def test_host_recovers_lost_check_clock_and_finishes_after_window_without_rechecking(execution_host, tmp_path):
    model, _, watch, clock, _ = execution_host
    slot, keys = reserve(model, watch, tmp_path)
    expected = model.git.head()
    def lose(_):
        raise UnknownWrite("SIMULATED_EXECUTION_RECEIPT_LOST")
    model.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        command(model, tmp_path / "lost", "save-odds-watch-execution-check", {"slot_ref": slot}, keys["check"], expected)
    model.after_write = None
    clock.set("2026-01-04T00:00:00Z")
    snapshot, writes = model.git.head(), model.writes
    restored = command(model, tmp_path / "reconcile", "reconcile-odds-watch-execution-check", {"slot_ref": slot}, keys["check"], expected)
    assert restored["write"] == "reconciled-without-write" and restored["result"]["readback"]["status"] == "verified"
    assert restored["result"]["reference"]["commit"] == snapshot and model.writes == writes
    result = command(model, tmp_path / "finish", "finish-odds-watch-execution", {"slot_ref": slot}, keys["finish"])
    assert result["result"]["record"]["checked_at"] == "2026-01-03T04:02:05Z"
    assert model.writes == writes + 1


def test_execution_host_preflight_never_creates_output_for_unapproved_plan(execution_host, tmp_path):
    model, _, watch, _, _ = execution_host
    payload = authorized("plan", plan_payload(watch)); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="RIGHTS_REQUIRED"):
        command(model, tmp_path / "blocked", "freeze-odds-watch-execution", payload, "unapproved")
    assert model.writes == 0 and not (tmp_path / "blocked").exists()


def test_manual_host_keeps_explicit_write_selection_and_no_schedule():
    workflow = (Path(__file__).parents[1] / ".github/workflows/research-runtime.yml").read_text()
    for name in watch_execution_commands.WRITES:
        assert name in research_runtime.WRITE_OPERATIONS
        assert f"inputs.operation == '{name}'" in workflow and f"inputs.operation != '{name}'" in workflow
    for name in watch_execution_commands.READS + watch_execution_commands.RECONCILES:
        assert name in research_runtime.READ_OPERATIONS and name not in research_runtime.WRITE_OPERATIONS
    assert "schedule:" not in workflow


def exercise_installed_execution(call, research):
    """Continue the original one-wheel cold test with a separate synthetic Watch."""
    now = lambda: datetime.now(timezone.utc).isoformat()
    source = call("put-source", {"source_id": "installed-execution-source", "kind": "content",
        "title": "Synthetic operating event", "origin": "synthetic:installed-execution",
        "text": "The synthetic operation met the stated evidence condition."}, "installed-execution-source")
    start = datetime.now(timezone.utc)
    watch = watch_v2(research, "research", [evidence_condition()])
    watch["watch_id"] = "installed-execution-watch"
    watch["activation"]["as_of"] = watch["window"]["starts_at"] = start.isoformat()
    watch["window"]["ends_at"] = (start + timedelta(days=2)).isoformat()
    frozen = call("freeze-odds-watch", authorized("watch", watch), "installed-execution-watch")
    w = frozen["reference"]
    registered = call("change-odds-watch-activity", authorized("change", change(w, at=now())), "installed-execution-register")
    call("change-odds-watch-activity", authorized("change", change(w, registered["reference"], "resume", now())), "installed-execution-resume")
    plan = plan_payload(w, plan_id="installed-execution", starts_at=now(), interval_seconds=3600,
                        slot_count=2, late_tolerance_seconds=1800)
    planned = call("freeze-odds-watch-execution", authorized("plan", plan), "installed-execution-plan")
    assessed = now()
    evidence = {"condition_id": "operating", "observed_at": assessed, "assessed_at": assessed, "conclusion": "met",
                "reason": "Synthetic retained line explicitly supports the stated event", "sources": [source]}
    q = query(planned["reference"], at=now(), price=v2_input(evidence=[evidence]))
    preview = call("preview-odds-watch-execution", q)
    assert preview["write"] == "not-requested" and not preview["effects"]["scheduler_created"]
    keys = preview["submission_ids"]
    slot = call("reserve-odds-watch-execution", authorized("execution", q), keys["reserve"])
    assert slot["readback"]["status"] == "verified"
    checked = call("save-odds-watch-execution-check", {"slot_ref": slot["reference"]}, keys["check"])
    assert checked["readback"]["status"] == "verified"
    finished = call("finish-odds-watch-execution", {"slot_ref": slot["reference"]}, keys["finish"])
    assert finished["readback"]["status"] == "verified"
    result = call("read-odds-watch-execution-result", finished["reference"], reference=True)
    assert result["record"] == finished["record"] and len(result["record"]["notices"]) == 1
    notice = result["record"]["notices"][0]
    delivery = call("change-odds-watch-delivery", authorized("change", delivery_change(finished["reference"],
        notice["notice_id"], at=now())), "installed-delivery-attempt")
    assert delivery["readback"]["status"] == "verified"
    read = call("read-odds-watch-delivery", delivery["reference"], reference=True)
    assert read["event"]["state"]["status"] == "attempt-reserved-no-sender"
    assert not read["effects"]["notification_sent"] and not read["effects"]["user_read_verified"]
