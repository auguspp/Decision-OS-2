"""One CAS-selected lifecycle per frozen Watch; no new store or scheduler.

All history states are re-derived from original events and original Watch
arithmetic. Summary fields are outputs, never caller-supplied checkpoint truth.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import json

from decision_kernel.application.conditional_odds import ConditionalOdds, publication, record_artifact
from decision_kernel.application import watch_history as history_store
from decision_kernel.application.odds_watch import OddsWatch, _Reads, _Sources
from decision_kernel.domain.conditional_odds import day, shape, timestamp
from decision_kernel.domain.odds_watch import project, validate_request
from decision_kernel.domain.records import Conflict, MissingObject, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.domain.watch_activity import (
    ACTIVITY_PREFIX, ACTIVITY_RECORD, MAX_EVENTS, blank_summary, summarize, validate_change, validate_check,
)
from decision_kernel.domain.watch_history import EVENT_SCHEMA, HISTORY_VERSION, MAX_SEGMENTS, SEGMENT_EVENTS, validate_history
from decision_kernel.infrastructure.watch_snapshot import (
    first_parent_change, first_parent_changes, verify_event_parent, watch_read_scope,
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

    def _history_current(self, key, snapshot, reader, native):
        """The v2 contract includes the original pointer's selection history."""
        selected = self._current(key, snapshot, reader)
        original = first_parent_change(native, snapshot, pointer_path(key))
        if selected is None:
            require(original is None, "WATCH_HISTORY_POINTER_MISSING")
        else:
            require(original == selected["commit"], "WATCH_HISTORY_SELECTION_LINEAGE_MISMATCH")
        return selected

    def _current_for_use(self, key, snapshot, reader):
        """No new use may delete or downgrade an already adopted v2 lineage.

        Exact v1 history reads keep their original replay. Only this current-use
        gate checks prior pointer originals, without treating their summaries
        as semantic proof. The one owned memo may expose its original backend;
        no arbitrary facade or separate permission owner is substituted.
        """
        selected = self._current(key, snapshot, reader)
        event = history_store.event_record(selected, reader) if selected else None
        backend = self.store.backend
        if type(backend) is _Reads:
            backend = backend.original
        with watch_read_scope(backend, snapshot) as native:
            local = _Sources(native)
            require(self._current(key, snapshot, local) == selected, "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
            if event is not None and event["schema"] == EVENT_SCHEMA:
                selected = self._history_current(key, snapshot, local, native)
                verify_event_parent(native, selected["commit"], event["snapshot"])
            else:
                for previous in first_parent_changes(native, snapshot, pointer_path(key),
                                                     limit=MAX_SEGMENTS * SEGMENT_EVENTS):
                    prior_ref = self._current(key, previous, local)
                    if prior_ref is None:  # A native deletion remains in the complete change list.
                        continue
                    prior = history_store.event_record(prior_ref, local)
                    require(prior["schema"] != EVENT_SCHEMA,
                            "WATCH_HISTORY_POINTER_MISSING" if selected is None else "WATCH_HISTORY_FORMAT_DOWNGRADE")
            return selected

    def current(self, reference, *, snapshot=None):
        reader = _Sources(self.store.backend)
        frozen = self._watch(reference, reader)
        at = snapshot or self.store.backend.head()
        ref = self._current_for_use(frozen["watch_sha256"], at, reader)
        return {"snapshot": at, "watch_key": frozen["watch_sha256"], "reference": ref}

    def _replay(self, tip, reader):
        event = history_store.event_record(tip, reader)
        if event["schema"] != EVENT_SCHEMA:
            return self._replay_v1(tip, reader)
        with watch_read_scope(self.store.backend) as native:
            return history_store.replay(self, tip, _Sources(native), native)

    def _replay_v1(self, tip, reader):
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

    def _project(self, frozen, command, snapshot, before, reader, *, history=None):
        kind, payload = command["kind"], command["input"]
        p = validate_change(payload) if kind == "change" else validate_check(payload)
        if history is None:
            require("history_version" not in p and not (before and before.get("history")),
                    "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
        else:
            history = validate_history(history)
            if before is None:
                require(kind == "change" and p["action"] == "register" and p.get("history_version") == HISTORY_VERSION,
                        "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
            elif before.get("history") is None:
                require(kind == "change" and p["action"] == "migrate" and p.get("history_version") == HISTORY_VERSION,
                        "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
                require(before["summary"]["events"] < MAX_EVENTS - 1, "WATCH_HISTORY_LEGACY_LIMIT")
            else:
                require("history_version" not in p, "WATCH_ACTIVITY_HISTORY_ALREADY_REGISTERED")
            if history["segment_index"] == MAX_SEGMENTS - 1 and history["event_index"] == SEGMENT_EVENTS - 1:
                require(kind == "change" and p["action"] == "retire", "WATCH_ACTIVITY_LAST_SLOT_RESERVED_FOR_RETIRE")
        require(p["expected_event"] == (before["event_ref"] if before else None), "WATCH_ACTIVITY_PREDECESSOR_MISMATCH")
        at = p["at"] if kind == "change" else p["checked_at"]
        require(timestamp(at) >= timestamp(frozen["watch"]["activation"]["as_of"]), "WATCH_ACTIVITY_PRECEDES_WATCH")
        if before: require(timestamp(at) > timestamp(before["at"]), "WATCH_ACTIVITY_CLOCK_NOT_ADVANCING")
        state = deepcopy({k: v for k, v in before.items() if not k.startswith("_history_")}) if before else {
                  "state": None, "last_result": None, "last_check_ref": None,
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
                elif action in ("resume", "migrate"):
                    if action == "resume":
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
                    if action == "resume":
                        state["state"] = "active"
                    if last and action == "resume":
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
        if history is not None:
            state["history"] = history
            event.update(schema=EVENT_SCHEMA, history=history)
        return json.loads(encoded(event)), state

    def _prepare(self, command, snapshot, reader):
        return self._prepare_plan(command, snapshot, reader)[0]

    def _prepare_plan(self, command, snapshot, reader):
        p = command["input"]
        frozen = self._watch(p["watch_ref"], reader)
        current = self._current(frozen["watch_sha256"], snapshot, reader)
        if current != p["expected_event"]: raise Conflict("WATCH_ACTIVITY_STALE_EVENT")
        current_event = history_store.event_record(current, reader) if current else None
        versioned = p.get("history_version") == HISTORY_VERSION or (current_event and current_event["schema"] == EVENT_SCHEMA)
        if versioned:
            with watch_read_scope(self.store.backend, snapshot) as native:
                local = _Sources(native)
                frozen = self._watch(p["watch_ref"], local)
                require(self._history_current(frozen["watch_sha256"], snapshot, local, native) == current,
                        "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
                before = None
                if current:
                    replay = history_store.replay if current_event["schema"] == EVENT_SCHEMA else history_store.verify_legacy_prefix
                    before = replay(self, current, local, native)[1]
                    require(before["watch_ref"] == p["watch_ref"], "WATCH_ACTIVITY_USE_REGISTERED_REFERENCE")
                metadata, segment = history_store.next_position(before, command, frozen["watch_sha256"])
                event = self._project(frozen, command, snapshot, before, local, history=metadata)[0]
                if segment is not None:
                    for path in history_store.artifact_files(segment):
                        try: local.backend.read(snapshot, path)
                        except MissingObject: continue
                        raise RecordError("WATCH_HISTORY_SEAL_ALREADY_PRESENT")
                return event, segment
        require(self._current_for_use(frozen["watch_sha256"], snapshot, reader) == current,
                "WATCH_ACTIVITY_HISTORY_BRANCH_MISMATCH")
        before = self._replay_v1(current, reader)[1] if current else None
        if before: require(before["watch_ref"] == p["watch_ref"], "WATCH_ACTIVITY_USE_REGISTERED_REFERENCE")
        require(not before or before["summary"]["events"] < MAX_EVENTS, "WATCH_ACTIVITY_HISTORY_LIMIT_OR_CYCLE")
        if before and before["summary"]["events"] == MAX_EVENTS - 1:
            require(command["kind"] == "change" and p["action"] == "retire", "WATCH_ACTIVITY_LAST_SLOT_RESERVED_FOR_RETIRE")
        return self._project(frozen, command, snapshot, before, reader)[0], None

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
        event, segment = self._prepare_plan(command, head, reader)
        artifact = record_artifact(ACTIVITY_RECORD, event, payload["rights_basis"])
        path = artifact.path()
        files = history_store.artifact_files(artifact)
        if segment is not None:
            files.update(history_store.artifact_files(segment))
        files[pointer_path(event["watch_key"])] = encoded({"schema": "watch-activity-pointer-v1",
            "watch_key": event["watch_key"], "event_path": path})
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
        if state.get("history"):
            result.update(history_scope="current-segment-only; sealed-segments-are-exact-readable-artifacts",
                          sealed_segments=history_store.segment_listing(state),
                          history_validation=history_store.validation(state))
        if include_current_use:
            select = self._current_for_use if state.get("history") else self._current
            selected = select(frozen["watch_sha256"], head, reader)
            result["current_activity"] = {"snapshot": head, "reference": selected,
                                         "same_event": selected == asdict(artifact_reference(reference))}
        return result

    def read_segment(self, reference):
        ref = asdict(artifact_reference(reference))
        with watch_read_scope(self.store.backend) as native:
            reader = _Sources(native)
            segment = history_store.open_segment(ref, reader)
            publication_ref = self._history_current(segment["watch_key"], ref["commit"], reader, native)
            require(publication_ref is not None, "WATCH_HISTORY_SEAL_NOT_ATOMIC")
            event = history_store.event_record(publication_ref, reader)
            require(event["schema"] == EVENT_SCHEMA and event["history"]["segment_index"] == segment["segment_index"] + 1
                    and event["history"]["event_index"] == 0 and event["history"]["previous_segment"] == {
                        "kind": "same-commit", "path": ref["path"], "sha256": ref["sha256"]}
                    and event["command"]["input"]["expected_event"] == segment["events"][-1],
                    "WATCH_HISTORY_SEAL_NOT_ATOMIC")
            _, state, rows = history_store.replay(self, publication_ref, reader, native,
                                                 collect_segment=segment["segment_index"])
            require(any(item["reference"] == ref for item in state["_history_segments"]),
                    "WATCH_HISTORY_SEGMENT_NOT_IN_PREFIX")
            proof = history_store.validation(state)
            proof["scope"] = "complete-original-prefix-and-seal-publication"
            result = {"reference": ref, "segment": segment, "history": rows,
                "summary": state["_history_selected_summary"], "recalculation": "matches",
                "publication_event_ref": publication_ref, "history_validation": proof,
                "effects": {"mode": "record-only", "scheduler_created": False,
                    "notification_sent": False, "investment_authority": "none"}}
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
            ref = self._current_for_use(key, head, reader)
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
    selected = WatchActivity(store)._current_for_use(key, snapshot or store.backend.head(), store)
    require(selected is None, "WATCH_REGISTERED_USE_ACTIVITY_ENTRY")


def verify_fresh(receipt, operation, reader_factory, registry=None):
    try:
        result = WatchActivity(reader_factory(), registry).read(receipt["reference"])
        require(digest(encoded(result["event"])) == receipt["event_sha256"], "WATCH_ACTIVITY_FRESH_MISMATCH")
        status = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    except RecordError:
        status = {"status": "failed", "new_reader": True, "error": "WATCH_ACTIVITY_FRESH_READBACK_FAILED"}
    return {**receipt, "readback": status}
