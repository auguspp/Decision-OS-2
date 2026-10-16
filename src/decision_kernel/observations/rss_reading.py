"""RSS metadata projection for the existing news reader; no parser or source I/O.

The caller qualifies the capture through rss_intake.open_capture first. Publisher
version identity is retained, while news exposure identity is namespaced by feed.
Capture history and original availability belong to the batch, never an article.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

from decision_kernel.domain.records import SourceRef, digest, encoded, require
from decision_kernel.observations.rss_news import ATTRIBUTION, FEEDS, _entry, publication_claim

ROW_KEYS = {"article_id", "body_archived", "event_verified", "observed_at", "other_time_claims",
            "provider", "publication_verified", "published_claim", "representation",
            "research_started", "source_id", "title", "url", "version_id", "rss"}
RSS_KEYS = {"version_id", "title_type", "summary_sha256", "summary_type"}
ORIGINAL_STATES = {"not-retained-rights-unconfirmed", "retained-with-explicit-public-rights"}


def _news_row(entry, feed_id, observed_at):
    require(feed_id in FEEDS, "RSS_NEWS_FEED_INVALID")
    p = entry["payload"]
    native = _entry(entry["url"], entry.get("entry_id", ""), p)
    require(entry == native, "RSS_NEWS_ENTRY_IDENTITY")
    return {"provider": "rss", "source_id": feed_id, "article_id": entry["document_id"],
            "version_id": digest(encoded({"provider": "rss", "feed_id": feed_id,
                                           "version_id": entry["version_id"]})),
            "title": p["title"], "url": entry["url"], "observed_at": observed_at,
            "published_claim": publication_claim(p["published_raw"]),
            "other_time_claims": {"rss.updated": publication_claim(p["updated_raw"])},
            "rss": {"version_id": entry["version_id"], "title_type": p["title_type"],
                    "summary_sha256": p["summary_sha256"], "summary_type": p["summary_type"]},
            "representation": "headline-and-locator-only", "publication_verified": False,
            "event_verified": False, "research_started": False, "body_archived": False}


def verify_news_row(row):
    require(isinstance(row, dict) and set(row) == ROW_KEYS and isinstance(row["rss"], dict)
            and set(row["rss"]) == RSS_KEYS and isinstance(row["source_id"], str)
            and row["source_id"] in FEEDS and isinstance(row["published_claim"], dict)
            and isinstance(row["other_time_claims"], dict)
            and set(row["other_time_claims"]) == {"rss.updated"}
            and isinstance(row["other_time_claims"]["rss.updated"], dict), "RSS_NEWS_ROW_SHAPE")
    payload = {"title": row["title"], **{k: row["rss"][k] for k in RSS_KEYS - {"version_id"}},
               "published_raw": row["published_claim"].get("raw"),
               "updated_raw": row["other_time_claims"]["rss.updated"].get("raw")}
    native = _entry(row["url"], "", payload)
    require(row == _news_row(native, row["source_id"], row["observed_at"]), "RSS_NEWS_ROW_IDENTITY")
    return row


def news_rows(capture):
    """Use this capture's receipt clock, not its inherited first-seen/publication."""
    return [_news_row(entry, capture["window"]["feed_id"], capture["received_at"])
            for entry in capture["window"]["entries"]]


def news_outcome(capture):
    delta = capture["delta"]
    result = {"source": capture["window"]["feed_id"], "provider": "rss", "status": "captured",
              "observations": len(capture["observations"]),
              "capture": {"requested_at": capture["requested_at"], "received_at": capture["received_at"],
                          "finished_at": capture["received_at"]},
              "rss": {"feed_url": capture["window"]["feed_url"], "capture_sha256": capture["capture_sha256"],
                      "original_status": capture["original"]["status"], "attribution": ATTRIBUTION,
                      "predecessor": deepcopy(capture["predecessor"]),
                      "delta": {"status": delta["status"], "raw_bytes_unchanged": delta["raw_bytes_unchanged"],
                                "missing_meaning": delta["missing_meaning"],
                                **{key: len(delta[key]) for key in ("baseline_versions", "new_to_selected_predecessor",
                                                                   "unchanged_versions", "missing_from_current_window")}},
                      "row_clock_meaning": "CAPTURE_RECEIPT_NOT_PUBLICATION_OR_INHERITED_FIRST_SEEN",
                      "count_meaning": "SELECTED_BATCHES_IN_NEWS_WINDOW_NOT_RSS_CHAIN_COUNT"}}
    return verify_news_outcome(result)


def verify_news_outcome(value):
    require(isinstance(value, dict) and set(value) == {"source", "provider", "status", "observations", "capture", "rss"}
            and value["provider"] == "rss" and value["status"] == "captured"
            and isinstance(value["source"], str) and value["source"] in FEEDS
            and type(value["observations"]) is int and 0 <= value["observations"] <= 512,
            "RSS_NEWS_OUTCOME_SHAPE")
    meta = value["rss"]
    require(isinstance(meta, dict) and set(meta) == {"feed_url", "capture_sha256", "original_status", "attribution", "predecessor", "delta", "row_clock_meaning", "count_meaning"}
            and meta["feed_url"] == FEEDS[value["source"]] and isinstance(meta["original_status"], str)
            and meta["original_status"] in ORIGINAL_STATES
            and meta["attribution"] == ATTRIBUTION and isinstance(meta["capture_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", meta["capture_sha256"])
            and meta["row_clock_meaning"] == "CAPTURE_RECEIPT_NOT_PUBLICATION_OR_INHERITED_FIRST_SEEN"
            and meta["count_meaning"] == "SELECTED_BATCHES_IN_NEWS_WINDOW_NOT_RSS_CHAIN_COUNT",
            "RSS_NEWS_OUTCOME_METADATA")
    receipt = value["capture"]
    require(isinstance(receipt, dict) and set(receipt) == {"requested_at", "received_at", "finished_at"}
            and receipt["received_at"] == receipt["finished_at"], "RSS_NEWS_RECEIPT")
    times = []
    for key in ("requested_at", "received_at"):
        raw = receipt[key]
        require(isinstance(raw, str) and len(raw) <= 80 and "T" in raw, "RSS_NEWS_RECEIPT")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            require(False, "RSS_NEWS_RECEIPT")
        require(stamp.tzinfo is not None, "RSS_NEWS_RECEIPT")
        times.append(stamp)
    require(times[0] <= times[1], "RSS_NEWS_RECEIPT")
    prior = meta["predecessor"]
    require(isinstance(prior, dict) and set(prior) == {"reference", "recovery", "capture_sha256"},
            "RSS_NEWS_PREDECESSOR")
    if prior["reference"] is None:
        require(prior["recovery"] == "explicit-bootstrap" and prior["capture_sha256"] is None,
                "RSS_NEWS_PREDECESSOR")
    else:
        ref = SourceRef.from_dict(prior["reference"])
        require(ref.selection.kind == "all" and prior["recovery"] in ("restored-original", "restored-metadata-only")
                and isinstance(prior["capture_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", prior["capture_sha256"]),
                "RSS_NEWS_PREDECESSOR")
    delta = meta["delta"]
    counts = {"baseline_versions", "new_to_selected_predecessor", "unchanged_versions", "missing_from_current_window"}
    require(isinstance(delta, dict) and set(delta) == counts | {"status", "raw_bytes_unchanged", "missing_meaning"}
            and all(type(delta[k]) is int and 0 <= delta[k] <= 512 for k in counts)
            and delta["missing_meaning"] == "NOT_RETRACTION_OR_DELETION", "RSS_NEWS_DELTA")
    if prior["reference"] is None:
        require(delta["status"] == "baseline-not-new-publications" and delta["raw_bytes_unchanged"] is None
                and delta["baseline_versions"] == value["observations"]
                and not any(delta[k] for k in counts - {"baseline_versions"}), "RSS_NEWS_DELTA")
    else:
        require(delta["baseline_versions"] == 0 and type(delta["raw_bytes_unchanged"]) is bool
                and delta["new_to_selected_predecessor"] + delta["unchanged_versions"] == value["observations"]
                and delta["status"] == ("changed-window" if delta["new_to_selected_predecessor"] or delta["missing_from_current_window"] else "no-new-feed-versions"), "RSS_NEWS_DELTA")
    return value
