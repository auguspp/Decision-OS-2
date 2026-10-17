"""Opaque research files. Names are metadata; no rendering, unpacking or execution."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import re

from decision_kernel.domain.records import (
    BlobRef, RecordError, checked_text, digest, encoded, identifier, require,
)

ARTIFACT_SCHEMA = "research-artifact-v1"
MAX_ARTIFACT_BYTES = 1024 * 1024
MEDIA_TYPES = frozenset({"text/html", "text/plain", "text/markdown", "text/csv",
    "application/json", "application/pdf", "application/octet-stream", "application/zip",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
ARTIFACT_PATH = re.compile(
    r"research/artifacts/[a-z0-9][a-z0-9_-]{0,95}/[a-f0-9]{64}/manifest\.json\Z")


@dataclass(frozen=True)
class ArtifactInput:
    artifact_id: str
    name: str
    media_type: str
    data_base64: str
    sha256: str
    size: int

    def __post_init__(self):
        identifier(self.artifact_id)
        checked_text(self.name, limit=1024)
        require(self.name not in (".", "..") and not any(
            ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in self.name), "INVALID_ARTIFACT_NAME")
        require(isinstance(self.media_type, str) and self.media_type in MEDIA_TYPES, "UNSUPPORTED_ARTIFACT_TYPE")
        require(type(self.size) is int and 0 <= self.size <= MAX_ARTIFACT_BYTES, "ARTIFACT_SIZE_LIMIT")
        require(isinstance(self.sha256, str) and re.fullmatch(r"[a-f0-9]{64}", self.sha256) is not None,
                "INVALID_ARTIFACT_DIGEST")
        require(isinstance(self.data_base64, str) and len(self.data_base64) <= 4 * ((MAX_ARTIFACT_BYTES + 2) // 3),
                "ARTIFACT_SIZE_LIMIT")
        try:
            raw = base64.b64decode(self.data_base64, validate=True)
        except (ValueError, UnicodeError):
            raise RecordError("INVALID_ARTIFACT_ENCODING") from None
        require(base64.b64encode(raw).decode("ascii") == self.data_base64, "NONCANONICAL_ARTIFACT_ENCODING")
        require(len(raw) == self.size and digest(raw) == self.sha256, "ARTIFACT_BYTES_MISMATCH")

    @classmethod
    def from_dict(cls, value: dict) -> ArtifactInput:
        require(isinstance(value, dict), "INVALID_ARTIFACT_INPUT")
        try:
            return cls(**value)
        except (TypeError, KeyError):
            raise RecordError("INVALID_ARTIFACT_INPUT") from None

    @classmethod
    def from_bytes(cls, artifact_id: str, name: str, media_type: str, raw: bytes) -> ArtifactInput:
        require(isinstance(raw, bytes) and len(raw) <= MAX_ARTIFACT_BYTES, "ARTIFACT_SIZE_LIMIT")
        return cls(artifact_id, name, media_type, base64.b64encode(raw).decode("ascii"), digest(raw), len(raw))

    def raw(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)

    def metadata(self) -> dict:
        return {"artifact_id": self.artifact_id, "name": self.name, "media_type": self.media_type,
                "sha256": self.sha256, "size": self.size}

    def path(self) -> str:
        return f"research/artifacts/{self.artifact_id}/{digest(encoded(self.metadata()))}/manifest.json"


def artifact_reference(value: dict | BlobRef) -> BlobRef:
    try:
        ref = value if isinstance(value, BlobRef) else BlobRef(**value)
    except (TypeError, KeyError):
        raise RecordError("INVALID_ARTIFACT_REFERENCE") from None
    require(ARTIFACT_PATH.fullmatch(ref.path) is not None, "INVALID_ARTIFACT_REFERENCE")
    return ref
