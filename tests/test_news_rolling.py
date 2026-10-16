"""Owns the 24-hour observation clock, exact predecessor and normal read contract."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces.news_rolling_cli import main
from decision_kernel.application import news_rolling as roll
from decision_kernel.observations.news_windows import combine, newsnow_window
from test_news_windows import newsnow

END = "2026-10-16T12:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "news.git"))


def save(store, name, value):
    return store.put_source(name, SourceInput(source_id=name, kind="content", title=name,
        origin="synthetic://news-rolling", text=encoded(value).decode(), custody="provided"))


def window(at, *, title="合成新闻观察", item="a", failed=False):
    windows = [newsnow_window(newsnow(title=title, item_id=item), "cls", observed_at=at)]
    if failed:
        windows.append({"status": "source-unavailable", "source_id": "thepaper"})
    return combine(windows)


def build(store, refs, **kw):
    return roll.rolling_news(store.read_source, refs, as_of=kw.pop("as_of", END), **kw)


@pytest.mark.parametrize("at,expected", [
    ("2026-10-15T13:00:00Z", 1),  # 23 hours: would have been lost by the legacy 18h setting.
    ("2026-10-15T12:00:00Z", 1),
    ("2026-10-15T11:59:59Z", 0),
    ("2026-10-16T12:00:00Z", 1),
])
def test_exact_24h_closed_boundary_not_18h(store, at, expected):
    ref = save(store, "boundary", window(at))
    view = build(store, [ref])
    assert view["window_hours"] == 24 and len(view["entries"]) == expected
    assert view["window_start"] == "2026-10-15T12:00:00+00:00"
    assert view["coverage"]["complete_news_coverage"] is False


def test_timezone_equivalence_and_publisher_claim_is_not_rolling_clock(store):
    ref = save(store, "zone", window("2026-10-15T21:00:00+08:00"))
    a = build(store, [ref])
    b = build(store, [ref], as_of="2026-10-16T20:00:00+08:00")
    assert a == b
    row = a["entries"][0]["observation"]
    assert row["published_claim"]["status"] == "unverified-provider-claim"
    assert row["published_claim"]["raw"] != a["entries"][0]["first_seen_in_window"]


@pytest.mark.parametrize("as_of", ["2026-10-16", "2026-10-16T12:00:00", "bad", "0001-01-01T00:00:00Z"])
def test_invalid_as_of_makes_no_reader_calls(as_of):
    with pytest.raises(RecordError):
        roll.rolling_news(lambda _: pytest.fail("no read"), [], as_of=as_of)


def test_predecessor_replays_exact_source_versions_without_recounting_same_ref(store, tmp_path):
    first = save(store, "first", window("2026-10-15T13:00:00Z", failed=True))
    a = build(store, [first], as_of="2026-10-16T10:00:00Z")
    prior = save(store, "prior", a)
    second = save(store, "second", window("2026-10-16T11:00:00Z"))
    b = build(store, [second, first, second], predecessor=prior)
    assert b["predecessor"]["status"] == "restored-exact-reference"
    assert len(b["entries"]) == 1
    entry = b["entries"][0]
    assert entry["seen_batch_count_in_window"] == 2
    assert entry["first_seen_in_window"] == "2026-10-15T13:00:00+00:00"
    assert entry["last_seen_in_window"] == "2026-10-16T11:00:00+00:00"
    assert b["coverage"]["failed_source_windows"] == 1
    assert store.read_source(first)["data"] == encoded(window("2026-10-15T13:00:00Z", failed=True)).decode()
    # Another store object reads the saved successor using the existing immutable SourceRef.
    result = save(store, "successor", b)
    fresh = VersionedStore(GitStore(tmp_path / "news.git"))
    assert roll.verify_rolling(json.loads(fresh.read_source(result)["data"])) == b


def test_same_batch_bytes_under_two_source_ids_are_not_two_captures(store):
    value = window("2026-10-16T10:00:00Z")
    refs = [save(store, "alias-a", value), save(store, "alias-b", value)]
    view = build(store, refs)
    assert view["entries"][0]["seen_batch_count_in_window"] == 1
    assert view["coverage"]["retained_batches"] == 1
    assert len(view["source_outcomes"]) == 1


def test_changed_title_is_new_version_not_duplicate_article_evidence(store):
    a = save(store, "old-title", window("2026-10-16T10:00:00Z", title="原标题"))
    b = save(store, "new-title", window("2026-10-16T11:00:00Z", title="修订标题"))
    entries = build(store, [a, b])["entries"]
    assert len(entries) == 2
    assert len({e["observation"]["article_id"] for e in entries}) == 1
    assert all(e["observation"]["event_verified"] is False for e in entries)


def test_window_counts_expire_not_lifetime_or_artificial_new_publication(store):
    old = save(store, "expiring", window("2026-10-15T13:00:00Z"))
    recent = save(store, "retained", window("2026-10-16T11:00:00Z"))
    prior = save(store, "expiring-prior", build(store, [old, recent]))
    later = build(store, [], predecessor=prior, as_of="2026-10-16T14:00:00Z")
    assert later["entries"][0]["seen_batch_count_in_window"] == 1
    assert later["entries"][0]["first_seen_in_window"] == "2026-10-16T11:00:00+00:00"
    assert len(later["batch_references"]) == 1


def test_clocked_failure_without_articles_stays_visible(store):
    view = combine([{"status": "source-unavailable", "source_id": "thepaper"}])
    view["source_outcomes"][0]["capture"] = {"requested_at": "2026-10-16T10:00:00Z",
        "finished_at": "2026-10-16T10:00:02Z", "error_code": "NEWS_HEADLINE_INVALID"}
    view.pop("reading_sha256"); view["reading_sha256"] = digest(encoded(view))
    ref = save(store, "failure", view)
    output = roll.read_rolling(build(store, [ref]), source="nonexistent", limit=1)
    assert output["rows"] == [] and output["selected_count"] == 0
    assert output["coverage"]["failed_source_windows"] == 1
    assert output["source_outcomes"][0]["outcome"]["capture"]["error_code"] == "NEWS_HEADLINE_INVALID"
    assert output["source_outcomes_scope"] == "whole-24h-window"


def test_rejected_predecessor_does_not_erase_new_window_or_gap_on_successor(store):
    unrelated = save(store, "not-history", {"schema": "not-rolling"})
    current = save(store, "current", window("2026-10-16T10:00:00Z"))
    view = build(store, [current], predecessor=unrelated)
    assert len(view["entries"]) == 1
    assert view["predecessor"]["status"] == "unavailable-or-rejected"
    assert view["losses"][0]["kind"] == "PREDECESSOR_GAP"
    prior = save(store, "gap-prior", view)
    next_view = build(store, [], predecessor=prior, as_of="2026-10-16T13:00:00Z")
    assert next_view["predecessor"]["status"] == "restored-exact-reference"
    assert next_view["losses"] == view["losses"]


def test_future_or_corrupt_batch_is_a_gap_not_successful_empty_news(store):
    future = save(store, "future", window("2026-10-16T13:00:00Z"))
    corrupted = window("2026-10-16T10:00:00Z"); corrupted["reading_sha256"] = "0" * 64
    bad = save(store, "corrupt", corrupted)
    valid = save(store, "valid", window("2026-10-16T11:00:00Z"))
    view = build(store, [future, bad, valid])
    assert len(view["entries"]) == 1 and len(view["rejected_references"]) == 2
    assert view["losses"][0]["count"] == 2


def test_metadata_only_does_not_forward_extra_body_or_research_authority(store):
    for n, change in enumerate(({"full_text": "not authorized"}, {"research_started": True})):
        value = window("2026-10-16T10:00:00Z")
        value["observations"][0].update(change)
        value.pop("reading_sha256"); value["reading_sha256"] = digest(encoded(value))
        ref = save(store, "extra-"+str(n), value)
        result = build(store, [ref])
        assert result["entries"] == [] and result["losses"]


def test_truncation_and_reconstruction_limits_are_explicit(store, monkeypatch):
    monkeypatch.setattr(roll, "MAX_OBSERVATIONS", 1)
    refs = [save(store, "limit-"+str(n), window("2026-10-16T10:00:00Z", item=str(n))) for n in range(3)]
    view = build(store, refs)
    assert len(view["entries"]) == 1
    assert {x["kind"]: x["count"] for x in view["losses"]}["OBSERVATION_LIMIT"] == 2
    assert len(view["source_outcomes"]) == 3
    monkeypatch.setattr(roll, "MAX_OBSERVATIONS", 2000)
    monkeypatch.setattr(roll, "MAX_BATCHES", 2)
    second = build(store, refs)
    assert len(second["batch_references"]) == 2
    assert any(x["kind"] == "BATCH_LIMIT" for x in second["losses"])


def test_tampered_rolling_and_changed_window_are_refused_even_with_new_hash(store):
    ref = save(store, "tamper", window("2026-10-16T10:00:00Z"))
    value = build(store, [ref])
    bad = deepcopy(value); bad["entries"] = []
    with pytest.raises(RecordError): roll.read_rolling(bad)
    value["window_hours"] = 18; value.pop("rolling_sha256")
    value["rolling_sha256"] = digest(encoded(value))
    with pytest.raises(RecordError): roll.read_rolling(value)


def test_pagination_binds_window_and_filters_and_always_retains_health(store):
    refs = [save(store, "page-"+str(n), window("2026-10-16T10:00:00Z", item=str(n), failed=True)) for n in range(3)]
    value = build(store, refs)
    page = roll.read_rolling(value, limit=1)
    second = roll.read_rolling(value, limit=1, cursor=page["next_cursor"])
    assert page["rows"] != second["rows"] and page["source_outcomes"] == second["source_outcomes"]
    with pytest.raises(RecordError): roll.read_rolling(value, query="changed", cursor=page["next_cursor"])
    with pytest.raises(RecordError): roll.read_rolling(value, source="cls", cursor=page["next_cursor"])
    empty = roll.read_rolling(value, query="unmatched")
    assert not empty["rows"] and empty["source_outcomes"] == page["source_outcomes"]


def test_offline_installed_cli_new_process_preserves_input_and_health(store, tmp_path):
    ref = save(store, "cli", window("2026-10-16T10:00:00Z", failed=True))
    path = tmp_path / "rolling.json"; original = encoded(build(store, [ref])); path.write_bytes(original)
    run = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.news_rolling_cli", "read",
                          "--window", str(path), "--source", "nonexistent"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert not result["rows"] and result["coverage"]["failed_source_windows"] == 1
    assert path.read_bytes() == original


def test_native_host_not_available_is_not_silently_a_local_write(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    out = tmp_path / "out"
    assert main(["build", "--out", str(out)]) == 2 and not out.exists()


def test_dedicated_workflow_is_manual_and_reuses_source_writer_without_source_secrets():
    import yaml
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/news-rolling.yml").read_text()
    flow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"} and flow["permissions"] == {}
    assert flow["jobs"]["build"]["permissions"]["contents"] == "read"
    assert flow["jobs"]["save"]["permissions"]["contents"] == "write"
    assert "public_metadata_custody_confirmed" in flow["jobs"]["save"]["if"]
    assert "TUSHARE_PROXY_API_KEY" not in text and "schedule:" not in text


@pytest.mark.parametrize("field,bad", [("published_claim", 42), ("other_time_claims", {"extra.date": 42})])
def test_malformed_nested_claim_is_local_rejection_not_uncaught_error(store, field, bad):
    value = window("2026-10-16T10:00:00Z")
    value["observations"][0][field] = bad
    value.pop("reading_sha256"); value["reading_sha256"] = digest(encoded(value))
    malformed = save(store, "nested-invalid", value)
    valid = save(store, "nested-valid", window("2026-10-16T11:00:00Z"))
    result = build(store, [malformed, valid])
    assert len(result["entries"]) == 1 and len(result["rejected_references"]) == 1


def test_saved_view_cannot_promote_research_or_forward_extra_body_by_resealing(store):
    ref = save(store, "saved-extra", window("2026-10-16T10:00:00Z"))
    value = build(store, [ref])
    value["entries"][0]["observation"]["full_text"] = "not news metadata"
    roll._seal(value)
    with pytest.raises(RecordError): roll.read_rolling(value)


def test_byte_limit_does_not_silently_shorten_window_or_erase_health(store, monkeypatch):
    references = [save(store, "bytes-"+str(n), window("2026-10-16T10:00:00Z", item=str(n), title="长标题"*500))
                  for n in range(4)]
    monkeypatch.setattr(roll, "MAX_BYTES", 14000)
    value = build(store, references)
    assert len(encoded(value)) <= 14000 and len(value["entries"]) < 4
    assert value["window_hours"] == 24 and len(value["source_outcomes"]) == 4
    assert any(loss["kind"] == "BYTE_LIMIT" for loss in value["losses"])
