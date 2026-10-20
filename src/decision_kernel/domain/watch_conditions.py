"""Bounded, ordered Watch conditions; declarations never become investment authority.

Version two reuses the version-one clocks, custody and scalar projection while
keeping each condition's truth and unknown intervals distinct from the group.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext

from decision_kernel.domain.conditional_odds import (
    CONTEXT, decimal_text, number, shape, timestamp,
)
from decision_kernel.domain.records import (
    SourceRef, checked_text, encoded, identifier, require,
)

SCHEMA = "odds-watch-v2"
INPUT_SCHEMA = "odds-watch-input-v2"
CHECK_SCHEMA = "odds-watch-check-v2"
MAX_CONDITIONS = 16


def scalar_condition(condition):
    """A validation/qualification adapter only; never stored as a user threshold."""
    if condition["metric"] == "evidence":
        return {"metric": "price", "world_id": None, "comparison": "lte", "threshold": "1",
                "basis_note": condition["basis_note"]}
    result = {k: deepcopy(v) for k, v in condition.items() if k != "condition_id"}
    if result["comparison"] == "between":
        result = {"metric": result["metric"], "world_id": result["world_id"], "comparison": "lte",
                  "threshold": result["upper"], "basis_note": result["basis_note"]}
    return result


def scalar_watch(watch, condition=None):
    result = {k: deepcopy(v) for k, v in watch.items() if k not in ("conditions", "combination", "tracking")}
    result["schema"] = "odds-watch-v1"
    if watch["basis"]["kind"] == "research":
        result["condition"] = {"metric": "research_version", "world_id": None, "comparison": "changed",
                               "threshold": None, "basis_note": "Version comparison only"}
    else:
        candidate = condition or next((c for c in watch["conditions"] if c["metric"] != "evidence"), watch["conditions"][0])
        result["condition"] = scalar_condition(candidate)
    return result


def validate_watch(payload, validate_scalar):
    shape(payload, {"schema", "watch_id", "basis", "conditions", "combination", "activation", "window",
                    "data_policy", "effect", "limitations"})
    require(payload["schema"] == SCHEMA and len(encoded(payload)) <= 64 * 1024, "WATCH_SIZE_LIMIT")
    require(payload["combination"] in ("any", "all"), "WATCH_COMBINATION_REQUIRED")
    shape(payload["basis"], {"kind", "reference"})
    conditions = payload["conditions"]
    require(isinstance(conditions, list) and 1 <= len(conditions) <= MAX_CONDITIONS, "WATCH_CONDITION_LIMIT")
    ids = set()
    for condition in conditions:
        require(isinstance(condition, dict) and "condition_id" in condition and "metric" in condition,
                "WATCH_CONDITION_SHAPE")
        identifier(condition["condition_id"])
        checked_text(condition["metric"], limit=64)
        require(condition["condition_id"] not in ids, "WATCH_DUPLICATE_CONDITION")
        ids.add(condition["condition_id"])
        if condition["metric"] == "evidence":
            shape(condition, {"condition_id", "metric", "condition_text", "required_evidence", "basis_note"})
            for key in ("condition_text", "required_evidence", "basis_note"):
                checked_text(condition[key], limit=4096)
        else:
            common = {"condition_id", "metric", "world_id", "comparison", "basis_note"}
            if condition.get("comparison") == "between":
                shape(condition, common | {"lower", "upper", "lower_inclusive", "upper_inclusive"})
                low, high = number(condition["lower"]), number(condition["upper"])
                require(type(condition["lower_inclusive"]) is bool and type(condition["upper_inclusive"]) is bool
                        and low <= high and (low < high or condition["lower_inclusive"] and condition["upper_inclusive"]),
                        "WATCH_INTERVAL_INVALID")
                if condition["metric"] == "price": require(low > 0, "WATCH_POSITIVE_PRICE_THRESHOLD")
                if condition["metric"] == "positive_return_scenario_probability":
                    require(0 <= low <= high <= 1, "WATCH_PROBABILITY_THRESHOLD_RANGE")
            else:
                shape(condition, common | {"threshold"})
            # Run the existing metric/world, scalar bound and common Watch contract.
            if payload["basis"].get("kind") == "research":
                require(condition["metric"] == "research_version", "WATCH_RESEARCH_CONDITION_ONLY")
                validate_scalar({**scalar_watch(payload), "condition": scalar_condition(condition)})
            else:
                validate_scalar(scalar_watch(payload, condition))
    validate_scalar(scalar_watch(payload))
    return deepcopy(payload)


def validate_input(value, watch, checked_at):
    if value is None:
        value = {"schema": INPUT_SCHEMA, "price": None, "evidence": []}
    shape(value, {"schema", "price", "evidence"})
    require(value["schema"] == INPUT_SCHEMA and len(encoded(value)) <= 64 * 1024, "WATCH_V2_INPUT_INVALID")
    require(value["price"] is None or isinstance(value["price"], dict), "WATCH_INPUT_INVALID")
    numeric = [c for c in watch["conditions"] if c["metric"] not in ("evidence", "research_version")]
    require(numeric or value["price"] is None, "WATCH_V2_UNEXPECTED_PRICE")
    entries = value["evidence"]
    require(isinstance(entries, list) and len(entries) <= MAX_CONDITIONS, "WATCH_EVIDENCE_LIMIT")
    evidence_ids = {c["condition_id"] for c in watch["conditions"] if c["metric"] == "evidence"}
    seen, sources = set(), 0
    for entry in entries:
        shape(entry, {"condition_id", "observed_at", "assessed_at", "conclusion", "reason", "sources"})
        identifier(entry["condition_id"])
        require(entry["condition_id"] in evidence_ids and entry["condition_id"] not in seen,
                "WATCH_EVIDENCE_CONDITION_MISMATCH")
        seen.add(entry["condition_id"])
        require(entry["conclusion"] in ("met", "not-met", "cannot-determine", "review-required"), "WATCH_EVIDENCE_CONCLUSION_INVALID")
        checked_text(entry["reason"], limit=4096)
        require(timestamp(entry["observed_at"]) <= timestamp(entry["assessed_at"]) <= timestamp(checked_at)
                and timestamp(watch["activation"]["as_of"]) <= timestamp(entry["assessed_at"]),
                "WATCH_EVIDENCE_TIME_INVALID")
        refs = entry["sources"]
        require(isinstance(refs, list) and len(refs) <= 8, "WATCH_EVIDENCE_SOURCE_LIMIT")
        # Validate exact stored SourceRefs, not URLs or caller-signed availability.
        normalized = [encoded(asdict(SourceRef.from_dict(ref))) for ref in refs]
        require(len(set(normalized)) == len(refs), "WATCH_EVIDENCE_DUPLICATE_SOURCE")
        sources += len(refs)
    require(sources <= 32, "WATCH_EVIDENCE_SOURCE_LIMIT")
    return deepcopy(value)


def compare(condition, value):
    number_value = Decimal(value)  # Calculated values may use the full 80-digit context.
    mode = condition["comparison"]
    if mode == "lte": return number_value <= number(condition["threshold"])
    if mode == "gte": return number_value >= number(condition["threshold"])
    lower, upper = number(condition["lower"]), number(condition["upper"])
    return ((number_value >= lower if condition["lower_inclusive"] else number_value > lower)
            and (number_value <= upper if condition["upper_inclusive"] else number_value < upper))


def combine(rows, mode):
    values = [row["condition_met"] for row in rows]
    if mode == "any":
        return True if True in values else None if None in values else False
    return False if False in values else None if None in values else True


def boundaries(conditions, rows):
    """Nearest declared numeric edges on each side; never an inferred direction."""
    groups = {}
    for condition, row in zip(conditions, rows):
        if condition["metric"] in ("evidence", "research_version"): continue
        key = (condition["metric"], condition["world_id"])
        group = groups.setdefault(key, {"metric": key[0], "world_id": key[1], "value": row["value"], "edges": {}})
        if condition["comparison"] == "between":
            edges = [(condition[side], side, condition[side + "_inclusive"]) for side in ("lower", "upper")]
        else:
            edges = [(condition["threshold"], condition["comparison"], True)]
        for value, role, inclusive in edges:
            edge = group["edges"].setdefault(Decimal(value), [])
            edge.append({"condition_id": condition["condition_id"], "role": role, "inclusive": inclusive})
    result = []
    for group in groups.values():
        edges, value = group.pop("edges"), group["value"]
        def edge_at(point):
            if point is None: return None
            with localcontext(CONTEXT):
                distance = abs(point - Decimal(value))
            return {"value": decimal_text(point), "distance": decimal_text(distance), "conditions": edges[point]}
        if value is None:
            group.update(below=None, at=None, above=None, status="cannot-determine")
        else:
            current = Decimal(value)
            group.update(below=edge_at(max((p for p in edges if p < current), default=None)),
                         at=edge_at(current) if current in edges else None,
                         above=edge_at(min((p for p in edges if p > current), default=None)), status="observed")
        result.append({**group, "meaning": "nearest-declared-edges-by-metric-not-order-or-investment-instruction"})
    return result


def project(watch, request, observation, previous, scalar_project):
    rows = []
    old_rows = {r["condition_id"]: r for r in previous["condition_results"]} if previous else {}
    control = ("status", "condition_met", "check_performed", "trigger_continuity", "last_decisive_match",
               "gap_since_decisive", "ever_triggered", "last_attempt_at")
    for condition, observed in zip(watch["conditions"], observation["conditions"]):
        state = scalar_project(watch, request, observed, old_rows.get(condition["condition_id"]))
        rows.append({"condition_id": condition["condition_id"], "metric": condition["metric"],
                     "world_id": condition.get("world_id"), "condition": deepcopy(condition),
                     **{key: state[key] for key in control}, "value": observed["value"],
                     "reason": observed["reason"], "evidence": observed.get("evidence")})
    result = scalar_project(watch, request, observation, previous)
    schema = "odds-watch-check-v3" if watch["schema"] == "odds-watch-v3" else CHECK_SCHEMA
    return {**result, "schema": schema, "combination": watch["combination"],
            "condition_results": rows, "next_boundaries": boundaries(watch["conditions"], rows),
            "condition_order": "frozen-declaration-order-not-investment-priority",
            "limitations": result["limitations"] + [
                "GROUP_TRUTH_FOLLOWS_EXPLICIT_ANY_ALL_NOT_EACH_CONDITION_TRUTH",
                "NEXT_BOUNDARIES_ARE_DECLARED_METRIC_EDGES_NOT_BUY_SELL_INSTRUCTIONS",
                "NON_NUMERIC_CONCLUSIONS_ARE_SUBMITTED_RESEARCHER_ASSESSMENTS_NOT_AUTOMATED_ECONOMIC_VERIFICATION",
                "SOURCE_READBACK_VERIFIES_RETAINED_SELECTION_NOT_AUTHENTICITY_OR_EVIDENCE_SUFFICIENCY"]}
