"""Research method/workpaper commands composed inside the existing store CLI."""
from pathlib import Path

from decision_kernel.interfaces import agenda_commands, reading_commands

from decision_kernel.interfaces.research_use_commands import audit_source_use
from decision_kernel.application.numerical_audit import NumericalAudit
from decision_kernel.application.research_catalogue import ResearchCatalogue
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.domain.records import BlobRef
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest
from decision_kernel.methods.research import research_method, writable_research_method

COMMANDS = ("prepare-research", "checkpoint-research", "research-catalogue", "audit-numbers",
            "audit-source-use", "verify-reference-fixing") + agenda_commands.LOCAL_COMMANDS + (reading_commands.COMMAND,)


def configure(actions) -> None:
    reading_commands.configure(actions)
    agenda_commands.configure(actions)
    usage = actions.add_parser("audit-source-use", help="verify the saved method's source-use contract; no writes")
    usage.add_argument("--input", type=Path, required=True, help="external: exact saved reference only; default: native receipts and research selector")
    audit = actions.add_parser("audit-numbers", help="recompute declared source-bound numbers; read only, not economic approval")
    select_numbers = audit.add_mutually_exclusive_group(required=True)
    select_numbers.add_argument("--input", type=Path, help="research-numbers-v1/v2/v3 JSON; v2 checks cutoff times, v3 checks evidence uses with optional times")
    select_numbers.add_argument("--ref", type=Path, help="exact saved research BlobRef; recompute its numerical appendix")
    prepare = actions.add_parser("prepare-research", help="prepare researcher guidance and exact context; no execution")
    select = prepare.add_mutually_exclusive_group(required=True)
    select.add_argument("--request", type=Path, help="caller-selected scope JSON; not a permission grant")
    select.add_argument("--case-id", help="continue the saved workpaper's scope, selecting current once")
    select.add_argument("--ref", type=Path, help="exact saved workpaper BlobRef JSON")
    prepare.add_argument("--source-limit", type=int, default=4)
    prepare.add_argument("--method-id", help="supported current method; omitted preserves an external predecessor's contract")
    prepare.add_argument("--evidence-plan", type=Path, help="question-scoped pool queries, not source URLs")
    prepare.add_argument("--evidence-cursor", type=Path, help="exact evidence-page continuation")
    prepare.add_argument("--evidence-limit", type=int, default=4)
    checkpoint = actions.add_parser("checkpoint-research", help="save substantive work via the existing authorized store")
    checkpoint.add_argument("--request", type=Path, required=True)
    checkpoint.add_argument("--input", type=Path, required=True, help="progress JSON; cannot select case, kind or permissions")
    checkpoint.add_argument("--submission-id", required=True)
    checkpoint.add_argument("--revision-id", required=True)
    checkpoint.add_argument("--method-sha256", help="exact supported method digest from prepare; omitted selects the default method")
    catalogue = actions.add_parser("research-catalogue", help="browse saved research and remaining work; read only")
    catalogue.add_argument("--cursor", type=Path, help="exact catalogue next_cursor JSON, not a source cursor")
    catalogue.add_argument("--limit", type=int, default=4, help="case cards per page, 1 to 8")
    fixing = actions.add_parser("verify-reference-fixing",
                                help="read back the exact slices a saved revision cites; no writes, no fetch")
    fixing.add_argument("--ref", type=Path, required=True, help="exact saved research BlobRef JSON")


def execute(args, store, load, *, before_export=None) -> dict:
    if args.store_command == reading_commands.COMMAND:
        return reading_commands.execute(args, store, load, before_export=before_export)
    if args.store_command in agenda_commands.LOCAL_COMMANDS:
        return agenda_commands.execute(args, store, load)
    if args.store_command == "audit-source-use":
        return audit_source_use(store, load(args.input))
    if args.store_command == "audit-numbers":
        audit = NumericalAudit(store)
        return audit.saved(BlobRef(**load(args.ref))) if args.ref else audit.audit(load(args.input))
    if args.store_command == "research-catalogue":
        return ResearchCatalogue(store).page(cursor=load(args.cursor) if args.cursor else None, limit=args.limit)
    if args.store_command == "verify-reference-fixing":
        from decision_kernel.application.reference_fixing import from_revision
        return from_revision(store, BlobRef(**load(args.ref)))
    workflow = ResearchWorkflow(store)
    if args.store_command == "prepare-research":
        return workflow.prepare(
            ResearchRequest.from_dict(load(args.request)) if args.request else None,
            case_id=args.case_id, reference=BlobRef(**load(args.ref)) if args.ref else None,
            source_limit=args.source_limit,
            evidence_plan=load(args.evidence_plan) if args.evidence_plan else None,
            evidence_cursor=load(args.evidence_cursor) if args.evidence_cursor else None,
            evidence_limit=args.evidence_limit,
            method_id=args.method_id,
        )
    request = ResearchRequest.from_dict(load(args.request))
    method = writable_research_method(request.kind, args.method_sha256 if args.method_sha256 is not None
                                     else research_method(request.kind)["sha256"])
    return workflow.checkpoint(
        request, ResearchProgress.from_dict(load(args.input)),
        submission_id=args.submission_id, revision_id=args.revision_id,
        method_sha256=method["sha256"],
    )
