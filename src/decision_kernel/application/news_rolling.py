"""24-hour, exact-source rolling news. Observation clocks are not publisher clocks.

Reuses news_windows for observation validation and the caller's existing source
reader for custody. Only the bounded set of retained batch references is replayed;
no publisher URL, crawler, scheduler, or second content store is introduced.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import re
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, require
from decision_kernel.observations.news_windows import _bytes_json, _claim, _reading, verify_reading
from decision_kernel.observations.rss_reading import news_outcome, news_rows, verify_news_row
from decision_kernel.application.rss_intake import SCHEMA as RSS_SCHEMA, open_capture

SCHEMA = "dos2-news-rolling-v1"
WINDOW_HOURS = 24
MAX_BATCHES = 256
MAX_NEW_BATCHES = 12
MAX_OBSERVATIONS = 2000
MAX_BYTES = 512 * 1024
MEANING = "OBSERVED_IN_LAST_24_HOURS_NOT_PUBLISHED_IN_LAST_24_HOURS"


def clock(value):
    require(isinstance(value, str) and len(value) <= 80 and "T" in value,
            "NEWS_ROLL_CLOCK_REQUIRED")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "NEWS_ROLL_TIMEZONE_REQUIRED")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecordError("NEWS_ROLL_CLOCK_INVALID") from None


def reference(value):
    ref = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(ref.selection.kind == "all", "NEWS_ROLL_WHOLE_SOURCE_REQUIRED")
    return json.loads(encoded(asdict(ref)))


def _key(ref):
    return digest(encoded(ref))


def _read_verified(reader, ref):
    """Require the source reader's exact, verified stored representation, not a URL."""
    result = reader(SourceRef.from_dict(ref))
    require(isinstance(result, dict) and reference(result.get("reference")) == ref
            and result.get("availability") == "stored-representation"
            and isinstance(result.get("data"), str), "NEWS_ROLL_SOURCE_UNAVAILABLE")
    manifest = result.get("manifest")
    require(isinstance(manifest, dict)
            and digest(encoded(manifest)) == ref["manifest"]["sha256"],
            "NEWS_ROLL_MANIFEST_MISMATCH")
    raw = result["data"].encode("utf-8")
    representation = manifest.get("representation", {})
    require(isinstance(representation, dict) and len(raw) <= 2 * 1024 * 1024
            and representation.get("size") == len(raw)
            and representation.get("sha256") == digest(raw), "NEWS_ROLL_SOURCE_BYTES_MISMATCH")
    return result


def _load(reader, ref):
    return _bytes_json(_read_verified(reader, ref)["data"].encode("utf-8"))


def read_news_batch(reader, ref):
    """Read a news batch or qualify one native RSS transition, without source HTTP."""
    exact = reference(ref)
    result = _read_verified(reader, exact)
    value = _bytes_json(result["data"].encode("utf-8"))
    if not isinstance(value, dict) or value.get("schema") != RSS_SCHEMA:
        return verify_reading(value)
    # Adapt the existing reader, not its store/writer. Verify predecessor bytes too.
    store = SimpleNamespace(read_source=lambda r: result if reference(r) == exact
                            else _read_verified(reader, reference(r)))
    try:
        capture = open_capture(store, exact)
    except (PackageNotFoundError, ModuleNotFoundError):
        raise RecordError("NEWS_RSS_ORIGINAL_PARSER_REQUIRED") from None
    return _reading(news_rows(capture), [news_outcome(capture)])


def _metadata_rows(rows):
    row_keys = {"article_id", "body_archived", "event_verified", "observed_at", "other_time_claims",
                "provider", "publication_verified", "published_claim", "representation",
                "research_started", "source_id", "title", "url", "version_id"}
    for row in rows:
        if isinstance(row, dict) and row.get("provider") == "rss":
            verify_news_row(row)
            continue
        require(isinstance(row, dict) and set(row) == row_keys and row["research_started"] is False,
                "NEWS_ROLL_METADATA_ONLY")
        require(isinstance(row["published_claim"], dict)
                and row["published_claim"] == _claim(row["published_claim"].get("raw"))
                and isinstance(row["other_time_claims"], dict)
                and set(row["other_time_claims"]) <= {"extra.date"}
                and all(isinstance(claim, dict) and claim == _claim(claim.get("raw")) for claim in row["other_time_claims"].values()),
                "NEWS_ROLL_PUBLICATION_CLAIM")


def _outcomes(reading, ref, end):
    _metadata_rows(reading["observations"])
    outcomes = reading["source_outcomes"]
    require(0 < len(outcomes) <= 12, "NEWS_ROLL_SOURCE_OUTCOMES_BOUND")
    result, times = [], [clock(row["observed_at"]) for row in reading["observations"]]
    for item in outcomes:
        require(isinstance(item, dict) and isinstance(item.get("source"), str)
                and re.fullmatch(r"[A-Za-z0-9_:\u4e00-\u9fff-]{1,100}", item["source"])
                and item.get("status") in ("captured", "unavailable"), "NEWS_ROLL_OUTCOME_INVALID")
        n = item.get("observations")
        require((type(n) is int and n >= 0) if item["status"] == "captured" else n is None,
                "NEWS_ROLL_OUTCOME_COUNT")
        receipt = item.get("capture")
        at, basis = None, "unlocated-within-selected-batch"
        if receipt is not None:
            require(isinstance(receipt, dict), "NEWS_ROLL_RECEIPT_INVALID")
            start = clock(receipt.get("requested_at"))
            received = clock(receipt["received_at"]) if "received_at" in receipt else None
            finish = clock(receipt["finished_at"]) if "finished_at" in receipt else received
            require(finish is not None and start <= finish <= end
                    and (received is None or start <= received <= finish), "NEWS_ROLL_RECEIPT_CLOCK")
            at, basis = finish, "capture-receipt-clock"
        else:
            matching = [clock(r["observed_at"]) for r in reading["observations"]
                        if r["source_id"] == item["source"]]
            if matching:
                at, basis = max(matching), "legacy-observed-at-not-receipt-clock"
        if at is not None:
            times.append(at)
        result.append({"reference": ref, "outcome": deepcopy(item),
                       "observed_at": at.isoformat() if at is not None else None,
                       "clock_basis": basis})
    require(all(t <= end for t in times), "NEWS_ROLL_FUTURE_OBSERVATION")
    return result, times


def _coverage(value):
    start, end = clock(value["window_start"]), clock(value["as_of"])
    times = [start, end]
    times += [clock(e["first_seen_in_window"]) for e in value["entries"]]
    times += [clock(e["last_seen_in_window"]) for e in value["entries"]]
    times += [clock(x["observed_at"]) for x in value["source_outcomes"]
              if x["observed_at"] is not None and start <= clock(x["observed_at"]) <= end]
    times.sort()
    return {"retained_batches": len(value["batch_references"]),
            "retained_versions": len(value["entries"]),
            "failed_source_windows": sum(x["outcome"]["status"] != "captured"
                                         for x in value["source_outcomes"]),
            "unlocated_source_windows": sum(x["observed_at"] is None for x in value["source_outcomes"]),
            "maximum_observation_gap_seconds": max((b-a).total_seconds() for a, b in zip(times, times[1:])),
            "complete_news_coverage": False, "continuous_capture_verified": False,
            "gap_meaning": "OBSERVATION_CLOCK_GAPS_NOT_PROOF_OF_NO_NEWS"}


def _seal(value):
    value.pop("rolling_sha256", None)
    value["coverage"] = _coverage(value)
    value["rolling_sha256"] = digest(encoded(value))
    return value


def verify_rolling(value):
    require(isinstance(value, dict) and value.get("schema") == SCHEMA
            and value.get("window_hours") == WINDOW_HOURS
            and value.get("meaning") == MEANING, "NEWS_ROLL_SCHEMA")
    require(set(value) == {"schema", "window_hours", "as_of", "window_start", "meaning", "interval",
                          "predecessor", "batch_references", "rejected_references", "entries",
                          "source_outcomes", "losses", "count_meaning", "coverage", "rolling_sha256"}
            and value["interval"] == "closed-both-ends", "NEWS_ROLL_SHAPE")
    recovery = value["predecessor"]
    require(isinstance(recovery, dict) and set(recovery) == {"status", "reference"}
            and recovery["status"] in ("not-supplied", "restored-exact-reference", "unavailable-or-rejected"),
            "NEWS_ROLL_RECOVERY_INVALID")
    if recovery["status"] == "not-supplied":
        require(recovery["reference"] is None, "NEWS_ROLL_RECOVERY_INVALID")
    else:
        reference(recovery["reference"])
    original = deepcopy(value)
    checksum = original.pop("rolling_sha256", None)
    require(checksum == digest(encoded(original)), "NEWS_ROLL_DIGEST")
    start, end = clock(value["window_start"]), clock(value["as_of"])
    require(end - start == timedelta(hours=WINDOW_HOURS), "NEWS_ROLL_WINDOW")
    refs = value["batch_references"]
    require(isinstance(refs, list) and len(refs) <= MAX_BATCHES
            and len({_key(reference(r)) for r in refs}) == len(refs), "NEWS_ROLL_BATCH_BOUND")
    keys = {_key(reference(r)) for r in refs}
    rejected = value["rejected_references"]
    require(isinstance(rejected, list) and len(rejected) <= MAX_BATCHES + MAX_NEW_BATCHES
            and all(_key(reference(r)) not in keys for r in rejected), "NEWS_ROLL_REJECTIONS_INVALID")
    entries = value["entries"]
    require(isinstance(entries, list) and len(entries) <= MAX_OBSERVATIONS, "NEWS_ROLL_ENTRY_BOUND")
    ids = set()
    for entry in entries:
        row = entry["observation"]
        first, last = clock(entry["first_seen_in_window"]), clock(entry["last_seen_in_window"])
        require(start <= first <= last <= end and clock(row["observed_at"]) == first,
                "NEWS_ROLL_ENTRY_CLOCK")
        batch_ids = entry["seen_reading_sha256"]
        require(isinstance(batch_ids, list) and 0 < len(batch_ids) <= MAX_BATCHES
                and len(set(batch_ids)) == len(batch_ids)
                and all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in batch_ids)
                and entry["seen_batch_count_in_window"] == len(batch_ids)
                and _key(reference(entry["first_reference"])) in keys
                and _key(reference(entry["last_reference"])) in keys,
                "NEWS_ROLL_ENTRY_PROVENANCE")
        require(row["version_id"] not in ids, "NEWS_ROLL_DUPLICATE_VERSION")
        ids.add(row["version_id"])
    require(isinstance(value["source_outcomes"], list)
            and len(value["source_outcomes"]) <= MAX_BATCHES * 12, "NEWS_ROLL_HEALTH_BOUND")
    for item in value["source_outcomes"]:
        require(_key(reference(item["reference"])) in keys, "NEWS_ROLL_HEALTH_REFERENCE")
        require(item["observed_at"] is None or start <= clock(item["observed_at"]) <= end,
                "NEWS_ROLL_HEALTH_CLOCK")
    _metadata_rows([e["observation"] for e in entries])
    # Same news owner: reuse its row/authority/domain validation, not another codec.
    _reading([e["observation"] for e in entries], [x["outcome"] for x in value["source_outcomes"]])
    require(isinstance(value["losses"], list) and len(value["losses"]) <= MAX_BATCHES,
            "NEWS_ROLL_LOSS_BOUND")
    for loss in value["losses"]:
        require(isinstance(loss, dict) and start <= clock(loss["at"]) <= end
                and isinstance(loss.get("kind"), str) and re.fullmatch(r"[A-Z_]{1,64}", loss["kind"])
                and type(loss.get("count")) is int and loss["count"] > 0, "NEWS_ROLL_LOSS_INVALID")
    require(value["coverage"] == _coverage(value) and len(encoded(value)) <= MAX_BYTES,
            "NEWS_ROLL_COVERAGE_OR_SIZE")
    return value


def rolling_news(reader, references, *, as_of, predecessor=None):
    """Read exact retained batches again. A bad predecessor limits, not erases, new news."""
    end = clock(as_of)
    require(end.year > 1, "NEWS_ROLL_CLOCK_RANGE")
    start = end - timedelta(hours=WINDOW_HOURS)
    require(isinstance(references, (tuple, list)) and len(references) <= MAX_NEW_BATCHES,
            "NEWS_ROLL_NEW_BATCH_BOUND")
    incoming = [reference(r) for r in references]
    prior_ref = reference(predecessor) if predecessor is not None else None
    refs, losses, previous = [], [], None
    recovery = {"status": "not-supplied", "reference": prior_ref}

    def loss(kind, count=1):
        if count:
            losses.append({"kind": kind, "at": end.isoformat(), "count": count})

    if prior_ref is not None:
        try:
            previous = verify_rolling(_load(reader, prior_ref))
            require(clock(previous["as_of"]) <= end, "NEWS_ROLL_PREDECESSOR_FROM_FUTURE")
            refs.extend(previous["batch_references"])
            losses.extend(deepcopy(x) for x in previous["losses"] if clock(x["at"]) >= start)
            recovery["status"] = "restored-exact-reference"
        except (RecordError, OSError, TypeError, KeyError, UnicodeError):
            previous = None
            recovery["status"] = "unavailable-or-rejected"
            loss("PREDECESSOR_GAP")
    refs.extend(incoming)
    # Identical explicit refs are idempotent; no second HTTP capture or exposure count.
    refs = sorted({_key(r): r for r in refs}.values(), key=_key)
    batches, rejected = [], []
    for ref in refs:
        try:
            reading = read_news_batch(reader, ref)
            health, times = _outcomes(reading, ref, end)
            if times and max(times) < start:
                continue
            batches.append((max(times) if times else end, ref, reading, health))
        except (RecordError, OSError, TypeError, KeyError, UnicodeError):
            rejected.append(ref)
    loss("BATCH_UNAVAILABLE_OR_REJECTED", len(rejected))
    distinct_batches = {}
    for batch in batches:
        distinct_batches.setdefault(batch[2]["reading_sha256"], batch)
    batches = list(distinct_batches.values())
    batches.sort(key=lambda b: (b[0], _key(b[1])), reverse=True)
    loss("BATCH_LIMIT", max(0, len(batches) - MAX_BATCHES))
    batches = batches[:MAX_BATCHES]
    entries, outcomes, seen_batches = {}, [], set()
    for _, ref, reading, health in reversed(batches):
        # Same bytes saved under another source ID are not another capture.
        batch_id = reading["reading_sha256"]
        if batch_id in seen_batches:
            continue
        seen_batches.add(batch_id)
        outcomes.extend(x for x in health if x["observed_at"] is None
                        or start <= clock(x["observed_at"]) <= end)
        for row in reading["observations"]:
            when = clock(row["observed_at"])
            if not start <= when <= end:
                continue
            key = row["version_id"]
            if key not in entries:
                entries[key] = {"observation": deepcopy(row), "first_seen_in_window": when.isoformat(),
                                "last_seen_in_window": when.isoformat(), "first_reference": ref,
                                "last_reference": ref, "seen_reading_sha256": []}
            entry = entries[key]
            require({k: v for k, v in entry["observation"].items() if k != "observed_at"}
                    == {k: v for k, v in row.items() if k != "observed_at"}, "NEWS_ROLL_VERSION_COLLISION")
            if when < clock(entry["first_seen_in_window"]):
                entry.update(observation=deepcopy(row), first_seen_in_window=when.isoformat(), first_reference=ref)
            if when >= clock(entry["last_seen_in_window"]):
                entry.update(last_seen_in_window=when.isoformat(), last_reference=ref)
            if batch_id not in entry["seen_reading_sha256"]:
                entry["seen_reading_sha256"].append(batch_id)
            entry["seen_batch_count_in_window"] = len(entry["seen_reading_sha256"])
    ordered = sorted(entries.values(), key=lambda e: (clock(e["last_seen_in_window"]), e["observation"]["version_id"]), reverse=True)
    loss("OBSERVATION_LIMIT", max(0, len(ordered) - MAX_OBSERVATIONS))
    ordered = ordered[:MAX_OBSERVATIONS]
    if len(losses) > MAX_BATCHES - 1:
        dropped = len(losses) - MAX_BATCHES + 2
        losses = losses[dropped:]
        loss("LOSS_LOG_LIMIT", dropped)
    value = {"schema": SCHEMA, "window_hours": WINDOW_HOURS, "as_of": end.isoformat(),
             "window_start": start.isoformat(), "meaning": MEANING,
             "interval": "closed-both-ends", "predecessor": recovery,
             "batch_references": [b[1] for b in batches], "rejected_references": rejected,
             "entries": ordered, "source_outcomes": outcomes, "losses": losses,
             "count_meaning": "DISTINCT_RETAINED_BATCHES_WITHIN_WINDOW_NOT_LIFETIME_OR_INDEPENDENT_EVENTS"}
    removed = 0
    while len(encoded(_seal(value))) > MAX_BYTES and value["entries"]:
        count = max(1, len(value["entries"]) // 10)
        removed += count
        del value["entries"][-count:]
    if removed:
        loss("BYTE_LIMIT", removed)
        _seal(value)
        # Reserve the small loss receipt as well; never trim health to look green.
        while len(encoded(value)) > MAX_BYTES and value["entries"]:
            value["entries"].pop(); losses[-1]["count"] += 1; _seal(value)
    return verify_rolling(_seal(value))


def read_rolling(value, *, source=None, query="", limit=40, cursor=None):
    """Page titles while always returning whole-window source health and losses."""
    verify_rolling(value)
    require(type(limit) is int and 1 <= limit <= 200 and isinstance(query, str) and len(query) <= 512
            and (source is None or (isinstance(source, str) and len(source) <= 100)), "NEWS_ROLL_READ_FILTER")
    selector = digest(encoded({"rolling_sha256": value["rolling_sha256"], "source": source, "query": query}))
    offset = 0
    if cursor is not None:
        require(isinstance(cursor, dict) and set(cursor) == {"selector", "offset"}
                and cursor["selector"] == selector and type(cursor["offset"]) is int
                and cursor["offset"] >= 0, "NEWS_ROLL_CURSOR_MISMATCH")
        offset = cursor["offset"]
    rows = [e for e in value["entries"] if (source is None or e["observation"]["source_id"] == source)
            and query.casefold() in e["observation"]["title"].casefold()]
    require(offset <= len(rows), "NEWS_ROLL_CURSOR_RANGE")
    following = offset + limit
    return {"status": "observation-only", "window_hours": WINDOW_HOURS, "as_of": value["as_of"],
            "window_start": value["window_start"], "rolling_sha256": value["rolling_sha256"],
            "rows": deepcopy(rows[offset:following]), "selected_count": len(rows),
            "next_cursor": {"selector": selector, "offset": following} if following < len(rows) else None,
            "source_outcomes": deepcopy(value["source_outcomes"]), "source_outcomes_scope": "whole-24h-window",
            "coverage": deepcopy(value["coverage"]), "losses": deepcopy(value["losses"]),
            "predecessor": deepcopy(value["predecessor"]), "rejected_references": deepcopy(value["rejected_references"]),
            "meaning": MEANING, "complete_news_coverage": False}
