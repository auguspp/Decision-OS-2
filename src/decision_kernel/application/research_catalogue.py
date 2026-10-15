"""Snapshot-paged research reading over existing records; no index or generated summary."""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import TYPE_CHECKING

from decision_kernel.domain.records import (
    IntegrityError, MissingObject, RecordError, SHA, StoreUnavailable, encoded, require,
)

if TYPE_CHECKING:
    from decision_kernel.application.versioned_store import VersionedStore

CURSOR_SCHEMA = "research-catalogue-cursor-v1"
CASE_PATH = re.compile(r"research/cases/([a-z0-9][a-z0-9_-]{0,95})/")
MAX_CASES = 8192
MAX_CARD_BYTES = 1024 * 1024


class ResearchCatalogue:
    """Read only. A catalogue lists stored cases, not every task that ever executed."""

    def __init__(self, store: VersionedStore):
        self.store = store

    def page(self, *, cursor: dict | None = None, limit: int = 4) -> dict:
        require(type(limit) is int and 1 <= limit <= 8, "INVALID_RESEARCH_CATALOGUE_LIMIT")
        backend = self.store.backend
        offset = 0
        if cursor is None:
            snapshot = backend.head()
        else:
            require(isinstance(cursor, dict)
                    and set(cursor) == {"schema", "repository", "snapshot", "offset"}
                    and cursor["schema"] == CURSOR_SCHEMA
                    and cursor["repository"] == backend.repository
                    and type(cursor["offset"]) is int and cursor["offset"] >= 0,
                    "INVALID_RESEARCH_CATALOGUE_CURSOR")
            snapshot, offset = cursor["snapshot"], cursor["offset"]
        require(isinstance(snapshot, str) and SHA.fullmatch(snapshot) is not None,
                "INVALID_RESEARCH_CATALOGUE_SNAPSHOT")
        # Revalidate access/reachability every call. A cursor is not authorization.
        backend.check_commit(snapshot)
        paths = backend.paths(snapshot, "research/cases/")
        cases = sorted({match[1] for path in paths if (match := CASE_PATH.match(path))})
        require(len(cases) <= MAX_CASES, "RESEARCH_CATALOGUE_CASE_LIMIT")
        require(offset <= len(cases), "RESEARCH_CATALOGUE_CURSOR_OUT_OF_BOUNDS")
        items, used = [], 0
        end = offset
        for index in range(offset, min(offset + limit, len(cases))):
            item = self._item(cases[index], snapshot)
            size = len(encoded(item))
            if size > MAX_CARD_BYTES:
                # Omit a display card, never truncate or alter its saved research.
                item = {"case_id": cases[index], "status": "not-included",
                        "reference": item.get("reference"),
                        "error": "RESEARCH_CARD_EXCEEDS_PAGE_LIMIT"}
                size = len(encoded(item))
            if items and used + size > MAX_CARD_BYTES:
                break  # Unreturned case is first on the next page, not lost.
            items.append(item)
            used += size
            end = index + 1
        return {
            "schema": "research-catalogue-v1", "repository": backend.repository,
            "snapshot": snapshot, "version_scope": "current-per-case-at-snapshot",
            "order": "case-id-not-priority-or-recency", "total_cases": len(cases),
            "offset": offset, "returned": len(items), "remaining": len(cases) - end,
            "status": "no-saved-research" if not cases else
                      "partial-page" if any(i["status"] != "readable" for i in items) else "readable-page",
            "coverage": "this-page-only", "items": items,
            "next_cursor": None if end == len(cases) else {
                "schema": CURSOR_SCHEMA, "repository": backend.repository,
                "snapshot": snapshot, "offset": end},
            "task_execution": "not-inferred-from-records", "market_quiet": "not-assessed",
            "human_selection": "not-recorded-by-read", "research_assessment": "not-performed",
            "persistence": "not-written", "execution_authority": "not-granted-by-catalogue",
        }

    def _item(self, case_id: str, snapshot: str) -> dict:
        from decision_kernel.application.research_continuation import continuation_plan
        from decision_kernel.application.research_reader import ResearchReader
        from decision_kernel.domain.research_work import continue_request, read_workpaper

        row = {"case_id": case_id, "reference": None}
        try:
            reference = self.store.current(case_id, snapshot=snapshot)
            if reference is None:
                raise MissingObject("CURRENT_RESEARCH_NOT_FOUND")
            row["reference"] = asdict(reference)
            # Do NOT choose current again after pinning the catalogue snapshot.
            context = ResearchReader(self.store).resume(reference=reference, source_limit=1)
            research = context["research"]
            payload = research["record"]["input"]
            require(payload["case_id"] == case_id, "RESEARCH_CASE_MISMATCH")
            work = read_workpaper(research)
            plan = continuation_plan(continue_request(research, reference), context) if work else None
            page = context["source_page"]
            source_page = {key: page[key] for key in
                           ("total", "offset", "returned", "remaining", "end_of_list", "coverage")}
            source_page["items"] = [{
                "index": item["index"], "reference": item["reference"], "status": item["status"],
                "error": item["error"], "original_verified": (item.get("view") or {}).get("original_verified", False),
            } for item in page["items"]]
            row.update(status="readable", kind=payload["kind"], revision_id=payload["revision_id"],
                       mandate=payload["mandate"], method=payload["method"],
                       stored_at=research["record"]["stored_at"],
                       time_basis="storage-time-not-research-or-market-cutoff",
                       predecessor=payload["predecessor"], recorded_unresolved=payload["unresolved"],
                       structured_workpaper=work is not None, continuation=plan,
                       source_page=source_page, source_next_cursor=context["next_cursor"],
                       open_payload={"reference": asdict(reference)},
                       open_operation="prepare" if work else "resume",
                       body="not-included-use-exact-open-payload", research_assessment="not-performed")
        except MissingObject:
            row.update(status="missing", error="RESEARCH_OBJECT_MISSING")
        except StoreUnavailable:
            row.update(status="unavailable", error="RESEARCH_UNAVAILABLE")
        except IntegrityError:
            row.update(status="invalid", error="RESEARCH_INTEGRITY_FAILED")
        except RecordError:
            row.update(status="invalid", error="RESEARCH_VALIDATION_FAILED")
        except (TypeError, KeyError):
            row.update(status="invalid", error="RESEARCH_METADATA_INVALID")
        return row
