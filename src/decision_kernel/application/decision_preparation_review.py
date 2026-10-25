"""Bind one original preparation horizon to an exact adjacent research review.

The preparation owns its declared criteria and history; ResearchReview owns the
comparison. This read adds neither a new judgment record nor human acceptance.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.decision_preparation import DecisionPreparation, source_clock_check
from decision_kernel.application.market_reading_scope import operation_failure
from decision_kernel.application.research_review import open_review, review_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.decision_preparation import MAX_INPUT_BYTES, moment
from decision_kernel.domain.records import IntegrityError, MissingObject, RecordError, encoded, identifier, require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.infrastructure.github_reading import reading_scope

MAX_RESULT_BYTES = 768 * 1024


def preparation_review_plan(value):
    require(isinstance(value, dict) and set(value) == {"reference", "horizon_id", "at", "review"},
            "DECISION_PREPARATION_REVIEW_SHAPE")
    require(len(encoded(value)) <= MAX_INPUT_BYTES, "DECISION_PREPARATION_REVIEW_INPUT_BOUND")
    reference = asdict(artifact_reference(value["reference"]))
    identifier(value["horizon_id"])
    moment(value["at"])
    return {"reference": reference, "horizon_id": value["horizon_id"], "at": value["at"],
            "review": review_plan(value["review"])}


def _source_row(preparation, index, ref, at):
    """Keep ordinary local gaps, but never swallow operation or cutoff failures."""
    row = {"index": index, "reference": asdict(ref), "view": None}
    try:
        view = preparation._source(index, ref)
    except RecordError as error:
        if operation_failure(error) or str(error) in {"DECISION_PREPARATION_READ_BOUND", "PATH_OUTSIDE_STORE"}:
            raise
        if isinstance(error, MissingObject):
            return {**row, "status": "missing", "error": "SOURCE_OBJECT_MISSING"}
        if isinstance(error, IntegrityError):
            return {**row, "status": "invalid", "error": "SOURCE_INTEGRITY_FAILED"}
        return {**row, "status": "invalid", "error": "SOURCE_VALIDATION_FAILED"}
    try:
        source_clock_check(view, moment(at))
    except RecordError as error:
        codes = {
            "DECISION_PREPARATION_EVIDENCE_AFTER_PREPARATION": "DECISION_PREPARATION_REVIEW_EVIDENCE_AFTER_CUTOFF",
            "DECISION_PREPARATION_SOURCE_CLOCK_AFTER_PREPARATION": "DECISION_PREPARATION_REVIEW_SOURCE_CLOCK_AFTER_CUTOFF",
        }
        if str(error) in codes:
            raise RecordError(codes[str(error)]) from None
        raise
    manifest = view["manifest"]
    clocks = {"stored_at": manifest["stored_at"],
              **{name: manifest["input"][name] for name in ("acquired_at", "published_at")}}
    # Keep the clocks even when the original Review omits a large source view.
    return {**row, "status": view["availability"], "view": view, "error": None,
            "cutoff_check": {"at": at, **clocks,
                             "missing_clocks": [name for name, value in clocks.items() if value is None],
                             "meaning": "recorded-source-precision-not-intraday-availability-certification"}}


def _open(store, plan, registry, clock, registry_loader):
    preparation = DecisionPreparation(store, registry, clock=clock, registry_loader=registry_loader)
    original = preparation.read(plan["reference"], at=plan["at"])
    document = original["document"]
    horizon = next((row for row in original["projection"]["horizons"]
                    if row["horizon_id"] == plan["horizon_id"]), None)
    require(horizon is not None, "DECISION_PREPARATION_REVIEW_UNKNOWN_HORIZON")
    require(plan["review"]["question_id"] in horizon["question_ids"],
            "DECISION_PREPARATION_REVIEW_QUESTION_OUTSIDE_HORIZON")
    require(plan["review"]["before"]["reference"] == document["input"]["research_ref"],
            "DECISION_PREPARATION_REVIEW_RESEARCH_MISMATCH")

    def research_reader(ref):
        research = preparation._research(asdict(ref))
        saved = moment(research["record"]["stored_at"])
        require(saved <= moment(plan["at"]), "DECISION_PREPARATION_REVIEW_RESEARCH_AFTER_CUTOFF")
        require(saved >= moment(document["research"]["stored_at"]),
                "DECISION_PREPARATION_REVIEW_RESEARCH_TIME_ORDER")
        return research

    not_supplied = {"reference": None, "status": "not-supplied", "view": None}
    comparison = open_review(
        store, plan["review"], agenda=not_supplied, brief=not_supplied,
        research_reader=research_reader,
        source_reader=lambda index, ref: _source_row(preparation, index, ref, plan["at"]),
    )
    saved = moment(comparison["after"]["stored_at"])
    window_relation = ("before-window" if saved < moment(horizon["window_start"]) else
                       "after-window" if saved > moment(horizon["window_end"]) else "in-window")
    result = {"schema": "decision-preparation-review-v1", "reference": plan["reference"],
              "horizon_id": plan["horizon_id"], "at": plan["at"], "preparation": original,
              "horizon": deepcopy(horizon), "review": comparison,
              "timing": {"after_stored_at": comparison["after"]["stored_at"],
                         "relative_to_prepared_at": "after-preparation-declaration"
                         if saved > moment(document["input"]["prepared_at"]) else "no-later-than-preparation-declaration",
                         "relative_to_window": window_relation,
                         "meaning": "research-retention-time-not-economic-event-time-or-prospective-proof",
                         "evidence_cutoff_scope": "read-evidence-only-pending-references-not-time-qualified"},
              "persistence": "not-written", "execution": "not-started"}
    require(len(encoded(result)) <= MAX_RESULT_BYTES, "DECISION_PREPARATION_REVIEW_RESULT_BOUND")
    store.backend.head()
    return json.loads(encoded(result))


def open_preparation_review(store, value, registry=None, *, clock=None, registry_loader=None):
    plan = preparation_review_plan(value)
    # One original API and request budget; exit must succeed before releasing any
    # composition. The same preparation caches account every exact material once.
    with reading_scope(store.backend, fail_closed=True) as backend:
        reader = store if backend is store.backend else VersionedStore(backend)
        result = _open(reader, plan, registry, clock, registry_loader)
    return result
