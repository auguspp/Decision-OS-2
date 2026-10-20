"""Bounded original-event segments, never trusted lifecycle checkpoints.

The v1 activity bound is unchanged. Only an explicit v2 registration/adoption
uses this separate storage contract; all condition and window rules still apply.
"""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.records import DIGEST, require
from decision_kernel.domain.research_artifacts import ARTIFACT_PATH, artifact_reference

HISTORY_VERSION = "watch-activity-history-v2"
EVENT_SCHEMA = "watch-activity-event-v2"
SEGMENT_SCHEMA = "watch-activity-segment-v1"
SEGMENT_EVENTS = 128
MAX_SEGMENTS = 32
SEGMENT_PREFIX = "odds-watch-segment-"
SEGMENT_NAME = "odds-watch-segment.json"


def exact(reference):
    return asdict(artifact_reference(reference))


def validate_history(value):
    shape(value, {"version", "segment_index", "event_index", "previous_segment"})
    require(value["version"] == HISTORY_VERSION, "WATCH_HISTORY_VERSION_UNSUPPORTED")
    require(type(value["segment_index"]) is int and 0 <= value["segment_index"] < MAX_SEGMENTS,
            "WATCH_HISTORY_SEGMENT_LIMIT")
    require(type(value["event_index"]) is int and 0 <= value["event_index"] < SEGMENT_EVENTS,
            "WATCH_HISTORY_EVENT_LIMIT")
    edge = value["previous_segment"]
    if value["segment_index"] == 0:
        require(edge is None, "WATCH_HISTORY_UNEXPECTED_SEGMENT")
    elif value["event_index"] == 0:
        shape(edge, {"kind", "path", "sha256"})
        require(edge["kind"] == "same-commit" and isinstance(edge["path"], str)
                and ARTIFACT_PATH.fullmatch(edge["path"]) is not None
                and edge["path"].startswith("research/artifacts/" + SEGMENT_PREFIX)
                and isinstance(edge["sha256"], str) and DIGEST.fullmatch(edge["sha256"]),
                "WATCH_HISTORY_LOCAL_SEGMENT_INVALID")
    else:
        shape(edge, {"kind", "reference"})
        require(edge["kind"] == "exact", "WATCH_HISTORY_SEGMENT_REFERENCE_INVALID")
        exact(edge["reference"])
    return deepcopy(value)


def validate_segment(value):
    shape(value, {"schema", "history_version", "watch_key", "watch_ref", "segment_index",
                  "kind", "sealed_because", "previous_segment", "entry_event", "events"})
    require(value["schema"] == SEGMENT_SCHEMA and value["history_version"] == HISTORY_VERSION,
            "WATCH_HISTORY_SEGMENT_INVALID")
    require(isinstance(value["watch_key"], str) and DIGEST.fullmatch(value["watch_key"]),
            "WATCH_HISTORY_WATCH_KEY_INVALID")
    exact(value["watch_ref"])
    require(type(value["segment_index"]) is int and 0 <= value["segment_index"] < MAX_SEGMENTS,
            "WATCH_HISTORY_SEGMENT_LIMIT")
    refs = value["events"]
    require(isinstance(refs, list) and 1 <= len(refs) <= SEGMENT_EVENTS, "WATCH_HISTORY_EVENT_LIMIT")
    for ref in refs:
        exact(ref)
    if value["segment_index"] == 0:
        require(value["previous_segment"] is None and value["entry_event"] is None,
                "WATCH_HISTORY_FIRST_SEGMENT_INVALID")
    else:
        exact(value["previous_segment"]); exact(value["entry_event"])
    if value["kind"] == "legacy-v1":
        # v1's original last slot remains reserved for retirement. Adoption
        # cannot rescue a chain that already reached that execution boundary.
        require(value["segment_index"] == 0 and len(refs) < SEGMENT_EVENTS - 1
                and value["sealed_because"] == "legacy-adoption", "WATCH_HISTORY_LEGACY_LIMIT")
    else:
        require(value["kind"] == "native-v2" and len(refs) == SEGMENT_EVENTS
                and value["sealed_because"] == "capacity", "WATCH_HISTORY_SEGMENT_NOT_FULL")
    return deepcopy(value)


def position(segment_index, event_index, previous_segment=None):
    return validate_history({"version": HISTORY_VERSION, "segment_index": segment_index,
                             "event_index": event_index, "previous_segment": previous_segment})
