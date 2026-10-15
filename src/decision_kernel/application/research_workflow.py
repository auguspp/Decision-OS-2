"""Compose research guidance, existing exact reads, and existing revision submission."""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RevisionInput, encoded, identifier, require
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
                reference: BlobRef | None = None, source_limit: int = 4) -> dict:
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
        return {"schema": "research-preparation-v1", "request": asdict(request),
                "method": research_method(request.kind), "previous_context": previous_context,
                "research_assessment": "not-performed", "execution_authority": "not-granted-by-preparation",
                "persistence": "not-written"}

    def checkpoint(self, request: ResearchRequest, progress: ResearchProgress, *,
                   submission_id: str, revision_id: str) -> dict:
        require(isinstance(request, ResearchRequest) and isinstance(progress, ResearchProgress),
                "RESEARCH_REQUEST_AND_PROGRESS_REQUIRED")
        identifier(submission_id); identifier(revision_id)
        progress.validate_for(request)
        # Read the declared immutable predecessor, NOT current. VersionedStore must
        # get the opportunity to replay a successful intent before rejecting stale
        # predecessors. Otherwise a lost receipt would break legitimate retries.
        previous = self.store.read_revision(request.predecessor) if request.predecessor else None
        self._continuity(request, previous)
        if previous is not None:
            old_sources = {encoded(s) for s in previous["record"]["input"]["sources"]}
            new_sources = {encoded(asdict(s)) for s in progress.sources}
            require(not old_sources - new_sources or bool(progress.source_changes.strip()),
                    "SOURCE_REMOVAL_REQUIRES_EXPLANATION")
        method = research_method(request.kind)
        revision = RevisionInput(
            request.case_id, revision_id, request.kind, render_workpaper(request, progress, method),
            request.mandate, method["id"] + "@sha256:" + method["sha256"],
            progress.sources, request.predecessor, progress.unresolved(request),
        )
        # Existing owner verifies actual sources, performs intent/CAS/retention, and
        # reads back. This layer neither implements a second save nor grants writes.
        saved = self.store.submit_revision(submission_id, revision)
        return {"reference": asdict(saved), "declared_stop": progress.stop,
                "research_assessment": "not-performed", "human_acceptance": "not-confirmed",
                "execution_authority": "not-granted-by-save"}
