"""Frozen handoff identity for one completed native news capture.

This is control metadata in the existing SourceInput store, not another news
pool or a scheduler. Journal recovery only reads the one original submission;
it never interprets an absent stage as permission to issue that write again.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import re

from decision_kernel.application.news_rolling import clock
from decision_kernel.domain.records import (
    DIGEST, SHA, MissingObject, RecordError, Selection, SourceInput, SourceRef,
    digest, encoded, identifier, require,
)
from decision_kernel.observations.news_windows import verify_reading

REPOSITORY = "auguspp/Decision-OS-2"
SCHEMA = "dos2-news-handoff-intent-v1"
MAX_PLAN_BYTES = 8 * 1024
PLAN_FIELDS = {"source_run", "artifact_id", "artifact_sha256", "as_of", "predecessor"}
INTENT_FIELDS = PLAN_FIELDS | {"schema", "source_code_sha", "handoff_code_sha", "handoff_run_id",
                              "expected_head", "batch_source_sha256", "prior_batch_reference"}
LIMITATIONS = (
    "FROZEN_HANDOFF_OF_ONE_COMPLETED_NATIVE_NEWS_CAPTURE",
    "NO_SOURCE_ACQUISITION_SCHEDULE_RESEARCH_OR_NOTIFICATION_AUTHORITY",
    "MISSING_OR_UNCONFIRMED_STAGES_REQUIRE_ORIGINAL_READ_ONLY_RECONCILIATION",
    "EXACT_EXISTING_SOURCE_STORE_NO_SECOND_NEWS_POOL",
)


def _native_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None,
            "NEWS_HANDOFF_NATIVE_ID_REQUIRED")
    return value


def keys(source_run):
    """Stable keys retain the existing batch submission across hosts."""
    source_run = _native_id(source_run)
    return {"intent": "news-handoff-" + source_run + "-intent",
            "batch": "news-window-" + source_run,
            "window": "news-handoff-" + source_run + "-window"}


def _exact(value):
    # The transport must carry a complete reference, not rely on implicit
    # Selection defaults or a caller's current snapshot.
    require(isinstance(value, dict) and set(value) == {"manifest", "selection"}
            and isinstance(value["manifest"], dict)
            and set(value["manifest"]) == {"repository", "commit", "path", "sha256"}
            and isinstance(value["selection"], dict)
            and set(value["selection"]) == {"kind", "start", "end", "columns"},
            "NEWS_HANDOFF_EXACT_REFERENCE_REQUIRED")
    ref = SourceRef.from_dict(value)
    require(ref.manifest.repository == REPOSITORY and ref.selection == Selection(),
            "NEWS_HANDOFF_REFERENCE_SCOPE")
    return json.loads(encoded(asdict(ref)))


def plan_input(value):
    require(isinstance(value, dict) and set(value) == PLAN_FIELDS,
            "NEWS_HANDOFF_PLAN_REQUIRED")
    require(len(encoded(value)) <= MAX_PLAN_BYTES, "NEWS_HANDOFF_PLAN_BOUND")
    _native_id(value["source_run"])
    _native_id(value["artifact_id"])
    require(isinstance(value["artifact_sha256"], str)
            and DIGEST.fullmatch(value["artifact_sha256"]) is not None,
            "NEWS_HANDOFF_ARCHIVE_DIGEST_REQUIRED")
    clock(value["as_of"])
    if value["predecessor"] is not None:
        _exact(value["predecessor"])
    # Freeze the caller's exact UTC/offset spelling rather than taking a fresh
    # clock or replacing the selected predecessor on a later attempt.
    return json.loads(encoded(value))


def validate_intent(value):
    require(isinstance(value, dict) and set(value) == INTENT_FIELDS
            and value["schema"] == SCHEMA, "NEWS_HANDOFF_INTENT_REQUIRED")
    require(len(encoded(value)) <= MAX_PLAN_BYTES, "NEWS_HANDOFF_INTENT_BOUND")
    plan_input({key: value[key] for key in PLAN_FIELDS})
    _native_id(value["handoff_run_id"])
    require(all(isinstance(value[key], str) and SHA.fullmatch(value[key]) is not None
                for key in ("source_code_sha", "handoff_code_sha", "expected_head"))
            and isinstance(value["batch_source_sha256"], str)
            and DIGEST.fullmatch(value["batch_source_sha256"]) is not None,
            "NEWS_HANDOFF_INTENT_IDENTITY")
    if value["prior_batch_reference"] is not None:
        ref = _exact(value["prior_batch_reference"])
        require(ref["manifest"]["path"] == "source-views/" + keys(value["source_run"])["batch"]
            + "/" + value["batch_source_sha256"] + "/manifest.json",
            "NEWS_HANDOFF_PRIOR_BATCH_IDENTITY")
    return json.loads(encoded(value))


def make_intent(plan, source, source_code_sha, expected_head, prior_batch_reference=None,
                *, handoff_code_sha, handoff_run_id):
    plan = plan_input(plan)
    require(isinstance(source, SourceInput) and source.source_id == keys(plan["source_run"])["batch"]
            and source.kind == "content" and source.custody == "provided"
            and source.origin == "https://github.com/" + REPOSITORY + "/actions/runs/" + plan["source_run"]
            and {"NATIVE_ARTIFACT_ID_" + plan["artifact_id"],
                 "NATIVE_ARCHIVE_SHA256_" + plan["artifact_sha256"],
                 "NATIVE_SOURCE_CODE_" + str(source_code_sha)} <= set(source.limitations),
            "NEWS_HANDOFF_BATCH_IDENTITY")
    try:
        reading = verify_reading(json.loads(source.text))
    except (ValueError, TypeError, UnicodeError) as error:
        if isinstance(error, RecordError):
            raise
        raise RecordError("NEWS_HANDOFF_BATCH_READING_INVALID") from None
    end = clock(plan["as_of"])
    times = [clock(row["observed_at"]) for row in reading["observations"]]
    for outcome in reading["source_outcomes"]:
        receipt = outcome.get("capture")
        if receipt is not None:
            times.extend(clock(receipt[key]) for key in ("requested_at", "received_at", "finished_at")
                         if receipt.get(key) is not None)
    require(all(at <= end for at in times), "NEWS_HANDOFF_CUTOFF_BEFORE_CAPTURE")
    return validate_intent({"schema": SCHEMA, **plan, "source_code_sha": source_code_sha,
        "handoff_code_sha": handoff_code_sha, "handoff_run_id": handoff_run_id,
        "expected_head": expected_head, "batch_source_sha256": digest(encoded(asdict(source))),
        "prior_batch_reference": prior_batch_reference})


def intent_source(intent):
    intent = validate_intent(intent)
    return SourceInput(source_id=keys(intent["source_run"])["intent"], kind="content",
        title="新闻原生采集交接意图 / " + intent["source_run"],
        origin="https://github.com/" + REPOSITORY + "/actions/runs/" + intent["source_run"],
        text=encoded(intent).decode("utf-8"), custody="provided", limitations=LIMITATIONS)


def retained_source(store, submission_id, expected_source_id):
    """Recover one immutable source through its original journal, without writes.

    A missing journal is the only absent result. A present journal whose source,
    identity, bytes or access are unavailable is an error, not a missing stage.
    The original journal commit is retained even if records has since advanced.
    """
    identifier(submission_id)
    identifier(expected_source_id)
    backend = store.backend
    head = backend.head()
    path = "submissions/" + submission_id + ".json"
    try:
        raw = backend.read(head, path)
    except MissingObject:
        return None
    require(len(raw) <= 4096, "NEWS_HANDOFF_JOURNAL_BOUND")
    try:
        journal = json.loads(raw)
    except (ValueError, UnicodeError, TypeError):
        raise RecordError("NEWS_HANDOFF_JOURNAL_INVALID") from None
    require(isinstance(journal, dict) and set(journal) == {"schema", "intent_sha256", "result_path"}
            and journal["schema"] == "submission-v1"
            and isinstance(journal["intent_sha256"], str)
            and DIGEST.fullmatch(journal["intent_sha256"]) is not None
            and isinstance(journal["result_path"], str)
            and re.fullmatch(r"source-views/" + re.escape(expected_source_id)
                + r"/[a-f0-9]{64}/manifest\.json", journal["result_path"]) is not None,
            "NEWS_HANDOFF_JOURNAL_INVALID")
    commit = backend.last_change(head, path)
    ref = SourceRef(backend.reference(commit, journal["result_path"]), Selection())
    data = store.read_source(ref)
    require(isinstance(data, dict) and encoded(data.get("reference")) == encoded(asdict(ref))
            and data.get("availability") == "stored-representation"
            and isinstance(data.get("data"), str), "NEWS_HANDOFF_STORED_SOURCE_REQUIRED")
    try:
        source = SourceInput.from_dict({**data["manifest"]["input"], "text": data["data"]})
    except (KeyError, TypeError):
        raise RecordError("NEWS_HANDOFF_STORED_SOURCE_REQUIRED") from None
    require(source.source_id == expected_source_id and source.kind == "content"
            and source.custody == "provided"
            and digest(encoded({"operation": "source", "input": asdict(source)})) == journal["intent_sha256"],
            "NEWS_HANDOFF_SUBMISSION_INTENT_MISMATCH")
    return {"reference": json.loads(encoded(asdict(ref))), "source": source, "data": data}
