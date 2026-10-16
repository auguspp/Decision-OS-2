"""RSS observations and exact, explicitly selected predecessor transitions.

VersionedStore owns custody and integrity. This module has no current pointer,
scheduler or alternate store. Only the selected transition is reconstructed.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import json
import zlib

from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, digest, encoded, require
from decision_kernel.observations.rss_news import (
    ATTRIBUTION, FEEDS, MAX_BODY, parse_window, publication_claim, text, verify_window,
)

SCHEMA = "dos2-rss-capture-v1"
AUTHORITY = {"publication_verified": False, "event_verified": False, "research_started": False,
             "linked_articles_archived": False, "complete_news_coverage": False}
COUNT_MEANING = "CONSECUTIVE_SELECTED_CAPTURES_RETAINING_THIS_VERSION_NOT_LIFETIME_OR_UNREAD"


def clock(value):
    require(isinstance(value, str) and len(value) <= 80 and "T" in value, "RSS_CLOCK_REQUIRED")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "RSS_CLOCK_ZONE_REQUIRED")
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecordError("RSS_CLOCK_INVALID") from None


def now():
    return datetime.now(timezone.utc).isoformat()


def reference(value):
    ref = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(ref.selection.kind == "all", "RSS_WHOLE_SOURCE_REQUIRED")
    return json.loads(encoded(asdict(ref)))


def _seal(value):
    value = deepcopy(value)
    value.pop("capture_sha256", None)
    value["capture_sha256"] = digest(encoded(value))
    require(len(encoded(value)) <= 1900 * 1024, "RSS_CAPTURE_STORAGE_BOUND")
    return value


def original_bytes(capture):
    original = capture["original"]
    require(isinstance(original, dict), "RSS_ORIGINAL_SHAPE")
    if original.get("status") == "not-retained-rights-unconfirmed":
        require(set(original) == {"status"}, "RSS_UNDECLARED_ORIGINAL")
        return None
    require(set(original) == {"status", "gzip_base64", "rights_basis"}
            and original["status"] == "retained-with-explicit-public-rights"
            and bool(text(original["rights_basis"]).strip())
            and isinstance(original["gzip_base64"], str)
            and len(original["gzip_base64"]) <= 1800 * 1024, "RSS_ORIGINAL_SHAPE")
    try:
        packed = base64.b64decode(original["gzip_base64"], validate=True)
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(packed, MAX_BODY + 1)
        require(0 < len(raw) <= MAX_BODY and decoder.eof and not decoder.unused_data
                and not decoder.unconsumed_tail, "RSS_ORIGINAL_DECOMPRESSION_BOUND")
    except (ValueError, zlib.error):
        raise RecordError("RSS_ORIGINAL_ENCODING") from None
    require(len(raw) == capture["window"]["raw_size"]
            and digest(raw) == capture["window"]["raw_sha256"], "RSS_ORIGINAL_INTEGRITY")
    return raw


def _assemble(window, *, batch_id, requested_at, received_at, previous, predecessor, original):
    before = {r["entry"]["version_id"]: r for r in previous["observations"]} if previous else {}
    rows = []
    for entry in window["entries"]:
        prior = before.get(entry["version_id"])
        if prior:
            require(prior["entry"] == entry, "RSS_VERSION_COLLISION")
        rows.append({"entry": entry, "first_seen_at": prior["first_seen_at"] if prior else received_at,
                     "last_seen_at": received_at, "seen_capture_count": prior["seen_capture_count"] + 1 if prior else 1})
    current = {row["entry"]["version_id"] for row in rows}
    new = sorted(current - set(before)) if previous else []
    recovery = ("restored-original" if previous["original"]["status"] == "retained-with-explicit-public-rights"
                else "restored-metadata-only") if previous else "explicit-bootstrap"
    return _seal({"schema": SCHEMA, "batch_id": batch_id, "window": window,
                  "requested_at": requested_at, "received_at": received_at,
                  "source_outcome": "captured", "attribution": ATTRIBUTION, "authority": AUTHORITY,
                  "predecessor": {"reference": predecessor, "recovery": recovery,
                                  "capture_sha256": previous["capture_sha256"] if previous else None},
                  "observations": rows, "original": original, "count_meaning": COUNT_MEANING,
                  "delta": {"status": "baseline-not-new-publications" if not previous else "changed-window" if new or set(before) - current else "no-new-feed-versions",
                            "baseline_versions": sorted(current) if not previous else [],
                            "new_to_selected_predecessor": new,
                            "unchanged_versions": sorted(current & set(before)),
                            "missing_from_current_window": sorted(set(before) - current),
                            "missing_meaning": "NOT_RETRACTION_OR_DELETION",
                            "raw_bytes_unchanged": previous["window"]["raw_sha256"] == window["raw_sha256"] if previous else None}})


def verify_capture(value):
    require(isinstance(value, dict) and set(value) == {"schema", "batch_id", "window", "requested_at", "received_at", "source_outcome", "attribution", "authority", "predecessor", "observations", "original", "count_meaning", "delta", "capture_sha256"}, "RSS_CAPTURE_SHAPE")
    require(value["schema"] == SCHEMA and value["authority"] == AUTHORITY
            and value["source_outcome"] == "captured" and value["attribution"] == ATTRIBUTION
            and value["count_meaning"] == COUNT_MEANING and value == _seal(value), "RSS_CAPTURE_INTEGRITY")
    require(bool(text(value["batch_id"], 96).strip()), "RSS_BATCH_REQUIRED")
    start, end = clock(value["requested_at"]), clock(value["received_at"])
    require(start <= end, "RSS_CAPTURE_CLOCK_ORDER")
    window = verify_window(value["window"])
    prior = value["predecessor"]
    require(isinstance(prior, dict) and set(prior) == {"reference", "recovery", "capture_sha256"}, "RSS_PREDECESSOR_SHAPE")
    if prior["reference"] is None:
        require(prior["recovery"] == "explicit-bootstrap" and prior["capture_sha256"] is None, "RSS_PREDECESSOR_SHAPE")
    else:
        reference(prior["reference"])
        require(prior["recovery"] in ("restored-original", "restored-metadata-only")
                and isinstance(prior["capture_sha256"], str) and len(prior["capture_sha256"]) == 64, "RSS_PREDECESSOR_SHAPE")
    rows = value["observations"]
    require(isinstance(rows, list) and len(rows) == len(window["entries"]), "RSS_OBSERVATIONS_SHAPE")
    for row, entry in zip(rows, window["entries"]):
        require(isinstance(row, dict) and set(row) == {"entry", "first_seen_at", "last_seen_at", "seen_capture_count"}
                and row["entry"] == entry and clock(row["first_seen_at"]) <= end
                and clock(row["last_seen_at"]) == end and type(row["seen_capture_count"]) is int
                and 0 < row["seen_capture_count"] <= 1000000, "RSS_OBSERVATION_INVALID")
    delta = value["delta"]
    require(isinstance(delta, dict) and set(delta) == {"status", "baseline_versions", "new_to_selected_predecessor", "unchanged_versions", "missing_from_current_window", "missing_meaning", "raw_bytes_unchanged"}, "RSS_DELTA_SHAPE")
    current = {entry["version_id"] for entry in window["entries"]}
    lists = [delta[k] for k in ("baseline_versions", "new_to_selected_predecessor", "unchanged_versions", "missing_from_current_window")]
    require(all(isinstance(items, list) and len(items) <= 512 and all(isinstance(s, str) and len(s) == 64 for s in items)
                and items == sorted(set(items)) for items in lists), "RSS_DELTA_SHAPE")
    baseline, new, unchanged, missing = map(set, lists)
    require(not(new & unchanged) and not(current & missing) and delta["missing_meaning"] == "NOT_RETRACTION_OR_DELETION", "RSS_DELTA_IDENTITY")
    if prior["reference"] is None:
        require(baseline == current and not(new or unchanged or missing) and delta["raw_bytes_unchanged"] is None
                and delta["status"] == "baseline-not-new-publications"
                and all(row["seen_capture_count"] == 1 and clock(row["first_seen_at"]) == end for row in rows), "RSS_BASELINE_INVALID")
    else:
        require(not baseline and new | unchanged == current and type(delta["raw_bytes_unchanged"]) is bool
                and delta["status"] == ("changed-window" if new or missing else "no-new-feed-versions"), "RSS_DELTA_IDENTITY")
    raw = original_bytes(value)
    if raw is not None:
        require(parse_window(raw, window["feed_id"]) == window, "RSS_ORIGINAL_REPLAY_DIFFERS")
    return value


def _load(store, ref):
    exact = reference(ref)
    result = store.read_source(SourceRef.from_dict(exact))
    require(result.get("availability") == "stored-representation" and isinstance(result.get("data"), str)
            and reference(result["reference"]) == exact, "RSS_SOURCE_UNAVAILABLE")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "RSS_DUPLICATE_STORED_KEY")
            value[key] = item
        return value
    try:
        value = json.loads(result["data"], object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise RecordError("RSS_STORED_JSON_INVALID") from None
    return verify_capture(value)


def _check_previous(previous, feed_id, requested_at, batch_id):
    require(previous["window"]["feed_id"] == feed_id, "RSS_PREDECESSOR_FEED_MISMATCH")
    require(clock(previous["received_at"]) <= clock(requested_at)
            and previous["batch_id"] != batch_id, "RSS_PREDECESSOR_NOT_EARLIER")


def open_capture(store, ref):
    """Reparse available original bytes and reconstruct exactly one saved transition."""
    value = _load(store, ref)
    prior_ref = value["predecessor"]["reference"]
    previous = _load(store, prior_ref) if prior_ref is not None else None
    if previous:
        _check_previous(previous, value["window"]["feed_id"], value["requested_at"], value["batch_id"])
    expected = _assemble(value["window"], batch_id=value["batch_id"], requested_at=value["requested_at"],
                         received_at=value["received_at"], previous=previous, predecessor=prior_ref, original=value["original"])
    require(value == expected, "RSS_TRANSITION_REPLAY_DIFFERS")
    return value


def collect(store, plan, *, batch_id, fetch, clock_fn=now):
    require(isinstance(plan, dict) and set(plan) <= {"feed_id", "bootstrap", "predecessor", "retain_original", "rights_basis"}
            and {"feed_id", "bootstrap"} <= set(plan), "RSS_PLAN_SHAPE")
    feed = plan["feed_id"]
    require(isinstance(feed, str) and feed in FEEDS and type(plan["bootstrap"]) is bool, "RSS_PLAN_SHAPE")
    prior_ref = reference(plan["predecessor"]) if plan.get("predecessor") is not None else None
    require(plan["bootstrap"] == (prior_ref is None), "RSS_EXPLICIT_PREDECESSOR_REQUIRED")
    retain = plan.get("retain_original", False)
    require(type(retain) is bool, "RSS_ORIGINAL_RIGHTS_REQUIRED")
    basis = text(plan.get("rights_basis", ""))
    require(bool(basis.strip()) if retain else not basis, "RSS_ORIGINAL_RIGHTS_REQUIRED")
    # Exact predecessor is read and checked before the only publisher request.
    previous = open_capture(store, prior_ref) if prior_ref is not None else None
    requested = clock_fn(); clock(requested)
    if previous:
        _check_previous(previous, feed, requested, batch_id)
    raw = fetch(feed)
    received = clock_fn()
    require(clock(requested) <= clock(received), "RSS_CAPTURE_CLOCK_ORDER")
    window = parse_window(raw, feed)
    original = {"status": "not-retained-rights-unconfirmed"}
    if retain:
        original = {"status": "retained-with-explicit-public-rights", "rights_basis": basis,
                    "gzip_base64": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")}
    return verify_capture(_assemble(window, batch_id=batch_id, requested_at=requested, received_at=received,
                                    previous=previous, predecessor=prior_ref, original=original))


def source_input(value, submission_id):
    verify_capture(value)
    return SourceInput(source_id="rss-" + submission_id, kind="content", title="RSS观察 / " + value["window"]["feed_id"],
                       origin=value["window"]["feed_url"], text=encoded(value).decode("utf-8"), custody="provided",
                       acquired_at=value["received_at"], limitations=(ATTRIBUTION,
                       "RSS_ORIGINAL_RETAINED" if original_bytes(value) is not None else "RSS_ORIGINAL_NOT_RETAINED",
                       "PUBLISHER_TIME_AND_ECONOMIC_EVENT_NOT_VERIFIED", COUNT_MEANING,
                       "SOURCE_FEED_SNAPSHOT_NOT_COMPLETE_24H_NEWS_OR_LINKED_ARTICLES"))


def read_capture(value, *, query="", offset=0, limit=40):
    verify_capture(value)
    require(isinstance(query, str) and len(query) <= 512 and type(offset) is int and offset >= 0
            and type(limit) is int and 1 <= limit <= 200, "RSS_READ_FILTER")
    selected = [row for row in value["observations"] if query.casefold() in row["entry"]["payload"]["title"].casefold()]
    require(offset <= len(selected), "RSS_READ_OFFSET")
    rows = deepcopy(selected[offset:offset+limit])
    for row in rows:
        p = row["entry"]["payload"]
        row["published_claim"] = publication_claim(p["published_raw"])
        row["updated_claim"] = publication_claim(p["updated_raw"])
    return {"capture_sha256": value["capture_sha256"], "feed_id": value["window"]["feed_id"], "attribution": ATTRIBUTION,
            "requested_at": value["requested_at"], "received_at": value["received_at"], "rows": rows,
            "selected_count": len(selected), "next_offset": offset+limit if offset+limit < len(selected) else None,
            "source_outcome": value["source_outcome"], "predecessor": value["predecessor"], "delta": value["delta"],
            "original_status": value["original"]["status"], "authority": AUTHORITY,
            "health_scope": "whole-selected-feed-capture", "count_meaning": COUNT_MEANING}
