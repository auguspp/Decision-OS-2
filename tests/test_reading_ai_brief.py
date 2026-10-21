"""Original saved author text, adoption and fixed-page reads; no business writes."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import hosted_handoff as handoff, reading_room
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import RecordError, RevisionInput, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_hosted_handoff import saved, brief, AT
from test_hosted_watch_handoff import retained
from test_odds_watch import case
from test_reading_odds_book import html_model
from test_reading_odds_book_overview_page import exercise

LATEST = {"select": "latest-ai-brief"}


def test_exact_v1_keeps_original_body_digest_and_adoption_without_copying_research(saved, monkeypatch):
    store, research, _ = saved
    doc = brief(store, research)
    receipt = handoff.save(store, doc, "author-brief")
    head = store.backend.head()
    monkeypatch.setattr(handoff, "latest", lambda *_a, **_kw: pytest.fail("exact reference must not discover"))
    result = ReadingRoom(store).build({"as_of": AT, "ai_brief": receipt["reference"]})
    panel = result["ai_brief"]
    assert panel["status"] == "readable" and panel["view"]["document"] == doc
    assert panel["view"]["body_sha256"] == digest(doc["body"].encode("utf-8"))
    assert panel["view"]["research"][0]["reference"] == asdict(research)
    assert "body" not in panel["view"]["research"][0]
    assert result["readings"][0]["ai_brief_relation"]["status"] == "adopted"
    assert result["brief"]["status"] == "not-supplied"
    assert result["refresh_plan"]["ai_brief"] == receipt["reference"]
    assert result["human_acceptance"] == "not-established" and result["execution"] == "not-started"
    assert html_model(render_page(result)) == result and store.backend.head() == head


def test_v2_retains_actual_full_watch_check_and_unknown_in_same_author_body(retained):
    store, _, _, research, check, check_result, doc = retained
    receipt = handoff.save(store, doc, "author-full-watch")
    head = store.backend.head()
    result = ReadingRoom(store).build({"as_of": doc["cutoff"], "reference": research,
                                       "ai_brief": receipt["reference"]})
    panel = result["ai_brief"]
    row = panel["view"]["watch_evidence_adoption"][0]
    assert panel["watch_publication_required"] is True and panel["view"]["document"] == doc
    assert row["result"] == check_result and row["result"]["status"] == "cannot-determine"
    assert row["selection"]["check_ref"] == check and row["recalculation"] == "matches"
    assert next(r for r in result["readings"] if r["reference"] == research)["ai_brief_relation"]["status"] == "adopted"
    assert store.backend.head() == head


def test_latest_and_pagination_freeze_catalogue_and_author_brief_until_explicit_refresh(saved):
    store, research, _ = saved
    store.submit_revision("second-case", RevisionInput("z-other", "r1", "quick", "Synthetic other text",
                          "Synthetic", "synthetic", unresolved=("Synthetic unknown",)))
    doc = brief(store, research)
    old = handoff.save(store, doc, "old")
    request = {"as_of": "2026-10-20T12:00:00Z", "catalogue": {"limit": 1}, "ai_brief": LATEST}
    before = deepcopy(request)
    first = ReadingRoom(store).build(request)
    new_doc = {**doc, "execution_date": "2026-10-20", "cutoff": "2026-10-20T11:30:00Z",
               "body": "Next saved author text", "baseline": old["reference"]}
    newest = handoff.save(store, new_doc, "new")
    second = ReadingRoom(store).build(first["next_plan"])
    assert second["catalogue"]["snapshot"] == first["catalogue"]["snapshot"]
    assert second["ai_brief"]["reference"] == first["ai_brief"]["reference"]
    assert second["ai_brief"]["view"]["document"] == doc and "discovery" not in second["ai_brief"]
    assert request == before and first["refresh_plan"]["ai_brief"] == LATEST
    refreshed = ReadingRoom(store).build(first["refresh_plan"])
    assert refreshed["ai_brief"]["reference"]["path"] == newest["reference"]["path"]
    assert refreshed["catalogue"]["snapshot"] != first["catalogue"]["snapshot"]


def test_latest_with_historical_catalogue_cursor_uses_that_snapshot_not_new_head(saved):
    store, research, _ = saved
    doc = brief(store, research)
    old = handoff.save(store, doc, "before-cursor")
    original = ReadingRoom(store).build({"as_of": "2026-10-20T12:00:00Z", "ai_brief": LATEST})
    new_doc = {**doc, "execution_date": "2026-10-20", "cutoff": "2026-10-20T11:30:00Z",
               "body": "New head has another author text", "baseline": old["reference"]}
    newest = handoff.save(store, new_doc, "after-cursor")
    # Deliberately resolve the selector again at the old cursor. Reusing the
    # already-frozen exact reference would not detect a discovery-head leak.
    replay = ReadingRoom(store).build({**original["plan"], "ai_brief": LATEST})
    panel = replay["ai_brief"]
    assert panel["discovery"]["snapshot"] == original["catalogue"]["snapshot"]
    assert panel["reference"]["path"] == old["reference"]["path"]
    assert panel["view"]["document"] == doc
    assert store.backend.head() == newest["reference"]["commit"]


def test_bad_new_candidate_does_not_promote_good_old_brief_to_latest(saved):
    store, research, _ = saved
    doc = brief(store, research)
    old = handoff.save(store, doc, "good")
    bad = {**doc, "execution_date": "2026-10-20", "body": 123}
    ResearchArtifacts(store).put("bad-candidate", ArtifactInput.from_bytes(
        "ai-brief-20261020", "handoff.json", "application/json", encoded(bad)))
    result = ReadingRoom(store).build({"as_of": "2026-10-20T12:00:00Z", "ai_brief": LATEST})
    panel = result["ai_brief"]
    assert panel["status"] == "partial" and panel["reference"] is None and "view" not in panel
    assert panel["discovery"]["choices"][0]["reference"]["path"] == old["reference"]["path"]
    assert result["status"] == "partial" and result["plan"]["ai_brief"] == LATEST


def test_no_recent_object_and_future_candidate_are_not_execution_failure_or_silent_fallback(saved):
    store, research, _ = saved
    assert ReadingRoom(store).build({"as_of": AT, "ai_brief": LATEST})["ai_brief"]["status"] == "no-saved-handoff"
    doc = brief(store, research)
    receipt = handoff.save(store, doc, "saved")
    early = ReadingRoom(store).build({"as_of": "2026-10-19T10:00:00Z", "ai_brief": LATEST})
    assert early["ai_brief"]["status"] == "partial" and "view" not in early["ai_brief"]
    later = ReadingRoom(store).build({"as_of": "2026-10-28T12:00:00Z", "ai_brief": LATEST})
    assert later["ai_brief"]["status"] == "no-saved-handoff"
    exact = ReadingRoom(store).build({"as_of": "2026-10-28T12:00:00Z", "ai_brief": receipt["reference"]})
    assert exact["ai_brief"]["age"]["status"] == "older-than-24h" and exact["ai_brief"]["view"]["document"] == doc


def test_quick_handoff_and_caller_authority_are_not_accepted_as_ai_brief(saved):
    store, _, quick = saved
    receipt = handoff.save(store, quick, "quick")
    assert ReadingRoom(store).build({"as_of": AT, "ai_brief": receipt["reference"]})["ai_brief"]["status"] == "invalid"
    for selection in ({}, {"select": "latest-saved"}, {**LATEST, "watch_publication_required": False}):
        with pytest.raises((RecordError, TypeError)):
            ReadingRoom(store).build({"as_of": AT, "ai_brief": selection})
    with pytest.raises(RecordError, match="HOSTED_LATEST_INPUT"):
        handoff.perform(store, "latest-hosted-handoff", {"kind": "brief", "as_of": AT,
                        "snapshot": store.backend.head()}, "")


def test_optional_capacity_omission_retains_exact_reader_and_watch_publication(retained, monkeypatch):
    store, _, _, _, _, _, doc = retained
    receipt = handoff.save(store, doc, "bounded")
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 1)
    result = ReadingRoom(store).build({"as_of": doc["cutoff"], "ai_brief": receipt["reference"]})
    panel = result["ai_brief"]
    assert panel["status"] == "not-included" and "view" not in panel
    assert panel["watch_publication_required"] is True
    assert panel["open_request"] == {"operation": "read-hosted-handoff", "payload": {"reference": receipt["reference"]}}


def test_ai_only_counts_final_news_usage_before_real_six_mib_model_limit(saved, monkeypatch):
    store, research, _ = saved
    doc = brief(store, research)
    receipt = handoff.save(store, doc, "model-bound")
    plan = {"as_of": AT, "ai_brief": receipt["reference"]}
    complete = reading_commands.perform(store, {"plan": plan}, public=False)
    without_usage = {key: value for key, value in complete.items() if key != "news_source_reads"}
    usage_bytes = len(encoded(complete)) - len(encoded(without_usage))
    template = {**complete, "catalogue": {**complete["catalogue"], "synthetic_capacity_payload": ""}}
    padding = reading_room.MAX_VIEW_BYTES - len(encoded(template)) + usage_bytes - 1
    original = ReadingRoom._catalogue
    def full_existing_model(self, args):
        # Synthetic display pressure, not another saved object or source fetch.
        # Without the final news counter the full AI copy fits by exactly 1 B;
        # including the actual counter must omit only the optional AI display.
        return {**original(self, args), "synthetic_capacity_payload": "x" * padding}
    monkeypatch.setattr(ReadingRoom, "_catalogue", full_existing_model)
    head = store.backend.head()
    result = reading_commands.perform(store, {"plan": plan}, public=False)
    assert "odds_book" not in result["plan"] and result["ai_brief"]["status"] == "not-included"
    assert result["ai_brief"]["error"] == "ROOM_AI_BRIEF_VIEW_BOUND_USE_EXACT_READER"
    assert result["news_source_reads"] == complete["news_source_reads"]
    assert 6 * 1024 * 1024 - 16 * 1024 < len(encoded(result)) <= 6 * 1024 * 1024
    assert html_model(render_page(result)) == result and store.backend.head() == head


def test_shipped_page_exports_exact_baseline_and_research_requests_without_changing_model(saved):
    store, research, _ = saved
    doc = brief(store, research)
    old = handoff.save(store, doc, "baseline")
    doc = {**doc, "execution_date": "2026-10-20", "cutoff": "2026-10-20T11:30:00Z", "baseline": old["reference"]}
    doc["body"] = "# 合成作者原稿\r\n\r\n现金仍需核验。\n<script>untrusted()</script>\n"
    current = handoff.save(store, doc, "next")
    result = ReadingRoom(store).build({"as_of": doc["cutoff"], "ai_brief": current["reference"]})
    page = render_page(result)
    assert "save('ai-brief.md',doc.body,'text/markdown')" in page and "innerHTML" not in page
    response = exercise(page, [{"scope": "ai-brief-content", "click": "打开原 AI 晚报基线"},
                               {"scope": "ai-brief-content", "click": "导出 AI 晚报确切读取请求"},
                               {"scope": "ai-brief-content", "click": "下载 AI 晚报原正文"}])
    assert response["exports"][0]["value"]["payload"]["plan"]["ai_brief"] == old["reference"]
    assert response["exports"][1]["value"] == result["ai_brief"]["open_request"]
    assert response["exports"][2] == {"name": "ai-brief.md", "value": doc["body"]}
    assert "尚未执行" in response["notice"]
