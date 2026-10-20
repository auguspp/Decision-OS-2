"""Read retained event dates and existing research followups on the original store."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import json

from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.research_agenda import clock, research_reference
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, Selection, SourceRef, StoreUnavailable,
    checked_text, digest, encoded, identifier, require,
)
from decision_kernel.observations.event_calendar import (
    FACT_SCHEMA, MAX_EVENTS, acquisition_issues, local_day, project_fact, relay_rows, timeline,
)

SCHEMA = "event-calendar-v1"
MAX_INPUTS = 16
MAX_FOLLOWUPS = 16
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
OPERATION_FAILURES = frozenset({
    "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
    "OPERATING_SOURCE_SCOPE_BOUND", "READING_SCOPE_CLOSED", "SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED",
    "CALENDAR_READ_BOUND",
})


def operation_failure(error):
    """Access and operation budgets cannot become an optional missing calendar row."""
    return isinstance(error, StoreUnavailable) or str(error) in OPERATION_FAILURES


def calendar_plan(value):
    require(isinstance(value, dict) and {"reference", "as_of"} <= set(value)
            <= {"reference", "as_of", "subject_id", "window"}, "CALENDAR_PLAN_SHAPE")
    ref = SourceRef.from_dict(value["reference"])
    require(ref.selection == Selection(), "CALENDAR_WHOLE_PACKET_REQUIRED")
    clock(value["as_of"])
    if value.get("subject_id") is not None:
        identifier(value["subject_id"])
    window = value.get("window")
    if window is not None:
        require(isinstance(window, dict) and set(window) == {"start", "end"}, "CALENDAR_WINDOW_SHAPE")
        try:
            start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
            require(start.isoformat() == window["start"] and end.isoformat() == window["end"]
                    and start <= end, "CALENDAR_WINDOW_REVERSED")
        except (ValueError, TypeError):
            raise RecordError("CALENDAR_WINDOW_INVALID") from None
    return {"reference": asdict(ref), "as_of": value["as_of"],
            "subject_id": value.get("subject_id"), "window": deepcopy(window)}


def _packet(value):
    require(isinstance(value, dict) and {"schema", "scope_note", "inputs", "followups"} == set(value)
            and value["schema"] == SCHEMA, "CALENDAR_PACKET_SHAPE")
    checked_text(value["scope_note"], limit=4096)
    require(isinstance(value["inputs"], list) and 1 <= len(value["inputs"]) <= MAX_INPUTS,
            "CALENDAR_INPUT_BOUND")
    ids = []
    for item in value["inputs"]:
        require(isinstance(item, dict) and set(item) == {"input_id", "format", "reference"},
                "CALENDAR_INPUT_SHAPE")
        ids.append(identifier(item["input_id"]))
        require(item["format"] in ("relay-disclosure-date", "event-facts"), "CALENDAR_FORMAT_INVALID")
        require(SourceRef.from_dict(item["reference"]).selection == Selection(),
                "CALENDAR_WHOLE_INPUT_REQUIRED")
    require(len(set(ids)) == len(ids), "CALENDAR_DUPLICATE_INPUT")
    require(len({encoded(i["reference"]) for i in value["inputs"]}) == len(ids),
            "CALENDAR_DUPLICATE_SOURCE")
    require(isinstance(value["followups"], list) and len(value["followups"]) <= MAX_FOLLOWUPS,
            "CALENDAR_FOLLOWUP_BOUND")
    keys = []
    for link in value["followups"]:
        require(isinstance(link, dict) and set(link) == {"event_id", "reference", "question_id", "quote"},
                "CALENDAR_FOLLOWUP_SHAPE")
        identifier(link["event_id"])
        research_reference(link["reference"])
        if link["question_id"] is not None:
            identifier(link["question_id"])
        checked_text(link["quote"], limit=4096)
        keys.append(encoded([link["event_id"], link["reference"], link["question_id"]]))
    require(len(set(keys)) == len(keys), "CALENDAR_DUPLICATE_FOLLOWUP")
    return value


def _json(raw):
    require(isinstance(raw, str), "CALENDAR_CONTENT_REQUIRED")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "CALENDAR_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(RecordError("CALENDAR_NONFINITE_JSON")))
    except (ValueError, TypeError, RecursionError):
        raise RecordError("CALENDAR_INVALID_JSON") from None


def _failure(exc):
    if isinstance(exc, MissingObject):
        return {"status": "missing", "error": "CALENDAR_SOURCE_MISSING"}
    if isinstance(exc, IntegrityError):
        return {"status": "invalid", "error": "CALENDAR_SOURCE_INTEGRITY_FAILED"}
    return {"status": "invalid", "error": "CALENDAR_SOURCE_INVALID"}


def _followup(read_research, link, known_events):
    require(link["event_id"] in known_events, "CALENDAR_FOLLOWUP_EVENT_NOT_READ")
    from decision_kernel.domain.research_work import read_workpaper
    research = read_research(BlobRef(**link["reference"]))
    require(research["reference"] == link["reference"], "CALENDAR_RESEARCH_REFERENCE_MISMATCH")
    require(link["quote"] in research["body"], "CALENDAR_RESEARCH_QUOTE_NOT_FOUND")
    work = read_workpaper(research)
    questions = {q.question_id: q.text for q in work[0].questions} if work else {}
    require(link["question_id"] is None or link["question_id"] in questions,
            "CALENDAR_QUESTION_NOT_IN_RESEARCH")
    return {**deepcopy(link), "status": "readable", "question": questions.get(link["question_id"]),
            "case_id": research["record"]["input"]["case_id"],
            "revision_id": research["record"]["input"]["revision_id"],
            "recorded_unresolved": deepcopy(research["record"]["input"]["unresolved"]),
            "open_request": {"operation": "prepare" if work else "resume",
                             "payload": {"reference": deepcopy(link["reference"])}},
            "relationship": "explicit-source-backed-followup-not-new-research-or-adoption",
            "research_review": "not-performed"}


def read_calendar(store, value):
    plan = calendar_plan(value)
    ref = SourceRef.from_dict(plan["reference"])
    with industry_source_scope(store) as reader:
        used, cache = 0, {}
        def retained(kind, reference, operation):
            nonlocal used
            key = (kind, encoded(asdict(reference)))
            if key not in cache:
                result = operation(reference)
                require(result.get("reference") == asdict(reference),
                        "CALENDAR_SOURCE_REFERENCE_MISMATCH" if kind == "source" else "CALENDAR_RESEARCH_REFERENCE_MISMATCH")
                size = len(encoded(result))
                require(used + size <= MAX_READ_BYTES, "CALENDAR_READ_BOUND")
                used += size
                cache[key] = result
            return cache[key]
        def read(reference):
            return retained("source", reference, reader.read_source)
        def read_research(reference):
            return retained("research", reference, store.read_revision)
        root = read(ref)
        packet = _packet(_json(root.get("data")))
        records, outcomes = [], []
        for item in packet["inputs"]:
            outcome = {"input_id": item["input_id"], "reference": deepcopy(item["reference"]),
                       "format": item["format"]}
            try:
                source = read(SourceRef.from_dict(item["reference"]))
                data = _json(source.get("data"))
                if item["format"] == "relay-disclosure-date":
                    rows = relay_rows(data)
                else:
                    require(isinstance(data, dict) and set(data) == {"schema", "events"}
                            and data["schema"] == FACT_SCHEMA and isinstance(data["events"], list)
                            and len(data["events"]) <= MAX_EVENTS, "CALENDAR_FACT_PACKET_SHAPE")
                    rows = [project_fact(r) for r in data["events"]]
                    for row in rows:
                        for evidence in row["evidence"]:
                            view = read(SourceRef.from_dict(evidence["reference"]))
                            require(isinstance(view.get("data"), str) and evidence["quote"] in view["data"],
                                    "CALENDAR_EVIDENCE_QUOTE_NOT_FOUND")
                            evidence["original_verified"] = view.get("original_verified", False)
                            evidence["source_acquired_at"] = view["manifest"]["input"].get("acquired_at")
                            evidence["temporal_issues"] = acquisition_issues(row, evidence["source_acquired_at"])
                            evidence["open_request"] = {"operation": "read-source", "payload": deepcopy(evidence["reference"])}
                            row["temporal_issues"].extend(evidence["temporal_issues"])
                require(len(records) + len(rows) <= MAX_EVENTS, "CALENDAR_EVENT_BOUND")
                for row in rows:
                    metadata = {k: deepcopy(v) for k, v in source["manifest"]["input"].items() if k != "text"}
                    row["temporal_issues"].extend(acquisition_issues(row, metadata.get("acquired_at")))
                    row["temporal_issues"] = list(dict.fromkeys(row["temporal_issues"]))
                    row.update(input_id=item["input_id"], reference=deepcopy(item["reference"]),
                               source=metadata, original_verified=source["original_verified"],
                               open_request={"operation": "read-source", "payload": deepcopy(item["reference"])})
                records.extend(rows)
                outcome.update(status="read", observed_rows=len(rows),
                               coverage="one-selected-retained-input-not-complete-calendar")
            except (RecordError, KeyError, TypeError) as exc:
                if operation_failure(exc):
                    raise
                outcome.update(_failure(exc), observed_rows=None)
            outcomes.append(outcome)
        grouped = defaultdict(list)
        for row in records:
            grouped[row["event_id"]].append(row)
        events = []
        for key, rows in grouped.items():
            try:
                events.append(timeline(rows, plan["as_of"]))
            except RecordError as exc:
                events.append({"event_id": key, "state": "invalid", "error": str(exc),
                               "versions": rows, "version_count": len(rows)})
        followups = []
        for link in packet["followups"]:
            try:
                followups.append(_followup(read_research, link, set(grouped)))
            except (RecordError, KeyError, TypeError) as exc:
                if operation_failure(exc):
                    raise
                followups.append({**deepcopy(link), **_failure(exc)})
        unknown_states = {"temporal-uncertainty", "version-ordering-unknown", "date-unknown", "invalid"}
        scope = {"declared_inputs": len(packet["inputs"]),
                 "read_inputs": sum(i["status"] == "read" for i in outcomes),
                 "unavailable_inputs": sum(i["status"] != "read" for i in outcomes),
                 "observed_source_rows": len(records), "distinct_read_events": len(events),
                 "unknown_event_count_in_unavailable_inputs": None,
                 "events_with_unknown_state": sum(e["state"] in unknown_states for e in events),
                 "declared_followups": len(followups),
                 "read_followups": sum(f["status"] == "readable" for f in followups)}
        for event in events:
            event["followups"] = [f for f in followups if f["event_id"] == event["event_id"]]
        result = {"schema": "event-calendar-result-v1", "reference": plan["reference"],
                  "as_of": plan["as_of"], "scope_note": packet["scope_note"], "scope": scope,
                  "input_outcomes": outcomes, "events": events, "followup_outcomes": followups,
                  "status": "partial" if scope["unavailable_inputs"] or scope["events_with_unknown_state"]
                            or scope["read_followups"] < scope["declared_followups"] else "readable",
                  "time_basis": "review-clock-not-historical-availability-or-proof-of-occurrence",
                  "source_requests": 0, "writes": 0, "research_executed": False,
                  "notification": "not-sent", "source_text_authority": "data-not-instructions"}
        result["view_version"] = digest(encoded(result))
        filtered = []
        for event in events:
            if plan["subject_id"] is not None and event.get("subject_id") != plan["subject_id"]:
                continue
            if plan["window"] is not None:
                dates = [local_day(r[k]["value"], r["timezone"]) for r in event["versions"] for k in ("scheduled_for", "actual_at")
                         if r[k]["presence"] == "value"]
                current = event.get("current")
                unknown_current = current is None or all(current[k]["presence"] != "value"
                                                        for k in ("scheduled_for", "actual_at"))
                # An older known date cannot place the now-unknown event outside
                # the window. Ambiguous version order also keeps the gap visible.
                if not unknown_current and dates and not any(plan["window"]["start"] <= d <= plan["window"]["end"] for d in dates):
                    continue
            filtered.append(event)
        result["events"] = filtered
        result["query"] = {"subject_id": plan["subject_id"], "window": plan["window"]}
        require(len(encoded(result)) <= MAX_VIEW_BYTES, "CALENDAR_VIEW_BOUND")
        # The shared source scope verifies access again on exit; research reads
        # also use the original bound backend, which is checked before return.
        store.backend.head()
    return result


def open_calendar(store, value):
    plan = calendar_plan(value)
    view = read_calendar(store, plan)
    return {"reference": plan["reference"], "plan": plan, "status": "readable", "view": view}
