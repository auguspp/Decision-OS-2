"""New conditions use the original activity CAS, host budget and installed wheel."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.domain.records import RecordError, SourceInput
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD
from decision_kernel.interfaces import agenda_commands
from decision_kernel.interfaces.news_delivery_cli import watch_rights
from test_conditional_odds import authorized, price_payload
from test_odds_watch import case
from test_odds_watch_runtime import setup_host
from test_research_runtime import host, invoke
from test_watch_activity import apply, change, query
from test_watch_conditions import evidence_condition, interval, numeric, v2_input, watch_v2


def test_v2_activity_pause_resume_keeps_each_condition_and_derived_summary(case):
    store, _, spec, watches, _ = case
    watch = watches.freeze(authorized("watch", watch_v2(spec, conditions=[
        numeric("low"), interval("range", "90", "105", True, True)])), submission_id="conditions")["reference"]
    activity = WatchActivity(store)
    current = apply(activity, change(watch), "register-v2")
    current = apply(activity, change(watch, current, "resume", "2026-01-03T04:01:00Z"), "resume-v2")
    first = activity.save_check(authorized("check", query(store, watch, current,
        price=v2_input(price_payload()))), submission_id="first-v2")["reference"]
    current = apply(activity, change(watch, first, "pause", "2026-01-03T04:03:00Z"), "pause-v2")
    current = apply(activity, change(watch, current, "resume", "2026-01-03T04:04:00Z"), "resume-again-v2")
    second = activity.save_check(authorized("check", query(store, watch, current, "2026-01-03T04:05:00Z",
        v2_input(price_payload()))), submission_id="second-v2")["reference"]
    read = activity.read(second, include_current_use=False)
    result = read["event"]["result"]
    assert "current_activity" not in read
    assert [row["status"] for row in result["condition_results"]] == ["continued-trigger"] * 2
    assert all(row["trigger_continuity"] == "intervening-checks-unknown" for row in result["condition_results"])
    assert [(row["checks"], row["first_triggers"], row["continued_triggers"])
            for row in read["summary"]["condition_summaries"]] == [(2, 1, 1)] * 2
    assert read["summary"]["checks"] == 2 and read["summary"]["events"] == 6
    # A later current pointer and unrelated source never rewrite the frozen view.
    apply(activity, change(watch, second, "retire", "2026-01-03T04:06:00Z"), "retire-v2")
    store.put_source("unrelated", SourceInput("unrelated", "content", "Unrelated", "synthetic:unrelated", "Other fact"))
    assert activity.read(second, include_current_use=False) == read
    assert activity.read(second)["current_activity"]["same_event"] is False


def test_v2_activity_rejects_resealed_per_condition_summary(case):
    store, _, spec, watches, _ = case
    watch = watches.freeze(authorized("watch", watch_v2(spec)), submission_id="v2")["reference"]
    service = WatchActivity(store)
    current = apply(service, change(watch), "reg")
    current = apply(service, change(watch, current, "resume", "2026-01-03T04:01:00Z"), "res")
    saved = service.save_check(authorized("check", query(store, watch, current,
        price=v2_input(price_payload()))), submission_id="check")
    event = service.read(saved["reference"])["event"]
    event["summary"]["condition_summaries"][0]["continued_triggers"] += 1
    forged = ConditionalOdds(store)._put("forged", ACTIVITY_RECORD, event, "Synthetic corrupt fixture")
    with pytest.raises(RecordError, match="REPLAY_MISMATCH"):
        service.read(asdict(forged))


def test_native_host_multi_conditions_save_and_new_reader_keep_original_budget(host, tmp_path, monkeypatch):
    store, old, price = setup_host(host, monkeypatch)
    watch = watch_v2(old["basis"]["reference"], conditions=[numeric("low"), interval("middle")])
    original, calls = host.request, {"count": 0}
    def bounded(method, path, payload=None):
        calls["count"] += 1
        assert calls["count"] <= 512, "Original operation request budget exceeded"
        return original(method, path, payload)
    monkeypatch.setattr(host, "request", bounded)
    saved_watch = invoke(host, tmp_path / "freeze", "freeze-odds-watch", authorized("watch", watch), "v2-watch")["result"]
    assert saved_watch["readback"]["status"] == "verified"
    w = saved_watch["reference"]
    calls["count"] = 0
    reg = invoke(host, tmp_path / "reg", "change-odds-watch-activity", authorized("change", change(w)), "reg")["result"]
    calls["count"] = 0
    active = invoke(host, tmp_path / "resume", "change-odds-watch-activity", authorized("change",
        change(w, reg["reference"], "resume", "2026-01-03T04:01:00Z")), "resume")["result"]
    calls["count"] = 0
    saved = invoke(host, tmp_path / "check", "save-odds-watch-activity-check", authorized("check",
        query(store, w, active["reference"], price=v2_input(price))), "v2-check")["result"]
    assert saved["readback"]["status"] == "verified" and saved["readback"]["new_reader"]
    calls["count"] = 0
    before = host.writes
    result = invoke(host, tmp_path / "read", "read-odds-watch-activity", {"reference": saved["reference"]})["result"]
    assert result["event"]["result"]["schema"] == "odds-watch-check-v2"
    assert [row["status"] for row in result["event"]["result"]["condition_results"]] == ["first-trigger", "not-met"]
    assert result["summary"]["condition_summaries"][0]["first_triggers"] == 1 and host.writes == before


def test_watch_source_export_consent_is_required_for_direct_plan_and_omitted_brief(case):
    _, research, _, _, watch = case
    selection = {"reference": asdict(research), "question_id": None, "kind": "check", "check_ref": watch}
    payload = {"plan": {"references": [asdict(research)], "as_of": "2026-01-03T05:00:00Z", "watch_evidence": [selection]}}
    with pytest.raises(RecordError, match="WATCH_PUBLICATION_NOT_CONFIRMED"):
        agenda_commands.preflight("agenda", payload)
    payload["public_feedback_custody_confirmed"] = True
    with pytest.raises(RecordError, match="WATCH_PUBLICATION_NOT_CONFIRMED"):
        agenda_commands.preflight("agenda", payload)
    payload["public_sources_custody_confirmed"] = True
    agenda_commands.preflight("agenda", payload)
    # A capacity omission still exports exact selected evidence references.
    doc = {"research": {"rows": [{"status": "not-included", "watch_evidence": [{"selection": selection}]}]}}
    with pytest.raises(RecordError, match="BRIEF_WATCH_PUBLICATION_NOT_CONFIRMED"):
        watch_rights(doc, {"public_feedback_custody_confirmed": "true"})
    watch_rights(doc, {"public_feedback_custody_confirmed": "true", "public_sources_custody_confirmed": "true"})


def exercise_installed_conditions(call, research):
    """Normal v2 evidence and activity in the one already installed wheel."""
    now = lambda: datetime.now(timezone.utc).isoformat()
    source = call("put-source", {"source_id": "installed-watch-evidence", "kind": "content",
        "title": "Synthetic operating disclosure", "origin": "synthetic:installed-watch",
        "text": "The named synthetic plant started delivering in the specified period."}, "installed-watch-evidence")
    start = datetime.now(timezone.utc)
    watch = watch_v2(research, "research", [evidence_condition()])
    watch["activation"]["as_of"] = watch["window"]["starts_at"] = start.isoformat()
    watch["window"]["ends_at"] = (start + timedelta(days=2)).isoformat()
    frozen = call("freeze-odds-watch", authorized("watch", watch), "installed-v2-watch")
    registered = call("change-odds-watch-activity", authorized("change", change(frozen["reference"], at=now())), "installed-v2-reg")
    active = call("change-odds-watch-activity", authorized("change", change(frozen["reference"], registered["reference"],
        "resume", now())), "installed-v2-resume")
    observed = assessed = now()
    evidence = {"condition_id": "operating", "observed_at": observed, "assessed_at": assessed,
        "conclusion": "met", "reason": "Synthetic assessment supported by the retained line", "sources": [source]}
    check = {"watch_ref": frozen["reference"], "expected_event": active["reference"], "checked_at": now(),
        "records_snapshot": active["reference"]["commit"], "input": v2_input(evidence=[evidence]), "review_reasons": []}
    saved = call("save-odds-watch-activity-check", authorized("check", check), "installed-v2-check")
    assert saved["readback"]["status"] == "verified"
    reopened = call("read-odds-watch-activity", saved["reference"], reference=True)
    assert reopened["event"]["result"]["condition_results"][0]["status"] == "first-trigger"
    assert reopened["event"]["result"]["condition_results"][0]["evidence"]["source_reads"][0]["reference"] == source
    agenda = call("research-agenda", {"references": [research], "as_of": now(), "watch_evidence": [{
        "reference": research, "question_id": None, "kind": "activity", "check_ref": saved["reference"]}]})
    assert agenda["cards"][0]["watch_evidence"][0]["result"]["status"] == "first-trigger"
    stored = call("put-artifact", {"artifact": agenda["artifact"], "rights_confirmed": True,
        "rights_basis": "Synthetic engineering result and source, not a real user judgment"}, "installed-v2-agenda")
    opened = call("read-research-agenda", stored["reference"], reference=True)
    assert opened["document"] == agenda["document"]
