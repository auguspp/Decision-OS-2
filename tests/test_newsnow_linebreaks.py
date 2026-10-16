"""Evidence-scoped LF projection preserves source versions and existing consumers."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application.news_rolling_index import open_index, read_index, restore_index, rolling_index
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.news_capture import capture_newsnow
from decision_kernel.interfaces import news_cli
from decision_kernel.observations import news_windows as news
from test_news_headlines import wire

AT = "2026-10-16T07:00:00Z"
LATER = "2026-10-16T08:00:00Z"


def window(title, source="wallstreetcn"):
    return news.newsnow_window(wire(source, title=title), source, observed_at=AT)


@pytest.mark.parametrize("source", ["wallstreetcn", "thepaper"])
def test_only_lf_is_projected_one_for_one_with_disclosed_provenance(source):
    raw_title = "\n合成标题  A\n\nB\n"
    raw = wire(source, title=raw_title)
    result = news.newsnow_window(raw, source, observed_at=AT)
    projected = result["projection"]
    row, = projected["observations"]
    assert row["title"] == " 合成标题  A  B " and len(row["title"]) == len(raw_title)
    assert projected["upstream_sha256"] == digest(raw)
    assert row["published_claim"] == {"raw": None, "status": "missing"}
    assert row["body_archived"] is row["event_verified"] is row["publication_verified"] is False
    reading = news.combine([result])
    assert reading["source_outcomes"][0]["headline_projection"] == {
        "policy": "newsnow-lf-to-space-v1", "changed_titles": 1, "raw_titles_retained": False}
    assert news.verify_reading(reading) == reading


def test_display_collision_does_not_erase_distinct_raw_title_versions():
    raw_lf, raw_space = window("合成\n标题"), window("合成 标题")
    first, second = raw_lf["projection"]["observations"][0], raw_space["projection"]["observations"][0]
    assert first["title"] == second["title"] and first["article_id"] == second["article_id"]
    assert first["version_id"] != second["version_id"]
    assert window("合成\n标题") == raw_lf
    assert "headline_projection" not in raw_space["projection"]
    assert "headline_projection" not in news.combine([raw_space])["source_outcomes"][0]
    merged = news.merge_readings([news.combine([raw_lf]), news.combine([raw_space])])
    assert len(merged["observations"]) == 2


def test_unmodified_titles_keep_the_original_version_algorithm_and_shape():
    title = "合成标题  保留空格"
    result = window(title)
    row, = result["projection"]["observations"]
    body = news._sha({"title": title, "extra_date": None})
    assert row["version_id"] == news._sha({"article": row["article_id"], "title": title,
                                           "published": None, "body": body})
    assert row["title"] == title
    assert set(news.combine([result])["source_outcomes"][0]) == {"source", "status", "observations"}
    # Original rows remain acceptable; new projections introduce no per-row fields.
    assert set(row) == {"provider", "source_id", "article_id", "version_id", "title", "url",
        "published_claim", "other_time_claims", "observed_at", "representation", "publication_verified",
        "event_verified", "research_started", "body_archived"}


@pytest.mark.parametrize("source", ["cls", "fastbull", "jin10", "mktnews", "gelonghui"])
def test_unreviewed_sources_still_reject_lf(source):
    domain = news.NEWSNOW_DOMAINS[source][0]
    raw = encoded({"status": "success", "id": source, "items": [
        {"id": "synthetic", "url": "https://www." + domain + "/item", "title": "合成\n标题"}]})
    with pytest.raises(RecordError, match="NEWS_HEADLINE_INVALID"):
        news.newsnow_window(raw, source, observed_at=AT)


def test_all_other_controls_and_relay_remain_strict():
    for point in [*range(10), *range(11, 32), 127]:
        for source in ("wallstreetcn", "thepaper"):
            with pytest.raises(RecordError, match="NEWS_HEADLINE_INVALID"):
                window("合成\n标题" + chr(point), source)
    relay = encoded({"code": 0, "data": {"fields": ["datetime", "title"],
        "items": [["2026-10-16 08:00:00", "合成\n标题"]]}})
    with pytest.raises(RecordError, match="NEWS_HEADLINE_INVALID"):
        news.relay_window(relay, "news", "cls", observed_at=AT, request={})


@pytest.mark.parametrize("title", [None, "\n\n", "\ud800", "a"*4097, "合"*3000, "a"+"\n"*8192])
def test_projection_does_not_bypass_type_blank_encoding_or_size_bounds(title):
    for source in ("wallstreetcn", "thepaper"):
        with pytest.raises(RecordError, match="NEWS_HEADLINE_INVALID"):
            window(title, source)


@pytest.mark.parametrize("change", [
    {"changed_titles": 0}, {"changed_titles": True}, {"changed_titles": 2},
    {"policy": "anything"}, {"raw_titles_retained": True}, {"raw_title": "do not admit"},
])
def test_reading_rejects_invalid_projection_claim_even_with_resealed_digest(change):
    value = news.combine([window("合成\n标题")])
    value["source_outcomes"][0]["headline_projection"].update(change)
    value.pop("reading_sha256")
    value["reading_sha256"] = digest(encoded(value))
    with pytest.raises(RecordError, match="NEWS_HEADLINE_PROJECTION_INVALID"):
        news.verify_reading(value)


def test_projection_cannot_be_attributed_to_a_different_source_or_failed_window():
    for change in ({"source": "cls"}, {"status": "unavailable"}, {"observations": True}):
        value = news.combine([window("合成\n标题")])
        value["source_outcomes"][0].update(change)
        value.pop("reading_sha256")
        value["reading_sha256"] = digest(encoded(value))
        with pytest.raises(RecordError, match="NEWS_HEADLINE_PROJECTION_INVALID"):
            news.verify_reading(value)


def test_projection_counts_only_changed_titles_and_bad_urls_still_fail():
    data = json.loads(wire(title="合成\n标题"))
    second = {**data["items"][0], "id": "second", "url": "https://wallstreetcn.com/other", "title": "未改变"}
    data["items"].append(second)
    result = news.newsnow_window(encoded(data), "wallstreetcn", observed_at=AT)
    assert result["projection"]["headline_projection"]["changed_titles"] == 1
    assert len(result["projection"]["observations"]) == 2
    data["items"][0]["url"] = "https://evil.example/item"
    with pytest.raises(RecordError, match="NEWS_LINK_SOURCE_MISMATCH"):
        news.newsnow_window(encoded(data), "wallstreetcn", observed_at=AT)


def test_normal_read_export_and_exact_store_reconstruction_keep_failures_and_projection(tmp_path, capsys):
    calls = []
    def fetch(source):
        calls.append(source)
        return wire(source, title="合成\n标题")
    result = capture_newsnow(sources=["wallstreetcn", "thepaper"], fetcher=fetch, observed_at=LATER)
    assert calls == ["wallstreetcn", "thepaper"]
    assert len(result["observations"]) == 2
    assert all(x["capture"]["status"] == "normalized" for x in result["source_outcomes"])
    path = tmp_path / "window.json"; path.write_bytes(encoded(result))
    assert news_cli.main(["read", "--window", str(path), "--source", "cls"]) == 0
    assert json.loads(capsys.readouterr().out)["source_outcomes"] == result["source_outcomes"]
    out = tmp_path / "locators.json"
    assert news_cli.main(["export-locators", "--window", str(path), "--output", str(out)]) == 0
    capsys.readouterr()
    exported = json.loads(out.read_bytes())
    assert exported["source_outcomes"] == result["source_outcomes"]
    for locator in news.source_locators(result):
        assert SourceInput.from_dict(asdict(locator)) == locator
        assert locator.text is None and locator.custody == "external" and "\n" not in locator.title

    backend_path = tmp_path / "news.git"
    store = VersionedStore(GitStore.init_synthetic(backend_path))
    def put(name, value):
        return store.put_source(name, SourceInput(source_id=name, kind="content", title="合成观察",
            origin="synthetic:headline-tests", text=encoded(value).decode(), custody="provided"))
    old = capture_newsnow(sources=["wallstreetcn"], fetcher=lambda _: wire(title=""), observed_at=AT)
    old_ref = put("old-failure", old)
    old_index = rolling_index(store.read_source, [old_ref], as_of=AT)
    prior = put("old-index", old_index)
    new_ref = put("corrected", result)
    index = rolling_index(store.read_source, [new_ref], as_of=LATER, predecessor=prior)
    saved = put("joint", index)
    independent = VersionedStore(GitStore(backend_path))
    opened = open_index(independent.read_source, saved)
    logical = restore_index(independent.read_source, opened)
    assert len(logical["entries"]) == 2 and opened["losses"] == []
    assert opened["coverage"]["failed_source_windows"] == 1
    assert sorted(e["observation"]["title"] for e in logical["entries"]) == ["合成 标题"]*2
    assert independent.read_source(old_ref)["data"] == encoded(old).decode()
    for source in ("wallstreetcn", "thepaper", "cls"):
        page = read_index(independent.read_source, opened, source=source)
        assert page["selected_count"] == (0 if source == "cls" else 1)
        assert len(page["source_outcomes"]) == 3
        projected = [x["outcome"]["headline_projection"] for x in page["source_outcomes"]
                     if "headline_projection" in x["outcome"]]
        assert len(projected) == 2 and all(x["changed_titles"] == 1 for x in projected)
