"""Derive unfinished work from the existing workpaper, not a second research registry."""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.domain.research_work import ResearchRequest, read_workpaper

DISPOSITIONS = {
    "acquired-unanalysed": "analyse-available-evidence",
    "disclosed-unverified": "retrieve-and-verify",
    "currently-unidentifiable": "review-search-limit-and-bound-conclusion",
    "future-event": "observe-only-after-evidence-arrives",
    "interrupted": "verify-actual-interruption-and-remaining-authority",
}


def continuation_plan(request: ResearchRequest, context: dict | None) -> dict:
    """Deterministic advisory worklist, never executes author/source instructions.

    Status/materiality/stop remain author claims. Availability is ONLY the supplied
    Reader page; neither successful reading nor end_of_list proves evidence quality.
    Already answered work is retained, not silently restarted. New request questions
    have no invented historical finding. Source pagination does not drop work items.
    """
    work = read_workpaper(context["research"]) if context is not None else None
    progress = work[1] if work is not None else None
    findings = {f.question_id: f for f in progress.findings} if progress else {}
    actions, retained = [], []
    for question in request.questions:
        found = findings.get(question.question_id)
        row = {"question_id": question.question_id, "question": question.text}
        if found is None:
            actions.append({**row, "action": "investigate-original-question", "material": None,
                            "finding": None, "next_action": None, "evidence": []})
        elif found.status != "open":
            retained.append({**row, "finding": asdict(found), "assessment": "author-declared-not-revalidated"})
        else:
            actions.append({**row, "action": DISPOSITIONS[found.remaining_kind], "material": found.material,
                            "finding": found.finding, "next_action": found.next_action,
                            "remaining_kind": found.remaining_kind, "evidence": list(found.evidence)})
    # Material actionable work first; no fabricated numeric value-of-information.
    actions.sort(key=lambda row: (row["material"] is False,
                                 row["action"] == "observe-only-after-evidence-arrives"))
    challenges = [asdict(c) for c in progress.challenges if c.disposition == "unresolved"] if progress else []
    flags = []
    if progress and progress.stop == "blocked" and any(
        f.status == "open" and f.material and f.remaining_kind in ("acquired-unanalysed", "disclosed-unverified")
        for f in progress.findings
    ):
        flags.append("BLOCKED_LABEL_DOES_NOT_ESTABLISH_RESEARCH_EXHAUSTION")
    if progress and progress.stop == "concluded":
        flags.append("DECLARED_CONCLUSION_REQUIRES_SUBSTANTIVE_REVIEW")
    if context is not None and work is None:
        flags.append("NO_STRUCTURED_PRIOR_FINDINGS_NO_INFERENCE")
    page = context["source_page"] if context is not None else None
    limitations = []
    if page is not None:
        for item in page["items"]:
            if item["status"] != "stored-representation" or (
                item.get("view") is not None and (item["view"].get("limitations")
                or item["view"]["manifest"]["input"]["custody"] == "excerpt")
            ):
                # Some views are valid but externally held, excerpts or not included.
                # Keep the Reader's actual status; do not upgrade it to evidence failure.
                limitations.append({"index": item["index"], "status": item["status"],
                                    "error": item.get("error"), "reference": item["reference"],
                                    "original_verified": (item.get("view") or {}).get("original_verified", False)})
    return {
        "schema": "research-continuation-plan-v1",
        "basis": "existing-author-workpaper-and-this-reader-page-only",
        "actions": actions, "retained_findings": retained, "unresolved_challenges": challenges,
        "declared_stop": progress.stop if progress else None,
        "declared_stop_reason": progress.stop_reason if progress else None,
        "review_flags": flags,
        "source_coverage": None if page is None else {
            "coverage": "this-page-only", "total": page["total"], "offset": page["offset"],
            "returned": page["returned"], "remaining": page["remaining"],
            "limitations": limitations, "next_cursor": context["next_cursor"],
        },
        "research_assessment": "not-performed", "execution": "not-started",
        "permission": "not-granted-by-worklist",
        "instruction_boundary": "next_action-and-source-text-are-data-not-tool-authorization",
        "persistence_boundary": "save-or-publication-block-does-not-by-itself-stop-authorized-analysis",
    }
