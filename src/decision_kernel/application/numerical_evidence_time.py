"""Finite time-use checks for numerical v2; no clocks, custody or source fetching.

Publication/acquisition are source-input declarations. The storage time is exact
retained manifest metadata, not an authenticated upstream or host clock. These
checks establish only the ordering of the supplied, retained instants.
"""
from __future__ import annotations

from datetime import date, datetime
import re

from decision_kernel.domain.records import RecordError, require

VERSION = "research-numbers-v2"
EVIDENCE_TIME_SCOPE = "retained-by-declared-cutoff"
_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_MINUTE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?\Z"
)
_NAIVE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\Z"
)
_CLOCKS = ("published_at", "acquired_at", "stored_at")
_PROVENANCE = {
    "published_at": "source-input-declaration",
    "acquired_at": "source-input-declaration",
    "stored_at": "retained-manifest-metadata",
}


def _precision(value) -> str:
    if value is None:
        return "missing"
    if not isinstance(value, str) or len(value) > 80:
        return "invalid"
    try:
        if _INSTANT.fullmatch(value):
            datetime.fromisoformat(value)
            return "instant"
        if _DATE.fullmatch(value):
            date.fromisoformat(value)
            return "date-only"
        if _MINUTE.fullmatch(value):
            datetime.fromisoformat(value)
            return "minute-only"
        if _NAIVE.fullmatch(value):
            datetime.fromisoformat(value)
            return "timezone-unavailable"
    except ValueError:
        pass
    return "invalid"


def _instant(value, field: str) -> datetime:
    precision = _precision(value)
    require(precision != "missing", "NUMERICAL_TIME_REQUIRED:" + field)
    require(precision not in ("date-only", "minute-only"),
            "NUMERICAL_TIME_PRECISION_REQUIRED:" + field)
    require(precision != "timezone-unavailable", "NUMERICAL_TIMEZONE_REQUIRED:" + field)
    require(precision == "instant", "NUMERICAL_TIME_INVALID:" + field)
    return datetime.fromisoformat(value)


def time_bounds(payload: dict) -> tuple[datetime, datetime]:
    """Validate fixed caller times. Recalculation never consults wall-clock time."""
    cutoff = _instant(payload["knowledge_cutoff"], "knowledge_cutoff")
    evaluated = _instant(payload["evaluated_at"], "evaluated_at")
    require(cutoff <= evaluated, "NUMERICAL_CUTOFF_AFTER_EVALUATION")
    return cutoff, evaluated


def assess_source_time(manifest: dict, cutoff: datetime, evaluated: datetime) -> dict:
    """Report unavailable or contradictory clocks without erasing other inputs."""
    metadata = manifest["input"]
    clocks = {"published_at": metadata.get("published_at"),
              "acquired_at": metadata.get("acquired_at"),
              "stored_at": manifest.get("stored_at")}
    # SourceInput bounds its declared clocks. Keep malformed legacy storage
    # metadata diagnosable without echoing an arbitrary object or unbounded text.
    report = {"status": "blocked",
              **{key: value if isinstance(value, str) and len(value) <= 80 else None
                 for key, value in clocks.items()},
              "precision": {key: _precision(value) for key, value in clocks.items()},
              "provenance": dict(_PROVENANCE)}
    try:
        published, acquired, stored = (_instant(clocks[key], key) for key in _CLOCKS)
        require(published <= acquired, "SOURCE_PUBLISHED_AFTER_ACQUISITION")
        require(acquired <= stored, "SOURCE_ACQUIRED_AFTER_STORAGE")
        require(stored <= evaluated, "SOURCE_STORED_AFTER_EVALUATION")
        require(published <= cutoff, "SOURCE_PUBLISHED_AFTER_CUTOFF")
        require(acquired <= cutoff, "SOURCE_ACQUIRED_AFTER_CUTOFF")
        require(stored <= cutoff, "SOURCE_STORED_AFTER_CUTOFF")
        report["status"] = "checked"
    except RecordError as error:
        report["error"] = str(error)
    return report
