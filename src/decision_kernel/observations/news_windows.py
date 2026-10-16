"""Finite NewsNow, reviewed Relay and qualified RSS metadata, not verified events.

The original HTTP bodies are transient. Projected headlines, URLs and version
fingerprints are source *claims*, not publication rights or primary evidence.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from decision_kernel.domain.records import RecordError, SourceInput, encoded, require, source_time
from decision_kernel.observations.rss_reading import verify_news_outcome, verify_news_row
from decision_kernel.observations.rss_news import ATTRIBUTION as RSS_ATTRIBUTION

NEWSNOW_SOURCES = ("cls", "wallstreetcn", "fastbull", "jin10", "mktnews", "gelonghui", "thepaper")
NEWSNOW_DOMAINS = {
    "cls": ("cls.cn",), "wallstreetcn": ("wallstreetcn.com",),
    "fastbull": ("fastbull.com", "fastbull.cn"), "jin10": ("jin10.com",),
    "mktnews": ("mktnews.net",), "gelonghui": ("gelonghui.com",),
    "thepaper": ("thepaper.cn",),
}
NEWSNOW_ALIASES = {"cls": "cls-telegraph", "wallstreetcn": "wallstreetcn-quick",
                   "fastbull": "fastbull-express", "mktnews": "mktnews-flash"}
RELAY_APIS = ("news", "major_news", "cctv_news")
MAX_WINDOW = 2 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
SCHEMA = "dos2-news-window-v1"


def _sha(value) -> str:
    return sha256(encoded(value)).hexdigest()


def _seen_at(value: str) -> str:
    source_time(value)
    require(isinstance(value, str) and "T" in value, "NEWS_CAPTURE_CLOCK_REQUIRED")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError("NEWS_CAPTURE_CLOCK_INVALID") from None
    return value


def _headline(value) -> str:
    require(isinstance(value, str) and 0 < len(value.strip()) <= 4096
            and len(value.encode("utf-8")) <= 8192
            and not any(ord(x) < 32 or ord(x) == 127 for x in value), "NEWS_HEADLINE_INVALID")
    return value


def _claim(value):
    if value is None or value == "":
        return {"raw": value, "status": "missing"}
    require((isinstance(value, str) and len(value) <= 128
             and not any(ord(x) < 32 for x in value))
            or (type(value) is int and 0 < value < 100000000000000),
            "NEWS_PUBLICATION_CLAIM_INVALID")
    return {"raw": value, "status": "unverified-provider-claim"}


def _link(value, domains=None):
    require(isinstance(value, str) and 0 < len(value) <= 2048
            and not any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value),
            "NEWS_LINK_INVALID")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme == "https" and parsed.hostname and parsed.username is None \
            and parsed.password is None and parsed.port in (None, 443)
    except ValueError:
        valid = False
    require(bool(valid), "NEWS_LINK_INVALID")
    if domains is not None:
        require(any(parsed.hostname == d or parsed.hostname.endswith("." + d)
                    for d in domains), "NEWS_LINK_SOURCE_MISMATCH")
    return value


def _bytes_json(raw: bytes):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_RESPONSE, "NEWS_RESPONSE_BOUND")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "NEWS_DUPLICATE_JSON_KEY")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise RecordError("NEWS_JSON_INVALID") from None


def _row(provider, source, identity, title, link, time_claim, observed_at, content_digest,
         *, other_time_claims=None):
    article = _sha({"provider": provider, "source": source, "identity": identity})
    return {"provider": provider, "source_id": source, "article_id": article,
            "version_id": _sha({"article": article, "title": title,
                                "published": time_claim["raw"], "body": content_digest}),
            "title": title, "url": link, "published_claim": time_claim,
            "other_time_claims": other_time_claims or {}, "observed_at": observed_at,
            "representation": "headline-and-locator-only",
            "publication_verified": False, "event_verified": False,
            "research_started": False, "body_archived": False}


def _window(provider, source, observed_at, raw, rows, **claims):
    projection = {"schema": SCHEMA, "provider": provider, "source_id": source,
                  "observed_at": _seen_at(observed_at),
                  "upstream_sha256": sha256(raw).hexdigest(),
                  "observations": rows, "status": "captured",
                  "complete_news_coverage": False, "source_body_retained": False,
                  "meaning": "UNVERIFIED_HEADLINES_AND_LOCATORS_NOT_ECONOMIC_EVENTS",
                  **claims}
    result = {"projection": projection, "projection_sha256": _sha(projection)}
    require(len(encoded(result)) <= MAX_WINDOW, "NEWS_WINDOW_BOUND")
    return result


def newsnow_window(raw: bytes, source: str, *, observed_at: str):
    require(source in NEWSNOW_SOURCES, "NEWSNOW_SOURCE_NOT_ALLOWED")
    require(isinstance(raw, bytes) and len(raw) <= 1024 * 1024, "NEWSNOW_RESPONSE_BOUND")
    doc = _bytes_json(raw)
    require(isinstance(doc, dict) and doc.get("status") in ("success", "cache")
            and doc.get("id") in (source, NEWSNOW_ALIASES.get(source, source)),
            "NEWSNOW_ENVELOPE_INVALID")
    items = doc.get("items")
    require(isinstance(items, list) and len(items) <= 30, "NEWSNOW_WINDOW_LIMIT")
    rows, identities = [], {}
    for item in items:
        require(isinstance(item, dict), "NEWSNOW_ROW_INVALID")
        link = _link(item.get("url"), NEWSNOW_DOMAINS[source])
        if item.get("mobileUrl"):
            _link(item["mobileUrl"], NEWSNOW_DOMAINS[source])
        item_id = item.get("id")
        require(type(item_id) in (str, int) and 0 < len(str(item_id)) <= 256,
                "NEWSNOW_ITEM_ID_INVALID")
        key = str(item_id)
        require(key not in identities or identities[key] == link, "NEWSNOW_ID_REBOUND")
        identities[key] = link
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        # Keep distinct publisher claims; never infer a timezone from pubDate.
        date_claim = {"pubDate": _claim(item.get("pubDate")),
                      "extra.date": _claim(extra.get("date"))}
        rows.append(_row("newsnow", source,
                         {"item_id": key, "url": link}, _headline(item.get("title")),
                         link, date_claim["pubDate"], observed_at,
                         _sha({"title": item["title"], "extra_date": date_claim["extra.date"]["raw"]}),
                         other_time_claims={"extra.date": date_claim["extra.date"]}))
    return _window("newsnow", source, observed_at, raw, rows,
                   service_status=doc["status"], item_limit=30,
                   provider_timestamp_claim=_claim(doc.get("updatedTime")))


def relay_window(body: bytes, api: str, source: str, *, observed_at: str, request: dict):
    require(api in RELAY_APIS, "RELAY_NEWS_API_NOT_REVIEWED")
    require(isinstance(source, str) and 0 < len(source) <= 80, "RELAY_NEWS_SOURCE_INVALID")
    doc = _bytes_json(body)
    require(isinstance(doc, dict) and type(doc.get("code")) is int and doc["code"] == 0
            and doc.get("api_name") in (None, api) and doc.get("ok", True) is True
            and doc.get("error") in (None, ""), "RELAY_NEWS_SOURCE_FAILED")
    data = doc.get("data")
    require(isinstance(data, dict), "RELAY_NEWS_DATA_INVALID")
    fields, items = data.get("fields"), data.get("items")
    bound = 1500 if api == "news" else 400
    require(isinstance(fields, list) and len(fields) <= 80 and len(set(fields)) == len(fields)
            and all(isinstance(x, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", x)
                    for x in fields), "RELAY_NEWS_FIELDS_INVALID")
    require(isinstance(items, list) and len(items) <= bound, "RELAY_NEWS_ROWS_BOUND")
    require("title" in fields and ((api == "news" and "datetime" in fields)
            or (api == "major_news" and "pub_time" in fields)
            or (api == "cctv_news" and "date" in fields)), "RELAY_NEWS_EXPECTED_FIELDS_MISSING")
    observed = _seen_at(observed_at)
    rows = []
    for cells in items:
        require(isinstance(cells, list) and len(cells) == len(fields),
                "RELAY_NEWS_ROW_WIDTH_INVALID")
        raw = dict(zip(fields, cells))
        title = _headline(raw["title"])
        published = raw["datetime"] if api == "news" else raw["pub_time"] if api == "major_news" else raw["date"]
        claim = _claim(published)
        provider = raw.get("src_site") or raw.get("src") or source
        require(isinstance(provider, str) and 0 < len(provider) <= 128,
                "RELAY_NEWS_PROVIDER_INVALID")
        url = None
        if "url" in raw and raw["url"] not in (None, ""):
            url = _link(raw["url"])
        # No publisher article URL in the two official Tushare news contracts.
        # The news article identity is a candidate; the raw row is not archived.
        identity = {"publisher": provider, "published_claim": claim["raw"], "title": title}
        rows.append(_row("relay", "relay:" + api + ":" + source, identity, title,
                         url, claim, observed, _sha(raw)))
    return _window("relay", "relay:" + api + ":" + source, observed, body, rows,
                   api=api, request=request, item_limit=bound,
                   locator_without_item_url="AGGREGATE_ENDPOINT_ONLY")


def verify_window(value):
    require(isinstance(value, dict) and set(value) == {"projection", "projection_sha256"}
            and isinstance(value["projection"], dict), "NEWS_WINDOW_INVALID")
    p = value["projection"]
    require(p.get("schema") == SCHEMA and p.get("status") == "captured"
            and value["projection_sha256"] == _sha(p)
            and isinstance(p.get("observations"), list) and len(encoded(value)) <= MAX_WINDOW,
            "NEWS_WINDOW_INTEGRITY")
    _seen_at(p.get("observed_at"))
    return p


def combine(windows):
    require(isinstance(windows, (list, tuple)) and 0 < len(windows) <= 12,
            "NEWS_WINDOWS_INVALID")
    seen, rows, counts = set(), [], []
    for item in windows:
        if isinstance(item, dict) and item.get("status") == "source-unavailable":
            counts.append({"source": item.get("source_id"), "status": "unavailable", "observations": None})
            continue
        p = verify_window(item)
        key = (p["provider"], p["source_id"], p["observed_at"])
        require(key not in seen, "NEWS_DUPLICATE_WINDOW")
        seen.add(key)
        counts.append({"source": p["source_id"], "status": p["status"],
                       "observations": len(p["observations"])})
        rows.extend(p["observations"])
    return _reading(rows, counts)


def _reading(rows, counts):
    rss_sources = set()
    for item in counts:
        if isinstance(item, dict) and (item.get("provider") == "rss" or "rss" in item):
            verify_news_outcome(item)
            rss_sources.add(item["source"])
    distinct = {}
    for row in rows:
        require(isinstance(row, dict), "NEWS_ROW_INVALID")
        version, article, title = row.get("version_id"), row.get("article_id"), row.get("title")
        require(isinstance(version, str) and bool(re.fullmatch(r"[0-9a-f]{64}", version))
                and isinstance(article, str) and bool(re.fullmatch(r"[0-9a-f]{64}", article)),
                "NEWS_ROW_IDENTITY_INVALID")
        if row.get("provider") == "rss":
            verify_news_row(row)
            require(row["source_id"] in rss_sources, "RSS_NEWS_OUTCOME_REQUIRED")
        else:
            _headline(title)
        _seen_at(row.get("observed_at"))
        require(row.get("provider") in ("relay", "newsnow", "rss")
                and row.get("representation") == "headline-and-locator-only"
                and row.get("event_verified") is False
                and row.get("publication_verified") is False
                and row.get("body_archived") is False
                and isinstance(row.get("source_id"), str), "NEWS_ROW_AUTHORITY_INVALID")
        if row["provider"] == "newsnow":
            require(row["source_id"] in NEWSNOW_SOURCES, "NEWS_ROW_SOURCE_INVALID")
            _link(row.get("url"), NEWSNOW_DOMAINS[row["source_id"]])
        elif row["provider"] == "relay":
            source = row["source_id"].split(":")
            require(len(source) == 3 and source[0] == "relay"
                    and source[1] in RELAY_APIS and source[2], "NEWS_ROW_SOURCE_INVALID")
            if row.get("url") is not None:
                _link(row["url"])
        require(version not in distinct or distinct[version] == row,
                "NEWS_VERSION_COLLISION")
        distinct[version] = row
    grouped = {}
    for row in distinct.values():
        grouped.setdefault(" ".join(row["title"].split()), []).append(row)
    view = {"schema": "dos2-news-reading-v1", "observations": list(distinct.values()),
            "source_outcomes": counts,
            "headline_repetition_candidates": [
                {"title": key, "versions": sorted({r["version_id"] for r in group}),
                 "article_count": len({r["article_id"] for r in group}),
                 "event_identity": "unverified"}
                for key, group in grouped.items() if len({r["article_id"] for r in group}) > 1],
            "complete_news_coverage": False,
            "meaning": "SOURCE_WINDOWS_ONLY_NO_AUTOMATIC_QUICK_OR_FACT_ACCEPTANCE"}
    view["reading_sha256"] = _sha(view)
    require(len(encoded(view)) <= 5 * 1024 * 1024, "NEWS_READING_BOUND")
    return view


def verify_reading(value):
    require(isinstance(value, dict) and "reading_sha256" in value
            and isinstance(value.get("observations"), list)
            and isinstance(value.get("source_outcomes"), list), "NEWS_READING_INVALID")
    expect = _reading(value["observations"], value["source_outcomes"])
    require(value == expect, "NEWS_READING_INTEGRITY")
    return value


def merge_readings(readings):
    require(isinstance(readings, (list, tuple)) and 1 <= len(readings) <= 12,
            "NEWS_READING_INPUT_BOUND")
    items, outcomes = [], []
    for reading in readings:
        original = verify_reading(reading)
        items.extend(original["observations"])
        outcomes.extend(original["source_outcomes"])
    return _reading(items, outcomes)


def source_locators(reading):
    verify_reading(reading)
    result = []
    for row in reading["observations"]:
        origin = row["url"]
        if origin is None:
            require(row["provider"] == "relay", "NEWS_SOURCE_LINK_MISSING")
            origin = "https://pcd.mobcvb.cn/tushare/pro/" + row["source_id"].split(":")[1]
        limitations = ["NOT_A_VERIFIED_EVENT", "NO_PUBLISHER_PUBLICATION_TIME_VERIFICATION",
                       "EXTERNAL_LOCATOR_ONLY_NO_NEWS_BODY",
                       "ARTICLE_VERSION_SHA256_" + row["version_id"]]
        if row["provider"] == "rss":
            limitations.extend((RSS_ATTRIBUTION, "RSS_FEED_METADATA_NOT_LINKED_ARTICLE",
                                "RSS_NATIVE_VERSION_SHA256_" + row["rss"]["version_id"]))
        if row["url"] is None:
            limitations.append("NO_INDIVIDUAL_ARTICLE_URL_AGGREGATE_ENDPOINT_ONLY")
        result.append(SourceInput(source_id="news-" + row["article_id"][:40], kind="content",
                                  title=row["title"], origin=origin, text=None,
                                  custody="external", acquired_at=row["observed_at"],
                                  limitations=tuple(limitations)))
    return result
