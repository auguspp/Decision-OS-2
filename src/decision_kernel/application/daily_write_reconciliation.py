"""One reviewed reconciliation of settled FT writes; no source retry authority.

The original terminal control, journals and provider bytes stay immutable. A
single new control edge permits only the unspent requests of that exact plan.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import re

from decision_kernel.domain.records import (
    MissingObject, Selection, SourceInput, SourceRef, digest, encoded, identifier, require,
)

CYCLE_SCHEMA = "daily-market-cycle-v3"
PROOF_SCHEMA = "daily-write-reconciliation-v1"
POLICY_FILE = Path("config/daily-write-reconciliation.json")
ORIGINAL_REASON = "REMOTE_WRITE_REQUIRES_RECONCILIATION"
ORIGINAL_PRICE_BUDGET = 12
SETTLED_SLOT = "none:000001.SZ"


def _reference(value):
    ref = SourceRef.from_dict(value)
    require(ref.selection == Selection(), "DAILY_WRITE_RECONCILIATION_REFERENCE")
    return json.loads(encoded(asdict(ref)))


def proof(value):
    fields = {"schema", "original_reference", "settled_market_reference", "authorization_ref",
        "additional_source_requests", "native_run", "submissions"}
    require(isinstance(value, dict) and set(value) == fields and value["schema"] == PROOF_SCHEMA
        and type(value["additional_source_requests"]) is int and value["additional_source_requests"] == 0
        and isinstance(value["authorization_ref"], str) and re.fullmatch(
            r"https://github\.com/auguspp/Decision-OS-2/issues/10#issuecomment-[1-9][0-9]*",
            value["authorization_ref"]), "DAILY_WRITE_RECONCILIATION_PROOF")
    original = _reference(value["original_reference"])
    settled = _reference(value["settled_market_reference"])
    native = value["native_run"]
    require(isinstance(native, dict) and set(native) == {
        "run_id", "run_attempt", "code_sha", "artifact_id", "sha256", "size_bytes"}
        and all(type(native.get(key)) is int and 0 < native[key] < 10**15
                for key in ("run_id", "artifact_id"))
        and type(native.get("run_attempt")) is int and native["run_attempt"] == 1
        and type(native.get("size_bytes")) is int and 0 < native["size_bytes"] <= 1024 * 1024
        and isinstance(native.get("code_sha"), str) and re.fullmatch(r"[a-f0-9]{40}", native["code_sha"])
        and isinstance(native.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", native["sha256"]),
        "DAILY_WRITE_RECONCILIATION_NATIVE_PROOF")
    rows = value["submissions"]
    require(isinstance(rows, list) and len(rows) == 5, "DAILY_WRITE_RECONCILIATION_JOURNALS")
    keys, commits = set(), set()
    previous = None
    for row in rows:
        require(isinstance(row, dict) and set(row) == {
            "submission_id", "intent_sha256", "reference", "expected_head"},
            "DAILY_WRITE_RECONCILIATION_JOURNALS")
        key = identifier(row["submission_id"])
        ref = _reference(row["reference"])
        require(key not in keys and ref["manifest"]["commit"] not in commits
            and ref["manifest"]["repository"] == original["manifest"]["repository"]
            and isinstance(row["expected_head"], str) and re.fullmatch(r"[a-f0-9]{40}", row["expected_head"])
            and isinstance(row["intent_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", row["intent_sha256"])
            and (previous is None or row["expected_head"] == previous),
            "DAILY_WRITE_RECONCILIATION_JOURNALS")
        keys.add(key)
        commits.add(ref["manifest"]["commit"])
        previous = ref["manifest"]["commit"]
    require(rows[3]["reference"] == settled and rows[4]["reference"] == original
        and settled["manifest"]["repository"] == original["manifest"]["repository"],
        "DAILY_WRITE_RECONCILIATION_JOURNALS")
    return json.loads(encoded(value))


def load_grant():
    from decision_kernel.application.relay_registry import _json
    from decision_kernel.interfaces.ftshare_check_cli import _regular
    value = _json(_regular(POLICY_FILE, 8192))
    require(isinstance(value, dict) and set(value) == {"enabled", "reconciliation"}
        and value["enabled"] is True, "DAILY_WRITE_RECONCILIATION_NOT_AUTHORIZED")
    return proof(value["reconciliation"])


def eligible(state, grant):
    from decision_kernel.application.daily_calendar_repair import CYCLE_SCHEMA as CALENDAR_SCHEMA
    require(state["schema"] == CALENDAR_SCHEMA and state["revision"] == 8
        and state["status"] == "blocked" and state["reason_code"] == ORIGINAL_REASON
        and state["market_reference"] is None and state["price_end"] == state["date"]
        and all(row["status"] == "complete" for row in state["calendars"].values())
        and sum(row["reserved_requests"] for row in state["calendars"].values()) == 3
        and state["predecessor"] is not None
        and grant["submissions"][0]["expected_head"] == state["predecessor"]["manifest"]["commit"],
        "DAILY_WRITE_RECONCILIATION_NOT_SETTLED_WRITE")


def transition(previous, current):
    """Validate the one immutable history edge without consulting live policy."""
    from decision_kernel.application.daily_market import _stamp
    grant = proof(current.get("write_reconciliation"))
    eligible(previous, grant)
    require(current["schema"] == CYCLE_SCHEMA and current["revision"] == 9
        and current["predecessor"] == grant["original_reference"]
        and current["status"] == "market-pending" and current["reason_code"] is None
        and _stamp(current["updated_at"]) >= _stamp(previous["updated_at"]),
        "DAILY_WRITE_RECONCILIATION_EDGE")
    require(_stamp(current["updated_at"]) - _stamp(previous["created_at"]) < timedelta(days=7),
        "DAILY_WRITE_RECONCILIATION_WINDOW_EXPIRED")
    immutable = set(previous) - {"schema", "revision", "predecessor", "updated_at", "status", "reason_code"}
    require(all(previous[key] == current[key] for key in immutable), "DAILY_MARKET_INTENT_CHANGED")


def _journals(grant, store, snapshot):
    """Verify fixed old effects using existing read APIs, never try a write.

    The market reader intentionally cannot discover journal history. The proof
    already pins each original commit, so compare its original and current
    journal bytes, reconstruct the original intent, and check source history.
    Native parent OIDs are checked by the host before granting the new edge.
    """
    documents = []
    for row in grant["submissions"]:
        ref = SourceRef.from_dict(row["reference"])
        path = "submissions/" + row["submission_id"] + ".json"
        require(ref.manifest.repository == store.backend.repository,
            "DAILY_WRITE_RECONCILIATION_REFERENCE")
        original = store.backend.read(ref.manifest.commit, path)
        require(original == store.backend.read(snapshot, path), "DAILY_WRITE_RECONCILIATION_JOURNAL_CHANGED")
        try:
            store.backend.read(row["expected_head"], path)
        except MissingObject:
            pass
        else:
            require(False, "DAILY_WRITE_RECONCILIATION_JOURNAL_NOT_NEW")
        stored = store.read_source(ref)
        source = SourceInput.from_dict({**stored["manifest"]["input"], "text": stored["data"]})
        intent = {"operation": "source", "input": asdict(source)}
        require(digest(encoded(intent)) == row["intent_sha256"] and original == encoded({
            "schema": "submission-v1", "intent_sha256": row["intent_sha256"],
            "result_path": ref.manifest.path}), "DAILY_WRITE_RECONCILIATION_INTENT_CHANGED")
        representation = stored["manifest"]["representation"]
        require(representation is not None
            and all(store.backend.last_change(snapshot, source_path) == ref.manifest.commit
                for source_path in (ref.manifest.path, representation["path"])),
            "DAILY_WRITE_RECONCILIATION_REFERENCE_NOT_ORIGINAL")
        documents.append(stored)
    return documents


def verify_original(state, grant, *, registry, store, snapshot=None):
    """Reconcile the exact five immutable submissions and settled FT state."""
    from decision_kernel.application import ftshare_collection as ft
    from decision_kernel.application.daily_market import validate_cycle
    from decision_kernel.application.ftshare_single import restore_single
    from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
    grant = proof(grant)
    eligible(state, grant)
    snapshot = snapshot or store.backend.head()
    documents = _journals(grant, store, snapshot)
    require(validate_cycle(json.loads(documents[4]["data"])) == state,
        "DAILY_WRITE_RECONCILIATION_ORIGINAL_CHANGED")
    restored = ft.restore_ftshare_collection(grant["settled_market_reference"], registry, store)
    collection, plan = restored["collection"], restored["collection"]["plan"]
    require(plan["schema"] == ft.SINGLE_PLAN_SCHEMA and collection["revision"] == 2
        and collection["reserved_requests"] == 1 and collection["source_hold"] is None
        and plan["source_budget"] == ORIGINAL_PRICE_BUDGET
        and len(plan["new_slots"]) == ORIGINAL_PRICE_BUDGET
        and set(plan["new_slots"]) == set(plan["requests"]) == set(collection["units"])
        and plan["base_reference"] == state["base_market_reference"]
        and plan["generation"] == state["market_generation"]
        and (plan["start"], plan["end"], plan["calendar_end"]) == (
            state["calendar_start"], state["price_end"], state["calendar_end"])
        and plan.get("completion_policy") == EOD_COMPLETION_POLICY
        and plan["calendar_references"] == {venue: row["reference"] for venue, row in state["calendars"].items()},
        "DAILY_WRITE_RECONCILIATION_FT_STATE")
    require(SETTLED_SLOT in collection["units"] and collection["units"][SETTLED_SLOT] == {
        "status": "complete", "request_count": 1, "reference": grant["submissions"][2]["reference"],
        "reason_code": None} and all(unit == {
            "status": "pending", "request_count": 0, "reference": None, "reason_code": None}
            for slot, unit in collection["units"].items() if slot != SETTLED_SLOT),
        "DAILY_WRITE_RECONCILIATION_FT_STATE")
    zero, one = (json.loads(documents[n]["data"]) for n in (0, 1))
    two = json.loads(documents[3]["data"])
    require(all(isinstance(document, dict) and isinstance(document.get("collection"), dict)
        and {"revision", "reserved_requests"} <= set(document["collection"])
        and "predecessor" in document for document in (zero, one, two)),
        "DAILY_WRITE_RECONCILIATION_FT_HISTORY")
    require(zero["collection"]["revision"] == 0 and one["collection"]["revision"] == 1
        and zero["collection"]["reserved_requests"] == 0
        and one["predecessor"] == grant["submissions"][0]["reference"]
        and two["predecessor"] == grant["submissions"][1]["reference"]
        and two["collection"] == collection, "DAILY_WRITE_RECONCILIATION_FT_HISTORY")
    ft._transition(zero["collection"], one["collection"])
    ft._transition(one["collection"], collection)
    restore_single(grant["submissions"][2]["reference"], store, request=plan["requests"][SETTLED_SLOT])
    return restored


def verify_completed(state, *, registry, store):
    """A successful final edge must name the original fully settled FT plan."""
    from decision_kernel.application.ftshare_collection import restore_ftshare_collection
    grant = proof(state.get("write_reconciliation"))
    require(state["schema"] == CYCLE_SCHEMA and state["revision"] == 10 and state["status"] == "complete",
        "DAILY_WRITE_RECONCILIATION_PHASE")
    original = restore_ftshare_collection(grant["settled_market_reference"], registry, store)["collection"]
    final = restore_ftshare_collection(state["market_reference"], registry, store)["collection"]
    require(final["plan"] == original["plan"] and final["revision"] == 2 * ORIGINAL_PRICE_BUDGET
        and final["reserved_requests"] == ORIGINAL_PRICE_BUDGET and final["source_hold"] is None
        and set(final["units"]) == set(original["units"])
        and final["units"][SETTLED_SLOT] == original["units"][SETTLED_SLOT]
        and all(unit["status"] == "complete" and unit["request_count"] == 1
            and unit["reference"] is not None and unit["reason_code"] is None
            for unit in final["units"].values()), "DAILY_WRITE_RECONCILIATION_FT_INCOMPLETE")


def select_reconciliation(item, grant, *, at, registry, store, approved):
    from decision_kernel.application.daily_market import _stamp, policy_digest, validate_cycle
    from decision_kernel.application.ftshare_collection import latest_ftshare_reference
    from decision_kernel.application.market_custody import market_read_scope
    grant = proof(grant)
    require(isinstance(item, dict), "DAILY_WRITE_RECONCILIATION_TARGET_REQUIRED")
    state = validate_cycle(item["cycle"])
    require(state["policy_sha256"] == policy_digest(approved), "DAILY_MARKET_POLICY_CHANGED")
    require(_stamp(at) >= _stamp(state["updated_at"]), "DAILY_MARKET_CLOCK_ROLLBACK")
    if state.get("write_reconciliation") is not None:
        require(state["write_reconciliation"] == grant, "DAILY_WRITE_RECONCILIATION_TARGET_CHANGED")
        return False
    require(_stamp(at) - _stamp(state["created_at"]) < timedelta(days=7),
        "DAILY_WRITE_RECONCILIATION_WINDOW_EXPIRED")
    require(item["reference"] == grant["original_reference"], "DAILY_WRITE_RECONCILIATION_TARGET_CHANGED")
    with market_read_scope(store) as reader:
        restored = verify_original(state, grant, registry=registry, store=reader)
        plan = restored["collection"]["plan"]
        targets = sorted([{"symbol": symbol, "adjust_kind": kind} for symbol in approved["symbols"]
            for kind in ("none", "forward")], key=lambda target: (target["symbol"], target["adjust_kind"]))
        require(approved["max_price_requests"] == ORIGINAL_PRICE_BUDGET and plan["targets"] == targets,
            "DAILY_MARKET_POLICY_CHANGED")
        require(latest_ftshare_reference(reader, registry, collection_id=plan["collection_id"],
            generation=plan["generation"]) == grant["settled_market_reference"],
            "DAILY_WRITE_RECONCILIATION_FT_FRONTIER_CHANGED")
        from decision_kernel.application.daily_market import check_cycle_frontier
        check_cycle_frontier(state, registry, reader, protected_symbols=approved["symbols"])
    return True


def reserve(item, grant, *, at):
    from decision_kernel.application.daily_market import next_state
    grant = proof(grant)
    require(item["cycle"].get("write_reconciliation") is None,
        "DAILY_WRITE_RECONCILIATION_ALREADY_SPENT")
    require(item["reference"] == grant["original_reference"], "DAILY_WRITE_RECONCILIATION_TARGET_CHANGED")
    eligible(item["cycle"], grant)
    state = next_state(item, at=at, schema=CYCLE_SCHEMA, write_reconciliation=grant,
        status="market-pending", reason_code=None)
    transition(item["cycle"], state)
    return state
