"""Evidence-bound AI preparation and exact history on the existing artifact store."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone
import json

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.probability_odds import ProbabilityOdds
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_reader import REVISION_PATH
from decision_kernel.domain.decision_preparation import (
    MAX_CHAIN, cited_indices, moment, project_horizons, shape, validate_preparation,
)
from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, checked_text, digest, encoded, require
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference
from decision_kernel.domain.research_work import read_workpaper

RECORD_SCHEMA = "decision-preparation-record-v1"
DOCUMENT_SCHEMA = "decision-preparation-document-v1"
MAX_DOCUMENT_BYTES = 384 * 1024
MAX_RESULT_BYTES = 768 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024


def source_clock_check(view, cutoff):
    """Preserve the original stored/date/timestamp qualification for evidence."""
    manifest = view["manifest"]
    require(moment(manifest["stored_at"]) <= cutoff, "DECISION_PREPARATION_EVIDENCE_AFTER_PREPARATION")
    for field in ("acquired_at", "published_at"):
        source_clock = manifest["input"][field]
        if source_clock is not None:
            within = (moment(source_clock) <= cutoff if "T" in source_clock
                      else date.fromisoformat(source_clock) <= cutoff.date())
            require(within, "DECISION_PREPARATION_SOURCE_CLOCK_AFTER_PREPARATION")


def publication(value):
    shape(value, {"preparation", "rights_confirmed", "rights_basis"})
    require(value["rights_confirmed"] is True, "DECISION_PREPARATION_CUSTODY_RIGHTS_REQUIRED")
    checked_text(value["rights_basis"], limit=4096)
    validate_preparation(value["preparation"])
    return value["preparation"]


def preparation_artifact(document, rights_basis):
    raw = encoded({"schema": RECORD_SCHEMA, "document": document, "rights_basis": rights_basis})
    return ArtifactInput.from_bytes("decision-prep-" + digest(raw), "decision-preparation.json", "application/json", raw)


def _changes(before, after):
    if before is None:
        return {"relation": "initial-preparation", "research_changed": False,
                "judgment_changed": False, "preparation_context_changed": False,
                "added_horizons": [], "changed_horizons": [],
                "removed_horizons": [], "removed_question_ids": [],
                "removal_meaning": "removed-from-selected-draft-not-resolved-or-invalidated"}
    old, new = before["input"], after
    a = {h["horizon_id"]: h for h in old["horizons"]}
    b = {h["horizon_id"]: h for h in new["horizons"]}
    return {"relation": "exact-preparation-successor", "research_changed": old["research_ref"] != new["research_ref"],
            "judgment_changed": any(old[k] != new[k] for k in ("stance", "rationale", "source_indices")),
            "preparation_context_changed": any(old[k] != new[k] for k in (
                "target", "alternatives", "human_questions", "data_gaps", "analysis_context",
                "major_assumptions", "strongest_counterarguments")),
            "added_horizons": sorted(b.keys() - a.keys()),
            "changed_horizons": sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
            "removed_horizons": [deepcopy(a[k]) for k in sorted(a.keys() - b.keys())],
            "removed_question_ids": sorted(set(old["question_ids"]) - set(new["question_ids"])),
            "removal_meaning": "removed-from-selected-draft-not-resolved-or-invalidated"}


class DecisionPreparation:
    def __init__(self, store, registry=None, *, clock=None, registry_loader=None):
        self.store = store
        self.registry = registry
        self.registry_loader = registry_loader
        self.artifacts = ResearchArtifacts(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._reset()

    def _reset(self):
        self._research_cache, self._source_cache, self._analysis_cache = {}, {}, {}
        self._read_bytes = 0

    def _account(self, value):
        self._read_bytes += len(value) if isinstance(value, bytes) else len(encoded(value))
        require(self._read_bytes <= MAX_READ_BYTES, "DECISION_PREPARATION_READ_BOUND")

    def _research(self, value):
        ref = BlobRef(**value)
        require(REVISION_PATH.fullmatch(ref.path) is not None, "DECISION_PREPARATION_RESEARCH_REF")
        key = encoded(value)
        if key not in self._research_cache:
            research = self.store.read_revision(ref)
            self._account(research)
            self._research_cache[key] = research
        return self._research_cache[key]

    def _source(self, index, ref):
        """One exact source cache/account shared by preparation and its review."""
        key = encoded(asdict(ref))
        if key not in self._source_cache:
            # Do not convert native access/privacy/budget errors to item gaps.
            view = self.store.read_source(ref)
            row = {"index": index, "reference": asdict(ref), "view": view,
                   "status": view["availability"], "error": None}
            self._account(row)
            self._source_cache[key] = view
        return self._source_cache[key]

    def _evidence(self, sources, indices, prepared_at):
        evidence = []
        for index in sorted(indices):
            require(index < len(sources), "DECISION_PREPARATION_SOURCE_OUTSIDE_RESEARCH")
            ref = SourceRef.from_dict(sources[index])
            view = self._source(index, ref)
            manifest = view["manifest"]
            source_clock_check(view, prepared_at)
            meta = manifest["input"]
            evidence.append({"index": index, "reference": deepcopy(sources[index]),
                             "availability": view["availability"], "original_verified": False,
                             "selected_content_sha256": digest(encoded(view["data"])),
                             "title": meta["title"], "origin": meta["origin"],
                             "stored_at": manifest["stored_at"], "acquired_at": meta["acquired_at"],
                             "published_at": meta["published_at"], "report_period": meta["report_period"],
                             "time_precision": "recorded-source-precision-not-intraday-availability-certification",
                             "limitations": deepcopy(view["limitations"]),
                             "open_request": {"operation": "read-source", "payload": deepcopy(sources[index])}})
        return evidence

    def _research_lineage(self, newer, older, case_id):
        """Require retained ancestry, not labels, same ticker or the current pointer."""
        cursor, seen, lineage = newer, set(), []
        for _ in range(MAX_CHAIN):
            key = encoded(cursor)
            require(key not in seen, "DECISION_PREPARATION_RESEARCH_CYCLE")
            seen.add(key)
            research = self._research(cursor)
            payload = research["record"]["input"]
            require(payload["case_id"] == case_id, "DECISION_PREPARATION_CASE_MISMATCH")
            lineage.append(deepcopy(cursor))
            if cursor == older:
                return lineage
            cursor = payload["predecessor"]
            require(cursor is not None, "DECISION_PREPARATION_RESEARCH_ANCESTRY_REQUIRED")
        raise RecordError("DECISION_PREPARATION_RESEARCH_HISTORY_BOUND")

    def _analysis(self, value):
        """Read existing valuation/Odds; never invent a quote, probability or model."""
        selection = value["analysis_context"]
        key = encoded(selection)
        if key not in self._analysis_cache:
            odds = selection["odds"]
            # Every retained history version is rebuilt here. A later null
            # context cannot remove an earlier Odds reader's trust requirement.
            if odds is not None and self.registry is None and self.registry_loader is not None:
                self.registry = self.registry_loader()
            conditional = ConditionalOdds(self.store, self.registry, clock=self.clock)
            frozen, result, assessment, spec_ref = None, None, None, selection["valuation_ref"]
            if odds is not None:
                reader = conditional if odds["kind"] == "conditional" else ProbabilityOdds(
                    self.store, self.registry, clock=self.clock)
                restored = reader.read(odds["reference"])
                require(restored["current_use"].get("error") is None,
                        "DECISION_PREPARATION_ANALYSIS_ACCESS_FAILED")
                result = restored["result"]
                if odds["kind"] == "probability":
                    assessment = reader.read_assessment(result["assessment_ref"])
                if spec_ref is not None:
                    require(spec_ref == result["spec_ref"], "DECISION_PREPARATION_ANALYSIS_SPEC_MISMATCH")
                spec_ref = result["spec_ref"]
            if spec_ref is not None:
                frozen = conditional.read_spec(spec_ref)
            retained = {"valuation": None if frozen is None else {"reference": deepcopy(spec_ref), "view": frozen},
                        "odds": None if result is None else {"reference": deepcopy(odds["reference"]),
                                                            "kind": odds["kind"], "result": result},
                        "probability_assessment": assessment}
            self._account(retained)
            self._analysis_cache[key] = json.loads(encoded(retained))
        retained = deepcopy(self._analysis_cache[key])
        prepared = moment(value["prepared_at"])
        indices = set()
        if retained["valuation"] is not None:
            spec = retained["valuation"]["view"]["spec"]
            require(spec["research_ref"] == value["research_ref"], "DECISION_PREPARATION_ANALYSIS_RESEARCH_MISMATCH")
            require(value["target"]["security"] == spec["security"], "DECISION_PREPARATION_ANALYSIS_TARGET_MISMATCH")
            require(date.fromisoformat(spec["valuation_date"]) <= prepared.date(),
                    "DECISION_PREPARATION_ANALYSIS_AFTER_PREPARATION")
            for world in spec["worlds"]:
                indices.update(world["source_indices"])
        if retained["probability_assessment"] is not None:
            assessment = retained["probability_assessment"]["assessment"]
            require(moment(assessment["as_of"]) <= prepared, "DECISION_PREPARATION_ANALYSIS_AFTER_PREPARATION")
            for item in assessment["probabilities"] + [assessment[k] for k in ("coverage", "model_risk", "uncertainty")]:
                indices.update(item["source_indices"])
        price = None
        if retained["odds"] is not None:
            result = retained["odds"]["result"]
            require(result["research_ref"] == value["research_ref"], "DECISION_PREPARATION_ANALYSIS_RESEARCH_MISMATCH")
            require(moment(result["price"]["as_of"]) <= prepared, "DECISION_PREPARATION_ANALYSIS_AFTER_PREPARATION")
            evaluated_at = result.get("calculation_input", {}).get("evaluated_at")
            if evaluated_at is not None:
                require(moment(evaluated_at) <= prepared, "DECISION_PREPARATION_ANALYSIS_AFTER_PREPARATION")
            price = {"price": deepcopy(result["price"]),
                     "market_qualification": result["market_qualification"],
                     "probability_status": result["probability_status"],
                     "meaning": "original-dated-context-not-refreshed-or-executable-at-preparation-time"}
        missing = []
        if retained["valuation"] is None:
            missing.append("valuation-not-established")
        if retained["odds"] is None:
            missing.append("odds-not-established")
        if price is None:
            missing.append("price-context-not-established")
        elif price["market_qualification"] == "not-established":
            missing.append("qualified-market-not-established")
        return {**retained, "price_context": price, "missing": missing,
                "status": "not-established" if retained["valuation"] is None else "restored-exact-research-context",
                "target_binding": "author-selected" if retained["valuation"] is None else "matches-frozen-valuation-security",
                "quotation_freshness_at_preparation": "not-requalified", "source_requests": 0}, indices

    def _document(self, value, previous):
        value = validate_preparation(value)
        prepared_at = moment(value["prepared_at"])
        require(prepared_at <= self.clock(), "DECISION_PREPARATION_FUTURE_TIMESTAMP")
        research = self._research(value["research_ref"])
        record, payload = research["record"], research["record"]["input"]
        require(moment(record["stored_at"]) <= prepared_at, "DECISION_PREPARATION_RESEARCH_AFTER_PREPARATION")
        work = read_workpaper(research)
        questions, findings, challenges = [], [], []
        unselected, unselected_challenges = [], 0
        indices = cited_indices(value)
        if work is None:
            require(not value["question_ids"], "DECISION_PREPARATION_RECORDED_QUESTIONS_REQUIRED")
        else:
            request, progress = work
            selected = set(value["question_ids"])
            require(selected <= {q.question_id for q in request.questions}, "DECISION_PREPARATION_UNKNOWN_QUESTION")
            questions = [asdict(q) for q in request.questions if q.question_id in selected]
            findings = [asdict(f) for f in progress.findings if f.question_id in selected]
            challenges = [asdict(c) for c in progress.challenges if c.question_id in selected]
            unselected = [q.question_id for q in request.questions if q.question_id not in selected]
            unselected_challenges = sum(c.disposition == "unresolved" and c.question_id not in selected
                                        for c in progress.challenges)
            for row in findings + challenges:
                indices.update(row["evidence"])
        lineage = [deepcopy(value["research_ref"])]
        if previous is not None:
            old = previous["input"]
            require(old["preparation_id"] == value["preparation_id"], "DECISION_PREPARATION_ID_MISMATCH")
            require(previous["research"]["case_id"] == payload["case_id"], "DECISION_PREPARATION_CASE_MISMATCH")
            require(moment(old["prepared_at"]) < prepared_at, "DECISION_PREPARATION_TIME_ORDER")
            lineage = self._research_lineage(value["research_ref"], old["research_ref"], payload["case_id"])
        analysis, analysis_indices = self._analysis(value)
        indices.update(analysis_indices)
        evidence = self._evidence(payload["sources"], indices, prepared_at)
        context = {"reference": deepcopy(value["research_ref"]), "case_id": payload["case_id"],
                   "revision_id": payload["revision_id"], "kind": payload["kind"], "mandate": payload["mandate"],
                   "method": payload["method"], "body_sha256": record["body"]["sha256"],
                   "stored_at": record["stored_at"], "recorded_unresolved": deepcopy(payload["unresolved"]),
                   "questions_status": "selected-recorded-questions" if work else "not-recorded",
                   "questions": questions, "findings": findings, "challenges": challenges,
                   "unselected_question_ids": unselected,
                   "unselected_unresolved_challenges": unselected_challenges,
                   "scope_meaning": "selected-questions-do-not-clear-whole-research-limitations",
                   "open_request": {"operation": "prepare", "payload": {"reference": deepcopy(value["research_ref"])}}}
        data_gaps = list(value["data_gaps"]) + analysis["missing"]
        if value["target"]["security"] is None:
            data_gaps.append("target-security-not-established")
        partial = (bool(data_gaps) or not evidence or any(row["availability"] != "stored-representation" for row in evidence)
                   or bool(payload["unresolved"]) or any(row["disposition"] == "unresolved" for row in challenges)
                   or work is None or not questions or bool(unselected_challenges))
        document = {"schema": DOCUMENT_SCHEMA, "input": value, "research": context,
                    "analysis_context": analysis, "data_gaps": data_gaps,
                    "research_lineage_to_previous_preparation": lineage,
                    "evidence": {"coverage": "explicit-preparation-and-selected-workpaper-evidence",
                                 "total_research_sources": len(payload["sources"]), "cited": len(evidence),
                                 "uncited": len(payload["sources"]) - len(evidence), "items": evidence},
                    "changes": _changes(previous, value), "status": "partial" if partial else "readable",
                    "authorship": "AI-author-declarations", "economic_assessment": "not-performed",
                    "preparation_time_basis": "author-declared-not-proof-draft-existed-at-that-time",
                    "history_evidence": "exact-retained-artifact-versions-and-linked-research-not-prospective-certification",
                    "point_in_time_claim": "source-retention-clock-checked-not-market-PIT-certification",
                    "formal_decision": "not-created", "human_confirmation": "not-established",
                    "investment_authority": "none", "execution": "not-started"}
        require(len(encoded(document)) <= MAX_DOCUMENT_BYTES, "DECISION_PREPARATION_DOCUMENT_BOUND")
        return json.loads(encoded(document))

    def _open(self, reference):
        ref = artifact_reference(reference)
        artifact = self.artifacts.read(ref)
        raw = base64.b64decode(artifact["data_base64"], validate=True)
        self._account(raw)
        try:
            record = json.loads(raw)
            shape(record, {"schema", "document", "rights_basis"})
            require(record["schema"] == RECORD_SCHEMA and encoded(record) == raw,
                    "DECISION_PREPARATION_RECORD_MISMATCH")
            checked_text(record["rights_basis"], limit=4096)
            expected = preparation_artifact(record["document"], record["rights_basis"])
            require(expected.metadata() == artifact["metadata"] and expected.path() == ref.path,
                    "DECISION_PREPARATION_RECORD_MISMATCH")
            require(isinstance(record["document"], dict) and "input" in record["document"],
                    "DECISION_PREPARATION_RECORD_MISMATCH")
            validate_preparation(record["document"]["input"])
            return record["document"]
        except (TypeError, KeyError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError("DECISION_PREPARATION_RECORD_MISMATCH") from None

    def _history(self, reference):
        history, seen, cursor = [], set(), reference
        while cursor is not None:
            require(len(history) < MAX_CHAIN, "DECISION_PREPARATION_HISTORY_BOUND")
            ref = artifact_reference(cursor)
            key = encoded(asdict(ref))
            require(key not in seen, "DECISION_PREPARATION_HISTORY_CYCLE")
            seen.add(key)
            document = self._open(ref)
            history.append({"reference": asdict(ref), "document": document})
            cursor = document["input"]["predecessor"]
        history.reverse()
        previous = None
        for row in history:
            rebuilt = self._document(row["document"]["input"], previous)
            require(encoded(rebuilt) == encoded(row["document"]), "DECISION_PREPARATION_REPLAY_MISMATCH")
            previous = rebuilt
        return history

    def prepare(self, value):
        self._reset()
        value = validate_preparation(value)
        history = self._history(value["predecessor"])
        require(len(history) < MAX_CHAIN, "DECISION_PREPARATION_HISTORY_BOUND")
        document = self._document(value, history[-1]["document"] if history else None)
        result = {"document": document, "history": history,
                  "history_scope": "exact-selected-predecessor-chain-not-global-latest",
                  "projection": {"at": value["prepared_at"], "horizons": project_horizons(value, value["prepared_at"])},
                  "persistence": "not-written"}
        require(len(encoded(result)) <= MAX_RESULT_BYTES, "DECISION_PREPARATION_RESULT_BOUND")
        self.store.backend.head()
        return result

    def save(self, payload, *, submission_id):
        value = publication(payload)
        document = self.prepare(value)["document"]
        ref = self.artifacts.put(submission_id, preparation_artifact(document, payload["rights_basis"]))
        return {"reference": asdict(ref), "preparation_sha256": digest(encoded(document)),
                "preparation_id": value["preparation_id"], "prepared_at": value["prepared_at"],
                "research_ref": deepcopy(value["research_ref"]), "predecessor": deepcopy(value["predecessor"]),
                "formal_decision": "not-created", "investment_authority": "none"}

    def read(self, reference, *, at):
        self._reset()
        reference = asdict(artifact_reference(reference))
        require(moment(at) <= self.clock(), "DECISION_PREPARATION_FUTURE_TIMESTAMP")
        history = self._history(reference)
        document = history[-1]["document"]
        projection = project_horizons(document["input"], at)
        current = self.store.current(document["research"]["case_id"])
        result = {"reference": asdict(artifact_reference(reference)), "document": document,
                  "replay": "matches", "history": history,
                  "history_scope": "exact-selected-predecessor-chain-not-global-latest",
                  "projection": {"at": at, "horizons": projection,
                                 "meaning": "dates-require-review-not-economic-outcome-or-human-acceptance"},
                  "current_use": {"current_research": asdict(current) if current else None,
                                  "status": "same-revision" if current == BlobRef(**document["input"]["research_ref"])
                                            else "historical-research-not-current",
                                  "meaning": "pointer-comparison-not-economic-validity"},
                  "persistence": "not-written", "execution": "not-started"}
        require(len(encoded(result)) <= MAX_RESULT_BYTES, "DECISION_PREPARATION_RESULT_BOUND")
        self.store.backend.head()
        return result


def verify_fresh(receipt, reader_factory, registry=None):
    try:
        fresh = DecisionPreparation(reader_factory(), registry).read(receipt["reference"], at=receipt["prepared_at"])
        require(digest(encoded(fresh["document"])) == receipt["preparation_sha256"],
                "DECISION_PREPARATION_READBACK_MISMATCH")
        readback = {"status": "verified", "new_reader": True, "human_confirmation": "not-established"}
    except RecordError:
        readback = {"status": "failed", "new_reader": True, "error": "DECISION_PREPARATION_READBACK_FAILED"}
    return {**receipt, "readback": readback}
