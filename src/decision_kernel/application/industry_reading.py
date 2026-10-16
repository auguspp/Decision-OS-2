"""Exact retained industry observations and curated issuer roles; no acquisition."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
import re

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MAX_BYTES, Entity, RecordError, Selection, SourceRef, checked_text, encoded,
    identifier, require,
)

SCHEMA = "industry-reading-v1"
BASIS = ("subject_id", "metric", "specification", "region", "currency", "unit",
         "price_kind", "market", "contract", "tax_basis", "frequency",
         "period_kind", "assessment")
COLUMNS = (*BASIS, "period_start", "period_end", "value")
CHANNELS = {"revenue", "cost", "profit", "cash_flow"}
ROLES = {"producer", "purchaser", "processor", "transporter", "substitute", "inventory"}


def _keys(value, fields):
    require(isinstance(value, dict) and value.keys() == set(fields), "INDUSTRY_INPUT_SHAPE")


def _items(value, maximum, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum, "INDUSTRY_INPUT_BOUND")
    return value


def _day(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
            "INDUSTRY_DATE")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RecordError("INDUSTRY_DATE") from None


def _clock(value):
    try:
        parsed = datetime.fromisoformat(value)
        require("T" in value and parsed.tzinfo is not None, "INDUSTRY_TIMEZONE")
        return parsed
    except (ValueError, TypeError):
        raise RecordError("INDUSTRY_TIMESTAMP") from None


def _decimal(value):
    require(isinstance(value, str) and len(value) <= 64
            and re.fullmatch(r"-?\d+(?:\.\d+)?", value), "INDUSTRY_DECIMAL")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise RecordError("INDUSTRY_DECIMAL") from None


def _number(value):
    return format(value, "f")


def _document(text):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "INDUSTRY_DUPLICATE_JSON_KEY")
            out[key] = value
        return out
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise RecordError("INDUSTRY_JSON") from None


def _available(meta, prepared):
    for field in ("acquired_at", "published_at"):
        value = meta[field]
        require(value is not None, "INDUSTRY_SOURCE_TIME_REQUIRED")
        # Date-only publication stays date-only; it cannot prove intraday availability.
        eligible = (_day(value) <= prepared.date() if len(value) == 10
                    else _clock(value) <= prepared)
        require(eligible, "INDUSTRY_SOURCE_AFTER_PREPARATION")


def _point(reference, subject, read, prepared, cutoff):
    ref = SourceRef.from_dict(reference)
    require(ref.selection.kind == "rows" and ref.selection.start == ref.selection.end
            and set(ref.selection.columns) == set(COLUMNS), "INDUSTRY_ONE_EXACT_ROW_REQUIRED")
    view = read(ref)
    meta = view["manifest"]["input"]
    require(meta["kind"] == "dataset" and isinstance(view["data"], list)
            and len(view["data"]) == 1, "INDUSTRY_DATASET_REQUIRED")
    _available(meta, prepared)
    require(any(e["entity_id"] == subject.entity_id and e["kind"] == subject.kind
                for e in meta["entities"]), "INDUSTRY_SOURCE_SUBJECT")
    row = view["data"][0]
    _keys(row, COLUMNS)
    for field in BASIS:
        checked_text(row[field], limit=256)
    require(row["subject_id"] == subject.entity_id, "INDUSTRY_ROW_SUBJECT")
    require(row["period_kind"] in ("price-level", "index-level", "stock", "period-flow", "ytd-flow"),
            "INDUSTRY_PERIOD_KIND")
    require(row["market"] in ("spot", "futures", "not-applicable")
            and row["price_kind"] in ("quote", "transaction", "survey", "not-applicable"),
            "INDUSTRY_PRICE_KIND")
    require((row["market"] == "futures") == (row["contract"] != "not-applicable"),
            "INDUSTRY_CONTRACT_REQUIRED")
    start, end = _day(row["period_start"]), _day(row["period_end"])
    require(start <= end <= cutoff.date(), "INDUSTRY_PERIOD_RANGE")
    require(end <= min(_day(meta[field][:10]) for field in ("published_at", "acquired_at")),
            "INDUSTRY_PERIOD_AFTER_SOURCE")
    if row["period_kind"] == "ytd-flow":
        require(start == date(end.year, 1, 1), "INDUSTRY_YTD_START")
    if row["value"] != "":
        _decimal(row["value"])
    return {"status": "read", "reference": asdict(ref), **row,
            "value": row["value"] or None, "source": meta,
            "source_limitations": view["limitations"], "original_verified": False}


def _change(previous, current):
    result = {"status": "unavailable", "delta": None, "percent_change": None}
    if previous["status"] != "read" or current["status"] != "read":
        return {**result, "reason_code": "INDUSTRY_POINT_UNAVAILABLE"}
    if previous["value"] is None or current["value"] is None:
        return {**result, "reason_code": "INDUSTRY_VALUE_MISSING"}
    changed = [field for field in BASIS if previous[field] != current[field]]
    changed += ["source." + field for field in ("source_id", "origin")
                if previous["source"][field] != current["source"][field]]
    if changed:
        return {**result, "reason_code": "INDUSTRY_BASIS_CHANGED", "changed_fields": changed}
    if any(previous[field].casefold() in ("unknown", "unspecified") for field in BASIS):
        return {**result, "reason_code": "INDUSTRY_BASIS_UNKNOWN"}
    old_period = (previous["period_start"], previous["period_end"])
    new_period = (current["period_start"], current["period_end"])
    same_period = old_period == new_period
    if not same_period:
        if new_period[1] < old_period[1]:
            return {**result, "reason_code": "INDUSTRY_PERIOD_REGRESSED"}
        if current["period_kind"] == "ytd-flow":
            return {**result, "reason_code": "INDUSTRY_CUMULATIVE_NOT_PERIOD_FLOW"}
        if new_period[0] <= old_period[1]:
            return {**result, "reason_code": "INDUSTRY_PERIODS_OVERLAP"}
        if current["period_kind"] == "period-flow" or current["assessment"] == "period-average":
            # Calendar months and official ten-day survey periods vary in length.
            a, b, c, d = map(_day, (*old_period, *new_period))
            monthly = current["frequency"] == "monthly" and all(
                x.day == 1 and y.day == monthrange(y.year, y.month)[1]
                and (x.year, x.month) == (y.year, y.month) for x, y in ((a, b), (c, d)))
            ten_day = current["frequency"] == "ten-day" and all(
                (x.year, x.month) == (y.year, y.month) and
                (x.day, y.day) in ((1, 10), (11, 20), (21, monthrange(y.year, y.month)[1]))
                for x, y in ((a, b), (c, d)))
            if b - a != d - c and not (monthly or ten_day):
                return {**result, "reason_code": "INDUSTRY_PERIOD_WINDOWS_DIFFER"}
    with localcontext() as context:
        context.prec = 128
        before, after = _decimal(previous["value"]), _decimal(current["value"])
        delta = after - before
        percent = delta / abs(before) * 100 if before != 0 else None
    return {"status": ("unchanged" if delta == 0 else "same-period-revision") if same_period else "changed",
            "reason_code": "INDUSTRY_ZERO_BASE" if before == 0 else None,
            "delta": _number(delta), "percent_change": _number(percent) if percent is not None else None,
            "interpretation": "observation-change-not-company-benefit"}


def _relation(item, subjects, read, prepared, cutoff):
    _keys(item, ("relation_id", "subject_id", "company", "role", "business_scope",
                 "report_period", "applicable_start", "applicable_end", "evidence",
                 "exposure_size_note", "transmission", "unknowns"))
    identifier(item["relation_id"])
    require(item["subject_id"] in subjects, "INDUSTRY_RELATION_SUBJECT")
    company = Entity(**item["company"])
    require(company.kind == "company" and item["role"] in ROLES, "INDUSTRY_COMPANY_ROLE")
    for field in ("business_scope", "report_period", "exposure_size_note"):
        checked_text(item[field], limit=4096)
    start, end = _day(item["applicable_start"]), _day(item["applicable_end"])
    require(start <= end and start <= cutoff.date(), "INDUSTRY_RELATION_PERIOD")
    evidence = item["evidence"]
    _keys(evidence, ("reference", "source_role", "quote", "issuer_binding_note"))
    require(evidence["source_role"] in ("issuer-filing", "issuer-ir"), "INDUSTRY_ISSUER_REQUIRED")
    checked_text(evidence["quote"], limit=16384)
    checked_text(evidence["issuer_binding_note"], limit=4096)
    ref = SourceRef.from_dict(evidence["reference"])
    require(ref.selection.kind == "lines", "INDUSTRY_ISSUER_LINES_REQUIRED")
    view = read(ref)
    meta = view["manifest"]["input"]
    require(meta["kind"] == "content" and isinstance(view["data"], str)
            and evidence["quote"] in view["data"], "INDUSTRY_ISSUER_QUOTE")
    require(any(e["entity_id"] == company.entity_id and e["kind"] == "company"
                for e in meta["entities"]), "INDUSTRY_ISSUER_IDENTITY")
    require(meta["report_period"] == item["report_period"], "INDUSTRY_ISSUER_REPORT_PERIOD")
    _available(meta, prepared)
    require(meta["origin"].startswith(("https://", "synthetic:")), "INDUSTRY_ISSUER_ORIGIN")
    _keys(item["transmission"], CHANNELS)
    for channel in item["transmission"].values():
        _keys(channel, ("hypothesis", "counterevidence"))
        checked_text(channel["hypothesis"], limit=4096)
        checked_text(channel["counterevidence"], limit=4096)
    for unknown in _items(item["unknowns"], 16, 1):
        checked_text(unknown, limit=4096)
    return {**item, "company": asdict(company), "status": "retained-issuer-inputs",
            "evidence_view": {"reference": asdict(ref), "text": view["data"], "source": meta,
                              "limitations": view["limitations"], "original_verified": False},
            "applicability_at_cutoff": "within-declared-period" if cutoff.date() <= end else "historical",
            "qualification": "curated-role-and-transmission-hypotheses-not-authenticated-or-causal",
            "current_business_freshness": "not-revalidated",
            "business_share": None, "elasticity": None, "pass_through": None,
            "lag": None, "net_benefit": None}


def _attempt(action):
    try:
        return action()
    except RecordError as exc:
        return {"status": "unavailable", "reason_code": str(exc)}
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        return {"status": "unavailable", "reason_code": "INDUSTRY_INPUT_INVALID"}


def read_industry(reference, store: VersionedStore, *, subject_id=None, company_id=None):
    """Read a pinned packet and its exact dependencies. Filters never infer links."""
    require(subject_id is None or company_id is None, "INDUSTRY_ONE_QUERY_DIRECTION")
    for value in (subject_id, company_id):
        if value is not None:
            identifier(value)
    ref = reference if isinstance(reference, SourceRef) else SourceRef.from_dict(reference)
    require(ref.selection == Selection(), "INDUSTRY_WHOLE_PACKET_REQUIRED")
    root = store.read_source(ref)
    require(root["manifest"]["input"]["kind"] == "content"
            and isinstance(root["data"], str), "INDUSTRY_PACKET_BODY_REQUIRED")
    payload = _document(root["data"])
    _keys(payload, ("schema", "prepared_at", "as_of", "coverage_note", "use", "series", "relations"))
    require(payload["schema"] == SCHEMA, "INDUSTRY_SCHEMA")
    prepared, cutoff = _clock(payload["prepared_at"]), _clock(payload["as_of"])
    require(prepared <= cutoff, "INDUSTRY_PREPARATION_AFTER_CUTOFF")
    for field in ("coverage_note", "use"):
        checked_text(payload[field], limit=4096)
    cache, total_bytes = {}, len(root["data"].encode())

    def read(source_ref):
        nonlocal total_bytes
        key = encoded(asdict(source_ref))
        if key not in cache:
            view = store.read_source(source_ref)
            total_bytes += len(encoded(view))
            require(total_bytes <= 8 * MAX_BYTES, "INDUSTRY_READING_BOUND")
            cache[key] = view
        return cache[key]

    series, subjects, seen = [], {}, set()
    for item in _items(payload["series"], 32, 1):
        _keys(item, ("series_id", "subject", "observations"))
        identifier(item["series_id"])
        require(item["series_id"] not in seen, "INDUSTRY_DUPLICATE_SERIES")
        seen.add(item["series_id"])
        try:
            subject = Entity(**item["subject"])
        except (KeyError, TypeError):
            raise RecordError("INDUSTRY_SUBJECT") from None
        require(subject.kind in ("commodity", "industry"), "INDUSTRY_SUBJECT")
        require(subject.entity_id not in subjects or subjects[subject.entity_id] == subject,
                "INDUSTRY_SUBJECT_IDENTITY_CONFLICT")
        subjects[subject.entity_id] = subject
        points = [_attempt(lambda r=r: _point(r, subject, read, prepared, cutoff))
                  for r in _items(item["observations"], 64, 2)]
        changes = [{"from_index": i, "to_index": i + 1, **_change(a, b)}
                   for i, (a, b) in enumerate(zip(points, points[1:]))]
        series.append({"series_id": item["series_id"], "subject": asdict(subject),
                       "observations": points, "changes": changes, "latest_change": changes[-1]})
    relations, outcomes, seen = [], [], set()
    for index, item in enumerate(_items(payload["relations"], 64)):
        result = _attempt(lambda: _relation(item, subjects, read, prepared, cutoff))
        if result["status"] != "unavailable":
            require(result["relation_id"] not in seen, "INDUSTRY_DUPLICATE_RELATION")
            seen.add(result["relation_id"])
            relations.append(result)
        outcomes.append({"index": index, "status": result["status"],
                         "reason_code": result.get("reason_code")})
    failures = sum(p["status"] == "unavailable" for s in series for p in s["observations"])
    failures += sum(o["status"] == "unavailable" for o in outcomes)
    all_series, all_relations = len(series), len(relations)
    if company_id is not None:
        relations = [r for r in relations if r["company"]["entity_id"] == company_id]
        selected = {r["subject_id"] for r in relations}
        series = [s for s in series if s["subject"]["entity_id"] in selected]
    elif subject_id is not None:
        series = [s for s in series if s["subject"]["entity_id"] == subject_id]
        relations = [r for r in relations if r["subject_id"] == subject_id]
    return {"schema": "industry-reading-result-v1", "status": "partial" if failures else "read",
            "reference": asdict(ref), "prepared_at": payload["prepared_at"], "as_of": payload["as_of"],
            "coverage_note": payload["coverage_note"], "use": payload["use"],
            "query": {"subject_id": subject_id, "company_id": company_id},
            "scope": {"series": all_series, "accepted_relations": all_relations,
                      "declared_relations": len(outcomes), "unavailable_inputs": failures},
            "series": series, "relations": relations, "relation_outcomes": outcomes,
            "qualification": "declared-retained-scope-not-live-monitoring-or-beneficiary-selection",
            "source_authenticity": "not-established-by-retention-or-role-label",
            "intraday_availability": "not-established-for-date-only-source-times",
            "source_requests": 0, "writes": 0}
