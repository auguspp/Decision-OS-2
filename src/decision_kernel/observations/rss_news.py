"""Fixed NBS RSS byte interpretation, adapted from the existing Kernel profile.

Only bytes enter feedparser, never URLs. Dates without an explicit zone remain
claims; omitted summaries/content are not represented as archived news bodies.
"""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version
import io
import re

from decision_kernel.domain.records import RecordError, digest, encoded, require

PARSER = "6.0.14"
FEEDS = {"nbs-releases": "https://www.stats.gov.cn/sj/zxfb/rss.xml",
         "nbs-interpretations": "https://www.stats.gov.cn/sj/sjjd/rss.xml"}
MAX_BODY = 8 * 1024 * 1024
MAX_ITEMS = 512
LINK = re.compile(r"https?://www\.stats\.gov\.cn/sj/(?:zxfb|sjjd)/[0-9]{6}/t[0-9]{8}_[0-9]+\.html\Z")
ATTRIBUTION = "引自国家统计局网站 https://www.stats.gov.cn；来源权利保留，不授权他人继续转载。"
PAYLOAD_KEYS = {"title", "title_type", "summary_sha256", "summary_type", "published_raw", "updated_raw"}


def text(value, limit=4096):
    require(isinstance(value, str) and len(value) <= limit and "\0" not in value, "RSS_TEXT_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RecordError("RSS_TEXT_INVALID") from None
    return value


def publication_claim(raw):
    """Keep the legacy strict clock admission, not feedparser's lenient correction."""
    text(raw, 256)
    stamp = None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", raw):
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        elif re.fullmatch(r"(?:[A-Z][a-z]{2}, )?\d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} (?:GMT|UT|UTC|[+-]\d{4})", raw):
            candidate = parsedate_to_datetime(raw)
            if candidate.tzinfo is not None and ("," not in raw or candidate.strftime("%a") == raw[:3]):
                stamp = candidate
    except (ValueError, OverflowError):
        pass
    return {"raw": raw, "instant": stamp.isoformat() if stamp is not None else None,
            "status": "explicit-zone-unverified" if stamp is not None else "unresolved" if raw else "missing",
            "publication_verified": False}


def _entry(url, entry_id, payload):
    require(isinstance(url, str) and len(url) <= 2048 and LINK.fullmatch(url), "RSS_LINK_REJECTED")
    text(entry_id, 2048)
    require(isinstance(payload, dict) and set(payload) == PAYLOAD_KEYS, "RSS_PAYLOAD_SHAPE")
    require(bool(text(payload["title"]).strip()), "RSS_TITLE_REQUIRED")
    text(payload["title_type"], 64); text(payload["summary_type"], 64)
    require(isinstance(payload["summary_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", payload["summary_sha256"]), "RSS_SUMMARY_HASH")
    publication_claim(payload["published_raw"]); publication_claim(payload["updated_raw"])
    document = digest(encoded({"publisher": "NBS", "url": url}))
    return {"document_id": document, "version_id": digest(encoded({"document_id": document, "payload": payload})),
            "url": url, "entry_id": entry_id, "payload": payload}


def verify_window(window):
    require(isinstance(window, dict) and set(window) == {"feed_id", "feed_url", "parser_version", "format", "raw_size", "raw_sha256", "entries"}, "RSS_WINDOW_SHAPE")
    require(isinstance(window["feed_id"], str) and window["feed_id"] in FEEDS and window["feed_url"] == FEEDS[window["feed_id"]]
            and window["parser_version"] == PARSER and window["format"] in ("rss20", "rss10", "atom10"), "RSS_PROFILE_CHANGED")
    require(type(window["raw_size"]) is int and 0 < window["raw_size"] <= MAX_BODY
            and isinstance(window["raw_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", window["raw_sha256"]), "RSS_RAW_IDENTITY")
    rows = window["entries"]
    require(isinstance(rows, list) and len(rows) <= MAX_ITEMS, "RSS_ITEM_BOUND")
    documents, ids = set(), {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"document_id", "version_id", "url", "entry_id", "payload"}, "RSS_ENTRY_SHAPE")
        require(row == _entry(row["url"], row["entry_id"], row["payload"]), "RSS_ENTRY_IDENTITY")
        require(row["document_id"] not in documents, "RSS_DUPLICATE_DOCUMENT")
        documents.add(row["document_id"])
        if row["entry_id"]:
            require(row["entry_id"] not in ids or ids[row["entry_id"]] == row["url"], "RSS_CONFLICTING_ENTRY_ID")
            ids[row["entry_id"]] = row["url"]
    return window


def parse_window(raw, feed_id):
    require(isinstance(feed_id, str) and feed_id in FEEDS and isinstance(raw, bytes)
            and 0 < len(raw) <= MAX_BODY, "RSS_BODY_OR_FEED_REJECTED")
    require(version("feedparser") == PARSER, "RSS_PARSER_VERSION_CHANGED")
    require(not re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I), "RSS_DTD_REJECTED")
    import feedparser
    parsed = feedparser.parse(io.BytesIO(raw), sanitize_html=False, resolve_relative_uris=False)
    require(not parsed.get("bozo", True) and parsed.get("version") in ("rss20", "rss10", "atom10"), "RSS_XML_REJECTED")
    require(len(parsed.entries) <= MAX_ITEMS, "RSS_ITEM_BOUND")
    rows = []
    for entry in parsed.entries:
        summary = text(entry.get("summary", ""), 65536)
        payload = {"title": text(entry.get("title", "")),
                   "title_type": text(entry.get("title_detail", {}).get("type", "text/plain"), 64),
                   "summary_sha256": digest(summary.encode("utf-8")),
                   "summary_type": text(entry.get("summary_detail", {}).get("type", "text/plain"), 64),
                   "published_raw": text(entry.get("published", ""), 256),
                   "updated_raw": text(entry.get("updated", ""), 256)}
        rows.append(_entry(entry.get("link", ""), entry.get("id", ""), payload))
    return verify_window({"feed_id": feed_id, "feed_url": FEEDS[feed_id], "parser_version": PARSER,
                          "format": parsed["version"], "raw_size": len(raw), "raw_sha256": digest(raw), "entries": rows})
