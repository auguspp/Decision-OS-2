"""Read-only Quick/Full continuation, composed from the existing versioned store."""
from __future__ import annotations

from dataclasses import asdict
import re

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, SourceRef,
    StoreUnavailable, encoded, identifier, require,
)

MAX_SOURCE_PAGE_BYTES = 1024 * 1024
CURSOR_SCHEMA = "research-resume-cursor-v1"
REVISION_PATH = re.compile(
    r"research/cases/[a-z0-9][a-z0-9_-]{0,95}/revisions/"
    r"[a-z0-9][a-z0-9_-]{0,95}/record\.json\Z"
)


class ResearchReader:
    """A read is not a new mandate, completion assessment, or persistence owner."""

    def __init__(self, store: VersionedStore):
        self.store = store

    def resume(self, case_id: str | None = None, *, reference: BlobRef | None = None,
               cursor: dict | None = None, source_limit: int = 4) -> dict:
        """Choose current once, or read an exact revision; page only its own sources.

        The cursor is an explicit read locator, not an authorization or proof that
        earlier pages were read. A later call revalidates access and exact bytes.
        No current pointer, source URL, or predecessor is followed automatically
        after the initial revision selection.
        """
        require(sum(x is not None for x in (case_id, reference, cursor)) == 1,
                "ONE_RESEARCH_SELECTOR_REQUIRED")
        require(type(source_limit) is int and 1 <= source_limit <= 8,
                "INVALID_SOURCE_PAGE_LIMIT")
        offset = 0
        if cursor is not None:
            require(isinstance(cursor, dict)
                    and set(cursor) == {"schema", "reference", "source_offset"}
                    and cursor["schema"] == CURSOR_SCHEMA
                    and type(cursor["source_offset"]) is int
                    and cursor["source_offset"] >= 0,
                    "INVALID_RESEARCH_CURSOR")
            try:
                reference = BlobRef(**cursor["reference"])
            except (TypeError, RecordError):
                raise RecordError("INVALID_RESEARCH_CURSOR") from None
            offset = cursor["source_offset"]
        elif case_id is not None:
            identifier(case_id)
            reference = self.store.current(case_id)
            if reference is None:
                raise MissingObject("RESEARCH_NOT_FOUND")
        require(isinstance(reference, BlobRef)
                and REVISION_PATH.fullmatch(reference.path) is not None,
                "RESEARCH_REVISION_REFERENCE_REQUIRED")
        # Broken/denied research itself fails, rather than falling back to latest.
        research = self.store.read_revision(reference)
        payload = research["record"]["input"]
        if case_id is not None:
            require(payload["case_id"] == case_id, "RESEARCH_CASE_MISMATCH")
        sources = [SourceRef.from_dict(item) for item in payload["sources"]]
        require(offset <= len(sources), "RESEARCH_CURSOR_OUT_OF_BOUNDS")
        items, used_bytes = [], 0
        next_offset = offset
        for index in range(offset, min(offset + source_limit, len(sources))):
            item = self._source(index, sources[index])
            size = len(encoded(item))
            if size > MAX_SOURCE_PAGE_BYTES:
                # Explicit omission, with an exact locator; never truncate evidence.
                item = {"index": index, "reference": asdict(sources[index]),
                        "status": "not-included", "view": None,
                        "error": "SOURCE_VIEW_EXCEEDS_PAGE_LIMIT"}
                size = len(encoded(item))
            if items and used_bytes + size > MAX_SOURCE_PAGE_BYTES:
                break  # This source remains at the start of the next page.
            items.append(item)
            used_bytes += size
            next_offset = index + 1
        at_end = next_offset == len(sources)
        return {
            "schema": "research-context-v1", "research": research,
            "source_page": {"total": len(sources), "offset": offset,
                            "returned": len(items), "remaining": len(sources) - next_offset,
                            "end_of_list": at_end, "coverage": "this-page-only", "items": items},
            "next_cursor": None if at_end else {
                "schema": CURSOR_SCHEMA, "reference": asdict(reference), "source_offset": next_offset},
            "research_assessment": "not-performed", "execution_authority": "not-granted-by-read",
        }

    def _source(self, index: int, ref: SourceRef) -> dict:
        result = {"index": index, "reference": asdict(ref), "view": None}
        try:
            view = self.store.read_source(ref)
        except MissingObject:
            result.update(status="missing", error="SOURCE_OBJECT_MISSING")
        except StoreUnavailable:
            # Includes denied/not-found ambiguity. Do not reveal service error prose.
            result.update(status="unavailable", error="SOURCE_UNAVAILABLE")
        except IntegrityError:
            result.update(status="invalid", error="SOURCE_INTEGRITY_FAILED")
        except RecordError:
            result.update(status="invalid", error="SOURCE_VALIDATION_FAILED")
        else:
            result.update(status=view["availability"], view=view, error=None)
        return result
