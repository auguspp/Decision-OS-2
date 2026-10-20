"""Version-two Watch observations using the existing price and source owners."""
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone

from decision_kernel.application.versioned_store import _json
from decision_kernel.domain.conditional_odds import day, timestamp
from decision_kernel.domain.records import BlobRef, IntegrityError, MissingObject, SourceRef, digest, encoded, require
from decision_kernel.domain.watch_conditions import compare, combine, scalar_watch, validate_input


def _row(condition, status, *, met=None, value=None, reason=None, evidence=None):
    return {"condition_id": condition["condition_id"], "status": status, "condition_met": met,
            "value": value, "reason": reason, "evidence": evidence}


def _source_clock(value, assessed_at):
    if value is None: return "not-recorded; stored-time-only"
    at = timestamp(assessed_at)
    if "T" in value:
        return "not-after-assessment" if timestamp(value) <= at else "after-assessment"
    # A date has no source offset or intraday time. These are conservative
    # bounds over all datetime-supported offsets, not fabricated timestamps.
    start = datetime.combine(day(value), time.min, timezone.utc)
    # Compare distances so valid extreme calendar dates cannot overflow while
    # constructing the conservative interval outside datetime's year range.
    distance = at - start
    if distance >= timedelta(days=2): return "date-definitely-before-assessment"
    if distance <= -timedelta(days=1): return "date-definitely-after-assessment"
    return "date-order-uncertain"


def _evidence(condition, entry, reader, snapshot):
    if entry is None:
        return _row(condition, "cannot-determine", reason="WATCH_EVIDENCE_MISSING")
    report = {"observed_at": entry["observed_at"], "assessed_at": entry["assessed_at"],
              "conclusion": entry["conclusion"], "reason": entry["reason"], "source_reads": [],
              "assessment": "submitted-researcher-judgment-not-automated-economic-verification",
              "time_scope": "declared-observation-and-assessment; retained-source-storage-and-known-source-clocks-checked",
              "clock_limitations": ["OBSERVED_AND_ASSESSED_ARE_SUBMITTED_TIMES_NOT_SOURCE_CERTIFICATION",
                  "DATE_ONLY_SOURCE_TIMES_USE_CONSERVATIVE_INTERVALS_WITHOUT_ASSUMING_SOURCE_OFFSET_OR_MIDNIGHT",
                  "SNAPSHOT_MEMBERSHIP_AND_CLOCK_CHECKS_DO_NOT_ESTABLISH_HISTORICAL_PIT_OR_LATEST_INFORMATION"]}
    available, on_time, in_snapshot = bool(entry["sources"]), True, True
    uncertain = False
    for reference in entry["sources"]:
        source = SourceRef.from_dict(reference)
        manifest_raw = reader.backend.resolve(source.manifest)
        component = "manifest"
        try:
            snapshot_raw = reader.backend.read(snapshot, source.manifest.path)
            if snapshot_raw != manifest_raw:
                raise IntegrityError("WATCH_EVIDENCE_SNAPSHOT_MANIFEST_CHANGED")
            manifest = _json(manifest_raw)
            representation = manifest["representation"]
            if representation is not None:
                component = "representation"
                expected = source.manifest.path.rsplit("/", 1)[0] + (
                    "/data.csv" if manifest["input"]["kind"] == "dataset" else "/content.txt")
                require(representation["path"] == expected, "REPRESENTATION_PATH_MISMATCH")
                snapshot_ref = BlobRef(source.manifest.repository, snapshot, expected, representation["sha256"])
                raw = reader.backend.resolve(snapshot_ref)
                if len(raw) != representation["size"]:
                    raise IntegrityError("WATCH_EVIDENCE_SNAPSHOT_REPRESENTATION_SIZE_MISMATCH")
        except MissingObject:
            in_snapshot = False
            report["source_reads"].append({"reference": asdict(source), "availability": "not-at-records-snapshot",
                "records_snapshot": snapshot, "present_at_snapshot": False, "body_read": False,
                "missing_component": component})
            continue
        except (KeyError, TypeError) as exc:
            raise IntegrityError("SOURCE_MANIFEST_INVALID") from exc
        found = reader.read_source(source)
        manifest = found["manifest"]
        retained = found["availability"] == "stored-representation" and found["data"] is not None
        available = available and retained
        storage_on_time = timestamp(manifest["stored_at"]) <= timestamp(entry["assessed_at"])
        clocks = {key: _source_clock(manifest["input"][key], entry["assessed_at"]) for key in ("acquired_at", "published_at")}
        source_on_time = not any(state in ("after-assessment", "date-definitely-after-assessment") for state in clocks.values())
        uncertain = uncertain or "date-order-uncertain" in clocks.values()
        on_time = on_time and storage_on_time and source_on_time
        selected = encoded(found["data"]) if retained else None
        report["source_reads"].append({"reference": asdict(source), "availability": found["availability"],
            "records_snapshot": snapshot, "present_at_snapshot": True, "body_read": retained,
            "title": manifest["input"]["title"], "stored_at": manifest["stored_at"],
            "acquired_at": manifest["input"]["acquired_at"], "published_at": manifest["input"]["published_at"],
            "clock_qualification": clocks,
            "report_period": manifest["input"]["report_period"],
            "selection_sha256": digest(selected) if selected is not None else None,
            "selection_bytes": len(selected) if selected is not None else None,
            "limitations": found["limitations"], "original_verified": False})
    if not in_snapshot or not available or not on_time or uncertain or entry["conclusion"] == "cannot-determine":
        reason = ("WATCH_EVIDENCE_OUTSIDE_RECORDS_SNAPSHOT" if not in_snapshot else "WATCH_EVIDENCE_MISSING" if not available
                  else "WATCH_EVIDENCE_AFTER_ASSESSMENT" if not on_time else "WATCH_EVIDENCE_DATE_ORDER_UNKNOWN" if uncertain
                  else "WATCH_EVIDENCE_ASSESSMENT_INCONCLUSIVE")
        return _row(condition, "cannot-determine", reason=reason, evidence=report)
    if entry["conclusion"] == "review-required":
        return _row(condition, "review-required", reason="WATCH_EVIDENCE_CHALLENGES_FROZEN_PREMISE", evidence=report)
    return _row(condition, "evaluated", met=entry["conclusion"] == "met",
                reason="WATCH_EVIDENCE_SUBMITTED_ASSESSMENT", evidence=report)


def observe(owner, frozen, request, previous, reader):
    from decision_kernel.domain.odds_watch import AGGREGATE_METRICS, gate

    watch = frozen["watch"]
    inputs = validate_input(request["input"], watch, request["checked_at"])
    gated = gate(watch, request, previous)
    out = {"status": gated, "condition_met": None, "research_at_snapshot": None,
           "calculation": None, "value": None, "reason": None}
    if gated:
        return {**out, "conditions": [_row(c, gated) for c in watch["conditions"]]}

    current = reader.current(frozen["case_id"], snapshot=request["records_snapshot"])
    if current is None:
        return {**out, "status": "cannot-determine", "reason": "WATCH_RESEARCH_POINTER_MISSING",
                "conditions": [_row(c, "cannot-determine", reason="WATCH_RESEARCH_POINTER_MISSING") for c in watch["conditions"]]}
    reader.read_revision(current)
    out["research_at_snapshot"] = asdict(current)
    changed = current != BlobRef(**frozen["research_ref"])
    requires_review = changed or bool(request["review_reasons"])
    base = None
    if not requires_review and watch["basis"]["kind"] != "research":
        # The original qualification reads and calculates once. Its scalar
        # comparison is discarded; no synthetic threshold is stored or adopted.
        shadow = {**frozen, "watch": scalar_watch(watch)}
        query = {**request, "input": inputs["price"]}
        base = owner._observe(shadow, query, previous, reader, known_dates_first=True)
        out["calculation"] = base["calculation"]
    evidence = {entry["condition_id"]: entry for entry in inputs["evidence"]}
    rows = []
    for condition in watch["conditions"]:
        metric = condition["metric"]
        if metric == "research_version":
            rows.append(_row(condition, "evaluated", met=changed, reason="VERSION_COMPARISON_ONLY_NOT_ECONOMIC_REVIEW"))
        elif requires_review:
            rows.append(_row(condition, "review-required", reason="WATCH_RESEARCH_OR_ASSUMPTIONS_CHANGED"))
        elif base and base["status"] in ("ended", "review-required"):
            rows.append(_row(condition, base["status"], reason=base["reason"]))
        elif metric == "evidence":
            rows.append(_evidence(condition, evidence.get(condition["condition_id"]), reader, request["records_snapshot"]))
        elif base["status"] != "evaluated":
            rows.append(_row(condition, base["status"], reason=base["reason"]))
        else:
            calculation = base["calculation"]
            if metric == "price": value = calculation["price"]["price"]
            elif metric in AGGREGATE_METRICS: value = calculation["aggregate"][metric]
            else: value = next(world[metric] for world in calculation["world_results"] if world["world_id"] == condition["world_id"])
            rows.append(_row(condition, "evaluated", met=compare(condition, value), value=value))
    if any(row["status"] == "review-required" for row in rows):
        status, met = "review-required", None
    elif any(row["status"] == "ended" for row in rows):
        status, met = "ended", None
    else:
        met = combine(rows, watch["combination"])
        status = "cannot-determine" if met is None else "evaluated"
    return {**out, "status": status, "condition_met": met, "conditions": rows,
            "reason": "WATCH_EXPLICIT_CONDITION_COMBINATION", "unknown_condition_ids": [
                row["condition_id"] for row in rows if row["condition_met"] is None]}
