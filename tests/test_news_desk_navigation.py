"""Synthetic retained Git bytes; no provider, production records or remote calls."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import news_desk_navigation as nav, brief_delivery, brief_discovery, news_rolling
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


def retained_legacy_window(store, name="legacy", *, as_of=OLD,
                           observed="2026-10-16T10:00:00Z", predecessor=None):
    batch = save(store, "legacy-batch-" + name, window(observed, failed=True))
    value = news_rolling.rolling_news(store.read_source, [batch], as_of=as_of, predecessor=predecessor)
    return save(store, "news-roll-" + name, value), batch, value


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


def test_valid_inline_history_preserves_denominator_and_newer_index_uses_original_replay(store):
    first, _, _ = retained_legacy_window(store, "legacy-first")
    second, _, _ = retained_legacy_window(store, "legacy-successor", as_of="2026-10-16T13:00:00Z",
        observed="2026-10-16T11:00:00Z", predecessor=first)
    newest, _, index = retained_window(store, "new-index", as_of="2026-10-19T12:00:00Z",
                                       observed="2026-10-19T10:00:00Z", capture=True)
    result, model = read(store)
    latest, history = result["latest_window"], result["history"]
    assert latest["complete"] is True and latest["failures"] == []
    assert latest["total_candidates"] == latest["scanned"] == latest["valid_candidates"] == 3
    assert latest["reference"]["manifest"]["path"] == newest.manifest.path
    assert latest["snapshot_schema"] == "dos2-news-rolling-index-v1"
    assert latest["dependency_replay"] == "verified-exact-selected-batches"
    assert history["total_candidates"] == history["scanned_candidates"] == history["listed_candidates"] == 3
    assert history["invalid_candidates"] == 0 and history["inventory_complete"] is True
    old = [row for row in history["items"] if row.get("snapshot_schema") == news_rolling.SCHEMA]
    assert {row["reference"]["manifest"]["path"] for row in old} == {first.manifest.path, second.manifest.path}
    assert {row["original_time"] for row in old} == {"2026-10-16T12:00:00+00:00", "2026-10-16T13:00:00+00:00"}
    assert all(row["status"] == "candidate-metadata-verified" and row["open_request"] is None
               and row["read_status"] == "legacy-offline-reader-only" for row in old)
    assert result["display"]["status"] == "retained-window-preview-not-saved"
    assert model["saved_reference"] is None and model["edition"]["attention_state"] == []
    assert model["edition"]["plan"]["baseline"] is None and model["edition"]["plan"]["attention"] == []
    assert model["edition"]["plan"]["cutoff"] == index["as_of"]
    assert model["edition"]["window"]["index_sha256"] == index["index_sha256"]
    assert result["display"]["clocks"]["last_successful_capture_at"] == "2026-10-19T10:00:00+00:00"


@pytest.mark.parametrize("damage", ["digest", "unknown-schema"])
def test_invalid_inline_candidate_still_blocks_complete_discovery_even_when_older(store, damage):
    _, _, value = retained_legacy_window(store)
    bad = deepcopy(value)
    if damage == "digest":
        bad["rolling_sha256"] = "0" * 64
    else:
        bad["schema"] = "dos2-news-rolling-unknown"
        bad["rolling_sha256"] = digest(encoded({k: v for k, v in bad.items() if k != "rolling_sha256"}))
    invalid = save(store, "news-roll-damaged-legacy", bad)
    retained_window(store, "new-index", as_of="2026-10-19T12:00:00Z", observed="2026-10-19T10:00:00Z")
    result, model = read(store)
    assert model is None and result["status"] == "partial"
    latest, history = result["latest_window"], result["history"]
    assert latest["complete"] is False and latest["reference"] is None
    assert latest["total_candidates"] == latest["scanned"] == history["listed_candidates"] == 3
    assert latest["valid_candidates"] == 2 and history["invalid_candidates"] == 1
    assert latest["failures"][0]["path"] == invalid.manifest.path
    assert latest["failures"][0]["error"] == "NEWS_WINDOW_CANDIDATE_UNREADABLE"
    assert history["items"][-1]["open_request"] is None


@pytest.mark.parametrize("legacy_as_of", ["2026-10-19T13:00:00Z", "2026-10-19T12:00:00Z"])
def test_selected_inline_window_is_not_readable_by_desk_and_never_falls_back(store, legacy_as_of):
    compact, _, _ = retained_window(store, "zzz-index", as_of="2026-10-19T12:00:00Z",
                                    observed="2026-10-19T10:00:00Z")
    saved, _ = retained_edition(store, compact, cutoff="2026-10-19T12:00:00Z", attention=True)
    legacy, _, value = retained_legacy_window(store, "aaa-inline", as_of=legacy_as_of,
                                            observed="2026-10-19T11:00:00Z")
    assert news_rolling.read_rolling(value)["selected_count"] > 0  # Existing offline contract remains valid.
    result, model = read(store)
    latest = result["latest_window"]
    assert latest["complete"] is True and latest["valid_candidates"] == latest["total_candidates"] == 2
    assert latest["reference"]["manifest"]["path"] == legacy.manifest.path
    assert latest["latest_as_of"] == value["as_of"] and latest["snapshot_schema"] == news_rolling.SCHEMA
    assert latest["status"] == "selected-not-readable" and "clocks" not in latest
    assert latest["dependency_replay"] == "not-performed-for-legacy-materialized-window"
    assert latest["error"] == "NEWS_LEGACY_WINDOW_DESK_REPLAY_UNAVAILABLE"
    assert latest["same_as_of_count"] == (2 if legacy_as_of.endswith("12:00:00Z") else 1)
    assert result["latest_saved_edition"]["reference"]["path"] == saved.path
    assert result["latest_saved_edition"]["dependency_replay"] == "verified-original-edition-and-dependencies"
    assert model is None and result["status"] == "partial"
    assert result["display"] == {"status": "unavailable", "reference": None}
    old = next(row for row in result["history"]["items"] if row.get("snapshot_schema") == news_rolling.SCHEMA)
    assert old["open_request"] is None and old["read_status"] == "legacy-offline-reader-only"
