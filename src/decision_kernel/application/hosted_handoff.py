"""Hosted AI handoffs on the SAME artifact owner. This module never runs a model.

Quick has one canonical ResearchRevision; its handoff contains only its reference.
An AI Brief has author-written text and exact, actually read research adoptions.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import date, timedelta
import json
import re

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import BlobRef, Conflict, RecordError, checked_text, digest, encoded, require
from decision_kernel.domain.research_artifacts import ArtifactInput, ARTIFACT_PATH, artifact_reference
from decision_kernel.observations.announcement_discovery import observed_clock

SCHEMA = "hosted-ai-handoff-v1"
WATCH_SCHEMA = "hosted-ai-handoff-v2"
PREFIXES = {"quick": "hosted-quick-", "brief": "ai-brief-"}
OPERATIONS = {"save-hosted-handoff", "reconcile-hosted-handoff", "read-hosted-handoff", "latest-hosted-handoff"}


def validate(doc):
    require(isinstance(doc, dict) and doc.get("schema") in (SCHEMA, WATCH_SCHEMA)
            and doc.get("kind") in PREFIXES
            and (doc["schema"] != WATCH_SCHEMA or doc["kind"] == "brief"),
            "HOSTED_HANDOFF_SCHEMA")
    common = {"schema", "kind", "execution_date", "cutoff", "scope", "gaps"}
    expected = common | ({"research"} if doc["kind"] == "quick" else {"body", "adoptions", "baseline"})
    if doc["schema"] == WATCH_SCHEMA:
        expected.add("watch_adoptions")
    require(set(doc) == expected and len(encoded(doc)) <= 48 * 1024, "HOSTED_HANDOFF_FIELDS_OR_SIZE")
    day = date.fromisoformat(doc["execution_date"])
    require(day.isoformat() == doc["execution_date"], "HOSTED_HANDOFF_DATE")
    observed_clock(doc["cutoff"])
    checked_text(doc["scope"], limit=4096)
    require(isinstance(doc["gaps"], list) and len(doc["gaps"]) <= 24, "HOSTED_HANDOFF_GAPS")
    for gap in doc["gaps"]:
        checked_text(gap, limit=2048)
    if doc["kind"] == "quick":
        BlobRef(**doc["research"])
    else:
        checked_text(doc["body"], limit=32*1024)
        require(isinstance(doc["adoptions"], list) and len(doc["adoptions"]) <= 8, "HOSTED_ADOPTIONS_BOUND")
        seen = set()
        for item in doc["adoptions"]:
            require(isinstance(item, dict) and set(item) == {"reference", "body_sha256", "used_change"},
                    "HOSTED_ADOPTION_SHAPE")
            ref = BlobRef(**item["reference"])
            require(re.fullmatch(r"[a-f0-9]{64}", item["body_sha256"]) is not None and ref not in seen,
                    "HOSTED_ADOPTION_IDENTITY")
            seen.add(ref); checked_text(item["used_change"], limit=2048)
        if doc["baseline"] is not None:
            artifact_reference(doc["baseline"])
        if doc["schema"] == WATCH_SCHEMA:
            from decision_kernel.application.watch_evidence import selections
            rows = doc["watch_adoptions"]
            require(isinstance(rows, list) and len(rows) <= 8, "HOSTED_WATCH_ADOPTIONS_BOUND")
            selected = []
            for item in rows:
                require(isinstance(item, dict) and set(item) == {
                    "reference", "question_id", "kind", "check_ref", "result_sha256", "used_change"},
                    "HOSTED_WATCH_ADOPTION_SHAPE")
                require(isinstance(item["result_sha256"], str)
                        and re.fullmatch(r"[a-f0-9]{64}", item["result_sha256"]) is not None,
                        "HOSTED_WATCH_ADOPTION_IDENTITY")
                checked_text(item["used_change"], limit=2048)
                require(item["used_change"] in doc["body"], "HOSTED_WATCH_CHANGE_NOT_IN_BODY")
                selected.append({key: item[key] for key in ("reference", "question_id", "kind", "check_ref")})
            selections(selected)
    return doc


def artifact_id(doc):
    validate(doc)
    return PREFIXES[doc["kind"]] + doc["execution_date"].replace("-", "")


def _document(store, ref):
    found = ResearchArtifacts(store).read(ref)
    require(found["metadata"]["media_type"] == "application/json", "HOSTED_HANDOFF_MEDIA")
    raw = base64.b64decode(found["data_base64"], validate=True)
    doc = validate(json.loads(raw))
    require(encoded(doc) == raw and found["metadata"]["artifact_id"] == artifact_id(doc),
            "HOSTED_HANDOFF_IDENTITY")
    return doc


def publication_required(store, value):
    """Known Hosted documents and their actually read immediate baselines only.

    No arbitrary source dictionaries, recursive baseline traversal or inferred
    authority. The host checks this before input export and after actual reads.
    """
    if not isinstance(value, dict):
        return False
    if "items" in value and value.get("kind") in PREFIXES and isinstance(value["items"], list):
        return any(publication_required(store, item) for item in value["items"])
    doc = value.get("document", value)
    if not isinstance(doc, dict) or doc.get("schema") not in (SCHEMA, WATCH_SCHEMA):
        return False
    validate(doc)
    current = bool(doc.get("watch_adoptions"))
    if doc["kind"] == "brief" and doc["baseline"] is not None:
        prior = _document(store, doc["baseline"])
        return current or bool(prior.get("watch_adoptions"))
    return current


def resolve(store, doc):
    validate(doc)
    references = [doc["research"]] if doc["kind"] == "quick" else [i["reference"] for i in doc["adoptions"]]
    research = []
    for value in references:
        restored = store.read_revision(BlobRef(**value))
        require(restored["record"]["input"]["kind"] in ("quick", "full"), "HOSTED_RESEARCH_KIND")
        if doc["kind"] == "quick":
            require(restored["record"]["input"]["kind"] == "quick", "HOSTED_QUICK_REQUIRED")
        else:
            item = next(i for i in doc["adoptions"] if i["reference"] == value)
            require(digest(restored["body"].encode()) == item["body_sha256"], "HOSTED_ADOPTED_BODY_MISMATCH")
        research.append(restored)
    if doc["kind"] == "brief" and doc["baseline"] is not None:
        prior = _document(store, doc["baseline"])
        require(prior["kind"] == "brief" and observed_clock(prior["cutoff"]) <= observed_clock(doc["cutoff"]),
                "HOSTED_BASELINE_FROM_FUTURE_OR_WRONG_KIND")
    result = {"document": doc, "research": research, "status": "readable",
              "human_acceptance": "not-confirmed", "economic_quality": "not-certified-by-reader"}
    if doc["schema"] == WATCH_SCHEMA:
        from decision_kernel.application.watch_evidence import read_rows
        selected = [{key: item[key] for key in ("reference", "question_id", "kind", "check_ref")}
                    for item in doc["watch_adoptions"]]
        rows = read_rows(store, selected, doc["cutoff"])
        for item, row in zip(doc["watch_adoptions"], rows, strict=True):
            require(row["status"] == "readable" and row["recalculation"] == "matches"
                    and row["result_sha256"] == item["result_sha256"], "HOSTED_WATCH_RESULT_MISMATCH")
            definition = row["definition"]
            require(definition["schema"] == "odds-watch-v3", "HOSTED_FULL_WATCH_REQUIRED")
            require(row["activity"] is None or row["activity"]["action"] == "check",
                    "HOSTED_WATCH_ACTUAL_CHECK_REQUIRED")
            question = item["question_id"]
            links = definition["tracking"]["question_links"]
            if question is None:
                require(any(condition["metric"] != "evidence" for condition in definition["conditions"]),
                        "HOSTED_WATCH_QUESTION_LINK_REQUIRED")
            else:
                require(question in {link["question_id"] for link in links},
                        "HOSTED_WATCH_QUESTION_LINK_REQUIRED")
        result["watch_evidence_adoption"] = rows
    return result


def save(store, doc, submission_id):
    validate(doc); resolve(store, doc)
    artifact = ArtifactInput.from_bytes(artifact_id(doc), "handoff.json", "application/json", encoded(doc))
    def one_per_day(head):
        prefix = "research/artifacts/" + artifact.artifact_id + "/"
        # GitHubStore accepts family-level inventory prefixes, not arbitrary
        # artifact subdirectories. Keep its allowlist and filter locally.
        if any(p.startswith(prefix) and ARTIFACT_PATH.fullmatch(p)
               for p in store.backend.paths(head, "research/artifacts/")):
            raise Conflict("HOSTED_DAY_ALREADY_SAVED")
    ref = ResearchArtifacts(store).put(submission_id, artifact, before_new_write=one_per_day)
    # Canonical original text is read back, never rewritten after saving.
    opened = open_handoff(store, ref)
    return {"reference": asdict(ref), "status": opened["status"], "kind": doc["kind"]}


def open_handoff(store, ref):
    exact = artifact_reference(ref)
    return {"reference": asdict(exact), **resolve(store, _document(store, exact))}


def latest(store, *, kind, as_of, limit=3):
    require(kind in PREFIXES and type(limit) is int and 1 <= limit <= 3, "HOSTED_LATEST_INPUT")
    at = observed_clock(as_of)
    from zoneinfo import ZoneInfo
    today = at.astimezone(ZoneInfo("Asia/Singapore")).date()
    days = {(today - timedelta(days=n)).strftime("%Y%m%d") for n in range(7)}
    snapshot = store.backend.head()
    prefix = "research/artifacts/" + PREFIXES[kind]
    paths = [p for p in store.backend.paths(snapshot, "research/artifacts/") if p.startswith(prefix)
             and ARTIFACT_PATH.fullmatch(p) and p.split("/")[2].rsplit("-", 1)[-1] in days]
    require(len(paths) <= 32, "HOSTED_INVENTORY_BOUND")
    candidates, gaps = [], []
    for path in sorted(paths):
        try:
            ref = store.backend.reference(snapshot, path)
            doc = _document(store, ref)
            require(doc["kind"] == kind and observed_clock(doc["cutoff"]) <= at, "HOSTED_FUTURE_HANDOFF")
            candidates.append((doc["cutoff"], ref))
        except (RecordError, TypeError, KeyError, ValueError):
            gaps.append({"path": path, "error": "HOSTED_CANDIDATE_UNREADABLE"})
    candidates.sort(key=lambda x: observed_clock(x[0]), reverse=True)
    selected = [open_handoff(store, ref) for _, ref in candidates[:limit]]
    return {"snapshot": snapshot, "kind": kind, "as_of": as_of, "lookback_days": 7,
            "status": "partial" if gaps else "readable" if selected else "no-saved-handoff",
            "gaps": gaps, "items": selected, "inventory_count": len(paths),
            "omitted_older_candidates": max(0, len(candidates)-limit),
            "dedup_basis": "actual-saved-body-not-task-start-time", "write": "not-requested"}


def perform(store, operation, payload, submission_id):
    if operation in ("save-hosted-handoff", "reconcile-hosted-handoff"):
        require(set(payload) == {"document", "rights_confirmed"} and payload["rights_confirmed"] is True,
                "HOSTED_RIGHTS_REQUIRED")
        return save(store, payload["document"], submission_id)
    if operation == "read-hosted-handoff":
        require(set(payload) == {"reference"}, "HOSTED_EXACT_REFERENCE_REQUIRED")
        return open_handoff(store, payload["reference"])
    require(set(payload) <= {"kind", "as_of", "limit"} and {"kind", "as_of"} <= set(payload), "HOSTED_LATEST_INPUT")
    return latest(store, **payload)
