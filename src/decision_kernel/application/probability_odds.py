"""Frozen probability assessment -> qualified retained close -> exact Odds artifact.

Only the existing artifact/store/CAS owns persistence. A documented judgment is
not economic approval, a public-rights flag is not an authenticated human action.
"""
from dataclasses import asdict
import json
from datetime import datetime, timezone

from decision_kernel.application.conditional_odds import ConditionalOdds, publication
from decision_kernel.application.odds_market import qualify_close
from decision_kernel.domain.conditional_odds import shape, timestamp
from decision_kernel.domain.probability_odds import (
    ASSESSMENT_RECORD, RESULT_RECORD, calculate_probability, validate_assessment,
)
from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference


class ProbabilityOdds:
    def __init__(self, store, registry=None, *, clock=None):
        self.store, self.registry = store, registry
        self.conditional = ConditionalOdds(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _assessment(self, payload):
        request = validate_assessment(payload)
        frozen = self.conditional.read_spec(request["spec_ref"])
        request = validate_assessment(request, frozen["spec"])
        research = self.store.read_revision(BlobRef(**frozen["spec"]["research_ref"]))
        require(timestamp(research["record"]["stored_at"]) <= timestamp(request["as_of"]),
                "ODDS_ASSESSMENT_PRECEDES_RESEARCH_RECORD")
        require({r["item"] for r in request["unresolved_treatment"]} == set(frozen["research_unresolved"]),
                "ODDS_UNRESOLVED_NOT_DISPOSITIONED")
        declarations = request["probabilities"] + [request[k] for k in ("coverage", "model_risk", "uncertainty")]
        indices = sorted({i for row in declarations for i in row["source_indices"]})
        sources = frozen["research_sources"]
        evidence = []
        for i in indices:
            require(i < len(sources), "ODDS_PROBABILITY_SOURCE_OUTSIDE_RESEARCH")
            view = self.store.read_source(SourceRef.from_dict(sources[i]))
            require(view["availability"] == "stored-representation" and view["data"] is not None,
                    "ODDS_PROBABILITY_SOURCE_NOT_READABLE")
            require(timestamp(view["manifest"]["stored_at"]) <= timestamp(request["as_of"]),
                    "ODDS_ASSESSMENT_PRECEDES_SOURCE_RECORD")
            evidence.append({"index": i, "reference": sources[i], "availability": view["availability"],
                "limitations": view["limitations"], "meaning": "readable-basis-not-probability-truth-certification"})
        return {"schema": "frozen-odds-assessment-v1", "assessment": request,
            "assessment_sha256": digest(encoded(request)), "spec_sha256": frozen["spec_sha256"],
            "research_ref": frozen["spec"]["research_ref"], "research_body_sha256": frozen["research_body_sha256"],
            "research_unresolved": frozen["research_unresolved"], "evidence": evidence,
            "economic_assessment": "not-performed"}

    def freeze(self, payload, *, submission_id):
        request = publication(payload, "assessment")
        frozen = self._assessment(request)
        require(timestamp(request["as_of"]) <= self.clock(), "ODDS_ASSESSMENT_IN_FUTURE")
        ref = self.conditional._put(submission_id, ASSESSMENT_RECORD, frozen, payload["rights_basis"])
        return {"reference": asdict(ref), "assessment_sha256": frozen["assessment_sha256"],
                "spec_ref": frozen["assessment"]["spec_ref"], "research_ref": frozen["research_ref"]}

    def read_assessment(self, reference):
        frozen = self.conditional._open(reference, ASSESSMENT_RECORD)
        require(isinstance(frozen, dict) and "assessment" in frozen, "ODDS_ASSESSMENT_RECORD_INVALID")
        require(encoded(self._assessment(frozen["assessment"])) == encoded(frozen), "ODDS_ASSESSMENT_CHANGED")
        return frozen

    def compute(self, payload):
        shape(payload, {"assessment_ref", "market", "evaluated_at"})
        require(self.registry is not None, "ODDS_MATCHING_REGISTRY_REQUIRED")
        ref = artifact_reference(payload["assessment_ref"])
        frozen = self.read_assessment(ref)
        assessment = frozen["assessment"]
        evaluated = timestamp(payload["evaluated_at"])
        require(evaluated <= self.clock(), "ODDS_EVALUATION_IN_FUTURE")
        require(timestamp(assessment["as_of"]) <= evaluated, "ODDS_EVALUATION_PRECEDES_ASSESSMENT")
        spec = self.conditional.read_spec(assessment["spec_ref"])["spec"]
        market = qualify_close(payload["market"], spec, assessment["policy"], payload["evaluated_at"], self.registry, self.store)
        result = calculate_probability(spec, assessment, market["price"])
        result.update(assessment_ref=asdict(ref), assessment_sha256=frozen["assessment_sha256"],
            spec_ref=assessment["spec_ref"], spec_sha256=frozen["spec_sha256"],
            research_ref=frozen["research_ref"], research_body_sha256=frozen["research_body_sha256"],
            research_unresolved=frozen["research_unresolved"], market=market,
            market_qualification=market["status"], price=market["price"], calculation_input=payload,
            input_qualification="documented-assessment-and-bounded-retained-close-not-economic-approval")
        # Canonical JSON detaches caller input and normalizes inherited tuple references.
        return json.loads(encoded(result))

    def save(self, payload, *, submission_id):
        result = self.compute(publication(payload, "calculation"))
        ref = self.conditional._put(submission_id, RESULT_RECORD, result, payload["rights_basis"])
        return {"reference": asdict(ref), "calculation_sha256": digest(encoded(result)),
                "assessment_ref": result["assessment_ref"], "research_ref": result["research_ref"]}

    def read(self, reference):
        result = self.conditional._open(reference, RESULT_RECORD)
        require(isinstance(result, dict) and "calculation_input" in result, "ODDS_PROBABILITY_RESULT_INVALID")
        require(encoded(self.compute(result["calculation_input"])) == encoded(result), "ODDS_RECALCULATION_MISMATCH")
        hint = {"status": "unknown", "current_research": None,
                "meaning": "pointer-comparison-only-not-current-market-economic-validity-or-acceptance",
                "market_evaluation": "stored-as-of-not-refreshed-at-read-time"}
        try:
            case = self.store.read_revision(BlobRef(**result["research_ref"]))["record"]["input"]["case_id"]
            current = self.store.current(case)
            hint.update(status="same-revision" if current == BlobRef(**result["research_ref"])
                        else "historical-research-not-current",
                        current_research=asdict(current) if current else None)
        except RecordError:
            hint["error"] = "CURRENT_RESEARCH_NOT_AVAILABLE"
        return {"reference": asdict(artifact_reference(reference)), "result": result,
                "recalculation": "matches", "current_use": hint}


def verify_fresh(receipt, operation, reader_factory, registry=None):
    try:
        reader = ProbabilityOdds(reader_factory(), registry)
        if operation in ("freeze-odds-assessment", "reconcile-odds-assessment"):
            restored = reader.read_assessment(receipt["reference"])
            require(restored["assessment_sha256"] == receipt["assessment_sha256"], "ODDS_FRESH_READBACK_MISMATCH")
        else:
            restored = reader.read(receipt["reference"])
            require(digest(encoded(restored["result"])) == receipt["calculation_sha256"], "ODDS_FRESH_READBACK_MISMATCH")
        status = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    except RecordError:
        status = {"status": "failed", "new_reader": True, "error": "ODDS_FRESH_READBACK_FAILED"}
    return {**receipt, "readback": status}
