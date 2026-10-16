"""Bounded delivery composition: existing source/artifact custody then original checkpoint.

A multi-commit delivery is resumable, NOT an atomic transaction. The original
intent is reserved first using the same store journal. No background execution.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import base64

from decision_kernel.application.numerical_audit import NumericalAudit, START as NUMBERS_START
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.domain.records import (
    BlobRef, MAX_BYTES, MissingObject, RecordError, SourceInput, SourceRef, checked_text, digest,
    encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest, read_workpaper, continue_request
from decision_kernel.application.research_continuation import continuation_plan
from decision_kernel.methods.research import research_method

SCHEMA = "research-delivery-v1"
MANIFEST_SCHEMA = "research-delivery-manifest-v1"
START = "\n\n<!-- decision-os-delivery-v1:start -->\n```json\n"
END = "```\n<!-- decision-os-delivery-v1:end -->\n"
ROLES = {"report-original", "calculation-file", "source-evidence", "supporting-file"}
REQUIRED_ROLES = ROLES - {"supporting-file"}
OMISSIONS = {"missing", "rights-excluded", "unsupported", "not-applicable"}
CONTENT_KEYS = {"artifact", "artifact_ref", "source", "source_ref"}


def stage_key(submission_id: str, stage: str) -> str:
    identifier(submission_id)
    return "delivery-" + digest(encoded([submission_id, stage]))


def _shape(value, keys, optional=()):
    require(isinstance(value, dict) and set(keys) <= set(value) <= set(keys) | set(optional),
            "INVALID_DELIVERY_SHAPE")


def delivery_manifest(body: str) -> dict | None:
    if START not in body and END not in body:
        return None
    require(body.count(START) == body.count(END) == 1, "INVALID_DELIVERY_MANIFEST")
    first, last = body.index(START) + len(START), body.index(END)
    require(first < last, "INVALID_DELIVERY_MANIFEST")
    raw = body[first:last].encode("utf-8")
    require(len(raw) <= MAX_BYTES, "DELIVERY_SIZE_LIMIT")
    try:
        manifest = json.loads(raw)
        _shape(manifest, {"schema", "input_sha256", "intent_reference", "body_mode", "base_revision", "items"})
        require(encoded(manifest) == raw and manifest["schema"] == MANIFEST_SCHEMA,
                "INVALID_DELIVERY_MANIFEST")
        return manifest
    except (ValueError, TypeError, RecursionError):
        raise RecordError("INVALID_DELIVERY_MANIFEST") from None


def validate_delivery(payload: dict) -> tuple[ResearchRequest, list[dict]]:
    _shape(payload, {"schema", "request", "progress", "revision_id", "method_sha256",
                     "rights_confirmed", "rights_basis", "items", "body_mode"}, {"numerical_workpaper"})
    require(payload["schema"] == SCHEMA and len(encoded(payload)) <= MAX_BYTES, "DELIVERY_SIZE_LIMIT")
    require(payload["rights_confirmed"] is True, "DELIVERY_RIGHTS_NOT_CONFIRMED")
    checked_text(payload["rights_basis"], limit=4096)  # An attestation, not authentication.
    identifier(payload["revision_id"])
    request = ResearchRequest.from_dict(payload["request"])
    research_method(request.kind, sha256=payload["method_sha256"])
    require(payload["body_mode"] in ("snapshot", "delta") and
            (payload["body_mode"] != "delta" or request.predecessor is not None), "DELTA_BASE_REQUIRED")
    require(isinstance(payload["progress"], dict), "INVALID_RESEARCH_PROGRESS")
    body = checked_text(payload["progress"].get("body"))
    require(START.strip() not in body and END.strip() not in body, "RESERVED_DELIVERY_MARKER")
    if "numerical_workpaper" in payload:
        numeric = payload["numerical_workpaper"]
        _shape(numeric, {"schema", "inputs", "calculations"}, {"template"})
        require(numeric["schema"] == "research-numbers-v1" and NUMBERS_START not in body,
                "INVALID_DELIVERY_NUMERICAL_WORKPAPER")
    items = payload["items"]
    require(isinstance(items, list) and 1 <= len(items) <= 32, "DELIVERY_ITEM_LIMIT")
    seen, roles = set(), set()
    for item in items:
        _shape(item, {"id", "role", "status", "required", "reason"}, CONTENT_KEYS)
        identifier(item["id"])
        require(item["id"] not in seen, "DUPLICATE_DELIVERY_ITEM")
        seen.add(item["id"])
        require(isinstance(item["role"], str) and item["role"] in ROLES, "INVALID_DELIVERY_ROLE")
        roles.add(item["role"])
        require(type(item["required"]) is bool, "INVALID_DELIVERY_REQUIRED")
        checked_text(item["reason"], empty=item["status"] == "include", limit=4096)
        content = set(item) & CONTENT_KEYS
        if item["status"] == "include":
            require(len(content) == 1, "ONE_DELIVERY_REPRESENTATION_REQUIRED")
            key = next(iter(content))
            if key == "artifact":
                ArtifactInput.from_dict(item[key])
            elif key == "artifact_ref":
                artifact_reference(item[key])
            elif key == "source":
                SourceInput.from_dict(item[key])
            else:
                SourceRef.from_dict(item[key])
            require((item["role"] == "source-evidence") == key.startswith("source"),
                    "DELIVERY_ROLE_REPRESENTATION_MISMATCH")
        else:
            require(isinstance(item["status"], str) and item["status"] in OMISSIONS and not content,
                    "OMITTED_DELIVERY_MUST_NOT_CONTAIN_CONTENT")
            require(item["status"] != "not-applicable" or not item["required"],
                    "REQUIRED_ITEM_CANNOT_BE_NOT_APPLICABLE")
    require(REQUIRED_ROLES <= roles, "DELIVERY_ROLE_ACCOUNTING_REQUIRED")
    # Shape-check original progress without inventing SourceRefs for not-yet-saved
    # sources. Validate its indices against the planned, ordered source list.
    progress = dict(payload["progress"])
    existing = progress.get("sources", [])
    require(isinstance(existing, list), "INVALID_SOURCES")
    known = {encoded(asdict(SourceRef.from_dict(s))) for s in existing}
    known.update(encoded(asdict(SourceRef.from_dict(i["source_ref"]))) for i in items
                 if i["status"] == "include" and "source_ref" in i)
    pending = sum(i["status"] == "include" and "source" in i for i in items)
    source_ceiling = len(known) + pending
    require(source_ceiling <= 64, "INVALID_SOURCES")
    for name in ("findings", "challenges"):
        rows = progress.get(name, [])
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "INVALID_RESEARCH_PROGRESS")
        for row in rows:
            indices = row.get("evidence", [])
            require(isinstance(indices, list) and all(type(i) is int and 0 <= i < source_ceiling for i in indices)
                    and len(indices) == len(set(indices)),
                    "EVIDENCE_INDEX_OUT_OF_BOUNDS")
        progress[name] = [{**row, "evidence": []} for row in rows]
    if source_ceiling and not existing and not progress.get("limitations"):
        progress["limitations"] = ["Preflight only: source identities do not exist until custody succeeds."]
    checked = ResearchProgress.from_dict(progress)
    checked.validate_for(request)
    require(not (checked.stop == "concluded" and any(f.next_action.strip() for f in checked.findings)),
            "DECLARED_FOLLOWUP_PREVENTS_CONCLUSION")
    return request, items


class ResearchDelivery:
    """Explicit orchestration. The host supplies publication authority and fresh reads."""
    def __init__(self, store):
        self.store = store
        self.artifacts = ResearchArtifacts(store)

    def deliver(self, payload: dict, *, submission_id: str) -> dict:
        request, items = validate_delivery(payload)
        identifier(submission_id)
        previous = self.store.read_revision(request.predecessor) if request.predecessor else None
        ResearchWorkflow._continuity(request, previous)
        previous_manifest = delivery_manifest(previous["body"]) if previous else None
        if previous_manifest:
            require({i["id"] for i in previous_manifest["items"]} <= {i["id"] for i in items},
                    "PREVIOUS_DELIVERY_ITEMS_MUST_BE_ACCOUNTED")
        payload = json.loads(encoded(payload))
        intent_sha = digest(encoded(payload))
        # Reserving the whole input digest first prevents a changed partial retry
        # from mixing one delivery's sources with another delivery's report.
        reservation = ArtifactInput.from_bytes(stage_key(submission_id, "intent"), "delivery-intent.json",
            "application/json", encoded({"schema": SCHEMA, "input_sha256": intent_sha,
                                         "submission_id": submission_id}))
        intent_ref = self.artifacts.put(stage_key(submission_id, "intent"), reservation)
        completed = self._completed(payload, submission_id, intent_ref, intent_sha)
        if completed is not None:
            return completed
        sources = list(payload["progress"].get("sources", []))
        saved_items = []
        for item in items:
            row = {k: item[k] for k in ("id", "role", "status", "required", "reason")}
            if item["status"] == "include":
                key = next(iter(set(item) & CONTENT_KEYS))
                stage = stage_key(submission_id, "item:" + item["id"])
                if key.startswith("source"):
                    ref = (self.store.put_source(stage, SourceInput.from_dict(item[key])) if key == "source"
                           else SourceRef.from_dict(item[key]))
                    self.store.read_source(ref)
                    raw_ref = asdict(ref)
                    if raw_ref not in sources:
                        sources.append(raw_ref)
                    row.update(kind="source", reference=raw_ref, source_index=sources.index(raw_ref))
                else:
                    ref = (self.artifacts.put(stage, ArtifactInput.from_dict(item[key])) if key == "artifact"
                           else artifact_reference(item[key]))
                    view = self.artifacts.read(ref, include_data=False)
                    row.update(kind="artifact", reference=asdict(ref), metadata=view["metadata"])
                row["status"] = "retained"
            saved_items.append(row)
        manifest = {"schema": MANIFEST_SCHEMA, "input_sha256": intent_sha,
                    "intent_reference": asdict(intent_ref), "body_mode": payload["body_mode"],
                    "base_revision": asdict(request.predecessor) if payload["body_mode"] == "delta" else None,
                    "items": saved_items}
        body = payload["progress"]["body"]
        if "numerical_workpaper" in payload:
            audit = NumericalAudit(self.store).audit({**payload["numerical_workpaper"], "sources": sources})
            body += audit["appendix"]
        body += START + encoded(manifest).decode("utf-8") + END
        progress = ResearchProgress.from_dict({**payload["progress"], "body": body, "sources": sources})
        result = ResearchWorkflow(self.store).checkpoint(request, progress,
            submission_id=stage_key(submission_id, "revision"), revision_id=payload["revision_id"],
            method_sha256=payload["method_sha256"])
        # Fresh-reader verification is performed by the caller after this receipt.
        result.setdefault("numerical_audit", {"status": "not-declared"})
        return {**result, "delivery_input_sha256": intent_sha, "intent_reference": asdict(intent_ref),
                "delivery_manifest_sha256": digest(encoded(manifest)),
                "readback": "requires-fresh-reader", "multi_commit": True}

    def _completed(self, payload: dict, submission_id: str, intent_ref: BlobRef, intent_sha: str) -> dict | None:
        # Recover the actual saved revision, not a newly rendered approximation.
        # A future numeric checker can change its assessment without changing old bytes.
        backend = self.store.backend
        head = backend.head()
        journal_path = "submissions/" + stage_key(submission_id, "revision") + ".json"
        try:
            raw = backend.read(head, journal_path)
        except MissingObject:
            return None
        try:
            journal = json.loads(raw)
            expected_path = f"research/cases/{payload['request']['case_id']}/revisions/{payload['revision_id']}/record.json"
            require(journal["schema"] == "submission-v1" and journal["result_path"] == expected_path,
                    "DELIVERY_RECEIPT_MISMATCH")
            commit = backend.last_change(head, journal_path)
            ref = backend.reference(commit, expected_path)
            saved = self.store.read_revision(ref)
            stored_input = {**saved["record"]["input"], "body": saved["body"]}
            require(journal["intent_sha256"] == digest(encoded({"operation": "research", "input": stored_input})),
                    "DELIVERY_RECEIPT_MISMATCH")
            manifest = delivery_manifest(saved["body"])
            require(manifest is not None and manifest["input_sha256"] == intent_sha
                    and manifest["intent_reference"] == asdict(intent_ref), "DELIVERY_RECEIPT_MISMATCH")
            work = read_workpaper(saved)
            numerical = {"status": "not-declared"}
            if NUMBERS_START in saved["body"]:
                try:
                    audited = NumericalAudit(self.store).saved(ref)
                    numerical = {"status": audited["status"], "saved_render": audited.get("saved_render")}
                except RecordError:
                    numerical = {"status": "blocked", "error": "NUMERICAL_ASSESSMENT_UNAVAILABLE"}
            return {"reference": asdict(ref), "declared_stop": work[1].stop if work else None,
                    "research_assessment": "not-performed", "human_acceptance": "not-confirmed",
                    "execution_authority": "not-granted-by-save", "numerical_audit": numerical,
                    "delivery_input_sha256": intent_sha, "intent_reference": asdict(intent_ref),
                    "delivery_manifest_sha256": digest(encoded(manifest)),
                    "readback": "requires-fresh-reader", "multi_commit": True}
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError("DELIVERY_RECEIPT_MISMATCH") from None

    def verify(self, reference: BlobRef) -> dict:
        research = self.store.read_revision(reference)
        manifest = delivery_manifest(research["body"])
        if manifest is None:
            return {"status": "not-declared", "reference": asdict(reference),
                    "archive_scope": "narrative-only-no-inventory-proof", "research_assessment": "not-performed"}
        require(manifest["body_mode"] in ("snapshot", "delta"), "INVALID_DELIVERY_MANIFEST")
        pred = research["record"]["input"]["predecessor"]
        require((manifest["body_mode"] == "snapshot" and manifest["base_revision"] is None) or
                (manifest["body_mode"] == "delta" and pred is not None and manifest["base_revision"] == pred),
                "DELIVERY_BASE_MISMATCH")
        items = manifest["items"]
        require(isinstance(items, list) and 1 <= len(items) <= 32, "INVALID_DELIVERY_MANIFEST")
        result = {"reference": asdict(reference), "status": "verified-declared-inventory",
                  "archive_scope": "declared-inventory-only", "body_mode": manifest["body_mode"],
                  "base_revision": manifest["base_revision"], "items": [], "errors": [],
                  "research_assessment": "not-performed", "human_acceptance": "not-confirmed",
                  "source_authenticity": "not-certified", "persistence": "not-written"}
        try:
            intent = self.artifacts.read(manifest["intent_reference"])
            reservation = json.loads(base64.b64decode(intent["data_base64"], validate=True))
            require(reservation["schema"] == SCHEMA and reservation["input_sha256"] == manifest["input_sha256"],
                    "DELIVERY_INTENT_MISMATCH")
        except (RecordError, KeyError, TypeError, ValueError):
            result["errors"].append("DELIVERY_INTENT_NOT_VERIFIED")
        if manifest["body_mode"] == "delta":
            try:
                base = self.store.read_revision(BlobRef(**manifest["base_revision"]))
                require(base["record"]["input"]["case_id"] == research["record"]["input"]["case_id"],
                        "DELIVERY_BASE_CASE_MISMATCH")
            except (RecordError, KeyError, TypeError):
                result["errors"].append("DELIVERY_BASE_NOT_VERIFIED")
        source_refs = research["record"]["input"]["sources"]
        seen, roles = set(), set()
        for item in items:
            require(isinstance(item, dict) and {"id", "role", "status", "required", "reason"} <= set(item),
                    "INVALID_DELIVERY_MANIFEST")
            identifier(item["id"])
            require(item["id"] not in seen and isinstance(item["role"], str) and item["role"] in ROLES
                    and isinstance(item["status"], str) and type(item["required"]) is bool,
                    "INVALID_DELIVERY_MANIFEST")
            seen.add(item["id"]); roles.add(item["role"])
            row = dict(item)
            row["readback"] = "not-retained"
            if item["status"] == "retained":
                try:
                    if item.get("kind") == "source":
                        index = item["source_index"]
                        require(type(index) is int and 0 <= index < len(source_refs)
                                and source_refs[index] == item["reference"], "DELIVERY_SOURCE_NOT_RETAINED")
                        view = self.store.read_source(SourceRef.from_dict(item["reference"]))
                        row.update(readback="verified", representation=view["availability"],
                                   custody=view["manifest"]["input"]["custody"], limitations=view["limitations"])
                    else:
                        require(item.get("kind") == "artifact", "INVALID_DELIVERY_ITEM_KIND")
                        view = self.artifacts.read(item["reference"], include_data=False)
                        require(encoded(view["metadata"]) == encoded(item["metadata"]), "DELIVERY_ARTIFACT_MISMATCH")
                        row["readback"] = "verified"
                except (RecordError, KeyError, TypeError) as exc:
                    row.update(readback="failed", error=str(exc) if isinstance(exc, RecordError) else "INVALID_DELIVERY_ITEM")
                    result["errors"].append("DELIVERY_ITEM_NOT_VERIFIED:" + item["id"])
            else:
                require(item["status"] in OMISSIONS and not (set(item) & {"reference", "metadata", "source_index", *CONTENT_KEYS}),
                        "INVALID_DELIVERY_OMISSION")
                checked_text(item["reason"], limit=4096)
                if item["status"] != "not-applicable" or item["required"]:
                    result["status"] = "partial"
            result["items"].append(row)
        require(REQUIRED_ROLES <= roles, "DELIVERY_ROLE_ACCOUNTING_REQUIRED")
        # Also verify any inherited source not represented by a new inventory item.
        result["sources"] = []
        for raw in source_refs:
            try:
                view = self.store.read_source(SourceRef.from_dict(raw))
                result["sources"].append({"reference": raw, "status": view["availability"],
                    "custody": view["manifest"]["input"]["custody"], "limitations": view["limitations"]})
            except RecordError:
                result["sources"].append({"reference": raw, "status": "unavailable"})
                result["errors"].append("RETAINED_SOURCE_NOT_VERIFIED")
        if result["errors"]:
            result["status"] = "failed"
        return result

    def open(self, reference: BlobRef, *, history_limit: int = 2) -> dict:
        """Open an exact latest version and a bounded predecessor page, never infer snapshot semantics."""
        require(type(history_limit) is int and 1 <= history_limit <= 4, "INVALID_HISTORY_LIMIT")
        context = ResearchReader(self.store).resume(reference=reference)
        case_id = context["research"]["record"]["input"]["case_id"]
        next_ref = context["research"]["record"]["input"]["predecessor"]
        history, seen, budget = [], {encoded(asdict(reference))}, 2 * MAX_BYTES
        for _ in range(history_limit):
            if next_ref is None:
                break
            raw_ref = next_ref
            try:
                marker = encoded(raw_ref)
                require(marker not in seen, "RESEARCH_HISTORY_CYCLE")
                seen.add(marker)
                old = self.store.read_revision(BlobRef(**raw_ref))
                require(old["record"]["input"]["case_id"] == case_id, "RESEARCH_HISTORY_CASE_MISMATCH")
                size = len(encoded(old))
                if size > budget:
                    break
                history.append({"status": "read", "research": old})
                budget -= size
                next_ref = old["record"]["input"]["predecessor"]
            except (RecordError, KeyError, TypeError) as exc:
                history.append({"status": "unavailable", "reference": raw_ref,
                                "error": str(exc) if isinstance(exc, RecordError) else "INVALID_HISTORY"})
                break
        work = read_workpaper(context["research"])
        request = continue_request(context["research"], reference) if work is not None else None
        return {"context": context, "delivery": self.verify(reference), "history": history,
                "request": asdict(request) if request is not None else None,
                "method": research_method(request.kind) if request is not None else None,
                "continuation": continuation_plan(request, context) if request is not None else None,
                "next_history_reference": next_ref, "history_coverage": "this-page-only",
                "history_semantics": "predecessors-not-automatically-current-conclusions",
                "research_assessment": "not-performed", "persistence": "not-written"}


def verify_fresh(receipt: dict, reader_factory) -> dict:
    """A new reader/cache after save. Never erase a known save because readback failed."""
    result = dict(receipt)
    try:
        check = ResearchDelivery(reader_factory()).verify(BlobRef(**receipt["reference"]))
        result["delivery_verification"] = check
        result["readback"] = "fresh-reader-verified" if check["status"] == "verified-declared-inventory" else "fresh-reader-not-complete"
    except (RecordError, OSError):
        result["readback"] = "failed"
        result["delivery_verification"] = {"status": "failed", "error": "FRESH_DELIVERY_READBACK_FAILED"}
    return result
