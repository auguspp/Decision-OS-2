"""Record-only notice attempts and unverified evidence, with one original CAS.

There is deliberately no transport callback. Reserving an attempt neither
calls a sender nor proves one ran. Receipts and submitted feedback are retained
as exact Artifacts; without a selected transport they verify no delivery/read.
"""
from datetime import datetime, timezone

from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.watch_execution import WatchExecution, _Records, _native_read
from decision_kernel.domain.conditional_odds import shape, timestamp
from decision_kernel.domain.records import SHA, Conflict, MissingObject, encoded, require
from decision_kernel.domain.watch_execution import (
    DELIVERY_SCHEMA, MAX_DELIVERY_EVENTS, delivery_state, effects, reference, reference_identity,
    utc, validate_delivery_change,
)


class WatchDelivery:
    def __init__(self, store, registry=None, *, clock=None):
        self.store, self.registry = store, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.records = _Records(store)
        self.execution = WatchExecution(store, registry, clock=self.clock)

    def _notice(self, execution_ref, notice_id):
        execution = self.execution.read_result(execution_ref)
        matches = [n for n in execution["record"]["notices"] if n["notice_id"] == notice_id]
        require(len(matches) == 1, "WATCH_DELIVERY_NOTICE_NOT_FOUND")
        return execution, matches[0]

    def _receipt(self, ref, snapshot):
        if ref is None:
            return None
        view = self.records.artifact_at_snapshot(ref, snapshot)
        return {"reference": reference(ref), "metadata": view["metadata"], "bytes_verified": True,
                "content_assessment": "not-performed", "claim_status": "submitted-unverified",
                "transport_delivery_verified": False, "user_read_verified": False}

    @staticmethod
    def _project(change, snapshot, before, receipt):
        return {"schema": DELIVERY_SCHEMA, "notice_id": change["notice_id"], "snapshot": snapshot,
                "command": change, "receipt": receipt, "state": delivery_state(before, change, receipt),
                "effects": effects()}

    def _paths(self, snapshot, notice_id):
        prefix = "research/artifacts/watch-delivery-" + notice_id + "/"
        current = {p for p in self.store.backend.paths(snapshot, "research/artifacts/") if p.startswith(prefix)}
        paths = sorted(p for p in current if p.endswith("/manifest.json"))
        require(len(paths) <= MAX_DELIVERY_EVENTS, "WATCH_DELIVERY_HISTORY_LIMIT")
        expected = set(paths) | {p.rsplit("/", 1)[0] + "/content.bin" for p in paths}
        require(current == expected, "WATCH_DELIVERY_HISTORY_MISSING_FILES")
        # A complete current tree cannot be inferred from an empty or truncated
        # family. Bound the native introduction history by this family's finite
        # event limit; no journal reset or fallback database is involved.
        output = self.store.backend._run("log", "--first-parent", "--full-history", "--diff-merges=first-parent",
            "--no-renames", "--diff-filter=A", "--name-only", "--format=%H", "-" + str(MAX_DELIVERY_EVENTS + 1),
            snapshot, "--", prefix).stdout.decode("ascii").splitlines()
        commits = [line for line in output if SHA.fullmatch(line)]
        introduced = {line for line in output if line.startswith(prefix)}
        require(len(commits) <= MAX_DELIVERY_EVENTS and len(introduced) <= MAX_DELIVERY_EVENTS * 2,
                "WATCH_DELIVERY_HISTORY_LIMIT")
        require(introduced == current, "WATCH_DELIVERY_HISTORY_MISSING_FILES")
        return paths

    @_native_read
    def _history(self, execution_ref, notice_id, snapshot):
        """Derive one linear family from immutable originals; never choose latest by time.

        The existing store tree is read at a fixed commit. Each event's declared
        CAS base must contain precisely its predecessors. A fork, reset, mutable
        target or invented summary is an integrity failure, not another tip.
        """
        execution, notice = self._notice(execution_ref, notice_id)
        paths = self._paths(snapshot, notice_id)
        records = {}
        for path in paths:
            original = self.store.backend.last_change(snapshot, path)
            ref = reference(self.store.backend.reference(original, path))
            require(self.store.backend.read(snapshot, path) == self.store.backend.resolve(
                self.store.backend.reference(original, path)), "WATCH_DELIVERY_TARGET_CHANGED")
            _, event, basis = self.records.open(ref, DELIVERY_SCHEMA)
            body_path = path.rsplit("/", 1)[0] + "/content.bin"
            require(self.store.backend.read(snapshot, body_path) == self.records.artifact(event, basis).raw(),
                    "WATCH_DELIVERY_TARGET_CHANGED")
            shape(event, {"schema", "notice_id", "snapshot", "command", "receipt", "state", "effects"})
            p = validate_delivery_change(event["command"])
            require(p == event["command"] and event["notice_id"] == notice_id == p["notice_id"]
                    and reference_identity(p["execution_ref"]) == reference_identity(execution["reference"]),
                    "WATCH_DELIVERY_NOTICE_CHANGED")
            records[encoded(ref)] = (ref, event, basis)
        ordered, previous, before, previous_at = [], None, None, notice["checked_at"]
        while len(ordered) < len(records):
            candidates = [row for row in records.values() if row[1]["command"]["expected_event"] == previous]
            require(len(candidates) == 1, "WATCH_DELIVERY_HISTORY_FORK_OR_GAP")
            ref, event, basis = candidates[0]
            require(all(row[0] != ref for row in ordered), "WATCH_DELIVERY_HISTORY_CYCLE")
            p = event["command"]
            require(timestamp(p["at"]) > timestamp(previous_at), "WATCH_DELIVERY_CLOCK_NOT_ADVANCING")
            require(set(self._paths(event["snapshot"], notice_id)) == {row[0]["path"] for row in ordered},
                    "WATCH_DELIVERY_SNAPSHOT_BRANCH_MISMATCH")
            self.records.verify_atomic(ref, event["snapshot"])
            receipt = self._receipt(p["receipt_ref"], event["snapshot"])
            rebuilt = self._project(p, event["snapshot"], before, receipt)
            require(encoded(rebuilt) == encoded(event), "WATCH_DELIVERY_REPLAY_MISMATCH")
            ordered.append((ref, event, basis))
            previous, before, previous_at = ref, event["state"], p["at"]
        return execution, notice, ordered

    @_native_read
    def current(self, execution_ref, notice_id, *, snapshot=None):
        head = snapshot or self.store.backend.head()
        execution, notice, rows = self._history(execution_ref, notice_id, head)
        return {"snapshot": head, "execution_ref": execution["reference"], "notice": notice,
                "reference": rows[-1][0] if rows else None, "state": rows[-1][1]["state"] if rows else None,
                "events": len(rows), "effects": effects(), "write": "not-requested"}

    @_native_read
    def read(self, event_ref):
        ref, event, _ = self.records.open(event_ref, DELIVERY_SCHEMA)
        shape(event, {"schema", "notice_id", "snapshot", "command", "receipt", "state", "effects"})
        p = validate_delivery_change(event["command"])
        _, _, rows = self._history(p["execution_ref"], p["notice_id"], ref["commit"])
        selected = [row for row in rows if reference_identity(row[0]) == reference_identity(ref)]
        require(len(selected) == 1, "WATCH_DELIVERY_EVENT_NOT_IN_HISTORY")
        original, event, basis = selected[0]
        return {"reference": original, "event": event, "rights_basis": basis,
                "history": [{"reference": row[0], "action": row[1]["command"]["action"],
                             "at": row[1]["command"]["at"], "state": row[1]["state"]["status"]}
                            for row in rows[:rows.index(selected[0]) + 1]],
                "recalculation": "matches", "effects": effects()}

    def change(self, payload, *, submission_id, reconcile=False):
        p = validate_delivery_change(publication(payload, "change"))
        journal = self.records.journal(submission_id)
        if journal:
            saved = self.read(journal["reference"])
            if saved["event"]["command"] != p or saved["rights_basis"] != payload["rights_basis"]:
                raise Conflict("SUBMISSION_CONTENT_CONFLICT")
            self.records.verify_journal(journal, saved["event"], saved["rights_basis"])
            return {"reference": saved["reference"], "state": saved["event"]["state"], "effects": effects()}
        if reconcile:
            raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        require(timestamp(p["at"]) <= timestamp(utc(self.clock().isoformat())), "WATCH_DELIVERY_FUTURE_EVENT")
        head = self.store.backend.head()
        _, notice, rows = self._history(p["execution_ref"], p["notice_id"], head)
        require(len(rows) < MAX_DELIVERY_EVENTS, "WATCH_DELIVERY_HISTORY_LIMIT")
        previous = rows[-1][0] if rows else None
        if p["expected_event"] != previous:
            raise Conflict("WATCH_DELIVERY_STALE_EVENT")
        previous_at = rows[-1][1]["command"]["at"] if rows else notice["checked_at"]
        require(timestamp(p["at"]) > timestamp(previous_at), "WATCH_DELIVERY_CLOCK_NOT_ADVANCING")
        receipt = self._receipt(p["receipt_ref"], head)
        event = self._project(p, head, rows[-1][1]["state"] if rows else None, receipt)
        def precondition(at):
            if at != head:
                raise Conflict("WATCH_DELIVERY_SNAPSHOT_CHANGED")
        ref = self.records.put(event, payload["rights_basis"], submission_id, before_new_write=precondition)
        return {"reference": ref, "state": event["state"], "effects": effects()}
