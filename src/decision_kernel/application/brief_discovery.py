"""Discover normal-host editions at one records snapshot, without a new index.

Latest means the greatest declared delivery cutoff in the complete bounded
normal-host edition inventory. It is not a storage time, source freshness,
research adoption or a mutable reference. Dependency replay stays with Brief.
"""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.application import brief_delivery
from decision_kernel.application.news_rolling import clock
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import (
    IntegrityError, MissingObject, RecordError, SHA, StoreUnavailable, require,
)
from decision_kernel.domain.research_artifacts import ARTIFACT_PATH

LATEST_SAVED = {"select": "latest-saved"}
PREFIX = "research/artifacts/news-edition-"
MAX_EDITIONS = 128
MAX_CHOICES = 8


def latest_selector(value):
    return isinstance(value, dict) and value == LATEST_SAVED


def cutoff_age(cutoff, as_of):
    """A displayed age, not certification of continuous production or freshness."""
    seconds = int((clock(as_of) - clock(cutoff)).total_seconds())
    return {"cutoff": cutoff, "read_as_of": as_of, "age_seconds": seconds,
            "status": "read-clock-before-cutoff" if seconds < 0 else
                      "older-than-24h" if seconds > 86400 else "within-24h",
            "basis": "declared-delivery-cutoff-not-source-or-storage-time"}


def operation_failure(error):
    """A native access/scope failure is not a bad edition to skip past."""
    return isinstance(error, StoreUnavailable) or str(error).startswith(("READING_SCOPE_", "SNAPSHOT_")) or str(error) in {
        "COMMIT_OUTSIDE_BINDING", "FOREIGN_REPOSITORY", "PATH_OUTSIDE_STORE",
        "PUBLIC_BINDING_MISMATCH", "PRIVATE_BINDING_MISMATCH",
        "PRIVATE_UPGRADE_REVERSED", "STORE_MARKER_MISMATCH", "COMMIT_NOT_REACHABLE",
    }


def discover_editions(store, *, snapshot, as_of, strict=False):
    """Inspect all candidates or return an explicit incomplete discovery.

    Native news delivery owns the news-edition-* path family. Other arbitrary
    Artifact names remain readable by exact reference, but are not silently
    counted as covered by this catalogue. Prefixes nominate candidates only;
    canonical artifact bytes, edition digest, schema and plan must validate.
    """
    require(isinstance(snapshot, str) and SHA.fullmatch(snapshot) is not None,
            "BRIEF_DISCOVERY_SNAPSHOT_INVALID")
    clock(as_of)
    backend = store.backend
    backend.check_commit(snapshot)
    paths = sorted(p for p in backend.paths(snapshot, "research/artifacts/")
                   if p.startswith(PREFIX) and ARTIFACT_PATH.fullmatch(p))
    result = {"schema": "brief-discovery-v1", "snapshot": snapshot,
              "repository": backend.repository, "scope": "normal-news-host-editions",
              "prefix": PREFIX, "total_candidates": len(paths), "scanned": 0,
              "valid_candidates": 0, "failures": [], "reference": None, "choices": [],
              "complete": False, "candidates": [], "selection": "greatest-declared-delivery-cutoff",
              "dependency_replay": "not-performed-by-discovery", "persistence": "not-written"}
    if len(paths) > MAX_EDITIONS:
        return {**result, "status": "not-included", "error": "BRIEF_DISCOVERY_INVENTORY_BOUND"}
    if not paths:
        return {**result, "complete": True, "status": "no-saved-brief"}
    artifacts = ResearchArtifacts(store)
    candidates = []
    for path in paths:
        reference = None
        try:
            reference = asdict(backend.reference(snapshot, path))
            doc = brief_delivery._read_document(artifacts, reference)
            require(brief_delivery.plan_input(doc["plan"]) == doc["plan"],
                    "BRIEF_DISCOVERY_PLAN_INVALID")
            expected = (brief_delivery.INDUSTRY_SCHEMA if "industry" in doc["plan"] else
                        brief_delivery.SCHEMA if "research" in doc["plan"] else
                        brief_delivery.news.SCHEMA)
            require(doc["schema"] == expected and all(
                (name in doc) == (name in doc["plan"]) for name in brief_delivery.EXTENSIONS),
                "BRIEF_DISCOVERY_SCHEMA_INVALID")
            require(isinstance(doc.get("brief_markdown"), str), "BRIEF_DISCOVERY_BODY_INVALID")
            cases = sorted({item["reference"]["path"].split("/")[2]
                            for item in doc["plan"].get("research", {}).get("items", [])})
            candidates.append({"reference": reference, "cutoff": doc["plan"]["cutoff"],
                               "kind": doc["plan"]["kind"], "research_cases_in_plan": cases,
                               "edition_sha256": doc["edition_sha256"],
                               "window": doc["plan"]["window"]})
        except (RecordError, KeyError, TypeError) as error:
            if strict and operation_failure(error):
                raise
            status = ("missing" if isinstance(error, MissingObject) else
                      "unavailable" if isinstance(error, StoreUnavailable) else
                      "invalid" if isinstance(error, (IntegrityError, RecordError)) else "invalid")
            result["failures"].append({"reference": reference, "path": path,
                                       "status": status, "error": "BRIEF_DISCOVERY_CANDIDATE_UNREADABLE"})
        result["scanned"] += 1
    # Stable ordering is only a tie rule; path order is not economic priority.
    candidates.sort(key=lambda row: row["reference"]["path"])
    candidates.sort(key=lambda row: clock(row["cutoff"]), reverse=True)
    result.update(valid_candidates=len(candidates), candidates=candidates, choices=candidates[:MAX_CHOICES],
                  choices_omitted=max(0, len(candidates) - MAX_CHOICES))
    if result["failures"]:
        # A broken candidate might be newer. Do not silently fall back to an
        # older good edition and call it the latest one.
        return {**result, "status": "partial", "error": "BRIEF_DISCOVERY_INCOMPLETE"}
    chosen = candidates[0]
    ties = sum(clock(row["cutoff"]) == clock(chosen["cutoff"]) for row in candidates)
    return {**result, "status": "selected", "complete": True,
            "reference": chosen["reference"], "latest_cutoff": chosen["cutoff"],
            "same_cutoff_count": ties, "tie_rule": "artifact-path-ascending-not-storage-order",
            "age": cutoff_age(chosen["cutoff"], as_of)}


def discover_latest(store, *, snapshot, as_of):
    """Keep the original small Room projection; history uses the same inventory."""
    result = discover_editions(store, snapshot=snapshot, as_of=as_of)
    result.pop("candidates")
    result["choices"] = [{k: v for k, v in row.items() if k != "window"}
                         for row in result["choices"]]
    return result
