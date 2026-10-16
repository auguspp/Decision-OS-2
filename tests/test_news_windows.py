"""News is a finite source observation, not fact/economic/Research authority."""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.observations.news_windows import (
    newsnow_window, relay_window, verify_window, combine, source_locators,
)
from decision_kernel.infrastructure.news_capture import (
    capture_newsnow, capture_relay_news, relay_news_parameters,
)

NOW = "2026-10-16T00:00:00+00:00"


def newsnow(source="cls", title="合成新闻观察", item_id="abc", url=None):
    domain = {"cls": "cls.cn", "jin10": "jin10.com"}[source]
    value = {"status": "success", "id": source, "items": [{
        "id": item_id, "title": title, "url": url or "https://www." + domain + "/detail/123",
        "pubDate": 1792071000000, "extra": {"date": None}}]}
    return json.dumps(value, ensure_ascii=False).encode()


def relay(api="news", body="模拟新闻全文"):
    fields, row = {
        "news": (["title", "datetime", "content"], ["模拟标题", "2026-10-15 09:00:00", body]),
        "major_news": (["title", "content", "pub_time", "src_site"],
                       ["模拟标题", body, "2026-10-15 09:00:00", "财联社"]),
        "cctv_news": (["date", "title", "content"], ["20261015", "模拟标题", body]),
    }[api]
    return encoded({"code": 0, "api_name": api, "data": {"fields": fields, "items": [row]}})


def test_newsnow_preserves_publisher_claims_and_identity_without_fulltext():
    a = newsnow_window(newsnow(), "cls", observed_at=NOW)
    p = verify_window(a)
    row = p["observations"][0]
    assert p["item_limit"] == 30 and not p["complete_news_coverage"]
    assert row["url"].startswith("https://") and not row["event_verified"]
    assert row["published_claim"]["status"] == "unverified-provider-claim"
    assert row["other_time_claims"]["extra.date"]["raw"] is None
    assert a == newsnow_window(newsnow(), "cls", observed_at=NOW)
    assert row["body_archived"] is False


@pytest.mark.parametrize("url", ["http://cls.cn/a", "https://cls.cn.evil.org/a",
                                  "https://user:pass@cls.cn/x", "https://cls.cn:444/a",
                                  "https://127.0.0.1/a", "https://cls.cn/a\npayload"])
def test_newsnow_rejects_bad_or_unrelated_url(url):
    with pytest.raises(RecordError):
        newsnow_window(newsnow(url=url), "cls", observed_at=NOW)


def test_newsnow_rejects_wrong_source_overflow_rebound_and_duplicate_json():
    with pytest.raises(RecordError):
        newsnow_window(newsnow(), "jin10", observed_at=NOW)
    with pytest.raises(RecordError):
        newsnow_window(b" " * (1024 * 1024 + 1), "cls", observed_at=NOW)
    obj = json.loads(newsnow())
    obj["items"] *= 31
    with pytest.raises(RecordError):
        newsnow_window(encoded(obj), "cls", observed_at=NOW)
    obj["items"] = [dict(obj["items"][0]), dict(obj["items"][0], url="https://cls.cn/another")]
    with pytest.raises(RecordError):
        newsnow_window(encoded(obj), "cls", observed_at=NOW)
    with pytest.raises(RecordError):
        newsnow_window(b'{"status":"success","status":"cache","id":"cls","items":[]}',
                       "cls", observed_at=NOW)


def test_relay_news_major_cctv_are_observations_without_third_party_bodies():
    for api in ("news", "major_news", "cctv_news"):
        w = relay_window(relay(api), api, "cls", observed_at=NOW, request={})
        row = verify_window(w)["observations"][0]
        assert row["provider"] == "relay" and row["url"] is None
        assert row["body_archived"] is False and not row["publication_verified"]
        assert "模拟新闻全文" not in json.dumps(w, ensure_ascii=False)
        assert row["representation"] == "headline-and-locator-only"
    old = relay_window(relay(), "news", "cls", observed_at=NOW, request={})
    changed = relay_window(relay(body="不一样的合成文字"), "news", "cls",
                           observed_at=NOW, request={})
    assert old["projection"]["observations"][0]["version_id"] != \
           changed["projection"]["observations"][0]["version_id"]


def test_relay_invalid_fields_code_and_bounds_never_become_valid_empty():
    obj = json.loads(relay())
    for value in (dict(obj, code=2002),
                  {"code": 0, "data": {"fields": [], "items": []}},
                  {"code": 0, "data": {"fields": ["title", "datetime"],
                                         "items": [["partial"]]}}):
        with pytest.raises(RecordError):
            relay_window(encoded(value), "news", "cls", observed_at=NOW, request={})
    with pytest.raises(RecordError):
        relay_window(b'{"code":0,"code":0,"data":{}}', "news", "cls",
                     observed_at=NOW, request={})


def test_explicit_relay_window_and_disabled_unreviewed_apis():
    good = relay_news_parameters("news", source="cls",
                                 start="2026-10-15 09:00:00", end="2026-10-15 10:00:00")
    assert good["src"] == "cls"
    assert relay_news_parameters("cctv_news", date="20261015") == {"date": "20261015"}
    for api in ("express_news", "news_cctv", "news_economic_baidu", "p_save"):
        with pytest.raises(RecordError):
            relay_news_parameters(api, source="cls", start="2026-10-15 09:00:00",
                                  end="2026-10-15 10:00:00")
    for kw in ({"source": "bad", "start": "2026-10-15 09:00:00", "end": "2026-10-15 10:00:00"},
               {"source": "cls", "start": "2026-10-15 09:00:00", "end": "2026-10-17 09:00:00"},
               {"source": "cls", "start": "2026-10-15 10:00:00", "end": "2026-10-15 09:00:00"}):
        with pytest.raises(RecordError):
            relay_news_parameters("news", **kw)


def test_distinct_publisher_identical_headline_is_only_repetition_candidate():
    a = newsnow_window(newsnow(title="相同标题"), "cls", observed_at=NOW)
    b = newsnow_window(newsnow("jin10", title="相同标题"), "jin10", observed_at=NOW)
    result = combine([a, b])
    assert len(result["observations"]) == 2
    assert result["headline_repetition_candidates"][0]["article_count"] == 2
    assert result["headline_repetition_candidates"][0]["event_identity"] == "unverified"
    with pytest.raises(RecordError):
        combine([a, a])
    with pytest.raises(RecordError):
        verify_window({**a, "projection_sha256": "0" * 64})


def test_newsnow_capture_failure_is_local_and_not_zero_news():
    def fetcher(source):
        if source != "cls":
            raise RecordError("SYNTHETIC_GAP")
        return newsnow()
    result = capture_newsnow(observed_at=NOW, fetcher=fetcher)
    assert len(result["source_outcomes"]) == 7 and len(result["observations"]) == 1
    assert len([x for x in result["source_outcomes"] if x["status"] == "unavailable"]) == 6
    assert not result["complete_news_coverage"]


def test_limited_external_locators_can_use_single_existing_versioned_store(tmp_path):
    view = combine([newsnow_window(newsnow(), "cls", observed_at=NOW),
                    relay_window(relay(), "news", "cls", observed_at=NOW, request={})])
    sources = source_locators(view)
    assert len(sources) == 2 and all(s.custody == "external" and s.text is None for s in sources)
    assert sources[1].origin == "https://pcd.mobcvb.cn/tushare/pro/news"
    assert sources[0].published_at is None
    assert any(x == "ARTICLE_VERSION_SHA256_" + view["observations"][0]["version_id"]
               for x in sources[0].limitations)
    storage = VersionedStore(GitStore.init_synthetic(tmp_path / "synthetic-news.git"))
    for n, source in enumerate(sources):
        ref = storage.put_source("synthetic-news-" + str(n), source)
        result = storage.read_source(ref)
        assert result["availability"] == "external-only"
        assert result["original_verified"] is False


def test_narrow_relay_only_uses_fixed_api_and_existing_security_transport():
    class Stub:
        def __init__(self):
            self.last_response_bytes = relay()
            self.calls = []
        def _get_json(self, url, *, max_bytes, agent):
            self.calls.append(url)
            return json.loads(self.last_response_bytes)
    transport = Stub()
    result = capture_relay_news("news", source="cls",
                                start="2026-10-15 09:00:00", end="2026-10-15 10:00:00",
                                observed_at=NOW, transport=transport)
    assert len(result["observations"]) == 1
    assert transport.calls[0].startswith("https://pcd.mobcvb.cn/tushare/pro/news?")
    with pytest.raises(RecordError):
        capture_relay_news("p_delete", observed_at=NOW, transport=transport)
    assert len(transport.calls) == 1


def test_cross_source_merge_is_one_reproducible_view_without_event_promotion():
    from decision_kernel.observations.news_windows import merge_readings, verify_reading
    first = combine([newsnow_window(newsnow(title="模拟标题"), "cls", observed_at=NOW)])
    second = combine([relay_window(relay(), "news", "cls", observed_at=NOW, request={})])
    merged = merge_readings([first, second])
    assert verify_reading(merged) == merged
    assert len(merged["observations"]) == 2
    assert merged["headline_repetition_candidates"][0]["article_count"] == 2
    assert merged["headline_repetition_candidates"][0]["event_identity"] == "unverified"
    assert len(source_locators(merged)) == 2
    with pytest.raises(RecordError):
        merge_readings([first, {**second, "reading_sha256": "f" * 64}])
    with pytest.raises(RecordError):
        source_locators({**merged, "observations": []})
