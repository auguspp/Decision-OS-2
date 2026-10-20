"""Exact retained Watch results read by Agenda and its existing Brief consumer.

The Watch owner replays conditions. This module only checks the declared link to
an original research question and projects that retained result; no new check,
attention choice, research, source request or notification is performed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from decision_kernel.domain.records import (
    BlobRef, MissingObject, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import artifact_reference

MAX_ITEMS = 8
MAX_ITEM_BYTES = 64 * 1024
MAX_VIEW_BYTES = 256 * 1024


def selection_id(value):
    """An alias commit is not another copy of one retained result/question link."""
    return digest(encoded([
        [value["reference"][k] for k in ("repository", "path", "sha256")],
        value["question_id"], value["kind"],
        [value["check_ref"][k] for k in ("repository", "path", "sha256")],
    ]))


def selections(value):
    from decision_kernel.application.research_agenda import research_reference

    require(isinstance(value, list) and len(value) <= MAX_ITEMS, "AGENDA_WATCH_EVIDENCE_BOUND")
    result = []
    for row in value:
        require(isinstance(row, dict) and set(row) == {
            "reference", "question_id", "kind", "check_ref"}, "AGENDA_WATCH_EVIDENCE_SHAPE")
        require(isinstance(row["kind"], str) and row["kind"] in ("check", "activity"),
                "AGENDA_WATCH_EVIDENCE_KIND")
        if row["question_id"] is not None:
            identifier(row["question_id"])
        result.append({"reference": research_reference(row["reference"]),
                       "question_id": row["question_id"], "kind": row["kind"],
                       "check_ref": asdict(artifact_reference(row["check_ref"]))})
    require(len({selection_id(r) for r in result}) == len(result), "AGENDA_WATCH_EVIDENCE_DUPLICATE")
    return result


def export_required(value):
    """Host gate for a plan, document, agenda result/card or Brief research part.

    Inspect only the known projection/plan locations, never source text. A
    baseline's hidden selections must first be loaded through the normal reader.
    """
    if not isinstance(value, dict):
        return False
    if value.get("kind") in ("quick", "brief") and isinstance(value.get("items"), list):
        return any(export_required(item) for item in value["items"])
    document = value.get("document", value)
    if not isinstance(document, dict):
        return False
    if document.get("schema") == "hosted-ai-handoff-v2" and bool(document.get("watch_adoptions")):
        return True
    plan = document.get("plan", document)
    if isinstance(plan, dict) and bool(plan.get("watch_evidence")):
        return True
    if value.get("watch_evidence_adoption"):
        return True
    rows = value.get("rows", [])
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and (
        bool(row.get("watch_evidence")) or isinstance(row.get("context"), dict)
        and bool(row["context"].get("watch_evidence"))) for row in rows)


def _read(store, selected, as_of):
    from decision_kernel.application.odds_watch import OddsWatch
    from decision_kernel.application.research_agenda import clock
    from decision_kernel.domain.research_work import read_workpaper

    row = {"selection": deepcopy(selected), "selection_id": selection_id(selected),
           "open_request": {"operation": "read-odds-watch-check" if selected["kind"] == "check"
                            else "read-odds-watch-activity",
                            "payload": {"reference": deepcopy(selected["check_ref"])}}}
    watches = OddsWatch(store)
    try:
        if selected["question_id"] is not None:
            work = read_workpaper(store.read_revision(BlobRef(**selected["reference"])))
            require(work is not None and selected["question_id"] in {
                q.question_id for q in work[0].questions}, "AGENDA_WATCH_QUESTION_NOT_IN_RESEARCH")
        if selected["kind"] == "check":
            found = watches.read_check(selected["check_ref"], include_current_use=False)
            result = found["result"]
            watch_ref = result["request"]["watch_ref"]
            at = result["request"]["checked_at"]
            activity = None
        else:
            from decision_kernel.application.watch_activity import WatchActivity
            found = WatchActivity(store).read(selected["check_ref"], include_current_use=False)
            event = found["event"]
            command = event["command"]
            result = event["result"]
            watch_ref = command["input"]["watch_ref"]
            at = command["input"]["checked_at" if command["kind"] == "check" else "at"]
            activity = {"state": event["state"], "action": command["input"].get("action", "check"),
                        "summary": deepcopy(found["summary"])}
        require(found["reference"] == selected["check_ref"] and found["recalculation"] == "matches",
                "AGENDA_WATCH_EVIDENCE_REPLAY_REQUIRED")
        frozen = watches.read_watch(watch_ref)
        require(frozen["research_ref"] == selected["reference"], "AGENDA_WATCH_RESEARCH_MISMATCH")
        if clock(at) > clock(as_of):
            return {**row, "status": "after-as-of", "recorded_at": at,
                    "error": "AGENDA_WATCH_EVIDENCE_AFTER_CLOCK"}
        watch = frozen["watch"]
        definition = {k: deepcopy(watch[k]) for k in (
            "schema", "condition", "conditions", "combination") if k in watch}
        if watch["schema"] == "odds-watch-v3":
            definition["tracking"] = deepcopy(watch["tracking"])
        row.update(status="readable", watch_ref=deepcopy(watch_ref), watch_id=watch["watch_id"],
                   watch_sha256=frozen["watch_sha256"], definition=definition, recorded_at=at,
                   result=deepcopy(result), activity=activity, recalculation="matches",
                   result_sha256=digest(encoded(result)),
                   relation="explicit-question-link-not-an-independent-economic-assessment",
                   time_basis="retained-check-clock-not-custody-time-or-historical-PIT")
    except MissingObject:
        # Missing exact bytes are a visible gap. Transport/access/budget and
        # integrity failures propagate; another row must not reset their scope.
        return {**row, "status": "missing", "error": "AGENDA_WATCH_EVIDENCE_MISSING"}
    if len(encoded(row)) > MAX_ITEM_BYTES:
        return {k: deepcopy(row[k]) for k in ("selection", "selection_id", "open_request")} | {
            "status": "not-included", "error": "AGENDA_WATCH_EVIDENCE_ITEM_BOUND"}
    return row


def read_rows(store, value, as_of):
    selected = selections(value)
    if not selected:
        return []
    store.backend.head()
    rows, used = [], 0
    for item in selected:
        row = _read(store, item, as_of)
        size = len(encoded(row))
        if used + size > MAX_VIEW_BYTES:
            row = {k: deepcopy(row[k]) for k in ("selection", "selection_id", "open_request")} | {
                "status": "not-included", "error": "AGENDA_WATCH_EVIDENCE_VIEW_BOUND"}
        used += len(encoded(row))
        rows.append(row)
    store.backend.head()
    return rows
