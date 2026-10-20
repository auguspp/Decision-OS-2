"""Admission to new Full Watch work; historical v1/v2 replay stays unchanged."""
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.probability_odds import ProbabilityOdds
from decision_kernel.domain.conditional_odds import timestamp
from decision_kernel.domain.records import BlobRef, require
from decision_kernel.domain.research_work import read_workpaper


def require_full(frozen, reader):
    research = reader.read_revision(BlobRef(**frozen["research_ref"]))
    require(research["record"]["input"]["kind"] == "full", "WATCH_FULL_REQUIRED")
    return research


def qualify_tracking(frozen, reader, *, spec, assessment, registry=None, clock=None):
    """Restore the actual Odds and original questions, never infer their economics."""
    research = require_full(frozen, reader)
    watch = frozen["watch"]
    tracking = watch["tracking"]
    odds = tracking["odds_result"]
    if odds is not None:
        service = (ConditionalOdds(reader, registry, clock=clock) if odds["kind"] == "conditional"
                   else ProbabilityOdds(reader, registry, clock=clock))
        result = service.read(odds["reference"])["result"]
        require(result["research_ref"] == frozen["research_ref"]
                and result["research_body_sha256"] == frozen["research_body_sha256"],
                "WATCH_FULL_ODDS_RESEARCH_MISMATCH")
        spec_ref = assessment["assessment"]["spec_ref"] if assessment else watch["basis"]["reference"]
        require(result["spec_ref"] == spec_ref and result["spec_sha256"] == spec["spec_sha256"],
                "WATCH_FULL_ODDS_SPEC_MISMATCH")
        if assessment:
            require(result["assessment_ref"] == watch["basis"]["reference"]
                    and result["assessment_sha256"] == assessment["assessment_sha256"],
                    "WATCH_FULL_ODDS_ASSESSMENT_MISMATCH")
        activation = timestamp(watch["activation"]["as_of"])
        require(timestamp(result["price"]["as_of"]) <= activation
                and ("calculation_input" not in result
                     or timestamp(result["calculation_input"]["evaluated_at"]) <= activation),
                "WATCH_FULL_ACTIVATION_PRECEDES_ODDS")
    links = tracking["question_links"]
    if not links: return
    work = read_workpaper(research)
    require(work is not None, "WATCH_FULL_QUESTIONS_NOT_RECORDED")
    request, progress = work
    questions = {question.question_id for question in request.questions}
    findings = {finding.question_id: finding for finding in progress.findings}
    challenges = {challenge.question_id for challenge in progress.challenges if challenge.disposition == "unresolved"}
    worlds = {world["id"] for world in spec["spec"]["worlds"]} if spec else set()
    for link in links:
        question_id = link["question_id"]
        require(question_id in questions, "WATCH_FULL_QUESTION_NOT_IN_RESEARCH")
        finding = findings.get(question_id)
        require(finding is not None and finding.material
                and (finding.status == "open" or question_id in challenges),
                "WATCH_FULL_MATERIAL_UNRESOLVED_QUESTION_REQUIRED")
        require(set(link["world_ids"]) <= worlds, "WATCH_FULL_QUESTION_WORLD_NOT_IN_SPEC")
