"""Bounded source calendar facts; a due date never proves an event occurred."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from decision_kernel.domain.records import (
    RecordError, checked_text, digest, encoded, identifier, require, source_time,
)
from decision_kernel.observations.relay_disclosure_date import report_period, ticker

FACT_SCHEMA = "event-calendar-facts-v1"
MAX_EVENTS = 128
STATUSES = ("planned", "confirmed", "cancelled", "occurred", "unknown")


def zone(value):
    checked_text(value, limit=128)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise RecordError("CALENDAR_TIMEZONE_INVALID") from None


def time_window(value, timezone_name):
    """An exact instant or a local calendar day, never an invented midnight event."""
    source_time(value)
    require(value is not None, "CALENDAR_DATE_REQUIRED")
    tz = zone(timezone_name)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        try:
            lo = datetime.combine(day, time.min, tz)
            hi = datetime.combine(day + timedelta(days=1), time.min, tz)
        except (OverflowError, ValueError):
            raise RecordError("CALENDAR_DATE_INVALID") from None
        return lo.astimezone(timezone.utc), hi.astimezone(timezone.utc)
    point = datetime.fromisoformat(value).astimezone(timezone.utc)
    return point, point


def field(value, key):
    if key not in value:
        return {"presence": "missing", "value": None}
    if value[key] is None:
        return {"presence": "null", "value": None}
    source_time(value[key])
    return {"presence": "value", "value": value[key]}


def fact(value):
    require(isinstance(value, dict) and {
        "event_id", "category", "subject_id", "subject_name", "title", "period",
        "timezone", "status", "evidence"} <= set(value) <= {
        "event_id", "category", "subject_id", "subject_name", "title", "period",
        "timezone", "status", "evidence", "announced_at", "scheduled_for", "actual_at"},
        "CALENDAR_FACT_SHAPE")
    for name in ("event_id", "subject_id"):
        identifier(value[name])
    require(value["category"] in ("company", "macro"), "CALENDAR_CATEGORY_INVALID")
    require(value["status"] in STATUSES, "CALENDAR_STATUS_INVALID")
    for key in ("subject_name", "title"):
        checked_text(value[key], limit=2048)
    if value["period"] is not None:
        checked_text(value["period"], limit=128)
    zone(value["timezone"])
    for name in ("announced_at", "scheduled_for", "actual_at"):
        if name in value and value[name] is not None:
            time_window(value[name], value["timezone"])
    require(value["status"] != "occurred" or value.get("actual_at") is not None,
            "CALENDAR_OCCURRED_DATE_REQUIRED")
    require(value.get("actual_at") is None or value["status"] == "occurred",
            "CALENDAR_ACTUAL_STATUS_CONFLICT")
    require(value["status"] != "cancelled" or value.get("actual_at") is None,
            "CALENDAR_CANCELLED_WITH_ACTUAL")
    require(isinstance(value["evidence"], list) and 1 <= len(value["evidence"]) <= 4,
            "CALENDAR_EVIDENCE_BOUND")
    from decision_kernel.domain.records import SourceRef
    for item in value["evidence"]:
        require(isinstance(item, dict) and set(item) == {"reference", "quote"},
                "CALENDAR_EVIDENCE_SHAPE")
        SourceRef.from_dict(item["reference"])
        checked_text(item["quote"], limit=4096)
    # Calendar interpretation is explicitly supplied. Exact quotations are checked
    # by the reader; quotation presence does not certify the semantic extraction.
    return deepcopy(value)


def local_day(value, timezone_name):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return datetime.fromisoformat(value).astimezone(zone(timezone_name)).date().isoformat()


def project_fact(value):
    value = fact(value)
    return {**{k: value[k] for k in ("event_id", "category", "subject_id", "subject_name",
                                      "title", "period", "timezone", "status")},
            "announced_at": field(value, "announced_at"),
            "scheduled_for": field(value, "scheduled_for"), "actual_at": field(value, "actual_at"),
            "revision_record": {"presence": "missing", "value": None},
            "temporal_issues": [], "evidence": value["evidence"],
            "date_interpretation": "submitted-source-extraction-not-independent-date-validation"}


def acquisition_issues(row, acquired_at):
    """Qualify source claims against the acquired source, including overlapping days.

    A date-only claim spans its entire source-local day. It is established before
    acquisition only when that entire window precedes the acquisition window;
    later extraction/retention cannot make the original source know a future fact.
    """
    issues = []
    if acquired_at is None:
        if row["actual_at"]["presence"] == "value":
            issues.append("ACTUAL_EVENT_SOURCE_ACQUISITION_NOT_RETAINED")
        return issues
    acquired_lower, acquired_upper = time_window(acquired_at, row["timezone"])
    for key, prefix in (("announced_at", "ANNOUNCEMENT"), ("actual_at", "ACTUAL_EVENT")):
        if row[key]["presence"] != "value":
            continue
        lower, upper = time_window(row[key]["value"], row["timezone"])
        if lower > acquired_upper or (acquired_lower < acquired_upper and lower == acquired_upper):
            issues.append(prefix + "_AFTER_SOURCE_ACQUISITION")
        elif upper > acquired_lower:
            issues.append(prefix + "_TIME_RELATIVE_TO_SOURCE_ACQUISITION_UNKNOWN")
    return issues


def relay_rows(value):
    """Read the existing retained disclosure-date excerpt without relabeling it PDF."""
    require(isinstance(value, dict) and value.get("schema") == "relay-disclosure-date-v1"
            and set(value) == {"schema", "rows", "response_request_id", "parsed_response_sha256",
                               "response_transport", "scope"}, "CALENDAR_RELAY_SHAPE")
    scope = value["scope"]
    require(isinstance(scope, dict) and set(scope) == {"ts_code", "end_date", "limit"},
            "CALENDAR_RELAY_SCOPE")
    code, period = ticker(scope["ts_code"]), report_period(scope["end_date"])
    require(type(scope["limit"]) is int and 1 <= scope["limit"] <= 100,
            "CALENDAR_RELAY_LIMIT")
    require(isinstance(value["rows"], list) and len(value["rows"]) < scope["limit"],
            "CALENDAR_RELAY_ROWS_BOUND")
    out = []
    for index, row in enumerate(value["rows"]):
        require(isinstance(row, dict) and set(row) == {"ticker", "report_period", "source_ann_date",
                "original_fields", "pre_date", "actual_date", "modify_date", "source_row_index",
                "row_hash", "temporal_issues"}, "CALENDAR_RELAY_ROW_SHAPE")
        require(row["ticker"] == code and row["report_period"] == period
                and type(row["source_row_index"]) is int and row["source_row_index"] == index,
                "CALENDAR_RELAY_ROW_SCOPE")
        require(isinstance(row["row_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", row["row_hash"])
                and isinstance(row["original_fields"], list)
                and {"ts_code", "ann_date", "end_date", "pre_date", "actual_date"}
                    <= set(row["original_fields"]), "CALENDAR_RELAY_ROW_IDENTITY")
        time_window(row["source_ann_date"], "Asia/Shanghai")
        require(isinstance(row["temporal_issues"], list) and len(row["temporal_issues"]) <= 8
                and all(isinstance(x, str) for x in row["temporal_issues"]), "CALENDAR_RELAY_ISSUES")
        for name in ("pre_date", "actual_date", "modify_date"):
            v = row[name]
            require(isinstance(v, dict) and {"presence", "value"} <= set(v)
                    <= {"presence", "value", "interpretation"}
                    and v["presence"] in ("missing", "null", "value"), "CALENDAR_RELAY_DATE_FIELD")
            if v["presence"] == "value":
                if v.get("interpretation") == "unparsed-revision-record":
                    require(name == "modify_date", "CALENDAR_RELAY_REVISION_FIELD")
                    checked_text(v["value"], limit=1024)
                else:
                    time_window(v["value"], "Asia/Shanghai")
            else:
                require(v["value"] is None and "interpretation" not in v,
                        "CALENDAR_RELAY_DATE_FIELD")
        actual = row["actual_date"]["presence"] == "value"
        out.append({"event_id": "disclosure-" + code.lower().replace(".", "-") + "-" + scope["end_date"],
                    "category": "company", "subject_id": "security-" + code.lower().replace(".", "-"),
                    "subject_name": code, "title": "财报预约与披露", "period": period,
                    "timezone": "Asia/Shanghai", "status": "occurred" if actual else "planned",
                    "announced_at": {"presence": "value", "value": row["source_ann_date"]},
                    "scheduled_for": deepcopy(row["pre_date"]), "actual_at": deepcopy(row["actual_date"]),
                    "revision_record": deepcopy(row["modify_date"]),
                    "temporal_issues": deepcopy(row["temporal_issues"]), "evidence": [],
                    "source_row_index": index, "source_row_hash": row["row_hash"],
                    "date_interpretation": "third-party-relay-schedule-not-issuer-pdf-or-PIT"})
    return out


def event_state(row, as_of):
    """Report source dates and due windows separately from actual material/research."""
    now = datetime.fromisoformat(as_of).astimezone(timezone.utc)
    issues = list(row["temporal_issues"])
    ann = row["announced_at"]
    if ann["presence"] == "value":
        lo, hi = time_window(ann["value"], row["timezone"])
        if lo > now:
            issues.append("ANNOUNCEMENT_AFTER_REVIEW_CLOCK")
        elif hi > now:
            issues.append("ANNOUNCEMENT_INTRADAY_TIME_UNKNOWN")
    if row["actual_at"]["presence"] == "value":
        lo, hi = time_window(row["actual_at"]["value"], row["timezone"])
        if lo > now:
            issues.append("ACTUAL_EVENT_AFTER_REVIEW_CLOCK")
        elif hi > now:
            issues.append("ACTUAL_EVENT_INTRADAY_TIME_UNKNOWN")
    if issues:
        return {"state": "temporal-uncertainty", "issues": list(dict.fromkeys(issues)),
                "material_status": "not-confirmed-by-calendar"}
    if row["status"] == "cancelled":
        return {"state": "source-reported-cancelled", "issues": [], "material_status": "not-applicable"}
    if row["actual_at"]["presence"] == "value":
        return {"state": "source-reported-occurred", "issues": [],
                "material_status": "actual-material-not-read-by-calendar"}
    when = row["scheduled_for"]
    if when["presence"] != "value":
        return {"state": "date-unknown", "issues": [], "material_status": "not-confirmed-by-calendar"}
    lo, hi = time_window(when["value"], row["timezone"])
    state = "upcoming" if now < lo else "window-open" if now < hi else "due-no-actual-evidence"
    return {"state": state, "issues": [], "material_status": "not-confirmed-by-calendar"}


def timeline(rows, as_of):
    """Compare only source-ordered versions, keeping ties and missing dates visible."""
    require(bool(rows), "CALENDAR_EMPTY_EVENT")
    identities = {(r["category"], r["subject_id"], r["period"]) for r in rows}
    require(len(identities) == 1, "CALENDAR_EVENT_ID_COLLISION")
    for row in rows:
        row["observation_id"] = digest(encoded(row))
    windows = [time_window(r["announced_at"]["value"], r["timezone"])
               if r["announced_at"]["presence"] == "value" else None for r in rows]
    ordered = all(w is not None for w in windows)
    indices = sorted(range(len(rows)), key=lambda i: (windows[i][0], windows[i][1])) if ordered else list(range(len(rows)))
    transitions = []
    for a, b in zip(indices, indices[1:]):
        first, second = rows[a], rows[b]
        separated = (windows[a][1] <= windows[b][0] if windows[a][1] > windows[a][0]
                     else windows[a][1] < windows[b][0]) if ordered else False
        if not separated:
            ordered = False
            transitions.append({"before": first["observation_id"], "after": second["observation_id"],
                                "status": "ordering-unknown"})
            continue
        status = "unchanged"
        if first["scheduled_for"] != second["scheduled_for"]:
            status = "schedule-changed" if second["scheduled_for"]["presence"] == "value" else "date-became-unknown"
        if first["status"] != second["status"]:
            status = "source-status-changed"
        transitions.append({"before": first["observation_id"], "after": second["observation_id"],
                            "status": status, "previous_date": first["scheduled_for"],
                            "next_date": second["scheduled_for"]})
    selected = rows[indices[-1]] if ordered or len(rows) == 1 else None
    state = event_state(selected, as_of) if selected else {
        "state": "version-ordering-unknown", "issues": ["CALENDAR_VERSION_ORDER_AMBIGUOUS"],
        "material_status": "not-confirmed-by-calendar"}
    return {"event_id": rows[0]["event_id"], "category": rows[0]["category"],
            "subject_id": rows[0]["subject_id"], "subject_name": rows[0]["subject_name"],
            "title": rows[0]["title"], "period": rows[0]["period"],
            "versions": [deepcopy(rows[i]) for i in indices], "version_count": len(rows),
            "ordering": "source-announcement-order" if ordered else "not-established",
            "selected_observation": selected["observation_id"] if selected else None,
            "current": deepcopy(selected), "transitions": transitions, **state}
