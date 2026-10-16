"""Bounded RSS failed-attempt receipts, not empty feeds or successful predecessors.

These are system observations only: no upstream error text, raw XML, titles or
mutable current pointer. A stored failure stays readable even when its attempted
predecessor is unavailable; it is never eligible to advance a feed version chain.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import re

from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, identifier, require
from decision_kernel.observations.rss_news import FEEDS, MAX_BODY

SCHEMA = "dos2-rss-failure-v1"
MAX_RECEIPT = 16 * 1024
MEANING = "FAILED_ATTEMPT_NOT_EMPTY_FEED_OR_SUCCESSFUL_PREDECESSOR"
# Only known finite codes cross the public boundary, never str(arbitrary_error).
ERRORS = frozenset((
    "RSS_ATTEMPT_FAILED", "RSS_SOURCE_UNAVAILABLE", "RSS_STORED_JSON_INVALID",
    "RSS_DUPLICATE_STORED_KEY", "RSS_CAPTURE_SHAPE", "RSS_CAPTURE_INTEGRITY",
    "RSS_PREDECESSOR_FEED_MISMATCH", "RSS_PREDECESSOR_NOT_EARLIER",
    "RSS_PREDECESSOR_NOT_CAPTURED", "RSS_TRANSITION_REPLAY_DIFFERS",
    "RSS_ORIGINAL_INTEGRITY", "RSS_ORIGINAL_ENCODING", "RSS_ORIGINAL_REPLAY_DIFFERS",
    "RSS_HTTP_UNAVAILABLE", "RSS_HTTP_RESPONSE_REJECTED", "RSS_HTTP_ENCODING_REJECTED",
    "RSS_HTTP_SIZE_REJECTED", "RSS_BODY_OR_FEED_REJECTED", "RSS_PARSER_VERSION_CHANGED",
    "RSS_DTD_REJECTED", "RSS_XML_REJECTED", "RSS_ITEM_BOUND", "RSS_LINK_REJECTED",
    "RSS_TITLE_REQUIRED", "RSS_TEXT_INVALID", "RSS_DUPLICATE_DOCUMENT",
    "RSS_CONFLICTING_ENTRY_ID", "RSS_VERSION_COLLISION", "RSS_CAPTURE_STORAGE_BOUND",
))


def safe_error(error):
    code = str(error) if isinstance(error, RecordError) else "RSS_ATTEMPT_FAILED"
    return code if code in ERRORS or re.fullmatch(r"RSS_HTTP_STATUS_[1-5][0-9]{2}", code) else "RSS_ATTEMPT_FAILED"


def _clock(value):
    require(isinstance(value, str) and len(value) <= 80 and "T" in value, "RSS_FAILURE_CLOCK")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "RSS_FAILURE_CLOCK")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecordError("RSS_FAILURE_CLOCK") from None


def verify_failure(value):
    keys = {"schema", "batch_id", "feed_id", "feed_url", "started_at", "requested_at",
            "received_at", "finished_at", "stage", "error_code", "predecessor",
            "response", "source_outcome", "observations", "original", "meaning", "capture_sha256"}
    require(isinstance(value, dict) and set(value) == keys, "RSS_FAILURE_SHAPE")
    identifier(value["batch_id"])
    require(value["schema"] == SCHEMA and isinstance(value["feed_id"], str)
            and value["feed_id"] in FEEDS and value["feed_url"] == FEEDS[value["feed_id"]]
            and value["stage"] in ("predecessor", "fetch", "parse")
            and isinstance(value["error_code"], str)
            and safe_error(RecordError(value["error_code"])) == value["error_code"]
            and value["source_outcome"] == "unavailable" and value["observations"] is None
            and value["original"] == {"status": "not-retained-failed-attempt"}
            and value["meaning"] == MEANING, "RSS_FAILURE_AUTHORITY")
    start, end = _clock(value["started_at"]), _clock(value["finished_at"])
    require(start <= end, "RSS_FAILURE_CLOCK")
    prior = value["predecessor"]
    require(isinstance(prior, dict) and set(prior) == {"reference", "recovery", "capture_sha256"},
            "RSS_FAILURE_PREDECESSOR")
    if prior["reference"] is None:
        require(prior["recovery"] == "explicit-bootstrap" and prior["capture_sha256"] is None
                and value["stage"] != "predecessor", "RSS_FAILURE_PREDECESSOR")
    else:
        ref = SourceRef.from_dict(prior["reference"])
        require(ref.selection.kind == "all" and encoded(asdict(ref)) == encoded(prior["reference"]),
                "RSS_FAILURE_PREDECESSOR")
        if value["stage"] == "predecessor":
            require(prior["recovery"] == "unavailable-or-rejected" and prior["capture_sha256"] is None,
                    "RSS_FAILURE_PREDECESSOR")
        else:
            require(prior["recovery"] in ("restored-original", "restored-metadata-only")
                    and isinstance(prior["capture_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", prior["capture_sha256"]), "RSS_FAILURE_PREDECESSOR")
    if value["stage"] == "predecessor":
        require(value["requested_at"] is None and value["received_at"] is None
                and value["response"] is None, "RSS_FAILURE_NO_HTTP")
    else:
        requested = _clock(value["requested_at"])
        require(start <= requested <= end, "RSS_FAILURE_CLOCK")
        if value["stage"] == "fetch":
            require(value["received_at"] is None and value["response"] is None, "RSS_FAILURE_NO_RESPONSE")
        else:
            require(requested <= _clock(value["received_at"]) <= end, "RSS_FAILURE_CLOCK")
            response = value["response"]
            require(isinstance(response, dict) and set(response) == {"bytes", "sha256"}
                    and type(response["bytes"]) is int and 0 <= response["bytes"] <= MAX_BODY
                    and isinstance(response["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", response["sha256"]),
                    "RSS_FAILURE_RESPONSE")
    require(len(encoded(value)) <= MAX_RECEIPT
            and value["capture_sha256"] == digest(encoded({k: v for k, v in value.items() if k != "capture_sha256"})),
            "RSS_FAILURE_INTEGRITY")
    return value


def failed_attempt(*, feed_id, batch_id, started_at, requested_at, received_at, finished_at,
                   stage, error, predecessor, previous, raw):
    prior = {"reference": deepcopy(predecessor), "recovery": "explicit-bootstrap", "capture_sha256": None}
    if predecessor is not None:
        prior["recovery"] = "unavailable-or-rejected"
        if stage != "predecessor":
            prior.update(recovery="restored-original" if previous["original"]["status"] == "retained-with-explicit-public-rights"
                         else "restored-metadata-only", capture_sha256=previous["capture_sha256"])
    value = {"schema": SCHEMA, "batch_id": batch_id, "feed_id": feed_id, "feed_url": FEEDS[feed_id],
             "started_at": started_at, "requested_at": requested_at, "received_at": received_at,
             "finished_at": finished_at, "stage": stage, "error_code": safe_error(error),
             "predecessor": prior, "response": {"bytes": len(raw), "sha256": digest(raw)} if stage == "parse" else None,
             "source_outcome": "unavailable", "observations": None,
             "original": {"status": "not-retained-failed-attempt"}, "meaning": MEANING}
    value["capture_sha256"] = digest(encoded(value))
    return verify_failure(value)


def news_outcome(value):
    verify_failure(value)
    receipt = {"requested_at": value["started_at"], "finished_at": value["finished_at"],
               "http_requested_at": value["requested_at"], "clock_meaning": "ATTEMPT_START_NOT_HTTP_REQUEST"}
    if value["received_at"] is not None:
        receipt["received_at"] = value["received_at"]
    return {"source": value["feed_id"], "provider": "rss", "status": "unavailable", "observations": None,
            "capture": receipt, "rss": {"failure": deepcopy(value)}}
