"""Seal original Watch activity refs and replay every segment's original bytes.

Seals are ordinary native research artifacts in the same event/pointer/journal
CAS. They contain no state checkpoint. Only bounded ref lists survive between
segments; the existing Watch projection rebuilds all lifecycle/condition state.
"""
import base64
from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.records import BlobRef, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import ARTIFACT_SCHEMA, ArtifactInput
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD, validate_change, validate_check
from decision_kernel.domain.watch_history import (
    EVENT_SCHEMA, HISTORY_VERSION, MAX_SEGMENTS, SEGMENT_EVENTS, SEGMENT_NAME, SEGMENT_PREFIX,
    SEGMENT_SCHEMA, exact, position, validate_history, validate_segment,
)
from decision_kernel.infrastructure.watch_snapshot import verify_event_parent


def artifact_files(artifact):
    path = artifact.path()
    content_path = path.rsplit("/", 1)[0] + "/content.bin"
    return {path: encoded({"schema": ARTIFACT_SCHEMA, "input": artifact.metadata(), "data_path": content_path}),
            content_path: artifact.raw()}


def segment_artifact(value):
    raw = encoded(validate_segment(value))
    return ArtifactInput.from_bytes(SEGMENT_PREFIX + digest(raw), SEGMENT_NAME, "application/json", raw)


def local_edge(artifact):
    return {"kind": "same-commit", "path": artifact.path(),
            "sha256": digest(artifact_files(artifact)[artifact.path()])}


def open_segment(reference, reader):
    ref = exact(reference)
    view = ResearchArtifacts(reader).read(ref)
    try:
        raw = base64.b64decode(view["data_base64"], validate=True)
        value = validate_segment(json.loads(raw))
        artifact = segment_artifact(value)
        require(encoded(value) == raw and artifact.path() == ref["path"]
                and artifact.metadata() == view["metadata"], "WATCH_HISTORY_SEGMENT_BYTES_MISMATCH")
    except (TypeError, KeyError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("WATCH_HISTORY_SEGMENT_INVALID") from None
    return value


def event_record(reference, reader):
    event = ConditionalOdds(reader)._open(reference, ACTIVITY_RECORD)
    versioned = isinstance(event, dict) and event.get("schema") == EVENT_SCHEMA
    shape(event, {"schema", "watch_key", "command", "snapshot", "state", "summary", "result"}
          | ({"history"} if versioned else set()))
    require(event["schema"] in ("watch-activity-event-v1", EVENT_SCHEMA), "WATCH_ACTIVITY_EVENT_INVALID")
    shape(event["command"], {"kind", "input"})
    require(event["command"]["kind"] in ("change", "check"), "WATCH_ACTIVITY_EVENT_INVALID")
    command = event["command"]
    if command["kind"] == "change":
        validate_change(command["input"])
    else:
        validate_check(command["input"])
    if versioned:
        validate_history(event["history"])
    return event


def history_row(reference, event):
    payload = event["command"]["input"]
    return {"reference": reference, "action": payload.get("action", "check"),
            "at": payload.get("at", payload.get("checked_at")), "state": event["state"],
            "check_status": event["result"]["status"] if event["result"] else None}


def verify_native_event(owner, reference, event, reader, native):
    verify_event_parent(native, reference["commit"], event["snapshot"])
    selected = owner._history_current(event["watch_key"], reference["commit"], reader, native)
    require(selected == reference, "WATCH_HISTORY_EVENT_NOT_ATOMIC_SELECTION")
    content_path = reference["path"].rsplit("/", 1)[0] + "/content.bin"
    require(reader.backend.last_change(reference["commit"], content_path) == reference["commit"],
            "WATCH_HISTORY_EVENT_BODY_NOT_ATOMIC")


def verify_legacy_prefix(owner, tip, reader, native):
    """Explicit adoption inherits only genuine v1 CAS events, with no edits."""
    frozen, state, rows = owner._replay_v1(tip, reader)
    for row in rows:
        ref = row["reference"]
        event = event_record(ref, reader)
        verify_native_event(owner, ref, event, reader, native)
        current = owner._history_current(event["watch_key"], event["snapshot"], reader, native)
        require(current == event["command"]["input"]["expected_event"], "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
    state["_history_tail"] = [row["reference"] for row in rows]
    state["_history_segments"] = []
    return frozen, state, rows


def _segments(tip, reader):
    """Read at most one current tail and 31 bounded seal manifests."""
    refs, seen, ref = [], set(), exact(tip)
    first = event_record(ref, reader)
    require(first["schema"] == EVENT_SCHEMA, "WATCH_HISTORY_V2_EVENT_REQUIRED")
    index, offset = first["history"]["segment_index"], first["history"]["event_index"]
    for expected in range(offset, -1, -1):
        key = encoded(ref)
        require(key not in seen, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
        seen.add(key)
        event = first if expected == offset else event_record(ref, reader)
        require(event["schema"] == EVENT_SCHEMA and event["history"]["segment_index"] == index
                and event["history"]["event_index"] == expected, "WATCH_HISTORY_TAIL_BOUNDARY_MISMATCH")
        refs.append(ref)
        if expected:
            ref = exact(event["command"]["input"]["expected_event"])
    edge = event["history"]["previous_segment"]
    previous = (asdict(BlobRef(ref["repository"], ref["commit"], edge["path"], edge["sha256"]))
                if edge is not None else None)
    seals = []
    for expected in range(index - 1, -1, -1):
        require(previous is not None, "WATCH_HISTORY_SEGMENT_MISSING")
        previous = exact(previous)
        key = encoded(previous)
        require(key not in seen, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
        seen.add(key)
        value = open_segment(previous, reader)
        require(value["segment_index"] == expected, "WATCH_HISTORY_SEGMENT_BOUNDARY_MISMATCH")
        seals.append({"reference": previous, "segment": value})
        previous = value["previous_segment"]
    require(previous is None, "WATCH_HISTORY_UNEXPECTED_SEGMENT")
    seals.reverse()
    return seals, list(reversed(refs)), index


def replay(owner, tip, reader, native, *, collect_segment=None):
    seals, tail, current_index = _segments(tip, reader)
    selected_index = current_index if collect_segment is None else collect_segment
    require(type(selected_index) is int and 0 <= selected_index <= current_index,
            "WATCH_HISTORY_SEGMENT_NOT_IN_PREFIX")
    groups = [(item["segment"]["events"], item) for item in seals] + [(tail, None)]
    state, frozen, watch_ref, rows, seen = None, None, None, [], set()
    selected_summary = None
    previous_seal = None
    for index, (refs, seal) in enumerate(groups):
        entry_event = state["event_ref"] if state else None
        if seal:
            value = seal["segment"]
            require(value["entry_event"] == entry_event
                    and value["previous_segment"] == (previous_seal["reference"] if previous_seal else None),
                    "WATCH_HISTORY_SEGMENT_ENTRY_MISMATCH")
        legacy = bool(seal and seal["segment"]["kind"] == "legacy-v1")
        for offset, raw_ref in enumerate(refs):
            ref = exact(raw_ref)
            key = encoded(ref)
            require(key not in seen and len(seen) < MAX_SEGMENTS * SEGMENT_EVENTS,
                    "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
            seen.add(key)
            event = event_record(ref, reader)
            p = event["command"]["input"]
            if frozen is None:
                watch_ref = p["watch_ref"]
                frozen = owner._watch(watch_ref, reader)
            require(p["watch_ref"] == watch_ref and event["watch_key"] == frozen["watch_sha256"],
                    "WATCH_ACTIVITY_WATCH_CHANGED")
            if seal:
                require(seal["segment"]["watch_ref"] == watch_ref
                        and seal["segment"]["watch_key"] == frozen["watch_sha256"], "WATCH_HISTORY_WATCH_CHANGED")
            if legacy:
                require(index == 0 and event["schema"] == "watch-activity-event-v1",
                        "WATCH_HISTORY_LEGACY_EVENT_INVALID")
                metadata = None
            else:
                edge = None
                if previous_seal:
                    prior_ref = previous_seal["reference"]
                    edge = ({"kind": "same-commit", "path": prior_ref["path"], "sha256": prior_ref["sha256"]}
                            if offset == 0 else {"kind": "exact", "reference": prior_ref})
                    if offset == 0:
                        require(ref["commit"] == prior_ref["commit"], "WATCH_HISTORY_SEAL_NOT_ATOMIC")
                        for path in (prior_ref["path"], prior_ref["path"].rsplit("/", 1)[0] + "/content.bin"):
                            require(reader.backend.last_change(ref["commit"], path) == ref["commit"],
                                    "WATCH_HISTORY_SEAL_NOT_ATOMIC")
                metadata = position(index, offset, edge)
                require(event["schema"] == EVENT_SCHEMA and event["history"] == metadata,
                        "WATCH_HISTORY_EVENT_BOUNDARY_MISMATCH")
            verify_native_event(owner, ref, event, reader, native)
            current = owner._history_current(frozen["watch_sha256"], event["snapshot"], reader, native)
            require(current == p["expected_event"], "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
            rebuilt, state = owner._project(frozen, event["command"], event["snapshot"], state, reader, history=metadata)
            require(encoded(rebuilt) == encoded(event), "WATCH_ACTIVITY_REPLAY_MISMATCH")
            state["event_ref"] = ref
            if event["result"] is not None:
                state["last_check_ref"] = ref
            if index == selected_index:
                rows.append(history_row(ref, event))
                selected_summary = deepcopy(state["summary"])
        if seal:
            previous_seal = seal
    require(state is not None, "WATCH_ACTIVITY_EVENT_REQUIRED")
    state["_history_tail"] = tail
    state["_history_segments"] = seals
    state["_history_selected_summary"] = selected_summary
    return frozen, state, rows


def next_position(before, command, watch_key):
    """Plan a seal only on explicit legacy adoption or a completely full tail."""
    p = command["input"]
    if before is None:
        require(command["kind"] == "change" and p.get("history_version") == HISTORY_VERSION
                and p["action"] == "register", "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
        return position(0, 0), None
    previous = before.get("history")
    if previous is None:
        require(command["kind"] == "change" and p.get("history_version") == HISTORY_VERSION
                and p["action"] == "migrate", "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
        index, kind, reason = 0, "legacy-v1", "legacy-adoption"
    else:
        index, kind, reason = previous["segment_index"], "native-v2", "capacity"
        if previous["event_index"] + 1 < SEGMENT_EVENTS:
            edge = None
            if index:
                edge = {"kind": "exact", "reference": before["_history_segments"][-1]["reference"]}
            return position(index, previous["event_index"] + 1, edge), None
    require(index + 1 < MAX_SEGMENTS, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
    prior = before["_history_segments"][-1] if before["_history_segments"] else None
    segment = {"schema": SEGMENT_SCHEMA, "history_version": HISTORY_VERSION,
        "watch_key": watch_key, "watch_ref": p["watch_ref"], "segment_index": index,
        "kind": kind, "sealed_because": reason, "previous_segment": prior["reference"] if prior else None,
        "entry_event": prior["segment"]["events"][-1] if prior else None, "events": before["_history_tail"]}
    artifact = segment_artifact(segment)
    return position(index + 1, 0, local_edge(artifact)), artifact


def segment_listing(state):
    return [{"reference": row["reference"], "segment_index": row["segment"]["segment_index"],
             "kind": row["segment"]["kind"], "event_count": len(row["segment"]["events"]),
             "first_event": row["segment"]["events"][0], "last_event": row["segment"]["events"][-1]}
            for row in state["_history_segments"]]


def validation(state):
    return {"version": HISTORY_VERSION, "scope": "complete-original-prefix", "summary_trusted": False,
            "verified_events": state["summary"]["events"], "current_segment_index": state["history"]["segment_index"],
            "current_segment_events": len(state["_history_tail"]), "sealed_segments": len(state["_history_segments"])}
