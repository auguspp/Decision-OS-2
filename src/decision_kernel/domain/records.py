"""Versioned source and research inputs. These records grant no execution authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
import re
from urllib.parse import urlsplit

MAX_BYTES = 2 * 1024 * 1024
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class RecordError(ValueError):
    """A bounded, public error code; never embeds source text or Git stderr."""


class Conflict(RecordError):
    pass


class MissingObject(RecordError):
    pass


class IntegrityError(RecordError):
    pass


class StoreUnavailable(RecordError):
    pass


class UnknownWrite(RecordError):
    """Reconcile the same submission before issuing another write."""


def require(ok: bool, code: str) -> None:
    if not ok:
        raise RecordError(code)


def identifier(value: str) -> str:
    require(isinstance(value, str) and ID.fullmatch(value) is not None, "INVALID_ID")
    return value


def checked_text(value: str, *, empty: bool = False, limit: int = MAX_BYTES) -> str:
    require(isinstance(value, str), "INVALID_TEXT")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise RecordError("INVALID_UTF8") from exc
    require((empty or bool(value.strip())) and "\0" not in value and size <= limit, "INVALID_TEXT")
    return value


def strings(values, *, limit: int = 64) -> tuple[str, ...]:
    require(isinstance(values, (tuple, list)) and len(values) <= limit, "INVALID_LIST")
    result = tuple(checked_text(v, limit=4096) for v in values)
    require(len(result) == len(set(result)), "DUPLICATE_VALUE")
    return result


def source_time(value: str | None) -> None:
    if value is None:
        return
    checked_text(value, limit=80)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value)
            require("T" in value and parsed.tzinfo is not None, "TIMEZONE_REQUIRED")
    except ValueError as exc:
        raise RecordError("INVALID_SOURCE_TIME") from exc


def encoded(value) -> bytes:
    """Deterministic bytes for intent identity, not an economic truth signature."""
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RecordError("INVALID_JSON") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class BlobRef:
    repository: str
    commit: str
    path: str
    sha256: str

    def __post_init__(self):
        checked_text(self.repository, limit=256)
        require(isinstance(self.commit, str) and bool(SHA.fullmatch(self.commit)), "INVALID_COMMIT")
        require(isinstance(self.sha256, str) and bool(DIGEST.fullmatch(self.sha256)), "INVALID_DIGEST")
        require(isinstance(self.path, str) and bool(re.fullmatch(r"[a-z0-9_./-]{1,512}", self.path))
                and not self.path.startswith("/")
                and all(p not in ("", ".", "..") for p in self.path.split("/")), "INVALID_PATH")


@dataclass(frozen=True)
class Selection:
    kind: str = "all"
    start: int | None = None
    end: int | None = None
    columns: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "columns", strings(self.columns))
        require(self.kind in ("all", "lines", "rows", "locator"), "INVALID_SELECTION")
        if self.kind in ("lines", "rows"):
            require(type(self.start) is int and type(self.end) is int
                    and 1 <= self.start <= self.end, "INVALID_RANGE")
        else:
            require(self.start is None and self.end is None, "UNEXPECTED_RANGE")
        require(self.kind == "rows" or not self.columns, "UNEXPECTED_COLUMNS")


@dataclass(frozen=True)
class SourceRef:
    manifest: BlobRef
    selection: Selection = field(default_factory=Selection)

    def __post_init__(self):
        require(isinstance(self.manifest, BlobRef) and isinstance(self.selection, Selection), "INVALID_SOURCE_REF")
        require(bool(re.fullmatch(r"source-views/[a-z0-9][a-z0-9_-]{0,95}/[a-f0-9]{64}/manifest\.json",
                                  self.manifest.path)), "INVALID_SOURCE_PATH")

    @classmethod
    def from_dict(cls, value: dict) -> SourceRef:
        require(isinstance(value, dict) and set(value) <= {"manifest", "selection"}, "INVALID_SOURCE_REF")
        try:
            return cls(BlobRef(**value["manifest"]), Selection(**value.get("selection", {})))
        except (TypeError, KeyError) as exc:
            raise RecordError("INVALID_SOURCE_REF") from exc


@dataclass(frozen=True)
class Entity:
    entity_id: str
    kind: str
    name: str
    aliases: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        identifier(self.entity_id)
        require(self.kind in ("company", "security", "commodity", "industry", "concept"), "INVALID_ENTITY_KIND")
        checked_text(self.name, limit=1024)
        object.__setattr__(self, "aliases", strings(self.aliases))
        require(isinstance(self.attributes, (tuple, list)) and len(self.attributes) <= 32, "INVALID_ATTRIBUTES")
        pairs = []
        for pair in self.attributes:
            require(isinstance(pair, (tuple, list)) and len(pair) == 2, "INVALID_ATTRIBUTES")
            pairs.append((identifier(pair[0]), checked_text(pair[1], limit=1024)))
        require(len({p[0] for p in pairs}) == len(pairs), "DUPLICATE_ATTRIBUTE")
        object.__setattr__(self, "attributes", tuple(pairs))


@dataclass(frozen=True)
class SourceInput:
    source_id: str
    kind: str
    title: str
    origin: str
    text: str | None
    custody: str = "provided"
    entities: tuple[Entity, ...] = ()
    acquired_at: str | None = None
    published_at: str | None = None
    report_period: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.source_id)
        require(self.kind in ("content", "dataset"), "INVALID_SOURCE_KIND")
        require(self.custody in ("provided", "excerpt", "external"), "INVALID_CUSTODY")
        checked_text(self.title, limit=4096)
        checked_text(self.origin, limit=4096)
        try:
            url = urlsplit(self.origin)
        except ValueError as exc:
            raise RecordError("INVALID_ORIGIN") from exc
        require(url.scheme in ("https", "http", "synthetic") and not url.username and not url.password,
                "INVALID_ORIGIN")
        require(url.scheme == "synthetic" or bool(url.netloc), "INVALID_ORIGIN")
        object.__setattr__(self, "limitations", strings(self.limitations))
        if self.custody == "external":
            require(self.text is None and bool(self.limitations), "EXTERNAL_HAS_NO_STORED_BODY")
        else:
            checked_text(self.text)
        require(self.custody == "provided" or bool(self.limitations), "LIMITATION_REQUIRED")
        require(isinstance(self.entities, (tuple, list)) and len(self.entities) <= 64
                and all(isinstance(v, Entity) for v in self.entities), "INVALID_ENTITIES")
        require(len({e.entity_id for e in self.entities}) == len(self.entities), "DUPLICATE_ENTITY")
        object.__setattr__(self, "entities", tuple(self.entities))
        source_time(self.acquired_at)
        source_time(self.published_at)
        if self.report_period is not None:
            checked_text(self.report_period, limit=128)
        require(len(encoded(asdict(self))) <= MAX_BYTES, "SOURCE_TOO_LARGE")

    @classmethod
    def from_dict(cls, value: dict) -> SourceInput:
        require(isinstance(value, dict), "INVALID_SOURCE_INPUT")
        try:
            data = dict(value)
            data["entities"] = tuple(Entity(**v) for v in data.get("entities", []))
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise RecordError("INVALID_SOURCE_INPUT") from exc


@dataclass(frozen=True)
class RevisionInput:
    case_id: str
    revision_id: str
    kind: str
    body: str
    mandate: str
    method: str
    sources: tuple[SourceRef, ...] = ()
    predecessor: BlobRef | None = None
    unresolved: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.case_id)
        identifier(self.revision_id)
        require(self.kind in ("quick", "full"), "NOT_A_RESEARCH_KIND")
        for value in (self.body, self.mandate, self.method):
            checked_text(value)
        require(isinstance(self.sources, (tuple, list)) and len(self.sources) <= 64
                and all(isinstance(s, SourceRef) for s in self.sources), "INVALID_SOURCES")
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "unresolved", strings(self.unresolved))
        require(bool(self.sources) or bool(self.unresolved), "UNSOURCED_LIMITATION_REQUIRED")
        require(self.predecessor is None or isinstance(self.predecessor, BlobRef), "INVALID_PREDECESSOR")
        require(len(encoded(asdict(self))) <= MAX_BYTES, "REVISION_TOO_LARGE")

    @classmethod
    def from_dict(cls, value: dict) -> RevisionInput:
        require(isinstance(value, dict), "INVALID_REVISION_INPUT")
        try:
            data = dict(value)
            data["sources"] = tuple(SourceRef.from_dict(v) for v in data.get("sources", []))
            data["predecessor"] = BlobRef(**data["predecessor"]) if data.get("predecessor") is not None else None
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise RecordError("INVALID_REVISION_INPUT") from exc
