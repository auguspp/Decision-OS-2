"""Finite AI decision preparation; these declarations never confirm a Decision."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from decision_kernel.domain.conditional_odds import security
from decision_kernel.domain.records import (
    BlobRef, RecordError, checked_text, encoded, identifier, require, strings,
)
from decision_kernel.domain.research_artifacts import artifact_reference

SCHEMA = "decision-preparation-v1"
MAX_INPUT_BYTES = 128 * 1024
MAX_CHAIN = 32
MAX_HORIZONS = 12
STANCES = frozenset({"researching", "awaiting-evidence", "candidate-for-human-review", "not-pursued"})


def shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), "DECISION_PREPARATION_SHAPE")


def moment(value):
    checked_text(value, limit=80)
    try:
        result = datetime.fromisoformat(value)
        require("T" in value and result.tzinfo is not None and result.utcoffset() is not None,
                "DECISION_PREPARATION_AWARE_TIME_REQUIRED")
        return result
    except ValueError:
        raise RecordError("DECISION_PREPARATION_AWARE_TIME_REQUIRED") from None


def _ids(values, limit=32):
    require(isinstance(values, list) and len(values) <= limit, "DECISION_PREPARATION_LIST_BOUND")
    for value in values:
        identifier(value)
    require(len(values) == len(set(values)), "DECISION_PREPARATION_DUPLICATE_ID")


def _indices(values):
    require(isinstance(values, list) and len(values) <= 64
            and all(type(i) is int and 0 <= i < 64 for i in values)
            and len(values) == len(set(values)), "DECISION_PREPARATION_SOURCE_INDICES")


def validate_preparation(value):
    shape(value, {"schema", "preparation_id", "research_ref", "prepared_at", "predecessor",
                  "question_ids", "stance", "rationale", "source_indices", "horizons",
                  "revision_note", "limitations", "target", "alternatives", "human_questions",
                  "data_gaps", "analysis_context", "major_assumptions", "strongest_counterarguments"})
    require(value["schema"] == SCHEMA, "DECISION_PREPARATION_SCHEMA")
    identifier(value["preparation_id"])
    try:
        BlobRef(**value["research_ref"])
    except (TypeError, KeyError):
        raise RecordError("DECISION_PREPARATION_RESEARCH_REF") from None
    moment(value["prepared_at"])
    if value["predecessor"] is not None:
        artifact_reference(value["predecessor"])
    _ids(value["question_ids"])
    require(isinstance(value["stance"], str) and value["stance"] in STANCES,
            "DECISION_PREPARATION_STANCE")
    checked_text(value["rationale"], limit=8192)
    checked_text(value["revision_note"], limit=8192)
    shape(value["target"], {"security", "expression"})
    if value["target"]["security"] is not None:
        security(value["target"]["security"])
    checked_text(value["target"]["expression"], limit=4096)
    alternatives = value["alternatives"]
    require(isinstance(alternatives, list) and 2 <= len(alternatives) <= 8,
            "DECISION_PREPARATION_ALTERNATIVES_REQUIRED")
    choice_ids, choices = [], set()
    for alternative in alternatives:
        shape(alternative, {"alternative_id", "choice", "description", "case_for", "case_against", "unknowns"})
        choice_ids.append(identifier(alternative["alternative_id"]))
        require(isinstance(alternative["choice"], str) and alternative["choice"] in
                ("participate", "wait", "decline", "continue-research"), "DECISION_PREPARATION_ALTERNATIVE_CHOICE")
        choices.add(alternative["choice"])
        for field in ("description", "case_for", "case_against"):
            checked_text(alternative[field], limit=4096)
        strings(alternative["unknowns"], limit=16)
    require(len(choice_ids) == len(set(choice_ids)) and len(choices) >= 2,
            "DECISION_PREPARATION_DISTINCT_ALTERNATIVES_REQUIRED")
    for field in ("human_questions", "major_assumptions", "strongest_counterarguments"):
        require(bool(strings(value[field], limit=16)), "DECISION_PREPARATION_" + field.upper() + "_REQUIRED")
    strings(value["data_gaps"], limit=32)
    context = value["analysis_context"]
    shape(context, {"valuation_ref", "odds"})
    if context["valuation_ref"] is not None:
        artifact_reference(context["valuation_ref"])
    if context["odds"] is not None:
        shape(context["odds"], {"kind", "reference"})
        require(context["odds"]["kind"] in ("conditional", "probability"), "DECISION_PREPARATION_ODDS_KIND")
        artifact_reference(context["odds"]["reference"])
    _indices(value["source_indices"])
    require(bool(strings(value["limitations"])), "DECISION_PREPARATION_LIMITATIONS_REQUIRED")
    horizons = value["horizons"]
    require(isinstance(horizons, list) and 1 <= len(horizons) <= MAX_HORIZONS,
            "DECISION_PREPARATION_HORIZON_BOUND")
    horizon_ids = []
    for horizon in horizons:
        shape(horizon, {"horizon_id", "label", "question_ids", "mechanism", "expected_change",
                        "source_indices", "window_start", "window_end", "review_at", "catalysts",
                        "invalidation", "limitations"})
        horizon_ids.append(identifier(horizon["horizon_id"]))
        checked_text(horizon["label"], limit=1024)
        for field in ("mechanism", "expected_change"):
            checked_text(horizon[field], limit=8192)
        _ids(horizon["question_ids"])
        require(set(horizon["question_ids"]) <= set(value["question_ids"]),
                "DECISION_PREPARATION_QUESTION_OUTSIDE_SCOPE")
        _indices(horizon["source_indices"])
        require(moment(horizon["window_start"]) <= moment(horizon["window_end"]),
                "DECISION_PREPARATION_WINDOW_ORDER")
        moment(horizon["review_at"])
        strings(horizon["limitations"])
        for collection, fields in (
            ("catalysts", {"event_id", "description", "expected_at", "source_indices"}),
            ("invalidation", {"condition_id", "description", "source_indices"}),
        ):
            rows = horizon[collection]
            require(isinstance(rows, list) and len(rows) <= 16, "DECISION_PREPARATION_EVENT_BOUND")
            ids = []
            for row in rows:
                shape(row, fields)
                ids.append(identifier(row["event_id" if collection == "catalysts" else "condition_id"]))
                checked_text(row["description"], limit=4096)
                _indices(row["source_indices"])
                if collection == "catalysts" and row["expected_at"] is not None:
                    moment(row["expected_at"])
            require(len(ids) == len(set(ids)), "DECISION_PREPARATION_DUPLICATE_ID")
        require(bool(horizon["invalidation"]), "DECISION_PREPARATION_INVALIDATION_REQUIRED")
    require(len(horizon_ids) == len(set(horizon_ids)), "DECISION_PREPARATION_DUPLICATE_ID")
    require(len(encoded(value)) <= MAX_INPUT_BYTES, "DECISION_PREPARATION_INPUT_BOUND")
    return deepcopy(value)


def cited_indices(value):
    indices = set(value["source_indices"])
    for horizon in value["horizons"]:
        indices.update(horizon["source_indices"])
        for row in horizon["catalysts"] + horizon["invalidation"]:
            indices.update(row["source_indices"])
    return indices


def project_horizons(value, at):
    """Dates make an item due for review; they do not establish economic outcomes."""
    instant = moment(at)
    require(instant >= moment(value["prepared_at"]), "DECISION_PREPARATION_NOT_YET_RECORDED")
    rows = []
    for horizon in value["horizons"]:
        start, end = moment(horizon["window_start"]), moment(horizon["window_end"])
        state = "not-started" if instant < start else "window-ended" if instant > end else "in-window"
        catalysts = []
        for catalyst in horizon["catalysts"]:
            expected = catalyst["expected_at"]
            due = "undated" if expected is None else (
                "date-reached-needs-evidence" if instant >= moment(expected) else "not-due")
            catalysts.append({**deepcopy(catalyst), "timing": due, "outcome": "not-evaluated"})
        rows.append({**deepcopy(horizon), "window_state": state,
                     "review_state": "review-due" if instant >= moment(horizon["review_at"]) else "not-due",
                     "catalysts": catalysts,
                     "invalidation": [{**deepcopy(item), "outcome": "not-evaluated"}
                                      for item in horizon["invalidation"]],
                     "mechanism_outcome": "not-evaluated"})
    return rows
