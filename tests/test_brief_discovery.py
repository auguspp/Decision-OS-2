"""Latest saved Brief discovery over real immutable Git artifacts and room reads."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import brief_delivery, brief_discovery
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import MissingObject, RevisionInput, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from test_news_delivery import store, index_ref, plan
from test_news_read_budget import sources, transport
from test_research_runtime import inputs

READ_AT = "2026-10-19T07:20:00Z"


def retained(store, name, *, cutoff="2026-10-16T12:00:00Z", document=None):
    if document is None:
        value = plan(index_ref(store, name))
        value["cutoff"] = cutoff
        document, _ = brief_delivery.build_edition(store.read_source, ResearchArtifacts(store),
                                                   value)
    saved = ResearchArtifacts(store).put("retain-" + name, ArtifactInput.from_bytes(
        "news-edition-" + name, "news-edition.json", "application/json", encoded(document)))
    return saved, document


def discover(store, snapshot=None):
    return brief_discovery.discover_latest(store, snapshot=snapshot or store.backend.head(), as_of=READ_AT)


def test_latest_is_greatest_cutoff_not_filename_and_does_not_write_or_fetch_sources(store, monkeypatch):
    old, _ = retained(store, "z-old")
    newest, doc = retained(store, "a-new", cutoff="2026-10-18T12:00:00Z")
    head = store.backend.head()
    monkeypatch.setattr(store, "read_source", lambda _: pytest.fail("discovery must not replay source dependencies"))
    result = discover(store)
    assert result["status"] == "selected" and result["complete"] is True
    assert result["reference"]["path"] == newest.path != old.path
    assert result["reference"]["commit"] == head and result["scanned"] == result["total_candidates"] == 2
    assert result["latest_cutoff"] == doc["plan"]["cutoff"] and store.backend.head() == head
    assert result["age"]["status"] == "within-24h" and "brief_markdown" not in result


def test_stale_saved_edition_stays_readable_and_cannot_be_called_current_news(store):
    retained(store, "old")
    view = ReadingRoom(store).build({"as_of": READ_AT, "brief": {"select": "latest-saved"}})
    assert view["brief"]["status"] == "readable"
    assert view["brief"]["age"]["status"] == "older-than-24h"
    assert view["brief_discovery"]["dependency_replay"] == "not-performed-by-discovery"
    assert view["plan"]["brief"] == view["brief"]["reference"]


def test_page_navigation_keeps_original_brief_after_append_and_refresh_finds_new_version(store):
    for name in ("alpha", "zeta"):
        store.submit_revision(name, RevisionInput(name, "r1", "quick", "Synthetic original",
                                                 "Synthetic question", "synthetic", unresolved=("Synthetic only",)))
    old, _ = retained(store, "old")
    request = {"as_of": READ_AT, "catalogue": {"limit": 1}, "brief": {"select": "latest-saved"}}
    before = deepcopy(request)
    first = ReadingRoom(store).build(request)
    new, _ = retained(store, "new", cutoff="2026-10-18T12:00:00Z")
    second = ReadingRoom(store).build(first["next_plan"])
    assert second["catalogue"]["snapshot"] == first["catalogue"]["snapshot"]
    assert second["brief"]["reference"] == first["brief"]["reference"]
    assert second["brief"]["reference"]["path"] == old.path
    assert "brief_discovery" not in second and request == before
    refreshed = ReadingRoom(store).build(first["refresh_plan"])
    assert refreshed["brief"]["reference"]["path"] == new.path
    assert refreshed["catalogue"]["snapshot"] != first["catalogue"]["snapshot"]


def test_explicit_reference_never_scans_or_switches_to_later_brief(store, monkeypatch):
    old, doc = retained(store, "old")
    retained(store, "new", cutoff="2026-10-18T12:00:00Z")
    monkeypatch.setattr(brief_discovery, "discover_latest", lambda *_a, **_k: pytest.fail("exact read must not scan"))
    result = ReadingRoom(store).build({"as_of": READ_AT, "brief": asdict(old)})
    assert result["brief"]["reference"] == asdict(old) and result["brief"]["view"] == doc
    assert result["refresh_plan"]["brief"] == asdict(old)


def test_bad_candidate_prevents_false_latest_but_good_versions_remain_exact_choices(store):
    good, doc = retained(store, "good")
    invalid = deepcopy(doc)
    invalid["plan"]["cutoff"] = "bad-clock"
    invalid["edition_sha256"] = digest(encoded({k: v for k, v in invalid.items() if k != "edition_sha256"}))
    retained(store, "invalid", document=invalid)
    result = discover(store)
    assert result["status"] == "partial" and result["reference"] is None and result["complete"] is False
    assert result["choices"][0]["reference"]["path"] == good.path and len(result["failures"]) == 1
    view = ReadingRoom(store).build({"as_of": READ_AT, "brief": {"select": "latest-saved"}})
    assert view["status"] == "partial" and view["brief"]["status"] == "partial"
    assert "view" not in view["brief"]


def test_arbitrary_named_artifacts_are_not_covered_but_explicit_open_is_retained(store):
    _, doc = retained(store, "normal")
    custom = ResearchArtifacts(store).put("custom", ArtifactInput.from_bytes(
        "custom-human-attachment", "edition.json", "application/json", encoded(doc)))
    result = discover(store)
    assert result["total_candidates"] == 1 and result["scope"] == "normal-news-host-editions"
    assert ReadingRoom(store).build({"as_of": READ_AT, "brief": asdict(custom)})["brief"]["view"] == doc


def test_same_cutoff_reports_ambiguity_and_uses_reproducible_reference_order(store):
    z, _ = retained(store, "z-version")
    a, _ = retained(store, "a-version")
    result = discover(store)
    assert result["same_cutoff_count"] == 2 and result["reference"]["path"] == min(a.path, z.path)
    assert result["tie_rule"] == "artifact-path-ascending-not-storage-order"


def test_inventory_limit_does_not_scan_first_n_and_call_it_latest(store, monkeypatch):
    retained(store, "first")
    retained(store, "second")
    monkeypatch.setattr(brief_discovery, "MAX_EDITIONS", 1)
    monkeypatch.setattr(brief_delivery, "_read_document", lambda *_: pytest.fail("oversized inventory must stop before document reads"))
    result = discover(store)
    assert result["status"] == "not-included" and result["scanned"] == 0
    assert result["total_candidates"] == 2 and result["reference"] is None and not result["complete"]


def test_empty_normal_catalogue_is_not_a_completed_brief_or_quiet_market(store):
    result = ReadingRoom(store).build({"as_of": READ_AT, "brief": {"select": "latest-saved"}})
    assert result["brief"]["status"] == "no-saved-brief" and result["brief_discovery"]["complete"] is True
    assert result["brief"]["reference"] is None and result["execution"] == "not-started"


def test_discovery_does_not_substitute_for_original_dependency_replay(store, monkeypatch):
    retained(store, "missing-source")
    monkeypatch.setattr(store, "read_source", lambda _: (_ for _ in ()).throw(MissingObject("private detail")))
    result = ReadingRoom(store).build({"as_of": READ_AT, "brief": {"select": "latest-saved"}})
    assert result["brief_discovery"]["status"] == "selected"
    assert result["brief"]["status"] != "readable" and result["status"] == "partial"
    assert b"private detail" not in encoded(result)


def test_age_uses_review_clock_without_claiming_historical_pit():
    result = brief_discovery.cutoff_age("2026-10-19T12:00:00+08:00", "2026-10-19T03:00:00Z")
    assert result["status"] == "read-clock-before-cutoff" and result["age_seconds"] == -3600


def test_original_read_host_accepts_selector_and_outputs_human_page_without_records_writes(sources, tmp_path):
    from decision_kernel.interfaces import research_runtime
    model, store, _, _, _ = sources
    saved, doc = retained(store, "native-host")
    before, api = model.git.head(), transport(model)
    payload = {"plan": {"as_of": READ_AT, "brief": {"select": "latest-saved"}},
               "public_research_custody_confirmed": True}
    report = research_runtime.run_command(model.binding, api=api, inputs=inputs("reading-room", payload),
        code_sha="a" * 40, output=tmp_path / "native-room", run_id="123", attempt="1")
    result = report["result"]
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert model.writes == 0 and model.git.head() == before and api._requests < 512
    assert result["brief_discovery"]["status"] == "selected"
    assert result["brief"]["reference"]["path"] == saved.path and result["brief"]["view"] == doc
    page = (tmp_path / "native-room" / "reading-room.html").read_text()
    assert "<h1>日常阅读</h1>" in page and page.index('id="brief"') < page.index('id="research"')
    assert "readingDocument(b.brief_markdown)" in page and "innerHTML" not in page
