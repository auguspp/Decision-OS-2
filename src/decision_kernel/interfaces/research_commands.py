"""Research method/workpaper commands composed inside the existing store CLI."""
from pathlib import Path

from decision_kernel.interfaces.research_use_commands import audit_source_use
from decision_kernel.application.numerical_audit import NumericalAudit
from decision_kernel.application.research_catalogue import ResearchCatalogue
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.domain.records import BlobRef
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest

COMMANDS = ("prepare-research", "checkpoint-research", "research-catalogue", "audit-numbers", "audit-source-use")


def configure(actions) -> None:
    usage = actions.add_parser("audit-source-use", help="verify native pool/Relay receipts and exact research adoption; no writes")
    usage.add_argument("--input", type=Path, required=True, help="request/progress or saved reference, native receipt IDs and explicit exceptions")
    audit = actions.add_parser("audit-numbers", help="recompute declared source-bound numbers; read only, not economic approval")
    select_numbers = audit.add_mutually_exclusive_group(required=True)
    select_numbers.add_argument("--input", type=Path, help="research-numbers-v1 JSON")
    select_numbers.add_argument("--ref", type=Path, help="exact saved research BlobRef; recompute its numerical appendix")
    prepare = actions.add_parser("prepare-research", help="prepare researcher guidance and exact context; no execution")
    select = prepare.add_mutually_exclusive_group(required=True)
    select.add_argument("--request", type=Path, help="caller-selected scope JSON; not a permission grant")
    select.add_argument("--case-id", help="continue the saved workpaper's scope, selecting current once")
    select.add_argument("--ref", type=Path, help="exact saved workpaper BlobRef JSON")
    prepare.add_argument("--source-limit", type=int, default=4)
    prepare.add_argument("--evidence-plan", type=Path, help="question-scoped pool queries, not source URLs")
    prepare.add_argument("--evidence-cursor", type=Path, help="exact evidence-page continuation")
    prepare.add_argument("--evidence-limit", type=int, default=4)
    checkpoint = actions.add_parser("checkpoint-research", help="save substantive work via the existing authorized store")
    checkpoint.add_argument("--request", type=Path, required=True)
    checkpoint.add_argument("--input", type=Path, required=True, help="progress JSON; cannot select case, kind or permissions")
    checkpoint.add_argument("--submission-id", required=True)
    checkpoint.add_argument("--revision-id", required=True)
    catalogue = actions.add_parser("research-catalogue", help="browse saved research and remaining work; read only")
    catalogue.add_argument("--cursor", type=Path, help="exact catalogue next_cursor JSON, not a source cursor")
    catalogue.add_argument("--limit", type=int, default=4, help="case cards per page, 1 to 8")


def execute(args, store, load) -> dict:
    if args.store_command == "audit-source-use":
        return audit_source_use(store, load(args.input))
    if args.store_command == "audit-numbers":
        audit = NumericalAudit(store)
        return audit.saved(BlobRef(**load(args.ref))) if args.ref else audit.audit(load(args.input))
    if args.store_command == "research-catalogue":
        return ResearchCatalogue(store).page(cursor=load(args.cursor) if args.cursor else None, limit=args.limit)
    workflow = ResearchWorkflow(store)
    if args.store_command == "prepare-research":
        return workflow.prepare(
            ResearchRequest.from_dict(load(args.request)) if args.request else None,
            case_id=args.case_id, reference=BlobRef(**load(args.ref)) if args.ref else None,
            source_limit=args.source_limit,
            evidence_plan=load(args.evidence_plan) if args.evidence_plan else None,
            evidence_cursor=load(args.evidence_cursor) if args.evidence_cursor else None,
            evidence_limit=args.evidence_limit,
        )
    return workflow.checkpoint(
        ResearchRequest.from_dict(load(args.request)), ResearchProgress.from_dict(load(args.input)),
        submission_id=args.submission_id, revision_id=args.revision_id,
    )
