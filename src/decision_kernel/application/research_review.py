"""Question-scoped review of retained drafts and actual Brief excerpt coverage.

The workpapers own findings/challenges, Agenda owns submitted feedback, and Brief
owns adoption. This projection owns no new truth, writer or human acceptance.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.brief_research import excerpt, item_input
from decision_kernel.application.research_agenda import clock
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.domain.records import BlobRef, digest, encoded, identifier, require
from decision_kernel.domain.research_work import read_workpaper

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_REVIEW_BYTES = 768 * 1024


def review_plan(value):
    require(isinstance(value, dict) and {"before", "after", "question_id"} <= set(value)
            and set(value) <= {"before", "after", "question_id", "evidence_limit"}, "REVIEW_PLAN_INVALID")
    identifier(value["question_id"])
    before, after = item_input(value["before"]), item_input(value["after"])
    require(before["reference"] != after["reference"], "REVIEW_DISTINCT_REVISIONS_REQUIRED")
    limit = value.get("evidence_limit", 2)
    require(type(limit) is int and 1 <= limit <= 4, "REVIEW_EVIDENCE_LIMIT")
    return {"before": before, "after": after, "question_id": value["question_id"], "evidence_limit": limit}


def _question(research, question_id):
    work = read_workpaper(research)
    require(work is not None, "REVIEW_RECORDED_QUESTIONS_REQUIRED")
    request, progress = work
    question = next((q for q in request.questions if q.question_id == question_id), None)
    finding = next((f for f in progress.findings if f.question_id == question_id), None)
    challenges = [c for c in progress.challenges if c.question_id == question_id]
    return request, progress, question, finding, challenges


def _side(store, research, item, question_id, limit):
    request, progress, question, finding, challenges = _question(research, question_id)
    indices = sorted({i for claim in ([finding] if finding else []) + challenges for i in claim.evidence})
    evidence = []
    reader = ResearchReader(store)
    for index in indices[:limit]:
        # Reuse the Reader's exact SourceRef read and local-failure semantics.
        row = reader._source(index, progress.sources[index])
        row["open_request"] = {"operation": "read-source", "payload": asdict(progress.sources[index])}
        row["display"] = "included"
        if len(encoded(row)) > MAX_EVIDENCE_BYTES:
            row.update(view=None, display="not-included", error="REVIEW_EVIDENCE_DISPLAY_BOUND")
        evidence.append(row)
    pending = [{"index": i, "open_request": {"operation": "read-source", "payload": asdict(progress.sources[i])}}
               for i in indices[limit:]]
    payload = research["record"]["input"]
    return {"reference": deepcopy(item["reference"]), "case_id": request.case_id,
            "revision_id": payload["revision_id"], "kind": request.kind, "mandate": request.mandate,
            "stored_at": research["record"]["stored_at"], "scope_change_reason": request.scope_change_reason,
            "question": asdict(question) if question else None, "finding": asdict(finding) if finding else None,
            "challenges": [asdict(c) for c in challenges], "excerpt": excerpt(research, item),
            "recorded_unresolved": deepcopy(payload["unresolved"]), "stop": progress.stop,
            "stop_reason": progress.stop_reason, "limitations": list(progress.limitations),
            "source_changes": progress.source_changes, "challenge_note": progress.challenge_note,
            "evidence": {"coverage": "question-finding-and-challenge-indices-only", "total": len(indices),
                         "read": len(evidence), "remaining": len(pending), "items": evidence, "pending": pending},
            "open_request": {"operation": "prepare", "payload": {"reference": deepcopy(item["reference"])}}}


def adopted_range(item, quote, brief):
    """Coverage of selected literal BODY lines, not semantic correction acceptance.

The caller supplies the room's already independently reopened Brief panel, not a
candidate plan or pasted edition. A same-version badge is insufficient evidence.
"""
    result = {"brief_reference": brief["reference"], "version_included": None,
              "selected_range": [item["start"], item["end"]], "overlap_range": None,
              "uncovered_ranges": None, "scope": "selected-body-lines-not-semantic-equivalence-or-all-context"}
    if brief["status"] != "readable":
        return {**result, "status": "not-supplied" if brief["status"] == "not-supplied" else "unknown"}
    section = brief["view"].get("research")
    if section is None:
        return {**result, "status": "research-not-tracked-in-this-brief"}
    rows = [r for r in section["rows"] if r["item"]["reference"] == item["reference"]]
    adopted = next((r for r in rows if any(i["version_id"] == r["version_id"] for i in section["included"])), None)
    result.update(version_included=adopted is not None, uncovered_ranges=[result["selected_range"]])
    if adopted is None:
        return {**result, "status": "considered-not-included" if rows else "not-considered-in-this-brief"}
    prior = adopted["excerpt"]
    start, end = max(item["start"], prior["start"]), min(item["end"], prior["end"])
    result["adopted_range"] = [prior["start"], prior["end"]]
    if start > end:
        return {**result, "status": "outside-adopted-excerpt"}
    original = "".join(quote["text"].splitlines(keepends=True)[start-item["start"]:end-item["start"]+1])
    delivered = "".join(prior["text"].splitlines(keepends=True)[start-prior["start"]:end-prior["start"]+1])
    require(original == delivered, "REVIEW_ADOPTED_BYTES_MISMATCH")
    missing = []
    if item["start"] < start:
        missing.append([item["start"], start-1])
    if end < item["end"]:
        missing.append([end+1, item["end"]])
    result.update(overlap_range=[start, end], overlap_sha256=digest(delivered.encode()), uncovered_ranges=missing)
    return {**result, "status": "partial-range-included" if missing else "full-range-included"}


def _request(agenda, reference, question_id, revision_time):
    result = {"agenda_reference": agenda["reference"], "selection": None}
    if agenda["reference"] is None:
        return {**result, "status": "not-supplied"}
    if agenda["status"] != "readable":
        return {**result, "status": "unknown"}
    rows = [c for c in agenda["view"]["document"]["state"] if c["reference"] == reference]
    chosen = next((c for c in rows if c["question_id"] == question_id),
                  next((c for c in rows if c["question_id"] is None), None))
    result.update(selection=deepcopy(chosen), status="no-matching-feedback")
    if chosen and chosen["feedback"]:
        result.update(status=chosen["feedback"]["outcome"],
                      timing="recorded-after-revision" if clock(chosen["recorded_at"]) > clock(revision_time)
                      else "recorded-no-later-than-revision")
    return result


def open_review(store, value, *, agenda, brief):
    plan = review_plan(value)
    before = store.read_revision(BlobRef(**plan["before"]["reference"]))
    after = store.read_revision(BlobRef(**plan["after"]["reference"]))
    old, new = before["record"]["input"], after["record"]["input"]
    require(new["predecessor"] == before["reference"] and new["case_id"] == old["case_id"],
            "REVIEW_EXACT_DIRECT_SUCCESSOR_REQUIRED")
    require(_question(before, plan["question_id"])[2] is not None, "REVIEW_ORIGINAL_QUESTION_NOT_FOUND")
    sides = {name: _side(store, raw, plan[name], plan["question_id"], plan["evidence_limit"])
             for name, raw in (("before", before), ("after", after))}
    a, b = sides["before"], sides["after"]
    relation = ("not-carried-forward" if b["question"] is None else
                "same-question-text" if a["question"] == b["question"] else "reworded-question")
    absent = [c for c in a["challenges"] if c["disposition"] == "unresolved" and
              not any(n["argument"] == c["argument"] for n in b["challenges"])]
    pending = any(s["evidence"]["remaining"] or any(r["display"] != "included" or
                  r["status"] != "stored-representation" for r in s["evidence"]["items"])
                  for s in sides.values())
    mandate_changed = a["mandate"] != b["mandate"]
    result = {"schema": "research-review-v1", "plan": plan, **sides,
              "status": "partial" if pending or absent or mandate_changed or relation != "same-question-text" else "readable",
              "revision_relation": "exact-direct-successor",
              "question_relation": relation, "mandate_changed": mandate_changed,
              "kind_changed": a["kind"] != b["kind"],
              "selected_text_changed": a["excerpt"]["text"] != b["excerpt"]["text"],
              "finding_changed": a["finding"] != b["finding"],
              "old_unresolved_not_carried_forward": absent,
              "challenge_semantics": "missing-from-successor-does-not-mean-resolved",
              "request": _request(agenda, a["reference"], plan["question_id"], b["stored_at"]),
              "adoption": {name: adopted_range(plan[name], side["excerpt"], brief) for name, side in sides.items()},
              "assessment": "author-workpapers-and-submitted-feedback-not-independent-economic-review",
              "passage_association": "caller-selected-not-semantically-verified",
              "source_authenticity": "not-certified", "human_acceptance": "not-established",
              "correction_acceptance": "not-inferred-from-revision-or-excerpt-coverage",
              "persistence": "not-written", "execution": "not-started"}
    require(len(encoded(result)) <= MAX_REVIEW_BYTES, "REVIEW_DISPLAY_BOUND")
    store.backend.head()  # No partial export when the final mutable access check fails.
    return json.loads(encoded(result))
