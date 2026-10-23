"""Reference fixing on existing exact sources, in two deliberate tiers.

Tier 1 ``listing`` reads nothing. A SourceRef is content-addressed, so its
manifest commit/path/sha256 already fixes which bytes were cited; listing turns
that implicit binding into an explicit receipt at zero extra source reads. The
regular write path uses this tier, because a second read of every cited slice
would double the shared request budget.

Tier 2 ``inspect`` performs the reads. It is an explicit audit of an already
saved revision, so it never happens behind a write and never fetches.

Neither tier asserts that a cited price date falls inside the slice: the stored
excerpt is not a completeness claim, and existing research fields carry no
machine-readable price date. "Observed" values are what the retained bytes
contain under fixed key names, nothing more.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, require

REPORT_SCHEMA = "reference-fixing-report-v1"
HARVEST_BASIS = "whitelisted-keys-observed-in-retained-excerpt"
COVERAGE_ASSERTION = "not-declared-in-existing-fields"
LISTING_MEANING = "each citation is an immutable content-addressed slice; listing does not re-read it"
MAX_SOURCES = 64
MAX_NODES = 20000
MAX_VALUES = 256
MAX_DEPTH = 12
MAX_SCALAR = 96
DATE_KEYS = ("trade_date", "cal_date", "end_date", "start_date", "price_date", "as_of")
CODE_KEYS = ("ts_code", "con_code", "symbol")
MARKER_KEYS = ("coverage", "schema", "api", "status")


def _sources(values) -> tuple[SourceRef, ...]:
    require(isinstance(values, (tuple, list)) and len(values) <= MAX_SOURCES
            and all(isinstance(item, SourceRef) for item in values), "INVALID_SOURCES")
    return tuple(values)


def listing(sources, *, scope: str = "research-checkpoint") -> dict:
    """Zero-read receipt: what the citations fix, without touching the store."""
    fixed = _sources(sources)
    return {
        "schema": REPORT_SCHEMA,
        "tier": "listing",
        "scope": scope,
        "status": "fixed" if fixed else "no-references",
        "coverage_assertion": COVERAGE_ASSERTION,
        "meaning": LISTING_MEANING,
        "sources": len(fixed),
        "slices": [{"index": index, "reference": asdict(ref),
                    "selection": asdict(ref.selection)} for index, ref in enumerate(fixed)],
        "gaps": [],
        "write": "not-requested",
    }


def _harvest(payload) -> tuple[list[str], list[str], list[str], bool]:
    """Whitelisted scalar values under fixed key names, bounded in nodes and depth."""
    dates, codes, markers = set(), set(), set()
    stack = [(payload, 0)]
    visited, truncated = 0, False
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > MAX_NODES or depth > MAX_DEPTH:
            truncated = True
            break
        if isinstance(node, dict):
            for key, item in node.items():
                target = dates if key in DATE_KEYS else codes if key in CODE_KEYS else (
                    markers if key in MARKER_KEYS else None)
                if target is not None and isinstance(item, str) and 0 < len(item) <= MAX_SCALAR:
                    target.add(key + "=" + item if target is markers else item)
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
        elif isinstance(node, list):
            # A skipped tail is unread content, so say so instead of implying completeness.
            truncated = truncated or len(node) > MAX_VALUES
            for item in node[:MAX_VALUES]:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
    return sorted(dates), sorted(codes), sorted(markers), truncated


def _excerpt(view):
    """Retained text or the already-selected rows; never a second read."""
    data = view["data"]
    if not isinstance(data, str):
        return data
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        return None


def _audited_row(index: int, ref: SourceRef, view: dict) -> dict:
    manifest = view["manifest"]
    representation = manifest.get("representation") or {}
    excerpt = _excerpt(view)
    dates, codes, markers, truncated = _harvest(excerpt) if excerpt is not None else ([], [], [], False)
    return {
        "index": index,
        "reference": view["reference"],
        "source_id": manifest["input"]["source_id"],
        "selection": asdict(ref.selection),
        "stored_bytes": representation.get("size"),
        "stored_sha256": representation.get("sha256"),
        "custody": manifest["input"]["custody"],
        "availability": view["availability"],
        "harvest_basis": HARVEST_BASIS if excerpt is not None else "not-harvestable",
        "observed_dates": dates[:MAX_VALUES],
        "observed_securities": codes[:MAX_VALUES],
        "observed_markers": markers[:MAX_VALUES],
        "harvest_truncated": truncated,
        "original_verified": False,
    }


def inspect(store, sources, *, scope: str = "saved-research") -> dict:
    """Tier 2: read every cited slice as stored. A read failure is a gap, not a skip."""
    fixed, gaps = [], []
    for index, ref in enumerate(_sources(sources)):
        try:
            fixed.append(_audited_row(index, ref, store.read_source(ref)))
        except RecordError as error:
            gaps.append({"index": index, "problem": str(error), "reference": asdict(ref)})
    return {
        "schema": REPORT_SCHEMA,
        "tier": "inspect",
        "scope": scope,
        "status": "blocked" if gaps else "read" if fixed else "no-references",
        "coverage_assertion": COVERAGE_ASSERTION,
        "meaning": "each listed slice was read as stored; observed values are what those "
                   "bytes contain, not a coverage, completeness or historical-PIT claim",
        "sources": len(fixed) + len(gaps),
        "slices": fixed,
        "gaps": gaps,
        "write": "not-requested",
    }


def from_revision(store, reference: BlobRef, *, scope: str = "saved-research") -> dict:
    """Audit an already saved revision without writing or selecting its current pointer."""
    require(isinstance(reference, BlobRef), "INVALID_REFERENCE")
    revision = store.read_revision(reference)
    payload = revision["record"]["input"]
    report = inspect(store, tuple(SourceRef.from_dict(item) for item in payload["sources"]), scope=scope)
    report["revision"] = asdict(reference)
    report["case_id"] = payload["case_id"]
    report["revision_kind"] = payload["kind"]
    return report


def receipt(store, references, *, scope: str) -> dict:
    """Handoff receipt: citation inventory of the adopted revisions, zero extra source reads."""
    fixed, gaps = [], []
    for value in references:
        reference = value if isinstance(value, BlobRef) else BlobRef(**value)
        try:
            payload = store.read_revision(reference)["record"]["input"]
        except RecordError as error:
            gaps.append({"problem": str(error), "reference": asdict(reference)})
            continue
        fixed.extend(payload["sources"])
    return listing(tuple(SourceRef.from_dict(item) for item in fixed), scope=scope) | {"gaps": gaps}
