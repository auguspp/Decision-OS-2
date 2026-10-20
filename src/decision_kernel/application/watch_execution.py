"""Explicit finite Watch runner using the original records CAS and journals.

Every save method performs at most one original store transaction. No service
is scheduled and no source or notification transport is invoked here.
"""
from base64 import b64decode
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
import json

from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.odds_watch import OddsWatch, _Sources
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.application import watch_history
from decision_kernel.domain.conditional_odds import shape, timestamp
from decision_kernel.domain.records import (
    DIGEST, BlobRef, Conflict, MissingObject, RecordError, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.watch_execution import (
    DELIVERY_SCHEMA, PLAN_SCHEMA, RESULT_SCHEMA, SLOT_SCHEMA, effects, notice_intents,
    reference, reference_identity, select_slot, slot_identity, slot_window, submission_keys,
    utc, validate_plan, validate_query,
)
from decision_kernel.infrastructure.watch_snapshot import verify_event_parent, watch_read_scope


def _native_read(operation):
    """Reuse the original bounded native snapshot for one complete read/replay."""
    @wraps(operation)
    def read(self, *args, **kwargs):
        with watch_read_scope(self.store.backend) as native:
            reader = type(self)(VersionedStore(native), self.registry, clock=self.clock)
            return operation(reader, *args, **kwargs)
    return read


class _Records:
    """Immutable Artifact encoding, not another store, index or mutable head."""
    def __init__(self, store):
        self.store = store
        self.artifacts = ResearchArtifacts(store)

    @staticmethod
    def artifact(record, rights_basis):
        checked_text(rights_basis, limit=4096)
        schema = record["schema"]
        layouts = {
            PLAN_SCHEMA: ("watch-execution-plan-", "plan_sha256"),
            SLOT_SCHEMA: ("watch-slot-", "slot_key"),
            RESULT_SCHEMA: ("watch-execution-result-", "slot_key"),
            DELIVERY_SCHEMA: ("watch-delivery-", "notice_id"),
        }
        require(schema in layouts, "WATCH_EXECUTION_RECORD_SCHEMA")
        prefix, key = layouts[schema]
        raw = encoded({"schema": "watch-execution-custody-v1", "record": record, "rights_basis": rights_basis})
        return ArtifactInput.from_bytes(prefix + record[key], schema + ".json", "application/json", raw)

    def open(self, ref, schema):
        ref = reference(ref)
        view = self.artifacts.read(ref)
        try:
            raw = b64decode(view["data_base64"], validate=True)
            wrapper = json.loads(raw)
            shape(wrapper, {"schema", "record", "rights_basis"})
            require(wrapper["schema"] == "watch-execution-custody-v1" and encoded(wrapper) == raw,
                    "WATCH_EXECUTION_RECORD_INVALID")
            record = wrapper["record"]
            require(isinstance(record, dict) and record.get("schema") == schema, "WATCH_EXECUTION_RECORD_SCHEMA")
            artifact = self.artifact(record, wrapper["rights_basis"])
            require(artifact.path() == ref["path"] and artifact.metadata() == view["metadata"],
                    "WATCH_EXECUTION_RECORD_CHANGED")
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecordError):
                raise
            raise RecordError("WATCH_EXECUTION_RECORD_INVALID") from None
        return ref, record, wrapper["rights_basis"]

    def journal(self, submission_id, *, snapshot=None):
        """Find the original saved intent without inventing its missing clock.

        Absence requires both missing current bytes and no original path
        history. Access, budget, corruption and unknown effects propagate to
        the caller and cannot be turned into a new write.
        """
        head = snapshot or self.store.backend.head()
        path = "submissions/" + identifier(submission_id) + ".json"
        with watch_read_scope(self.store.backend, head) as native:
            # Journals are append-only identities. Missing now is not equivalent
            # to never saved, and a deletion/reintroduction cannot replace clocks.
            history = native._run("log", "--first-parent", "--full-history", "--diff-merges=first-parent",
                "--no-patch", "-2", "--format=%H", head, "--", path).stdout.decode("ascii").split()
            try:
                raw = native.read(head, path)
            except MissingObject:
                require(not history, "WATCH_EXECUTION_JOURNAL_MISSING_FROM_HISTORY")
                return None
            require(len(history) == 1, "WATCH_EXECUTION_JOURNAL_HISTORY_CHANGED")
            try:
                journal = json.loads(raw)
                shape(journal, {"schema", "intent_sha256", "result_path"})
                require(journal["schema"] == "submission-v1" and encoded(journal) == raw
                        and isinstance(journal["intent_sha256"], str) and DIGEST.fullmatch(journal["intent_sha256"]),
                        "WATCH_EXECUTION_JOURNAL_INVALID")
                ref = reference(native.reference(history[0], journal["result_path"]))
            except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
                if isinstance(exc, RecordError):
                    raise
                raise RecordError("WATCH_EXECUTION_JOURNAL_INVALID") from None
            # The exact historical target remains readable for reconciliation
            # even when today's family is incomplete. New writes separately
            # require a complete current prefix and cannot reset missing state.
            return {"reference": ref, "intent_sha256": journal["intent_sha256"]}

    def verify_journal(self, journal, record, rights_basis):
        intent = {"operation": "research-artifact", "input": asdict(self.artifact(record, rights_basis))}
        require(journal["intent_sha256"] == digest(encoded(intent)), "WATCH_EXECUTION_JOURNAL_INTENT_MISMATCH")

    def verify_atomic(self, ref, snapshot, *, submission_id=None):
        """A declaration cannot replace the original commit's real CAS parent."""
        ref = reference(ref)
        # The event being proved is not itself the authoritative records head.
        with watch_read_scope(self.store.backend) as native:
            verify_event_parent(native, ref["commit"], snapshot)
            paths = [ref["path"], ref["path"].rsplit("/", 1)[0] + "/content.bin"]
            if submission_id is not None:
                paths.append("submissions/" + identifier(submission_id) + ".json")
            for path in paths:
                require(native.last_change(ref["commit"], path) == ref["commit"], "WATCH_EXECUTION_SAVE_NOT_ATOMIC")
                history = native._run("log", "--first-parent", "--full-history", "--diff-merges=first-parent",
                    "--no-patch", "-1", "--format=%H", snapshot, "--", path).stdout.strip()
                require(not history, "WATCH_EXECUTION_SAVE_PATH_HAS_HISTORY")
                try:
                    native.read(snapshot, path)
                except MissingObject:
                    continue
                raise RecordError("WATCH_EXECUTION_SAVE_PATH_ALREADY_PRESENT")

    def artifact_at_snapshot(self, ref, snapshot):
        """Keep the original exact ref; prove manifest AND representation membership."""
        ref = reference(ref)
        with watch_read_scope(self.store.backend, snapshot) as native:
            original = native.resolve(BlobRef(**ref))
            ancestry = native._run("merge-base", "--is-ancestor", ref["commit"], snapshot, allowed=(0, 1, 128))
            require(ancestry.returncode == 0, "WATCH_DELIVERY_RECEIPT_OUTSIDE_SNAPSHOT")
            require(native.read(snapshot, ref["path"]) == original, "WATCH_DELIVERY_RECEIPT_OUTSIDE_SNAPSHOT")
            view = ResearchArtifacts(VersionedStore(native)).read(ref, include_data=False)
            body_path = ref["path"].rsplit("/", 1)[0] + "/content.bin"
            body = native.read(snapshot, body_path)
            require(digest(body) == view["metadata"]["sha256"] and len(body) == view["metadata"]["size"],
                    "WATCH_DELIVERY_RECEIPT_OUTSIDE_SNAPSHOT")
            return view

    def put(self, record, rights_basis, submission_id, *, before_new_write=None, reconcile=False):
        artifact = self.artifact(record, rights_basis)
        if reconcile:
            intent = {"operation": "research-artifact", "input": asdict(artifact)}
            ref = self.store._replay(self.store.backend.head(), submission_id, intent)
            if ref is None:
                raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
            self.artifacts.read(ref)
            return reference(ref)
        return reference(self.artifacts.put(submission_id, artifact, before_new_write=before_new_write))


class WatchExecution:
    def __init__(self, store, registry=None, *, clock=None):
        self.store, self.registry = store, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.records = _Records(store)
        self.activity = WatchActivity(store, registry, clock=self.clock)

    def _now(self):
        return utc(self.clock().isoformat())

    @_native_read
    def _activity_event(self, ref, *, selected_at):
        """New execution qualifies both original history and this selection.

        A genuine old active prefix does not prove that a later snapshot may
        select it after retirement. Keep exact v1 readers unchanged while
        applying the original native pointer/history proofs at this new use.
        """
        reader = _Sources(self.store.backend)
        event = watch_history.event_record(ref, reader)
        selected = self.activity._history_current(event["watch_key"], selected_at, reader, self.store.backend)
        require(selected == reference(ref), "WATCH_EXECUTION_ACTIVITY_SELECTION_CHANGED")
        if event["schema"] == "watch-activity-event-v1":
            watch_history.verify_legacy_prefix(self.activity, ref, reader, self.store.backend)
        else:
            event = self.activity.read(ref, include_current_use=False)["event"]
        return event

    @_native_read
    def _plan(self, value):
        plan = validate_plan(value)
        frozen = OddsWatch(self.store, self.registry, clock=self.clock).read_watch(plan["watch_ref"])
        window = frozen["watch"]["window"]
        last = slot_window(plan, plan["slot_count"] - 1)
        require(timestamp(plan["starts_at"]) >= timestamp(window["starts_at"])
                and timestamp(last["deadline"]) < timestamp(window["ends_at"]), "WATCH_EXECUTION_OUTSIDE_WATCH_WINDOW")
        require(plan["interval_seconds"] >= window["minimum_interval_seconds"], "WATCH_EXECUTION_WATCH_INTERVAL_REQUIRED")
        return {"schema": PLAN_SCHEMA, "plan": plan, "plan_sha256": digest(encoded(plan)),
                "watch_key": frozen["watch_sha256"], "effects": effects()}

    def freeze(self, payload, *, submission_id, reconcile=False):
        record = self._plan(publication(payload, "plan"))
        ref = self.records.put(record, payload["rights_basis"], submission_id, reconcile=reconcile)
        return {"reference": ref, "plan_sha256": record["plan_sha256"], "effects": effects()}

    @_native_read
    def read_plan(self, ref):
        ref, record, basis = self.records.open(ref, PLAN_SCHEMA)
        shape(record, {"schema", "plan", "plan_sha256", "watch_key", "effects"})
        require(encoded(self._plan(record["plan"])) == encoded(record), "WATCH_EXECUTION_PLAN_CHANGED")
        return {"reference": ref, "record": record, "rights_basis": basis, "recalculation": "matches"}

    @_native_read
    def preview(self, payload):
        query = validate_query(payload)
        require(timestamp(query["at"]) <= timestamp(self._now()), "WATCH_EXECUTION_FUTURE_SELECTION")
        plan = self.read_plan(query["plan_ref"])["record"]["plan"]
        selection = select_slot(plan, query["at"])
        head = self.store.backend.head()
        current = self.activity.current(plan["watch_ref"], snapshot=head)
        state = self._activity_event(current["reference"], selected_at=head)["state"] if current["reference"] else None
        out = {"schema": "watch-execution-preview-v1", "query": query, "selection": selection,
               "activity": {"reference": current["reference"], "state": state}, "snapshot": head,
               "existing_slot": None, "next_request": None, "write": "not-requested", "effects": effects()}
        if selection["slot"] is not None:
            key = slot_identity(plan, selection["slot"]["slot_index"])
            keys = submission_keys(key)
            out["slot_key"], out["submission_ids"] = key, keys
            saved = self.records.journal(keys["reserve"], snapshot=head)
            if saved:
                existing = self.read_slot(saved["reference"])
                out["existing_slot"] = {"reference": existing["reference"],
                    "same_input": existing["record"]["query"] == query, "status": self.status(existing["reference"])}
            elif selection["state"] == "due" and state == "active":
                out["next_request"] = {"stage": "reserve", "submission_id": keys["reserve"],
                    "expected_head": head, "execution": query}
        if self.store.backend.head() != head:
            raise Conflict("WATCH_EXECUTION_PREVIEW_SNAPSHOT_CHANGED")
        return out

    def reserve(self, payload, *, submission_id=None, reconcile=False):
        query = validate_query(publication(payload, "execution"))
        plan_record = self.read_plan(query["plan_ref"])["record"]
        plan = plan_record["plan"]
        selection = select_slot(plan, query["at"])
        require(selection["state"] == "due", "WATCH_EXECUTION_SLOT_NOT_DUE_NO_BACKFILL")
        key = slot_identity(plan, selection["slot"]["slot_index"])
        submission_id = self._key(submission_id, submission_keys(key)["reserve"])
        saved = self.records.journal(submission_id)
        if saved:
            existing = self.read_slot(saved["reference"])
            if existing["record"]["query"] != query or existing["rights_basis"] != payload["rights_basis"]:
                raise Conflict("SUBMISSION_CONTENT_CONFLICT")
            return self._slot_receipt(existing["reference"], existing["record"])
        if reconcile:
            raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        now = self._now()
        actual = select_slot(plan, now)
        require(timestamp(query["at"]) <= timestamp(now) and actual == selection,
                "WATCH_EXECUTION_SLOT_NOT_DUE_NO_BACKFILL")
        head = self.store.backend.head()
        current = self.activity.current(plan["watch_ref"], snapshot=head)["reference"]
        require(current is not None, "WATCH_ACTIVITY_NOT_REGISTERED")
        active = self._activity_event(current, selected_at=head)
        require(active["state"] == "active", "WATCH_ACTIVITY_NOT_ACTIVE")
        record = {"schema": SLOT_SCHEMA, "plan_ref": query["plan_ref"], "plan_sha256": plan_record["plan_sha256"],
            "slot_key": key, **selection["slot"], "query": query, "reserved_at": now,
            "snapshot_before": head, "expected_event": current, "effects": effects()}
        def precondition(at):
            if at != head:
                raise Conflict("WATCH_EXECUTION_RESERVATION_SNAPSHOT_CHANGED")
        ref = self.records.put(record, payload["rights_basis"], submission_id, before_new_write=precondition)
        return self._slot_receipt(ref, record)

    @staticmethod
    def _key(supplied, expected):
        require(supplied is None or supplied == expected, "WATCH_EXECUTION_STABLE_SUBMISSION_REQUIRED")
        return expected

    @staticmethod
    def _slot_receipt(ref, slot):
        return {"reference": ref, "slot_key": slot["slot_key"], "submission_ids": submission_keys(slot["slot_key"]),
                "records_snapshot": ref["commit"], "reserved_at": slot["reserved_at"], "effects": effects()}

    @_native_read
    def read_slot(self, ref):
        ref, slot, basis = self.records.open(ref, SLOT_SCHEMA)
        shape(slot, {"schema", "plan_ref", "plan_sha256", "slot_key", "slot_index", "due_at", "deadline", "query",
                     "reserved_at", "snapshot_before", "expected_event", "effects"})
        plan_record = self.read_plan(slot["plan_ref"])["record"]
        plan = plan_record["plan"]
        query = validate_query(slot["query"])
        window = slot_window(plan, slot["slot_index"])
        require(slot["plan_sha256"] == plan_record["plan_sha256"] and slot["slot_key"] == slot_identity(plan, slot["slot_index"])
                and all(slot[k] == value for k, value in window.items()) and query == slot["query"]
                and query["plan_ref"] == slot["plan_ref"] and slot["effects"] == effects(), "WATCH_EXECUTION_SLOT_CHANGED")
        reserved_selection = select_slot(plan, slot["reserved_at"])
        require(select_slot(plan, query["at"]) == reserved_selection
                and reserved_selection["state"] == "due" and reserved_selection["slot"] == window
                and timestamp(slot["reserved_at"]) >= timestamp(query["at"]), "WATCH_EXECUTION_SLOT_CLOCK_CHANGED")
        current = self.activity.current(plan["watch_ref"], snapshot=slot["snapshot_before"])["reference"]
        require(current == slot["expected_event"] and current is not None, "WATCH_EXECUTION_SLOT_ACTIVITY_CHANGED")
        require(self._activity_event(current, selected_at=slot["snapshot_before"])["state"] == "active",
                "WATCH_ACTIVITY_NOT_ACTIVE")
        journal = self.records.journal(submission_keys(slot["slot_key"])["reserve"], snapshot=ref["commit"])
        require(journal is not None and reference_identity(journal["reference"]) == reference_identity(ref),
                "WATCH_EXECUTION_SLOT_JOURNAL_REQUIRED")
        self.records.verify_journal(journal, slot, basis)
        # Aliases cannot move the slot's fixed snapshot forward to another commit.
        original = journal["reference"]
        self.records.verify_atomic(original, slot["snapshot_before"],
                                   submission_id=submission_keys(slot["slot_key"])["reserve"])
        return {"reference": original, "record": slot, "plan": plan, "rights_basis": basis, "recalculation": "matches"}

    @_native_read
    def _saved_check(self, slot_view, *, snapshot=None):
        slot = slot_view["record"]
        journal = self.records.journal(submission_keys(slot["slot_key"])["check"], snapshot=snapshot)
        if journal is None:
            return None
        event = watch_history.event_record(journal["reference"], _Sources(self.store.backend))
        require(event["command"]["kind"] == "check", "WATCH_EXECUTION_CHECK_KIND_CHANGED")
        check = event["command"]["input"]
        required = {"watch_ref": slot_view["plan"]["watch_ref"], "expected_event": slot["expected_event"],
                    "records_snapshot": slot_view["reference"]["commit"], "input": slot["query"]["input"],
                    "review_reasons": slot["query"]["review_reasons"]}
        require(all(check[k] == value for k, value in required.items()), "WATCH_EXECUTION_CHECK_INPUT_CHANGED")
        require(timestamp(slot["reserved_at"]) <= timestamp(check["checked_at"]) <= timestamp(slot["deadline"])
                and timestamp(check["checked_at"]) >= timestamp(slot["due_at"]),
                "WATCH_EXECUTION_CHECK_CLOCK_CHANGED")
        intent = {"operation": "watch-activity-check", "input": {"check": check, "rights_confirmed": True,
                  "rights_basis": slot_view["rights_basis"]}}
        require(journal["intent_sha256"] == digest(encoded(intent)), "WATCH_EXECUTION_JOURNAL_INTENT_MISMATCH")
        self.records.verify_atomic(journal["reference"], slot_view["reference"]["commit"],
                                   submission_id=submission_keys(slot["slot_key"])["check"])
        current = self.activity.current(slot_view["plan"]["watch_ref"], snapshot=journal["reference"]["commit"])
        require(current["reference"] == journal["reference"], "WATCH_EXECUTION_CHECK_NOT_ATOMICALLY_SELECTED")
        event = self._activity_event(journal["reference"], selected_at=journal["reference"]["commit"])
        return {"reference": journal["reference"], "event": event}

    def check(self, payload, *, submission_id=None, reconcile=False):
        shape(payload, {"slot_ref"})
        slot_view = self.read_slot(payload["slot_ref"])
        slot = slot_view["record"]
        key = self._key(submission_id, submission_keys(slot["slot_key"])["check"])
        saved = self._saved_check(slot_view)
        if saved:
            event = saved["event"]
            return {"reference": saved["reference"], "event_sha256": digest(encoded(event)), "state": event["state"],
                    "check_status": event["result"]["status"], "watch_key": event["watch_key"],
                    "slot_ref": slot_view["reference"]}
        if reconcile:
            raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        head = self.store.backend.head()
        if head != slot_view["reference"]["commit"]:
            raise Conflict("WATCH_EXECUTION_SLOT_SNAPSHOT_CHANGED")
        now = self._now()
        require(timestamp(slot["reserved_at"]) <= timestamp(now) <= timestamp(slot["deadline"])
                and timestamp(now) >= timestamp(slot["due_at"]),
                "WATCH_EXECUTION_SLOT_MISSED_NO_BACKFILL")
        check = {"watch_ref": slot_view["plan"]["watch_ref"], "expected_event": slot["expected_event"],
            "checked_at": now, "records_snapshot": head, "input": slot["query"]["input"],
            "review_reasons": slot["query"]["review_reasons"]}
        receipt = self.activity.save_check({"check": check, "rights_confirmed": True,
                                           "rights_basis": slot_view["rights_basis"]}, submission_id=key)
        return {**receipt, "slot_ref": slot_view["reference"]}

    @staticmethod
    def _result(slot_view, saved, snapshot):
        slot = slot_view["record"]
        return {"schema": RESULT_SCHEMA, "slot_key": slot["slot_key"], "slot_ref": slot_view["reference"],
            "snapshot_before": snapshot,
            "check_ref": saved["reference"], "event_sha256": digest(encoded(saved["event"])),
            "checked_at": saved["event"]["result"]["request"]["checked_at"],
            "notices": notice_intents(slot_view["plan"], saved["reference"], saved["event"]), "effects": effects()}

    def finish(self, payload, *, submission_id=None, reconcile=False):
        shape(payload, {"slot_ref"})
        slot_view = self.read_slot(payload["slot_ref"])
        key = self._key(submission_id, submission_keys(slot_view["record"]["slot_key"])["finish"])
        journal = self.records.journal(key)
        if journal:
            result = self.read_result(journal["reference"])
            require(result["record"]["slot_ref"] == slot_view["reference"], "WATCH_EXECUTION_RESULT_CHANGED")
            return {"reference": result["reference"], "record": result["record"], "effects": effects()}
        if reconcile:
            raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        head = self.store.backend.head()
        saved = self._saved_check(slot_view, snapshot=head)
        if saved is None:
            raise MissingObject("WATCH_EXECUTION_CHECK_NOT_SAVED")
        record = self._result(slot_view, saved, head)
        def precondition(at):
            if at != head:
                raise Conflict("WATCH_EXECUTION_FINISH_SNAPSHOT_CHANGED")
        ref = self.records.put(record, slot_view["rights_basis"], key, before_new_write=precondition)
        return {"reference": ref, "record": record, "effects": effects()}

    @_native_read
    def read_result(self, ref):
        ref, record, basis = self.records.open(ref, RESULT_SCHEMA)
        shape(record, {"schema", "slot_key", "slot_ref", "snapshot_before", "check_ref", "event_sha256", "checked_at", "notices", "effects"})
        slot_view = self.read_slot(record["slot_ref"])
        key = submission_keys(slot_view["record"]["slot_key"])["finish"]
        journal = self.records.journal(key, snapshot=ref["commit"])
        require(journal is not None and reference_identity(journal["reference"]) == reference_identity(ref),
                "WATCH_EXECUTION_RESULT_JOURNAL_REQUIRED")
        self.records.verify_journal(journal, record, basis)
        original = journal["reference"]
        self.records.verify_atomic(original, record["snapshot_before"], submission_id=key)
        saved = self._saved_check(slot_view, snapshot=record["snapshot_before"])
        require(saved is not None and encoded(self._result(slot_view, saved, record["snapshot_before"])) == encoded(record)
                and basis == slot_view["rights_basis"], "WATCH_EXECUTION_RESULT_CHANGED")
        return {"reference": original, "record": record, "rights_basis": basis, "recalculation": "matches"}

    @_native_read
    def status(self, slot_ref):
        slot_view = self.read_slot(slot_ref)
        slot = slot_view["record"]
        keys = submission_keys(slot["slot_key"])
        head = self.store.backend.head()
        saved = self._saved_check(slot_view, snapshot=head)
        out = {"schema": "watch-execution-status-v1", "slot_ref": slot_view["reference"], "slot_key": slot["slot_key"],
               "snapshot": head, "check_ref": saved["reference"] if saved else None, "result_ref": None,
               "state": "reserved", "next_request": None, "write": "not-requested", "effects": effects()}
        if saved:
            finished = self.records.journal(keys["finish"], snapshot=head)
            if finished:
                result = self.read_result(finished["reference"])
                self.records.verify_journal(finished, result["record"], result["rights_basis"])
                out.update(state="recorded", result_ref=result["reference"])
            else:
                out.update(state="check-saved-intents-pending", next_request={"stage": "finish", "submission_id": keys["finish"],
                    "expected_head": head, "payload": {"slot_ref": slot_view["reference"]}})
        elif head != slot_view["reference"]["commit"]:
            out.update(state="snapshot-conflict", reason="WATCH_EXECUTION_SLOT_SNAPSHOT_CHANGED",
                       continuation="wait-for-next-actual-slot; fixed-snapshot-must-not-be-refreshed")
        else:
            now = timestamp(self._now())
            if now > timestamp(slot["deadline"]):
                out.update(state="missed", reason="WATCH_EXECUTION_SLOT_MISSED_NO_BACKFILL",
                           continuation="wait-for-next-actual-slot; no-past-observation-created")
            elif now < timestamp(slot["reserved_at"]):
                out.update(state="clock-before-reservation")
            else:
                out["next_request"] = {"stage": "check", "submission_id": keys["check"], "expected_head": head,
                                       "payload": {"slot_ref": slot_view["reference"]}}
        return out

    def advance(self, slot_ref, *, mode="preview"):
        require(mode in ("preview", "save"), "WATCH_EXECUTION_MODE_INVALID")
        state = self.status(slot_ref)
        request = state["next_request"]
        if mode == "preview" or request is None:
            return state
        operation = self.check if request["stage"] == "check" else self.finish
        receipt = operation(request["payload"], submission_id=request["submission_id"])
        return {"stage": request["stage"], "receipt": receipt, "effects": effects()}
