"""File representations owned by the existing VersionedStore transaction machinery."""
from __future__ import annotations

from dataclasses import asdict
import json

from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, encoded, require
from decision_kernel.domain.research_artifacts import ArtifactInput, ARTIFACT_SCHEMA, artifact_reference


class ResearchArtifacts:
    """Narrow companion to VersionedStore, not a second database or journal."""
    def __init__(self, store):
        self.store = store

    def put(self, submission_id: str, artifact: ArtifactInput) -> BlobRef:
        require(isinstance(artifact, ArtifactInput), "INVALID_ARTIFACT_INPUT")
        head = self.store.backend.head()
        intent = {"operation": "research-artifact", "input": asdict(artifact)}
        replay = self.store._replay(head, submission_id, intent)
        if replay is not None:
            self.read(replay)
            return replay
        path = artifact.path()
        content_path = path.rsplit("/", 1)[0] + "/content.bin"
        manifest = {"schema": ARTIFACT_SCHEMA, "input": artifact.metadata(), "data_path": content_path}
        files = {path: encoded(manifest), content_path: artifact.raw()}
        try:
            existing = self.store.backend.reference(head, path)
        except MissingObject:
            pass
        else:
            self.read(existing)  # Corruption is not repaired by publishing another receipt.
            files = {}
        ref = self.store._save(head, submission_id, intent, path, files)
        self.read(ref)
        return ref

    def read(self, reference: BlobRef | dict, *, include_data: bool = True) -> dict:
        ref = artifact_reference(reference)
        raw_manifest = self.store.backend.resolve(ref)
        try:
            manifest = json.loads(raw_manifest)
            require(isinstance(manifest, dict) and set(manifest) == {"schema", "input", "data_path"}
                    and manifest["schema"] == ARTIFACT_SCHEMA and encoded(manifest) == raw_manifest,
                    "INVALID_ARTIFACT_MANIFEST")
            path = ref.path.rsplit("/", 1)[0] + "/content.bin"
            require(manifest["data_path"] == path, "ARTIFACT_PATH_MISMATCH")
            meta = manifest["input"]
            raw = self.store.backend.resolve(BlobRef(ref.repository, ref.commit, path, meta["sha256"]))
            artifact = ArtifactInput.from_bytes(meta["artifact_id"], meta["name"], meta["media_type"], raw)
            require(encoded(artifact.metadata()) == encoded(meta) and artifact.path() == ref.path, "ARTIFACT_MANIFEST_MISMATCH")
        except (TypeError, KeyError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError("INVALID_ARTIFACT_MANIFEST") from None
        result = {"reference": asdict(ref), "metadata": meta, "bytes_verified": True,
                  "content_assessment": "not-performed", "executed": False}
        if include_data:
            result["data_base64"] = artifact.data_base64
        return result
