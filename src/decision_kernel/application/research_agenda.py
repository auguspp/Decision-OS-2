"""Exact-research attention and review windows, without a second task/store owner.

The document records submitted choices, not authenticated human acceptance. Its
reading projection is rebuilt through the existing Reader. No research, event
check, timer, notification or investment operation is executed here.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re

from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, StoreUnavailable,
    checked_text, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference

SCHEMA = "research-agenda-v1"
WATCH_SCHEMA = "research-agenda-v2"
MAX_REFERENCES, MAX_CHANGES, MAX_STATE = 8, 32, 128
MAX_DOCUMENT_BYTES, MAX_CARD_BYTES = 256 * 1024, 1024 * 1024
MAX_VIEW_BYTES = 4 * 1024 * 1024
ACTIONS = ("focus", "defer", "dismiss", "reset")
RESEARCH_PATH = re.compile(
    r"research/cases/([a-z0-9][a-z0-9_-]{0,95})/revisions/"
    r"([a-z0-9][a-z0-9_-]{0,95})/record\.json\Z")


def clock(value):
    checked_text(value, limit=80)
    try:
        parsed = datetime.fromisoformat(value)
        require("T" in value and parsed.tzinfo is not None, "AGENDA_TIMEZONE_REQUIRED")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecordError("AGENDA_TIME_INVALID") from None


def research_reference(value):
    try:
        ref = value if isinstance(value, BlobRef) else BlobRef(**value)
    except (TypeError, KeyError):
        raise RecordError("AGENDA_RESEARCH_REFERENCE_INVALID") from None
    require(RESEARCH_PATH.fullmatch(ref.path) is not None, "AGENDA_RESEARCH_REFERENCE_INVALID")
    return asdict(ref)


def target_id(reference, question_id):
    return digest(encoded([research_reference(reference), question_id]))


def _review(value):
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) == {
        "variable", "event", "opens_at", "closes_at", "required_evidence", "end_condition"},
        "AGENDA_REVIEW_SHAPE")
    for key in ("variable", "event"):
        checked_text(value[key], empty=True, limit=1024)
    require(bool(value["variable"].strip() or value["event"].strip()), "AGENDA_REVIEW_OBJECT_REQUIRED")
    for key in ("required_evidence", "end_condition"):
        checked_text(value[key], limit=2048)
    require(clock(value["opens_at"]) <= clock(value["closes_at"]), "AGENDA_REVIEW_WINDOW_REVERSED")
    return deepcopy(value)


def _feedback(value):
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) == {"observation", "implication", "outcome"},
            "AGENDA_FEEDBACK_SHAPE")
    for key in ("observation", "implication"):
        checked_text(value[key], limit=4096)
    require(isinstance(value["outcome"], str) and value["outcome"] in (
        "unresolved", "revision-requested", "no-change-claimed"), "AGENDA_FEEDBACK_OUTCOME_INVALID")
    return deepcopy(value)


def choice(value):
    require(isinstance(value, dict) and {"reference", "question_id", "action", "reason"} <= set(value)
            and set(value) <= {"reference", "question_id", "action", "reason", "review", "feedback"},
            "AGENDA_CHOICE_SHAPE")
    ref = research_reference(value["reference"])
    qid = value["question_id"]
    if qid is not None:
        identifier(qid)
    require(isinstance(value["action"], str) and value["action"] in ACTIONS, "AGENDA_ACTION_INVALID")
    checked_text(value["reason"], limit=2048)
    review, feedback = _review(value.get("review")), _feedback(value.get("feedback"))
    require(review is None or value["action"] in ("focus", "defer"), "AGENDA_REVIEW_ACTION_INVALID")
    require(value["action"] != "reset" or feedback is None, "AGENDA_RESET_CANNOT_REVIEW")
    return {"reference": ref, "question_id": qid, "action": value["action"],
            "reason": value["reason"], "review": review, "feedback": feedback}


def plan_input(value):
    require(isinstance(value, dict) and {"references", "as_of"} <= set(value)
            and set(value) <= {"references", "as_of", "baseline", "changes", "watch_evidence"}, "AGENDA_PLAN_SHAPE")
    clock(value["as_of"])
    refs, changes = value["references"], value.get("changes", [])
    require(isinstance(refs, list) and len(refs) <= MAX_REFERENCES, "AGENDA_REFERENCE_BOUND")
    refs = [research_reference(ref) for ref in refs]
    require(len({encoded(ref) for ref in refs}) == len(refs), "AGENDA_DUPLICATE_REFERENCE")
    require(isinstance(changes, list) and len(changes) <= MAX_CHANGES, "AGENDA_CHANGE_BOUND")
    changes = [choice(c) for c in changes]
    require(len({target_id(c["reference"], c["question_id"]) for c in changes}) == len(changes),
            "AGENDA_DUPLICATE_TARGET")
    for item in changes:
        require(item["reference"] in refs, "AGENDA_CHOICE_OUTSIDE_SELECTION")
        if item["action"] == "defer" and item["review"]:
            require(clock(item["review"]["closes_at"]) > clock(value["as_of"]), "AGENDA_DEFER_ALREADY_ENDED")
    result = {"references": refs, "as_of": value["as_of"], "changes": changes,
              "baseline": asdict(artifact_reference(value["baseline"])) if value.get("baseline") is not None else None}
    if "watch_evidence" in value:
        from decision_kernel.application.watch_evidence import selections
        result["watch_evidence"] = selections(value["watch_evidence"])
    return result


def _state(value, as_of):
    require(isinstance(value, list) and len(value) <= MAX_STATE, "AGENDA_STATE_BOUND")
    result = []
    for row in value:
        require(isinstance(row, dict) and "recorded_at" in row, "AGENDA_STATE_SHAPE")
        selected = choice({k: v for k, v in row.items() if k != "recorded_at"})
        require(selected["action"] != "reset" and clock(row["recorded_at"]) <= clock(as_of),
                "AGENDA_STATE_TIME_OR_ACTION_INVALID")
        result.append({**selected, "recorded_at": row["recorded_at"]})
    keys = [target_id(c["reference"], c["question_id"]) for c in result]
    require(keys == sorted(set(keys)), "AGENDA_STATE_ORDER_OR_DUPLICATE")
    return result


def reduce_state(previous, plan):
    state = {target_id(c["reference"], c["question_id"]): deepcopy(c) for c in previous}
    for selected in plan["changes"]:
        key = target_id(selected["reference"], selected["question_id"])
        if selected["action"] == "reset":
            state.pop(key, None)
        else:
            state[key] = {**deepcopy(selected), "recorded_at": plan["as_of"]}
    require(len(state) <= MAX_STATE, "AGENDA_STATE_BOUND")
    return [state[key] for key in sorted(state)]


def review_status(review, as_of):
    if review is None:
        return "not-scheduled"
    now = clock(as_of)
    if now < clock(review["opens_at"]):
        return "not-due"
    return "due-for-review" if now <= clock(review["closes_at"]) else "overdue-for-review"


class ResearchAgenda:
    def __init__(self, store):
        self.store = store

    def _artifact(self, reference):
        from decision_kernel.application.research_artifacts import ResearchArtifacts
        return ResearchArtifacts(self.store).read(reference)

    def _document(self, reference):
        ref = asdict(artifact_reference(reference))
        found = self._artifact(ref)
        require(found.get("reference") == ref and found.get("bytes_verified") is True
                and found.get("metadata", {}).get("media_type") == "application/json", "AGENDA_ARTIFACT_INVALID")
        try:
            raw = base64.b64decode(found["data_base64"], validate=True)
            require(len(raw) <= MAX_DOCUMENT_BYTES, "AGENDA_DOCUMENT_BOUND")
            doc = json.loads(raw)
            require(isinstance(doc, dict) and set(doc) == {"schema", "plan", "state"}
                    and isinstance(doc["plan"], dict)
                    and doc["schema"] == (WATCH_SCHEMA if "watch_evidence" in doc["plan"] else SCHEMA)
                    and encoded(doc) == raw, "AGENDA_DOCUMENT_INVALID")
            require(plan_input(doc["plan"]) == doc["plan"]
                    and _state(doc["state"], doc["plan"]["as_of"]) == doc["state"], "AGENDA_DOCUMENT_INVALID")
            return doc
        except (ValueError, UnicodeError, KeyError, TypeError, RecursionError) as error:
            if isinstance(error, RecordError):
                raise
            raise RecordError("AGENDA_DOCUMENT_INVALID") from None

    def _checked_document(self, reference):
        doc = self._document(reference)
        baseline = doc["plan"]["baseline"]
        # One actual predecessor, not a recursive replay or a claim to audit all history.
        previous = self._document(baseline) if baseline else None
        if previous:
            require(clock(previous["plan"]["as_of"]) <= clock(doc["plan"]["as_of"]), "AGENDA_BASELINE_AFTER_CLOCK")
        require(reduce_state(previous["state"] if previous else [], doc["plan"]) == doc["state"],
                "AGENDA_STATE_TRANSITION_MISMATCH")
        return doc

    def _card(self, reference):
        from decision_kernel.application.research_continuation import continuation_plan
        from decision_kernel.application.research_reader import ResearchReader
        from decision_kernel.domain.research_work import continue_request, read_workpaper

        result = {"reference": deepcopy(reference)}
        try:
            ref = BlobRef(**reference)
            context = ResearchReader(self.store).resume(reference=ref, source_limit=1)
            research = context["research"]
            require(research["reference"] == reference, "AGENDA_RESEARCH_REFERENCE_MISMATCH")
            payload = research["record"]["input"]
            match = RESEARCH_PATH.fullmatch(ref.path)
            require((payload["case_id"], payload["revision_id"]) == match.groups(), "AGENDA_RESEARCH_PATH_MISMATCH")
            work = read_workpaper(research)
            continuation = continuation_plan(continue_request(research, ref), context) if work else None
            page = context["source_page"]
            result.update(status="readable", case_id=payload["case_id"], revision_id=payload["revision_id"],
                          kind=payload["kind"], mandate=payload["mandate"], stored_at=research["record"]["stored_at"],
                          recorded_unresolved=deepcopy(payload["unresolved"]),
                          questions=[{"question_id": q.question_id, "text": q.text} for q in work[0].questions] if work else [],
                          continuation=continuation, open_operation="prepare" if work else "resume",
                          open_payload={"reference": deepcopy(reference)},
                          source_page={k: page[k] for k in ("coverage", "total", "offset", "returned", "remaining")},
                          source_next_cursor=context["next_cursor"],
                          source_statuses=[{"index": i["index"], "status": i["status"], "error": i["error"]}
                                           for i in page["items"]])
            if len(encoded(result)) > MAX_CARD_BYTES:
                return {"reference": deepcopy(reference), "status": "not-included", "error": "AGENDA_CARD_BOUND"}
        except MissingObject:
            result.update(status="missing", error="AGENDA_RESEARCH_MISSING")
        except StoreUnavailable:
            result.update(status="unavailable", error="AGENDA_RESEARCH_UNAVAILABLE")
        except IntegrityError:
            result.update(status="invalid", error="AGENDA_RESEARCH_INTEGRITY_FAILED")
        except (RecordError, KeyError, TypeError):
            result.update(status="invalid", error="AGENDA_RESEARCH_INVALID")
        return result

    def _project(self, doc, as_of, cards):
        watch_rows = None
        if "watch_evidence" in doc["plan"]:
            from decision_kernel.application.watch_evidence import read_rows
            watch_rows = read_rows(self.store, doc["plan"]["watch_evidence"], as_of)
            for card in cards:
                card["watch_evidence"] = [deepcopy(row) for row in watch_rows
                                           if row["selection"]["reference"] == card["reference"]]
        state = {target_id(c["reference"], c["question_id"]): c for c in doc["state"]}
        for card in cards:
            if card["status"] != "readable":
                continue
            ref = card["reference"]
            known = {q["question_id"] for q in card["questions"]} | {None}
            require(all(c["question_id"] in known for c in doc["state"] if c["reference"] == ref),
                    "AGENDA_SAVED_QUESTION_NOT_IN_RESEARCH")
            default = state.get(target_id(ref, None))
            actions = {a["question_id"]: a for a in (card["continuation"] or {}).get("actions", [])}
            card["agenda"] = []
            questions = card["questions"] or [{"question_id": None, "text": "整份研究；没有结构化问题列表"}]
            for q in questions:
                selected = state.get(target_id(ref, q["question_id"]), default)
                due = review_status(selected["review"], as_of) if selected else "not-scheduled"
                status = selected["action"] if selected else (
                    "declared-followup" if q["question_id"] in actions else "no-structured-followup-declared")
                if selected and selected["action"] == "defer" and due in ("due-for-review", "overdue-for-review"):
                    status = due
                card["agenda"].append({**deepcopy(q), "selection": deepcopy(selected), "attention_status": status,
                                       "review_status": due, "declared_followup": deepcopy(actions.get(q["question_id"]))})
        used = 0
        for index, card in enumerate(cards):
            size = len(encoded(card))
            if size > MAX_CARD_BYTES or used + size > MAX_VIEW_BYTES:
                cards[index] = {"reference": card["reference"], "status": "not-included",
                                "error": "AGENDA_VIEW_BOUND_USE_EXACT_RESEARCH_READER"}
            else:
                used += size
        refs = doc["plan"]["references"]
        result = {"document": deepcopy(doc), "as_of": as_of, "cards": cards,
                  "status": "no-selected-research" if not cards else "partial" if any(
                      c["status"] != "readable" for c in cards) else "readable",
                  "unselected_attention": [deepcopy(c) for c in doc["state"] if c["reference"] not in refs],
                  "coverage": "selected-exact-research-only", "history_verification": "current-step-only",
                  "time_basis": "evaluation-clock-not-event-occurrence-or-research-cutoff",
                  "research_assessment": "not-performed", "feedback_assessment": "submitted-claims-not-verified",
                  "human_acceptance": "not-established-by-agenda", "execution": "not-started",
                  "notification": "not-sent", "market_quiet": "not-assessed", "persistence": "not-written"}
        if watch_rows is not None:
            result["watch_evidence"] = watch_rows
            result["unselected_watch_evidence"] = [deepcopy(row) for row in watch_rows
                                                    if row["selection"]["reference"] not in refs]
            result["watch_evidence_assessment"] = "retained-results-replayed-no-new-check-or-economic-assessment"
            if any(row["status"] != "readable" for row in watch_rows):
                result["status"] = "partial"
        return result

    def build(self, value):
        plan = plan_input(value)
        previous = self._checked_document(plan["baseline"]) if plan["baseline"] else None
        if previous:
            require(clock(previous["plan"]["as_of"]) <= clock(plan["as_of"]), "AGENDA_BASELINE_AFTER_CLOCK")
            if "watch_evidence" not in plan and "watch_evidence" in previous["plan"]:
                plan["watch_evidence"] = deepcopy(previous["plan"]["watch_evidence"])
        cards = [self._card(ref) for ref in plan["references"]]
        previous_targets = {target_id(c["reference"], c["question_id"]) for c in previous["state"]} if previous else set()
        for selected in plan["changes"]:
            card = cards[plan["references"].index(selected["reference"])]
            if selected["action"] == "reset" and target_id(selected["reference"], selected["question_id"]) in previous_targets:
                continue  # Remove an existing choice even when its research is unavailable; grant nothing new.
            require(card["status"] == "readable", "AGENDA_CHOICE_RESEARCH_UNREADABLE")
            require(selected["question_id"] is None or selected["question_id"] in {
                q["question_id"] for q in card["questions"]}, "AGENDA_QUESTION_NOT_IN_ORIGINAL_RESEARCH")
        doc = {"schema": WATCH_SCHEMA if "watch_evidence" in plan else SCHEMA, "plan": plan,
               "state": reduce_state(previous["state"] if previous else [], plan)}
        raw = encoded(doc)
        require(len(raw) <= MAX_DOCUMENT_BYTES, "AGENDA_DOCUMENT_BOUND")
        result = self._project(doc, plan["as_of"], cards)
        # Existing artifact custody/reconciliation owns every write. No current pointer.
        result["artifact"] = asdict(ArtifactInput.from_bytes(
            "research-agenda-" + digest(raw), "research-agenda.json", "application/json", raw))
        return result

    def open(self, reference, *, as_of=None):
        ref = asdict(artifact_reference(reference))
        doc = self._checked_document(ref)
        when = doc["plan"]["as_of"] if as_of is None else as_of
        require(clock(when) >= clock(doc["plan"]["as_of"]), "AGENDA_READ_BEFORE_DOCUMENT")
        result = self._project(doc, when, [self._card(r) for r in doc["plan"]["references"]])
        result.update(reference=ref, persistence="stored-document-read")
        return result
