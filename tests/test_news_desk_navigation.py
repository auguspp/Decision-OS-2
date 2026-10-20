"""Synthetic retained Git bytes; no provider, production records or remote calls."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import news_desk_navigation as nav, brief_delivery, brief_discovery
from decision_kernel.application.news_rolling_index import rolling_index
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import MissingObject, RecordError, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.interfaces.news_page import render_page, navigation_summary
from decision_kernel.observations.news_windows import combine, newsnow_window
from test_news_delivery import store, choice
from test_news_rolling import save, window

NOW = "2026-10-20T10:00:00Z"
OLD = "2026-10-16T12:00:00Z"


def retained_window(store, name="one", *, as_of=OLD, observed="2026-10-16T10:00:00Z", capture=False, raw=None):
    value = deepcopy(raw if raw is not None else window(observed, failed=True))
    if capture:
        value["source_outcomes"][0]["capture"] = {"requested_at": observed,
            "received_at": observed, "finished_at": observed}
        value.pop("reading_sha256")
        value["reading_sha256"] = digest(encoded(value))
    batch = save(store, "batch-" + name, value)
    index = rolling_index(store.read_source, [batch], as_of=as_of)
    ref = save(store, "news-roll-" + name, index)
    return ref, batch, index


def retained_edition(store, ref, name="one", *, cutoff=OLD, attention=False):
    plan = {"window": asdict(ref), "cutoff": cutoff, "kind": "evening"}
    artifacts = ResearchArtifacts(store)
    doc, logical = brief_delivery.build_edition(store.read_source, artifacts, plan)
    if attention:
        plan["attention"] = [choice(brief_delivery.news.entry_id(logical["entries"][0]))]
        doc, logical = brief_delivery.build_edition(store.read_source, artifacts, plan)
    saved = artifacts.put("edition-" + name, ArtifactInput.from_bytes(
        "news-edition-" + name, "news-edition.json", "application/json", encoded(doc)))
    return saved, doc


def read(store, plan=None, **kw):
    before = store.backend.head()
    result = nav.build_navigation(store, store.read_source, plan or {}, now=NOW, **kw)
    assert store.backend.head() == before
    return result


def test_today_render_and_later_cutoff_do_not_refresh_original_window_or_invent_capture(store):
    ref, _, _ = retained_window(store)
    saved, doc = retained_edition(store, ref, cutoff=NOW)
    result, model = read(store)
    assert result["display"]["status"] == "saved-edition" and model["edition"] == doc
    assert result["latest_saved_edition"]["reference"]["path"] == saved.path
    clocks = result["display"]["clocks"]
    assert clocks["window_age"]["status"] == "older-than-24h"
    assert clocks["delivery_age"]["status"] == "within-24h"
    assert clocks["last_successful_capture_at"] is None and clocks["capture_age"]["status"] == "unknown"
    assert clocks["latest_legacy_observation_at"] == "2026-10-16T10:00:00+00:00"
    assert clocks["rendered_at"] == NOW and clocks["complete_news_coverage"] is False
    assert encoded(model["edition"]["plan"]["window"]) == encoded(asdict(ref))
    assert "older-than-24h" in navigation_summary(model)


def test_new_retained_window_and_last_saved_edition_are_distinct_preview_has_no_choices_or_receipt(store):
    old, _, _ = retained_window(store, "old")
    saved, doc = retained_edition(store, old, attention=True)
    newest, _, _ = retained_window(store, "new", as_of="2026-10-19T12:00:00Z",
                                    observed="2026-10-19T10:00:00Z", capture=True)
    result, model = read(store)
    assert result["latest_window"]["reference"]["manifest"]["path"] == newest.manifest.path
    assert result["latest_saved_edition"]["reference"]["path"] == saved.path
    assert result["display"]["status"] == "retained-window-preview-not-saved"
    assert model["saved_reference"] is None and model["edition"]["attention_state"] == []
    assert model["edition"]["plan"]["cutoff"] == "2026-10-19T12:00:00+00:00"
    assert model["edition"]["plan"]["baseline"] is None
    reopened, _ = brief_delivery.open_edition(store.read_source, ResearchArtifacts(store), saved)
    assert reopened == doc and reopened["attention_state"]


def test_same_retained_window_reopens_exact_original_edition_after_snapshot_advances(store):
    ref, _, _ = retained_window(store)
    saved, document = retained_edition(store, ref, attention=True)
    save(store, "unrelated-synthetic-material", {"test": "not news"})
    result, model = read(store)
    assert model["edition"] == document and model["saved_reference"]["path"] == saved.path
    assert result["latest_window"]["reference"]["manifest"]["commit"] != ref.manifest.commit
    assert encoded(model["edition"]["plan"]["window"]) == encoded(asdict(ref))


@pytest.mark.parametrize("kind", ["window", "edition"])
def test_bad_candidate_preserves_full_denominator_and_does_not_fall_back_to_old_green(store, kind):
    ref, _, _ = retained_window(store)
    retained_edition(store, ref)
    if kind == "window":
        save(store, "news-roll-invalid", {"schema": "broken-newer-window"})
    else:
        ResearchArtifacts(store).put("invalid-edition", ArtifactInput.from_bytes(
            "news-edition-invalid", "news-edition.json", "application/json", encoded({"schema": "broken"})))
    result, model = read(store, {"history": {"limit": 1}})
    assert model is None and result["status"] == "partial"
    assert result["history"]["total_candidates"] == result["history"]["scanned_candidates"] == 3
    assert result["history"]["invalid_candidates"] == 1 and result["history"]["returned"] == 1
    seen = result["history"]["items"]
    while result["next_plan"] is not None:
        result, _ = read(store, result["next_plan"])
        seen.extend(result["history"]["items"])
    assert len(seen) == 3 and any(row["open_request"] is None for row in seen)


def test_fixed_history_pages_keep_snapshot_after_append_explicit_refresh_discovers_newer(store):
    first, _, _ = retained_window(store)
    retained_edition(store, first)
    initial, _ = read(store, {"history": {"limit": 1}})
    later, _, _ = retained_window(store, "later", as_of="2026-10-19T12:00:00Z", observed="2026-10-19T11:00:00Z")
    next_page, _ = read(store, initial["next_plan"])
    assert next_page["snapshot"] == initial["snapshot"]
    assert next_page["history"]["total_candidates"] == 2 and next_page["history"]["offset"] == 1
    refreshed, _ = read(store, initial["refresh_plan"])
    assert refreshed["snapshot"] != initial["snapshot"] and refreshed["history"]["total_candidates"] == 3
    assert refreshed["latest_window"]["reference"]["manifest"]["path"] == later.manifest.path
    bad = deepcopy(initial["next_plan"])
    bad["history"]["cursor"]["inventory_sha256"] = "0" * 64
    with pytest.raises(RecordError, match="CURSOR_SCOPE_CHANGED"):
        read(store, bad)
    foreign = deepcopy(initial["next_plan"])
    foreign["history"]["cursor"]["repository"] = "example/foreign"
    with pytest.raises(RecordError, match="CURSOR_SCOPE_CHANGED"):
        nav.build_navigation(store, lambda _: pytest.fail("foreign cursor must fail before body reads"), foreign, now=NOW)


def test_ties_are_explicit_and_inventory_bound_cannot_become_first_n_latest(store, monkeypatch):
    first, _, _ = retained_window(store, "z")
    second, _, _ = retained_window(store, "a")
    result, _ = read(store)
    assert result["latest_window"]["same_as_of_count"] == 2
    assert result["latest_window"]["reference"]["manifest"]["path"] == min(first.manifest.path, second.manifest.path)
    monkeypatch.setattr(nav, "MAX_WINDOWS", 1)
    monkeypatch.setattr(store, "read_source", lambda _: pytest.fail("bound must stop before reading index bodies"))
    result, model = read(store)
    assert model is None and result["latest_window"]["reference"] is None
    assert result["latest_window"]["total_candidates"] == 2 and result["latest_window"]["scanned"] == 0


def test_selected_dependency_failure_is_not_qualified_and_does_not_reopen_older(store, monkeypatch):
    old, _, _ = retained_window(store, "old")
    retained_edition(store, old)
    _, batch, _ = retained_window(store, "new", as_of="2026-10-19T12:00:00Z", observed="2026-10-19T11:00:00Z")
    original = store.read_source
    def missing(ref):
        if ref.manifest.path == batch.manifest.path:
            raise MissingObject("SYNTHETIC_MISSING")
        return original(ref)
    monkeypatch.setattr(store, "read_source", missing)
    result, model = read(store)
    assert model is None and result["status"] == "partial"
    assert result["latest_window"]["status"] == "selected-window-unavailable"
    assert "SYNTHETIC_MISSING" not in encoded(result).decode()


@pytest.mark.parametrize("empty_capture", [True, False])
def test_zero_success_and_all_failed_keep_different_original_health_and_capture_clocks(store, empty_capture):
    raw = (combine([newsnow_window(encoded({"status": "success", "id": "cls", "items": []}),
                "cls", observed_at="2026-10-16T10:00:00Z")]) if empty_capture else
           combine([{"status": "source-unavailable", "source_id": "thepaper"}]))
    retained_window(store, raw=raw, capture=empty_capture)
    result, model = read(store)
    assert model["articles"] == [] and result["status"] == "readable"
    clocks = result["display"]["clocks"]
    assert bool(clocks["last_successful_capture_at"]) is empty_capture
    assert model["edition"]["window"]["coverage"]["failed_source_windows"] == (0 if empty_capture else 1)
    assert model["edition"]["window"]["coverage"]["unlocated_source_windows"] == (0 if empty_capture else 1)


def test_empty_store_and_reversed_read_clock_are_not_silent_healthy_news(store):
    result, model = read(store)
    assert result["status"] == "empty" and model is None and result["history"]["total_candidates"] == 0
    html = render_page({"edition": None, "articles": [], "saved_reference": None, "navigation": result})
    assert 'id="navigation"' in html and "no-retained-desk" in html
    retained_window(store)
    result, _ = read(store, {"as_of": "2026-10-15T12:00:00Z"})
    assert result["display"]["clocks"]["window_age"]["status"] == "read-clock-before-original-time"
    fractional = nav.age("2026-10-20T10:00:00.500000Z", NOW, basis="synthetic-receipt")
    assert fractional["age_seconds"] == -0.5 and fractional["status"] == "read-clock-before-original-time"


def test_selected_publication_block_is_not_replaced_with_preview_or_older_edition(store):
    ref, _, _ = retained_window(store)
    retained_edition(store, ref, attention=True)
    def deny(_):
        raise RecordError("NEWS_DESK_FEEDBACK_PUBLICATION_NOT_CONFIRMED")
    result, model = read(store, authorize_document=deny)
    assert model is None and result["latest_saved_edition"]["status"] == "selected-edition-unavailable"
    assert result["latest_saved_edition"]["error"] == "NEWS_EDITION_PUBLICATION_NOT_CONFIRMED"
    assert result["display"]["status"] == "unavailable"
