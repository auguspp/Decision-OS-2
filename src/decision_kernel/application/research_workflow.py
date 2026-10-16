"""Compose research guidance, existing exact reads, and existing revision submission."""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.application.numerical_audit import START as NUMBERS_START, NumericalAudit
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.research_evidence import ResearchEvidence, guidance as evidence_guidance
from decision_kernel.application.research_sources import source_use_guidance
from decision_kernel.application.research_source_use import execution_contract, acceptance_pending
from decision_kernel.application.research_continuation import continuation_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceRef, encoded, identifier, require
from decision_kernel.domain.research_work import (
    ResearchProgress, ResearchRequest, continue_request, read_workpaper, render_workpaper,
)
from decision_kernel.methods.research import research_method


class ResearchWorkflow:
    """No model/collector/scheduler. Host authorization is separate from workpaper content."""
    def __init__(self, store: VersionedStore):
        self.store = store
        self.reader = ResearchReader(store)

    @staticmethod
    def _continuity(request: ResearchRequest, previous: dict | None) -> None:
        if previous is None:
            require(request.predecessor is None, "PREDECESSOR_CONTEXT_REQUIRED")
            return
        payload = previous["record"]["input"]
        require(payload["case_id"] == request.case_id, "RESEARCH_CASE_MISMATCH")
        if payload["kind"] != request.kind or payload["mandate"] != request.mandate:
            require(bool(request.scope_change_reason.strip()), "EXPLICIT_SCOPE_CHANGE_REQUIRED")
        work = read_workpaper(previous)
        if work is not None:
            questions = {q.question_id: q.text for q in request.questions}
            require(all(questions.get(q.question_id) == q.text for q in work[0].questions),
                    "ORIGINAL_QUESTIONS_MUST_BE_RETAINED")

    def prepare(self, request: ResearchRequest | None = None, *, case_id: str | None = None,
                reference: BlobRef | None = None, source_limit: int = 4,
                evidence_plan: dict | None = None, evidence_cursor: dict | None = None,
                evidence_limit: int = 4) -> dict:
        require(sum(v is not None for v in (request, case_id, reference)) == 1,
                "ONE_RESEARCH_SELECTOR_REQUIRED")
        require(type(source_limit) is int and 1 <= source_limit <= 8, "INVALID_SOURCE_PAGE_LIMIT")
        previous_context = None
        if request is None:
            previous_context = self.reader.resume(case_id, reference=reference, source_limit=source_limit)
            selected = BlobRef(**previous_context["research"]["reference"])
            request = continue_request(previous_context["research"], selected)
        else:
            require(isinstance(request, ResearchRequest), "INVALID_RESEARCH_REQUEST")
            if request.predecessor is not None:
                previous_context = self.reader.resume(reference=request.predecessor, source_limit=source_limit)
        previous = previous_context["research"] if previous_context is not None else None
        self._continuity(request, previous)
        require(evidence_plan is not None or evidence_cursor is None, "EVIDENCE_PLAN_REQUIRED_FOR_CURSOR")
        evidence = evidence_guidance(request)
        if evidence_plan is not None:
            linked = tuple(SourceRef.from_dict(ref) for ref in previous["record"]["input"]["sources"]) if previous else ()
            evidence = ResearchEvidence(self.store).page(request, evidence_plan, cursor=evidence_cursor,
                                                        limit=evidence_limit, linked_sources=linked)
            if evidence["next_cursor"] is not None:
                evidence["next_prepare_payload"] = {"request": asdict(request), "source_limit": source_limit,
                    "evidence_plan": evidence["plan"], "evidence_cursor": evidence["next_cursor"],
                    "evidence_limit": evidence_limit}
        return {"schema": "research-preparation-v1", "request": asdict(request),
                "evidence": evidence, "full_execution": execution_contract(request, evidence),
                "method": research_method(request.kind), "previous_context": previous_context,
                "continuation": continuation_plan(request, previous_context),
                "source_use": source_use_guidance(request.kind),
                "research_assessment": "not-performed", "execution_authority": "not-granted-by-preparation",
                "persistence": "not-written"}

    def checkpoint(self, request: ResearchRequest, progress: ResearchProgress, *,
                   submission_id: str, revision_id: str, method_sha256: str | None = None) -> dict:
        require(isinstance(request, ResearchRequest) and isinstance(progress, ResearchProgress),
                "RESEARCH_REQUEST_AND_PROGRESS_REQUIRED")
        identifier(submission_id); identifier(revision_id)
        progress.validate_for(request)
        # Historical methods reconstruct their original policy/bytes for replay.
        # Runtime writes require current; its reconciliation backend cannot write.
        method = research_method(request.kind, sha256=method_sha256)
        # Read the declared immutable predecessor, NOT current. VersionedStore must
        # get the opportunity to replay a successful intent before rejecting stale
        # predecessors. Otherwise a lost receipt would break legitimate retries.
        previous = self.store.read_revision(request.predecessor) if request.predecessor else None
        self._continuity(request, previous)
        if previous is not None:
            old_work = read_workpaper(previous)
            if old_work is not None and method["id"] == "decision-os-research-full-v2":
                pending = {(c.question_id, c.argument) for c in old_work[1].challenges
                           if c.disposition == "unresolved"}
                accounted = {(c.question_id, c.argument) for c in progress.challenges}
                require(pending <= accounted, "UNRESOLVED_CHALLENGE_MUST_BE_ADDRESSED")
            old_sources = {encoded(s) for s in previous["record"]["input"]["sources"]}
            new_sources = {encoded(asdict(s)) for s in progress.sources}
            require(not old_sources - new_sources or bool(progress.source_changes.strip()),
                    "SOURCE_REMOVAL_REQUIRES_EXPLANATION")
        revision = RevisionInput(
            request.case_id, revision_id, request.kind, render_workpaper(request, progress, method),
            request.mandate, method["id"] + "@sha256:" + method["sha256"],
            progress.sources, request.predecessor, progress.unresolved(request),
        )
        # Existing owner verifies actual sources, performs intent/CAS/retention, and
        # reads back. This layer neither implements a second save nor grants writes.
        saved = self.store.submit_revision(submission_id, revision)
        result = {"reference": asdict(saved), "declared_stop": progress.stop,
                "research_assessment": "not-performed", "human_acceptance": "not-confirmed",
                "execution_authority": "not-granted-by-save"}
        if request.kind == "full":
            result["source_use_acceptance"] = acceptance_pending(asdict(saved))
        if NUMBERS_START in progress.body:
            # Preserve the successful store receipt even if a numerical check fails.
            # This is post-save mechanical assessment, not a new persistence intent.
            try:
                audit = NumericalAudit(self.store).saved(saved)
                result["numerical_audit"] = {
                    "status": audit["status"], "input_sha256": audit.get("input_sha256"),
                    "saved_render": audit.get("saved_render"), "error": audit.get("error"),
                    "failures": [{"id": row["id"], "error": row["error"]}
                                 for row in audit.get("inputs", []) + audit.get("calculations", [])
                                 if row["status"] == "blocked"],
                    "warnings": sorted({warning for row in audit.get("inputs", []) + audit.get("calculations", [])
                                        for warning in row.get("warnings", [])}),
                    "coverage": audit.get("coverage"), "unmarked_prose": "not-audited",
                }
            except (RecordError, KeyError, TypeError) as error:
                result["numerical_audit"] = {"status": "blocked", "error": str(error)
                    if isinstance(error, RecordError) else "NUMERICAL_ASSESSMENT_UNAVAILABLE"}
        return result
