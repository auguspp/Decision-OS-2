"""One CAS-selected lifecycle per frozen Watch; no new store or scheduler.

All history states are re-derived from original events and original Watch
arithmetic. Summary fields are outputs, never caller-supplied checkpoint truth.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import json

from decision_kernel.application.conditional_odds import ConditionalOdds, publication, record_artifact
from decision_kernel.application.odds_watch import OddsWatch, _Sources
from decision_kernel.domain.conditional_odds import day, shape, timestamp
from decision_kernel.domain.odds_watch import project, validate_request
from decision_kernel.domain.records import BlobRef, Conflict, MissingObject, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import ARTIFACT_SCHEMA, artifact_reference
from decision_kernel.domain.watch_activity import (
    ACTIVITY_PREFIX, ACTIVITY_RECORD, MAX_EVENTS, blank_summary, summarize, validate_change, validate_check,
)


def pointer_path(key):
    return ACTIVITY_PREFIX + key + "/current.json"


class WatchActivity:
    def __init__(self, store, registry=None, *, clock=None):
        self.store, self.registry = store, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _watch(self, reference, reader):
        return OddsWatch(reader, self.registry, clock=self.clock).read_watch(reference)

    def _current(self, key, snapshot, reader):
        backend = reader.backend
        backend.check_commit(snapshot)
        try: raw = backend.read(snapshot, pointer_path(key))
        except MissingObject: return None
        try:
            value = json.loads(raw)
            shape(value, {"schema", "watch_key", "event_path"})
            require(value["schema"] == "watch-activity-pointer-v1" and value["watch_key"] == key
                    and encoded(value) == raw, "WATCH_ACTIVITY_POINTER_INVALID")
            path = value["event_path"]
            require(isinstance(path, str) and path.startswith("research/artifacts/odds-watch-activity-"),
                    "WATCH_ACTIVITY_POINTER_INVALID")
            original = backend.last_change(snapshot, path)
            require(backend.last_change(snapshot, pointer_path(key)) == original, "WATCH_ACTIVITY_POINTER_NOT_ATOMIC")
            ref = artifact_reference(backend.reference(original, path))
            require(backend.read(snapshot, path) == backend.resolve(ref), "WATCH_ACTIVITY_POINTER_TARGET_CHANGED")
            return asdict(ref)
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, RecordError): raise
            raise RecordError("WATCH_ACTIVITY_POINTER_INVALID") from None

    def current(self, reference, *, snapshot=None):
        reader = _Sources(self.store.backend)
        frozen = self._watch(reference, reader)
        at = snapshot or self.store.backend.head()
        ref = self._current(frozen["watch_sha256"], at, reader)
        return {"snapshot": at, "watch_key": frozen["watch_sha256"], "reference": ref}

    def _replay(self, tip, reader):
        """One linear replay, rather than recursively re-reading each check chain."""
        records, seen = [], set()
        ref = tip
        custody = ConditionalOdds(reader)
        while ref is not None:
            ref = asdict(artifact_reference(ref))
            key = encoded(ref)
            require(key not in seen and len(records) < MAX_EVENTS, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
            seen.add(key)
            event = custody._open(ref, ACTIVITY_RECORD)
            shape(event, {"schema", "watch_key", "command", "snapshot", "state", "summary", "result"})
            require(event["schema"] == "watch-activity-event-v1", "WATCH_ACTIVITY_EVENT_INVALID")
            records.append((ref, event))
            command = event["command"]
            shape(command, {"kind", "input"})
            require(command["kind"] in ("change", "check"), "WATCH_ACTIVITY_EVENT_INVALID")
            payload = validate_change(command["input"]) if command["kind"] == "change" else validate_check(command["input"])
            ref = payload["expected_event"]
        require(bool(records), "WATCH_ACTIVITY_EVENT_REQUIRED")
        state, history = None, []
        watch_ref = records[-1][1]["command"]["input"]["watch_ref"]
        frozen = self._watch(watch_ref, reader)
        for ref, event in reversed(records):
            require(event["command"]["input"]["watch_ref"] == watch_ref, "WATCH_ACTIVITY_WATCH_CHANGED")
            current = self._current(frozen["watch_sha256"], event["snapshot"], reader)
            require(current == event["command"]["input"]["expected_event"], "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
            rebuilt, state = self._project(frozen, event["command"], event["snapshot"], state, reader)
            require(encoded(rebuilt) == encoded(event), "WATCH_ACTIVITY_REPLAY_MISMATCH")
            state["event_ref"] = ref
            if event["result"] is not None: state["last_check_ref"] = ref
            history.append({"reference": ref, "action": event["command"]["input"].get("action", "check"),
                            "at": state["at"], "state": state["state"],
                            "check_status": event["result"]["status"] if event["result"] else None})
        return frozen, state, history

    def _project(self, frozen, command, snapshot, before, reader):
        kind, payload = command["kind"], command["input"]
        p = validate_change(payload) if kind == "change" else validate_check(payload)
        require(p["expected_event"] == (before["event_ref"] if before else None), "WATCH_ACTIVITY_PREDECESSOR_MISMATCH")
        at = p["at"] if kind == "change" else p["checked_at"]
        require(timestamp(at) >= timestamp(frozen["watch"]["activation"]["as_of"]), "WATCH_ACTIVITY_PRECEDES_WATCH")
        if before: require(timestamp(at) > timestamp(before["at"]), "WATCH_ACTIVITY_CLOCK_NOT_ADVANCING")
        state = deepcopy(before) if before else {"state": None, "last_result": None, "last_check_ref": None,
                  "summary": blank_summary(), "watch_ref": p["watch_ref"], "event_ref": None}
        result = None
        if kind == "change":
            action = p["action"]
            if action == "register":
                require(before is None, "WATCH_ACTIVITY_ALREADY_REGISTERED")
                state["state"] = "paused"  # Registering is not an authorization to execute.
                if p["seed_check"] is not None:
                    seed = OddsWatch(reader, self.registry, clock=self.clock).read_check(p["seed_check"], include_current_use=False)["result"]
                    require(seed["request"]["watch_ref"] == p["watch_ref"] and
                            timestamp(seed["request"]["checked_at"]) <= timestamp(at), "WATCH_ACTIVITY_SEED_MISMATCH")
                    state["last_result"], state["last_check_ref"] = seed, p["seed_check"]
                    state["summary"] = blank_summary(True)
            else:
                require(before is not None, "WATCH_ACTIVITY_NOT_REGISTERED")
                require(state["state"] != "retired", "WATCH_ACTIVITY_RETIRED")
                if action == "pause":
                    require(state["state"] == "active", "WATCH_ACTIVITY_NOT_ACTIVE")
                    state["state"] = "paused"
                elif action == "resume":
                    require(state["state"] == "paused", "WATCH_ACTIVITY_NOT_PAUSED")
                    w, last = frozen["watch"], state["last_result"]
                    require(w["activation"]["enabled"], "WATCH_ACTIVITY_WATCH_DISABLED")
                    require(timestamp(at) < timestamp(w["window"]["ends_at"]) and not
                            (last and (last["status"] == "ended" or (w["window"]["end_after_trigger"] and last["ever_triggered"]))),
                            "WATCH_ACTIVITY_WINDOW_ENDED")
                    if w["basis"]["kind"] != "research":
                        spec_ref = w["basis"]["reference"]
                        if w["basis"]["kind"] == "probability":
                            from decision_kernel.application.probability_odds import ProbabilityOdds
                            spec_ref = ProbabilityOdds(reader).read_assessment(spec_ref)["assessment"]["spec_ref"]
                        spec = ConditionalOdds(reader).read_spec(spec_ref)["spec"]
                        local_at = timestamp(at)
                        if spec["security"]["code"].endswith((".SH", ".SZ")):
                            local_at = local_at.astimezone(timezone(timedelta(hours=8)))
                        require(not spec["target_date"] or local_at.date() < day(spec["target_date"]), "WATCH_ACTIVITY_FIXED_HORIZON_ENDED")
                    state["state"] = "active"
                    if last:
                        # A pause is an unobserved interval, not a false or an erased trigger.
                        state["last_result"]["gap_since_decisive"] = True
                        if last["schema"] == "odds-watch-check-v2":
                            for condition in state["last_result"]["condition_results"]:
                                condition["gap_since_decisive"] = True
                elif action == "retire": state["state"] = "retired"
        else:
            require(before is not None and state["state"] == "active", "WATCH_ACTIVITY_NOT_ACTIVE")
            require(p["records_snapshot"] == snapshot, "WATCH_ACTIVITY_SNAPSHOT_MISMATCH")
            req = validate_request({"watch_ref": p["watch_ref"], "checked_at": at, "records_snapshot": snapshot,
                    "previous_check": state["last_check_ref"], "input": p["input"], "review_reasons": p["review_reasons"]})
            watch = OddsWatch(reader, self.registry, clock=self.clock)
            observation = watch._observe(frozen, req, state["last_result"], reader, known_dates_first=True)
            result = project(frozen["watch"], req, observation, state["last_result"])
            state["last_result"] = result
            if result["status"] == "ended": state["state"] = "retired"
        state["at"] = at
        state["summary"] = summarize(state["summary"], result)
        event = {"schema": "watch-activity-event-v1", "watch_key": frozen["watch_sha256"], "command": command,
                 "snapshot": snapshot, "state": state["state"], "summary": state["summary"], "result": result}
        return json.loads(encoded(event)), state

    def _prepare(self, command, snapshot, reader):
        p = command["input"]
        frozen = self._watch(p["watch_ref"], reader)
        current = self._current(frozen["watch_sha256"], snapshot, reader)
        if current != p["expected_event"]: raise Conflict("WATCH_ACTIVITY_STALE_EVENT")
        before = self._replay(current, reader)[1] if current else None
        if before: require(before["watch_ref"] == p["watch_ref"], "WATCH_ACTIVITY_USE_REGISTERED_REFERENCE")
        require(not before or before["summary"]["events"] < MAX_EVENTS, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
        if before and before["summary"]["events"] == MAX_EVENTS - 1:
            require(command["kind"] == "change" and p["action"] == "retire", "WATCH_ACTIVITY_LAST_SLOT_RESERVED_FOR_RETIRE")
        return self._project(frozen, command, snapshot, before, reader)[0]

    def _apply(self, payload, kind, submission_id, reconcile=False):
        key = "change" if kind == "change" else "check"
        data = publication(payload, key)
        p = validate_change(data) if kind == "change" else validate_check(data)
        command = {"kind": kind, "input": p}
        intent = {"operation": "watch-activity-" + kind, "input": payload}
        head = self.store.backend.head()
        # Stable input reconciliation precedes live-state checks; a retired newer state
        # must not cause a committed old action to be reissued with another identity.
        ref = self.store._replay(head, submission_id, intent)
        if ref is not None:
            return self._receipt(ref, self.read(ref)["event"])
        if reconcile: raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        at = p["at"] if kind == "change" else p["checked_at"]
        require(timestamp(at) <= self.clock(), "WATCH_ACTIVITY_FUTURE_EVENT")
        if kind == "check" and p["records_snapshot"] != head: raise Conflict("WATCH_ACTIVITY_SNAPSHOT_NOT_CURRENT")
        reader = _Sources(self.store.backend)
        event = self._prepare(command, head, reader)
        artifact = record_artifact(ACTIVITY_RECORD, event, payload["rights_basis"])
        path = artifact.path(); data_path = path.rsplit("/", 1)[0] + "/content.bin"
        files = {path: encoded({"schema": ARTIFACT_SCHEMA, "input": artifact.metadata(), "data_path": data_path}),
                 data_path: artifact.raw(), pointer_path(event["watch_key"]): encoded({"schema": "watch-activity-pointer-v1",
                     "watch_key": event["watch_key"], "event_path": path})}
        ref = self.store._save(head, submission_id, intent, path, files)
        return self._receipt(ref, event)

    @staticmethod
    def _receipt(ref, event):
        return {"reference": asdict(ref), "event_sha256": digest(encoded(event)), "state": event["state"],
                "check_status": event["result"]["status"] if event["result"] else None,
                "watch_key": event["watch_key"]}

    def change(self, payload, *, submission_id, reconcile=False): return self._apply(payload, "change", submission_id, reconcile)
    def save_check(self, payload, *, submission_id, reconcile=False): return self._apply(payload, "check", submission_id, reconcile)

    def preview(self, payload):
        p = validate_check(payload)
        require(timestamp(p["checked_at"]) <= self.clock(), "WATCH_ACTIVITY_FUTURE_EVENT")
        head = self.store.backend.head()
        if p["records_snapshot"] != head: raise Conflict("WATCH_ACTIVITY_SNAPSHOT_NOT_CURRENT")
        event = self._prepare({"kind": "check", "input": p}, head, _Sources(self.store.backend))
        if self.store.backend.head() != head: raise Conflict("WATCH_ACTIVITY_SNAPSHOT_CHANGED")
        return {"event": event, "write": "not-requested", "snapshot": head}

    def read(self, reference, *, include_current_use=True):
        reader = _Sources(self.store.backend)
        self.store.backend.head()
        frozen, state, history = self._replay(reference, reader)
        event = ConditionalOdds(reader)._open(reference, ACTIVITY_RECORD)
        head = self.store.backend.head()
        result = {"reference": asdict(artifact_reference(reference)), "event": event, "recalculation": "matches",
                "summary": state["summary"],
                "history": list(reversed(history)), "effects": {"mode": "record-only", "scheduler_created": False,
                    "notification_sent": False, "investment_authority": "none"}}
        if include_current_use:
            selected = self._current(frozen["watch_sha256"], head, reader)
            result["current_activity"] = {"snapshot": head, "reference": selected,
                                         "same_event": selected == asdict(artifact_reference(reference))}
        return result

    def catalogue(self, payload):
        shape(payload, {"snapshot", "after", "limit", "at"})
        head = payload["snapshot"] or self.store.backend.head()
        self.store.backend.check_commit(head)
        require(type(payload["limit"]) is int and 1 <= payload["limit"] <= 8, "WATCH_ACTIVITY_PAGE_LIMIT")
        require(timestamp(payload["at"]) <= self.clock(), "WATCH_ACTIVITY_FUTURE_EVENT")
        after = payload["after"]
        require(after is None or isinstance(after, str) and len(after) == 64 and all(c in "0123456789abcdef" for c in after),
                "WATCH_ACTIVITY_CURSOR_INVALID")
        paths = self.store.backend.paths(head, ACTIVITY_PREFIX)
        keys = sorted(p.split("/")[2] for p in paths if p.endswith("/current.json") and (after is None or p.split("/")[2] > after))
        rows = []
        reader = _Sources(self.store.backend)
        for key in keys[:payload["limit"]]:
            ref = self._current(key, head, reader)
            frozen, state, _ = self._replay(ref, reader)
            require(frozen["watch_sha256"] == key, "WATCH_ACTIVITY_POINTER_KEY_MISMATCH")
            require(timestamp(payload["at"]) >= timestamp(state["at"]), "WATCH_ACTIVITY_VIEW_PRECEDES_EVENT")
            ended = timestamp(payload["at"]) >= timestamp(frozen["watch"]["window"]["ends_at"])
            effective = "window-ended" if state["state"] != "retired" and ended else state["state"]
            rows.append({"watch_key": key, "watch_id": frozen["watch"]["watch_id"], "watch_ref": state["watch_ref"],
                         "event_ref": ref, "state": state["state"], "effective_state": effective,
                         "summary": state["summary"]})
        self.store.backend.check_commit(head)
        return {"snapshot": head, "at": payload["at"], "items": rows,
                "next_after": rows[-1]["watch_key"] if len(keys) > len(rows) else None,
                "scope": "registered-watches-only; explicit-snapshot-not-scheduled", "write": "not-requested"}


def assert_unmanaged(store, reference, *, snapshot=None):
    """Normal legacy execution cannot silently bypass a registered pause/retirement.

    Historical raw reads remain supported. This is application enforcement, not
    protection against a repository owner executing arbitrary local code.
    """
    from decision_kernel.domain.odds_watch import WATCH_RECORD, validate_watch
    frozen = ConditionalOdds(store)._open(reference, WATCH_RECORD)
    key = digest(encoded(validate_watch(frozen["watch"])))
    require(frozen["watch_sha256"] == key, "WATCH_RECORD_CHANGED")
    selected = WatchActivity(store)._current(key, snapshot or store.backend.head(), store)
    require(selected is None, "WATCH_REGISTERED_USE_ACTIVITY_ENTRY")


def verify_fresh(receipt, operation, reader_factory, registry=None):
    try:
        result = WatchActivity(reader_factory(), registry).read(receipt["reference"])
        require(digest(encoded(result["event"])) == receipt["event_sha256"], "WATCH_ACTIVITY_FRESH_MISMATCH")
        status = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    except RecordError:
        status = {"status": "failed", "new_reader": True, "error": "WATCH_ACTIVITY_FRESH_READBACK_FAILED"}
    return {**receipt, "readback": status}
