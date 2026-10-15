"""Bounded reception, exact reads and research revision commits on one Git owner."""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import unicodedata

from decision_kernel.domain.records import (
    BlobRef, Conflict, IntegrityError, MissingObject, RecordError, RevisionInput,
    Selection, SourceInput, SourceRef, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.application.store_backend import StoreBackend

MAX_QUERY_VERSIONS = 1024


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError) as exc:
        raise IntegrityError("STORED_JSON_INVALID") from exc


def _table(text: str) -> tuple[list[str], list[list[str]]]:
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise RecordError("INVALID_CSV") from exc
    require(bool(rows) and 0 < len(rows[0]) <= 128 and all(rows[0])
            and len(rows[0]) == len(set(rows[0])), "INVALID_CSV_HEADER")
    require(all(len(row) == len(rows[0]) for row in rows[1:]), "INVALID_CSV_WIDTH")
    return rows[0], rows[1:]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


class VersionedStore:
    """No source acquisition, model calls, automatic decisions or deletion methods."""

    def __init__(self, backend: StoreBackend):
        self.backend = backend

    def _replay(self, head: str, submission_id: str, intent: dict) -> BlobRef | None:
        journal = "submissions/" + identifier(submission_id) + ".json"
        try:
            previous = _json(self.backend.read(head, journal))
        except MissingObject:
            return None
        if previous.get("intent_sha256") != digest(encoded(intent)):
            raise Conflict("SUBMISSION_CONTENT_CONFLICT")
        require(previous.get("schema") == "submission-v1", "UNSUPPORTED_SUBMISSION")
        original = self.backend.last_change(head, journal)
        return self.backend.reference(original, previous["result_path"])

    def _save(self, head: str, submission_id: str, intent: dict, path: str,
              files: dict[str, bytes]) -> BlobRef:
        journal = "submissions/" + identifier(submission_id) + ".json"
        files[journal] = encoded({"schema": "submission-v1", "intent_sha256": digest(encoded(intent)),
                                  "result_path": path})
        commit = self.backend.commit(files, expected=head)
        # No success receipt before the returned version is actually readable.
        return self.backend.reference(commit, path)

    def put_source(self, submission_id: str, source: SourceInput) -> SourceRef:
        require(isinstance(source, SourceInput), "INVALID_SOURCE_INPUT")
        head = self.backend.head()
        payload = asdict(source)
        intent = {"operation": "source", "input": asdict(source)}
        replay = self._replay(head, submission_id, intent)
        selection = Selection("locator" if source.custody == "external" else "all")
        if replay is not None:
            ref = SourceRef(replay, selection)
            self.read_source(ref)
            return ref
        if source.kind == "dataset" and source.text is not None:
            _table(source.text)
        version = digest(encoded(payload))
        prefix = f"source-views/{source.source_id}/{version}"
        path = prefix + "/manifest.json"
        identity_path = f"source-views/{source.source_id}/identity.json"
        identity = encoded({"source_id": source.source_id, "kind": source.kind})
        files = {}
        try:
            if self.backend.read(head, identity_path) != identity:
                raise Conflict("SOURCE_KIND_CONFLICT")
        except MissingObject:
            files[identity_path] = identity
        try:
            self.backend.read(head, path)  # Identical versions retain the first stored metadata.
        except MissingObject:
            data = None
            if source.text is not None:
                data_path = prefix + ("/data.csv" if source.kind == "dataset" else "/content.txt")
                raw = source.text.encode("utf-8")
                files[data_path] = raw
                data = {"path": data_path, "sha256": digest(raw), "size": len(raw)}
            payload.pop("text")
            files[path] = encoded({"schema": "source-version-v1", "version_id": version,
                                   "input": payload, "representation": data, "stored_at": _now()})
        else:
            # Missing/corrupt bytes beneath an existing manifest are not a new source.
            self.read_source(SourceRef(self.backend.reference(head, path), selection))
        ref = SourceRef(self._save(head, submission_id, intent, path, files), selection)
        self.read_source(ref)
        return ref

    def read_source(self, ref: SourceRef) -> dict:
        require(isinstance(ref, SourceRef), "INVALID_SOURCE_REF")
        manifest = _json(self.backend.resolve(ref.manifest))
        try:
            require(manifest["schema"] == "source-version-v1", "UNSUPPORTED_SOURCE_SCHEMA")
            payload = dict(manifest["input"])
            representation = manifest["representation"]
            payload["text"] = None
            if representation is not None:
                prefix = ref.manifest.path.rsplit("/", 1)[0]
                expected = prefix + ("/data.csv" if payload["kind"] == "dataset" else "/content.txt")
                require(representation["path"] == expected, "REPRESENTATION_PATH_MISMATCH")
                data_ref = BlobRef(ref.manifest.repository, ref.manifest.commit, expected, representation["sha256"])
                raw = self.backend.resolve(data_ref)
                require(len(raw) == representation["size"], "REPRESENTATION_SIZE_MISMATCH")
                payload["text"] = raw.decode("utf-8")
            source = SourceInput.from_dict(payload)
            expected_version = digest(encoded(asdict(source)))
            expected_path = f"source-views/{source.source_id}/{expected_version}/manifest.json"
            require(manifest["version_id"] == expected_version and ref.manifest.path == expected_path,
                    "SOURCE_VERSION_MISMATCH")
        except (KeyError, TypeError, UnicodeError) as exc:
            raise IntegrityError("SOURCE_MANIFEST_INVALID") from exc
        selected = self._select(source, ref.selection)
        return {"reference": asdict(ref), "manifest": manifest, "data": selected,
                "availability": "external-only" if source.custody == "external" else "stored-representation",
                "original_verified": False, "limitations": list(source.limitations)}

    @staticmethod
    def _select(source: SourceInput, selection: Selection):
        if source.text is None:
            require(selection.kind == "locator", "BODY_NOT_STORED")
            return None
        require(selection.kind != "locator", "UNEXPECTED_LOCATOR_SELECTION")
        if source.kind == "dataset":
            require(selection.kind in ("all", "rows"), "DATASET_ROWS_REQUIRED")
            header, rows = _table(source.text)
            columns = selection.columns or tuple(header)
            require(all(c in header for c in columns), "COLUMN_MISSING")
            if selection.kind == "rows":
                require(selection.end <= len(rows), "RANGE_OUT_OF_BOUNDS")
                rows = rows[selection.start - 1:selection.end]
            return [{c: row[header.index(c)] for c in columns} for row in rows]
        require(selection.kind in ("all", "lines"), "CONTENT_LINES_REQUIRED")
        if selection.kind == "all":
            return source.text
        lines = source.text.splitlines(keepends=True)
        require(selection.end <= len(lines), "RANGE_OUT_OF_BOUNDS")
        return "".join(lines[selection.start - 1:selection.end])

    def current(self, case_id: str, *, snapshot: str | None = None) -> BlobRef | None:
        identifier(case_id)
        head = snapshot or self.backend.head()
        self.backend.check_commit(head)
        try:
            pointer = _json(self.backend.read(head, f"research/cases/{case_id}/current.json"))
        except MissingObject:
            return None
        path = pointer.get("revision_path")
        require(isinstance(path, str) and path.startswith(f"research/cases/{case_id}/revisions/")
                and path.endswith("/record.json"), "INVALID_CURRENT_POINTER")
        original = self.backend.last_change(head, path)
        return self.backend.reference(original, path)

    def submit_revision(self, submission_id: str, revision: RevisionInput) -> BlobRef:
        require(isinstance(revision, RevisionInput), "INVALID_REVISION_INPUT")
        head = self.backend.head()
        payload = asdict(revision)
        intent = {"operation": "research", "input": asdict(revision)}
        replay = self._replay(head, submission_id, intent)
        if replay is not None:
            self.read_revision(replay)
            return replay
        current = self.current(revision.case_id, snapshot=head)
        if revision.predecessor != current:
            raise Conflict("STALE_PREDECESSOR")
        if current is not None:
            self.read_revision(current)
        for ref in revision.sources:
            self.read_source(ref)  # Exact stored bytes and valid selection, before accepting references.
        prefix = f"research/cases/{revision.case_id}/revisions/{revision.revision_id}"
        path = prefix + "/record.json"
        try:
            self.backend.read(head, path)
        except MissingObject:
            pass
        else:
            raise Conflict("REVISION_ALREADY_EXISTS")
        raw = payload.pop("body").encode("utf-8")
        body_path = prefix + "/research.md"
        record = {"schema": "research-revision-v1", "input": payload,
                  "body": {"path": body_path, "sha256": digest(raw), "size": len(raw)},
                  "stored_at": _now(), "human_acceptance": "not-confirmed"}
        files = {path: encoded(record), body_path: raw,
                 f"research/cases/{revision.case_id}/current.json": encoded({"revision_path": path})}
        # Retention is the immutable SourceRef list in every revision. No deletion/GC is exposed.
        result = self._save(head, submission_id, intent, path, files)
        self.read_revision(result)
        return result

    def read_revision(self, ref: BlobRef) -> dict:
        record = _json(self.backend.resolve(ref))
        try:
            require(record["schema"] == "research-revision-v1" and record["human_acceptance"] == "not-confirmed",
                    "UNSUPPORTED_RESEARCH_RECORD")
            payload = dict(record["input"])
            prefix = f"research/cases/{identifier(payload['case_id'])}/revisions/{identifier(payload['revision_id'])}"
            require(ref.path == prefix + "/record.json" and record["body"]["path"] == prefix + "/research.md",
                    "REVISION_PATH_MISMATCH")
            raw = self.backend.resolve(BlobRef(ref.repository, ref.commit, record["body"]["path"], record["body"]["sha256"]))
            require(len(raw) == record["body"]["size"], "RESEARCH_SIZE_MISMATCH")
            payload["body"] = raw.decode("utf-8")
            RevisionInput.from_dict(payload)
        except (KeyError, TypeError, UnicodeError) as exc:
            raise IntegrityError("RESEARCH_RECORD_INVALID") from exc
        return {"reference": asdict(ref), "record": record, "body": payload["body"]}

    def search(self, query: str, *, entity_id: str | None = None, limit: int = 20,
               cursor: dict | None = None) -> dict:
        checked_text(query, empty=True, limit=1024)
        require(type(limit) is int and 1 <= limit <= 100, "INVALID_PAGE_LIMIT")
        if entity_id is not None:
            identifier(entity_id)
        fingerprint = digest(encoded({"query": _fold(query), "entity_id": entity_id}))
        snapshot, offset = self.backend.head(), 0
        if cursor is not None:
            require(isinstance(cursor, dict) and set(cursor) == {"snapshot", "offset", "query_sha256"}
                    and cursor["query_sha256"] == fingerprint and type(cursor["offset"]) is int
                    and cursor["offset"] >= 0, "INVALID_CURSOR")
            snapshot, offset = cursor["snapshot"], cursor["offset"]
        self.backend.check_commit(snapshot)
        paths = [p for p in self.backend.paths(snapshot, "source-views/") if p.endswith("/manifest.json")]
        require(len(paths) <= MAX_QUERY_VERSIONS, "QUERY_VERSION_LIMIT_EXCEEDED")
        hits = []
        for path in sorted(paths):
            manifest_ref = self.backend.reference(snapshot, path)
            manifest = _json(self.backend.resolve(manifest_ref))
            selection = Selection("locator" if manifest.get("input", {}).get("custody") == "external" else "all")
            source_ref = SourceRef(manifest_ref, selection)
            view = self.read_source(source_ref)
            data = view["manifest"]["input"]
            if entity_id and not any(e["entity_id"] == entity_id for e in data["entities"]):
                continue
            values = [data["source_id"], data["title"], data["origin"], *data["limitations"]]
            values.extend(v for v in (data["report_period"], data["published_at"], data["acquired_at"]) if v)
            for entity in data["entities"]:
                values.extend([entity["entity_id"], entity["name"], *entity["aliases"]])
                values.extend(v for _, v in entity["attributes"])
            body = view["data"]
            if isinstance(body, str):
                values.append(body)
            elif isinstance(body, list):
                values.extend(value for row in body for value in row.values())
            searchable = "\n".join(values)
            if _fold(query) in _fold(searchable):
                hits.append({"reference": asdict(source_ref), "source_id": data["source_id"],
                             "title": data["title"], "kind": data["kind"], "entities": data["entities"],
                             "custody": data["custody"], "limitations": data["limitations"]})
        require(offset <= len(hits), "CURSOR_OUT_OF_BOUNDS")
        end = offset + limit
        return {"snapshot": snapshot, "version_scope": "all-stored-versions", "total": len(hits),
                "items": hits[offset:end], "next_cursor": None if end >= len(hits) else
                {"snapshot": snapshot, "offset": end, "query_sha256": fingerprint}}
