"""Method workpapers carried by existing research revisions, not a task/authority registry."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json

from decision_kernel.domain.records import (
    BlobRef, MAX_BYTES, RecordError, SourceRef, checked_text, encoded, identifier, require, strings,
)

WORK_SCHEMA = "research-workpaper-v1"
WORK_MARKER = "\n\n## Research workpaper (author declarations, not acceptance)\n<!-- decision-os-research-workpaper-v1 -->\n```json\n"
REMAINING_KINDS = {"acquired-unanalysed", "disclosed-unverified", "currently-unidentifiable",
                   "future-event", "interrupted"}
ACTIONABLE_KINDS = {"acquired-unanalysed", "disclosed-unverified", "interrupted"}


def _items(values, cls, limit: int, code: str):
    require(isinstance(values, (tuple, list)) and len(values) <= limit
            and all(isinstance(v, cls) for v in values), code)
    return tuple(values)


def _indices(values) -> tuple[int, ...]:
    require(isinstance(values, (tuple, list)) and len(values) <= 64
            and all(type(v) is int and 0 <= v < 64 for v in values)
            and len(values) == len(set(values)), "INVALID_EVIDENCE_INDICES")
    return tuple(values)


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: str
    text: str

    def __post_init__(self):
        identifier(self.question_id)
        checked_text(self.text, limit=8192)


@dataclass(frozen=True)
class ResearchRequest:
    """Caller-selected scope. Supplying this object does not grant execution/write permission."""
    case_id: str
    kind: str
    mandate: str
    questions: tuple[ResearchQuestion, ...]
    predecessor: BlobRef | None = None
    scope_change_reason: str = ""

    def __post_init__(self):
        identifier(self.case_id)
        require(self.kind in ("quick", "full"), "NOT_A_RESEARCH_KIND")
        checked_text(self.mandate)
        object.__setattr__(self, "questions", _items(self.questions, ResearchQuestion, 32, "INVALID_QUESTIONS"))
        require(bool(self.questions) and len({q.question_id for q in self.questions}) == len(self.questions),
                "UNIQUE_RESEARCH_QUESTIONS_REQUIRED")
        require(self.predecessor is None or isinstance(self.predecessor, BlobRef), "INVALID_PREDECESSOR")
        checked_text(self.scope_change_reason, empty=True, limit=8192)
        require(len(encoded(asdict(self))) <= MAX_BYTES, "RESEARCH_REQUEST_TOO_LARGE")

    @classmethod
    def from_dict(cls, value: dict) -> ResearchRequest:
        require(isinstance(value, dict), "INVALID_RESEARCH_REQUEST")
        try:
            data = dict(value)
            data["questions"] = tuple(ResearchQuestion(**q) for q in data["questions"])
            data["predecessor"] = BlobRef(**data["predecessor"]) if data.get("predecessor") is not None else None
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise RecordError("INVALID_RESEARCH_REQUEST") from exc


@dataclass(frozen=True)
class QuestionFinding:
    question_id: str
    status: str
    finding: str
    evidence: tuple[int, ...] = ()
    remaining_kind: str | None = None
    next_action: str = ""
    material: bool = True

    def __post_init__(self):
        identifier(self.question_id)
        require(self.status in ("answered", "reframed", "open"), "INVALID_QUESTION_STATUS")
        checked_text(self.finding)
        object.__setattr__(self, "evidence", _indices(self.evidence))
        require(type(self.material) is bool, "INVALID_MATERIALITY")
        checked_text(self.next_action, empty=True, limit=8192)
        if self.status == "open":
            require(self.remaining_kind in REMAINING_KINDS and bool(self.next_action.strip()),
                    "OPEN_QUESTION_REQUIRES_DISPOSITION")
        else:
            require(self.remaining_kind is None, "UNEXPECTED_REMAINING_KIND")


@dataclass(frozen=True)
class ResearchChallenge:
    question_id: str
    argument: str
    disposition: str
    response: str
    evidence: tuple[int, ...] = ()

    def __post_init__(self):
        identifier(self.question_id)
        checked_text(self.argument)
        checked_text(self.response)
        require(self.disposition in ("changed", "retained-with-reason", "unresolved"),
                "INVALID_CHALLENGE_DISPOSITION")
        object.__setattr__(self, "evidence", _indices(self.evidence))


@dataclass(frozen=True)
class ResearchProgress:
    """Useful intermediate work is legal; stop labels are the researcher's claims only."""
    body: str
    findings: tuple[QuestionFinding, ...]
    stop: str
    stop_reason: str
    sources: tuple[SourceRef, ...] = ()
    challenges: tuple[ResearchChallenge, ...] = ()
    limitations: tuple[str, ...] = ()
    source_changes: str = ""
    challenge_note: str = ""

    def __post_init__(self):
        checked_text(self.body)
        require(WORK_MARKER not in self.body, "RESERVED_WORKPAPER_MARKER")
        object.__setattr__(self, "findings", _items(self.findings, QuestionFinding, 32, "INVALID_FINDINGS"))
        require(bool(self.findings), "QUESTION_ACCOUNTING_REQUIRED")
        object.__setattr__(self, "challenges", _items(self.challenges, ResearchChallenge, 64, "INVALID_CHALLENGES"))
        object.__setattr__(self, "sources", _items(self.sources, SourceRef, 64, "INVALID_SOURCES"))
        require(len({encoded(asdict(s)) for s in self.sources}) == len(self.sources), "DUPLICATE_SOURCE_REF")
        object.__setattr__(self, "limitations", strings(self.limitations))
        require(self.stop in ("continue", "blocked", "concluded"), "INVALID_RESEARCH_STOP")
        checked_text(self.stop_reason, limit=8192)
        checked_text(self.source_changes, empty=True, limit=8192)
        checked_text(self.challenge_note, empty=True, limit=8192)
        require(bool(self.sources) or bool(self.limitations), "UNSOURCED_LIMITATION_REQUIRED")
        for item in (*self.findings, *self.challenges):
            require(all(i < len(self.sources) for i in item.evidence), "EVIDENCE_INDEX_OUT_OF_BOUNDS")
        if self.stop == "concluded":
            require(not any(f.status == "open" and f.material and f.remaining_kind in ACTIONABLE_KINDS
                            for f in self.findings), "ACTIONABLE_WORK_PREVENTS_CONCLUSION")
            require(not any(c.disposition == "unresolved" for c in self.challenges),
                    "UNRESOLVED_CHALLENGE_PREVENTS_CONCLUSION")
            require(bool(self.challenges) or bool(self.challenge_note.strip()), "CHALLENGE_ACCOUNTING_REQUIRED")
        require(len(encoded(asdict(self))) <= MAX_BYTES, "RESEARCH_PROGRESS_TOO_LARGE")

    @classmethod
    def from_dict(cls, value: dict) -> ResearchProgress:
        require(isinstance(value, dict), "INVALID_RESEARCH_PROGRESS")
        try:
            data = dict(value)
            data["findings"] = tuple(QuestionFinding(**q) for q in data["findings"])
            data["challenges"] = tuple(ResearchChallenge(**c) for c in data.get("challenges", []))
            data["sources"] = tuple(SourceRef.from_dict(s) for s in data.get("sources", []))
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise RecordError("INVALID_RESEARCH_PROGRESS") from exc

    def validate_for(self, request: ResearchRequest) -> None:
        ids = {q.question_id for q in request.questions}
        require(len(self.findings) == len(ids) and {f.question_id for f in self.findings} == ids,
                "ALL_REQUEST_QUESTIONS_MUST_BE_ACCOUNTED")
        require(all(c.question_id in ids for c in self.challenges), "CHALLENGE_QUESTION_NOT_IN_REQUEST")

    def unresolved(self, request: ResearchRequest) -> tuple[str, ...]:
        # Small summaries point back to the full immutable workpaper. Do not silently
        # truncate a user's limitation or exceed the existing record's item budget.
        notes = [f"[{f.question_id}] {f.remaining_kind}; see workpaper" for f in self.findings if f.status == "open"]
        if any(c.disposition == "unresolved" for c in self.challenges):
            notes.append("Unresolved challenges; see workpaper")
        if self.stop == "blocked":
            notes.append("Research interrupted/blocked; not a completed mandate")
        if self.limitations:
            notes.append("Research limitations; see workpaper")
        return tuple(notes)


def render_workpaper(request: ResearchRequest, progress: ResearchProgress, method: dict) -> str:
    progress.validate_for(request)
    record = asdict(progress)
    record.pop("body")
    note = {"schema": WORK_SCHEMA, "request": asdict(request), "progress": record,
            "method": {"id": method["id"], "sha256": method["sha256"]}}
    return progress.body + WORK_MARKER + encoded(note).decode("utf-8") + "```\n"


def read_workpaper(research: dict) -> tuple[ResearchRequest, ResearchProgress] | None:
    """Parse our canonical appendix, never execute text or infer missing historic questions."""
    body = research["body"]
    if WORK_MARKER not in body:
        return None  # Ordinary RevisionInput reports remain valid and readable.
    require(body.count(WORK_MARKER) == 1, "WORKPAPER_INVALID")
    narrative, _, tail = body.partition(WORK_MARKER)
    require(tail.endswith("```\n"), "WORKPAPER_INVALID")
    raw = tail[:-4].encode("utf-8")
    try:
        note = json.loads(raw)
        require(encoded(note) == raw and set(note) == {"schema", "request", "progress", "method"}
                and note["schema"] == WORK_SCHEMA, "WORKPAPER_INVALID")
        request = ResearchRequest.from_dict(note["request"])
        progress = ResearchProgress.from_dict({**note["progress"], "body": narrative})
        progress.validate_for(request)
        payload = research["record"]["input"]
        require(payload["case_id"] == request.case_id and payload["kind"] == request.kind
                and payload["mandate"] == request.mandate
                and payload["predecessor"] == (asdict(request.predecessor) if request.predecessor else None)
                and encoded(payload["sources"]) == encoded([asdict(s) for s in progress.sources])
                and payload["method"] == note["method"]["id"] + "@sha256:" + note["method"]["sha256"],
                "WORKPAPER_RECORD_MISMATCH")
        return request, progress
    except (TypeError, KeyError, ValueError, UnicodeError, RecursionError) as exc:
        raise RecordError("WORKPAPER_INVALID") from exc


def continue_request(research: dict, reference: BlobRef) -> ResearchRequest:
    work = read_workpaper(research)
    require(work is not None, "QUESTIONS_NOT_RECORDED_SUPPLY_EXPLICIT_REQUEST")
    return replace(work[0], predecessor=reference, scope_change_reason="")
