"""Bounded discovery of stored Odds relationships, never a second Odds truth.

Each page verifies custody and snapshot membership. It does not replay market
qualification or Watch history: those remain the exact readers in open_payload.
An exhausted cursor says nothing about whether its caller read earlier pages.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import re

from decision_kernel.application.conditional_odds import (
    ConditionalOdds, RECORD_LAYOUTS, RESULT_RECORD, SPEC_RECORD,
)
from decision_kernel.application.market_reading_scope import SourceReads, operation_failure
from decision_kernel.application.odds_market import CLOSE_PRICE_KINDS
from decision_kernel.application.research_agenda import RESEARCH_PATH, research_reference
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application import watch_history
from decision_kernel.domain.conditional_odds import (
    number, shape, timestamp, validate_price, validate_spec,
)
from decision_kernel.domain.full_watch import CHECK_SCHEMA as FULL_CHECK_SCHEMA, SCHEMA as FULL_WATCH_SCHEMA
from decision_kernel.domain.odds_watch import CHECK_RECORD, WATCH_RECORD, validate_request, validate_watch
from decision_kernel.domain.probability_odds import (
    ASSESSMENT_RECORD, RESULT_RECORD as PROBABILITY_RECORD, ZONES, validate_assessment,
)
from decision_kernel.domain.records import (
    BlobRef, MissingObject, RecordError, SHA, StoreUnavailable, digest, encoded, require,
)
from decision_kernel.domain.research_artifacts import ARTIFACT_PATH, artifact_reference
from decision_kernel.domain.watch_activity import ACTIVITY_PREFIX, ACTIVITY_RECORD
from decision_kernel.domain.watch_conditions import CHECK_SCHEMA as CONDITIONS_CHECK_SCHEMA, SCHEMA as CONDITIONS_WATCH_SCHEMA
from decision_kernel.domain.watch_history import SEGMENT_PREFIX

CURSOR_SCHEMA = "odds-book-cursor-v1"
MAX_CANDIDATES = 8192
MAX_CARD_BYTES = 128 * 1024
MAX_VIEW_BYTES = 4 * 1024 * 1024
POINTER = re.compile(r"research/watch-activities/([a-f0-9]{64})/current\.json\Z")
CASE_POINTER = re.compile(r"research/cases/([a-z0-9][a-z0-9_-]{0,95})/current\.json\Z")
OPERATIONS = {
    SPEC_RECORD: "read-valuation", RESULT_RECORD: "read-odds",
    ASSESSMENT_RECORD: "read-odds-assessment", PROBABILITY_RECORD: "read-probability-odds",
    WATCH_RECORD: "read-odds-watch", CHECK_RECORD: "read-odds-watch-check",
    ACTIVITY_RECORD: "read-odds-watch-activity", "segment": "read-odds-watch-activity-segment",
    "research": "resume", "research-pointer": "resume", "activity-pointer": "read-odds-watch-activity",
}


def validate_plan(payload):
    """Pure preflight; cursors carry positions and filters, never read receipts."""
    require(isinstance(payload, dict) and set(payload) <= {
        "snapshot", "cursor", "limit", "scan_limit", "research_ref"}, "ODDS_BOOK_PLAN_INVALID")
    plan = {"snapshot": payload.get("snapshot"), "cursor": deepcopy(payload.get("cursor")),
            "limit": payload.get("limit", 4), "scan_limit": payload.get("scan_limit", 16),
            "research_ref": research_reference(payload["research_ref"])
            if payload.get("research_ref") is not None else None}
    require(type(plan["limit"]) is int and 1 <= plan["limit"] <= 8, "ODDS_BOOK_PAGE_LIMIT")
    require(type(plan["scan_limit"]) is int and 1 <= plan["scan_limit"] <= 32, "ODDS_BOOK_SCAN_LIMIT")
    cursor = plan["cursor"]
    if cursor is not None:
        require(isinstance(cursor, dict) and set(cursor) == {
            "schema", "repository", "snapshot", "offset", "research_ref"}
            and cursor["schema"] == CURSOR_SCHEMA
            and isinstance(cursor["repository"], str) and 0 < len(cursor["repository"]) <= 1024
            and type(cursor["offset"]) is int and 0 <= cursor["offset"] <= MAX_CANDIDATES,
            "ODDS_BOOK_CURSOR_INVALID")
        selected = research_reference(cursor["research_ref"]) if cursor["research_ref"] is not None else None
        require("research_ref" not in payload or plan["research_ref"] == selected,
                "ODDS_BOOK_CURSOR_FILTER_CHANGED")
        require(plan["snapshot"] is None or plan["snapshot"] == cursor["snapshot"],
                "ODDS_BOOK_CURSOR_SNAPSHOT_CHANGED")
        plan.update(snapshot=cursor["snapshot"], research_ref=selected)
    require(plan["snapshot"] is None or isinstance(plan["snapshot"], str)
            and SHA.fullmatch(plan["snapshot"]) is not None, "ODDS_BOOK_SNAPSHOT_INVALID")
    require(cursor is None or plan["snapshot"] is not None, "ODDS_BOOK_CURSOR_INVALID")
    return plan


def _identity(reference):
    ref = reference if isinstance(reference, BlobRef) else BlobRef(**reference)
    return {"repository": ref.repository, "path": ref.path, "sha256": ref.sha256}


def _key(reference):
    return digest(encoded(_identity(reference)))


def _output_number(value):
    # Result strings may contain the engine's full 80-digit arithmetic output;
    # input number() deliberately has a different 48-digit ingestion bound.
    require(isinstance(value, str) and len(value) <= 256
            and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is not None,
            "ODDS_BOOK_RESULT_NUMBER_INVALID")


def _result_shape(value, spec, assessment=None):
    """Validate displayed field types and identities, without redoing arithmetic."""
    probability = assessment is not None
    price = value["price"]
    require(isinstance(price, dict), "ODDS_BOOK_RESULT_PRICE_INVALID")
    if price.get("schema") == "odds-price-v1":
        require(not probability and value["schema"] == "conditional-odds-v1", "ODDS_BOOK_RESULT_PRICE_INVALID")
        validate_price(price, spec)
    else:
        shape(price, {"schema", "price_id", "kind", "security", "currency", "share_count",
                      "price", "as_of", "timestamp_semantics", "share_count_qualification"})
        require(price["schema"] == "odds-qualified-close-v1"
                and price["kind"] in ("retained-ftshare-raw-close", "retained-relay-raw-close")
                and price["security"] == spec["security"] and price["currency"] == spec["currency"]
                and number(price["share_count"]) == number(spec["share_count"])
                and number(price["price"]) > 0, "ODDS_BOOK_RESULT_PRICE_INVALID")
        require(probability or value["schema"] == "conditional-odds-retained-close-v1",
                "ODDS_BOOK_RESULT_PRICE_INVALID")
        timestamp(price["as_of"])
    worlds = value["world_results"]
    require(isinstance(worlds, list) and len(worlds) == len(spec["worlds"])
            and all(isinstance(world, dict) for world in worlds), "ODDS_BOOK_RESULT_WORLDS_INVALID")
    require(sorted(world["world_id"] for world in worlds) == sorted(world["id"] for world in spec["worlds"]),
            "ODDS_BOOK_RESULT_WORLDS_INVALID")
    metric = "value_to_price_distance" if spec["value_kind"] == "current-fair-value" else "undiscounted_holding_period_return"
    for world in worlds:
        keys = ("equity_value_per_share", metric)
        if spec["value_kind"] == "future-exit":
            keys += ("total_distributions_per_share", "total_payoff_per_share")
            require(isinstance(world["distributions"], list), "ODDS_BOOK_RESULT_WORLDS_INVALID")
        for key in keys:
            _output_number(world[key])
    require(value["human_acceptance"] == "not-confirmed" and value["investment_authority"] == "none",
            "ODDS_BOOK_RESULT_AUTHORITY_INVALID")
    require(value["is_annualized"] is False and value["is_discounted"] is False,
            "ODDS_BOOK_RESULT_METHOD_INVALID")
    if probability:
        require(isinstance(value["aggregate"], dict)
                and value["method"] == "declared-probability-nominal-return-v1"
                and value["probability_status"] == "explicit-research-assessment-not-empirically-certified",
                "ODDS_BOOK_PROBABILITY_RESULT_INVALID")
        shape(value["aggregate"], {"weighted_terminal_equity_per_share", "weighted_distributions_per_share",
            "weighted_total_payoff_per_share", "expected_holding_period_return", "positive_return_scenario_probability"})
        for amount in value["aggregate"].values(): _output_number(amount)
        probabilities = {row["world_id"]: number(row["probability"]) for row in assessment["probabilities"]}
        for world in worlds:
            require(number(world["probability"]) == probabilities[world["world_id"]],
                    "ODDS_BOOK_RESULT_PROBABILITY_MISMATCH")
        require(value["participation_zone"] in ("INSUFFICIENT_ODDS",) + ZONES,
                "ODDS_BOOK_RESULT_ZONE_INVALID")
    else:
        require(value["value_kind"] == spec["value_kind"]
                and value["method"] == spec["method"]
                and value["valuation_date"] == spec["valuation_date"] and value["target_date"] == spec["target_date"]
                and value["probability_status"] == "not-established" and value["weighted_aggregate"] is None,
                "ODDS_BOOK_CONDITIONAL_RESULT_INVALID")
    if probability or value["schema"] == "conditional-odds-retained-close-v1":
        calculation = value["calculation_input"]
        shape(calculation, {"assessment_ref", "market", "evaluated_at"} if probability else
              {"spec_ref", "market", "market_policy", "evaluated_at"})
        field = "assessment_ref" if probability else "spec_ref"
        require(_key(calculation[field]) == _key(value[field]), "ODDS_BOOK_RESULT_INPUT_MISMATCH")
        timestamp(calculation["evaluated_at"])
        if not probability:
            market = calculation["market"]
            require(isinstance(market, dict) and isinstance(market.get("schema"), str)
                    and CLOSE_PRICE_KINDS.get(market["schema"]) == price["kind"]
                    and market.get("price_id") == price["price_id"],
                    "ODDS_BOOK_RESULT_INPUT_MISMATCH")


def _check_shape(value, watch, watch_ref):
    """Check stored control types and frozen identities, without replaying truth."""
    schemas = {"odds-watch-v1": "odds-watch-check-v1",
               CONDITIONS_WATCH_SCHEMA: CONDITIONS_CHECK_SCHEMA, FULL_WATCH_SCHEMA: FULL_CHECK_SCHEMA}
    require(isinstance(value, dict) and value.get("schema") == schemas[watch["schema"]],
            "ODDS_BOOK_CHECK_VERSION_MISMATCH")
    request = validate_request(value["request"])
    require(_key(request["watch_ref"]) == _key(watch_ref), "ODDS_BOOK_CHECK_WATCH_MISMATCH")
    controls = [value]
    if watch["schema"] in (CONDITIONS_WATCH_SCHEMA, FULL_WATCH_SCHEMA):
        rows = value["condition_results"]
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                and [row["condition_id"] for row in rows] == [row["condition_id"] for row in watch["conditions"]],
                "ODDS_BOOK_CHECK_CONDITIONS_MISMATCH")
        controls.extend(rows)
    # These are the stored outputs of odds_watch._project_single; observations
    # use a separate vocabulary and are deliberately not re-evaluated here.
    statuses = ("not-enabled", "not-due", "ended", "cannot-determine", "review-required",
                "not-met", "first-trigger", "continued-trigger")
    for control in controls:
        require(control["status"] in statuses, "ODDS_BOOK_CHECK_STATUS_INVALID")
        require(all(type(control[key]) is bool for key in ("check_performed", "gap_since_decisive", "ever_triggered"))
                and all(control[key] is None or type(control[key]) is bool
                        for key in ("condition_met", "last_decisive_match")),
                "ODDS_BOOK_CHECK_CONTROL_INVALID")


@contextmanager
def _scope(backend, snapshot):
    # Reuse the original transport, native caches, budget and visibility latch.
    # The Room helper selects live HEAD; a Book cursor instead pins its own SHA.
    from decision_kernel.infrastructure.github_reading import _ReadingScope
    from decision_kernel.infrastructure.github_store import GitHubStore

    backend.check_commit(snapshot)
    authority = backend.owner if type(backend) is _ReadingScope else backend
    if authority is not backend:
        authority.check_commit(snapshot)
    scoped = _ReadingScope(authority, snapshot) if isinstance(authority, GitHubStore) else backend
    reads = SourceReads(scoped)
    try:
        yield VersionedStore(reads), reads
        reads.check()  # A caught terminal failure cannot be healed by a later good read.
        authority.check_commit(snapshot)
        reads.check()
    finally:
        reads.active = False
        if scoped is not backend:
            scoped.active = False


class _Page:
    def __init__(self, store, reads, snapshot, native):
        self.store, self.reads, self.snapshot, self.native = store, reads, snapshot, native
        self.bound, self.values, self.research = {}, {}, {}

    def bind(self, reference):
        ref = reference if isinstance(reference, BlobRef) else BlobRef(**reference)
        token = encoded(asdict(ref))
        if token not in self.bound:
            require(ref.repository == self.reads.repository, "FOREIGN_REPOSITORY")
            # GitHub's original _ReadingScope checks ancestry to this snapshot.
            # Local Git retains the same restriction without changing its reader.
            from decision_kernel.infrastructure.git_store import GitStore
            if isinstance(self.native, GitStore):
                self.reads.check_commit(ref.commit)
                result = self.native._run("merge-base", "--is-ancestor", ref.commit,
                                          self.snapshot, allowed=(0, 1, 128))
                require(result.returncode == 0, "ODDS_BOOK_REFERENCE_AFTER_SNAPSHOT")
            raw = self.reads.resolve(ref)
            require(self.reads.read(self.snapshot, ref.path) == raw, "ODDS_BOOK_SNAPSHOT_BYTES_CHANGED")
            self.bound[token] = asdict(ref)
        return deepcopy(self.bound[token])

    def reference(self, path):
        self.reads.read(self.snapshot, path)  # Missing companions are not invented original refs.
        original = self.reads.last_change(self.snapshot, path)
        return self.bind(self.reads.reference(original, path))

    def references_in(self, value):
        """Displayed refs retain original commits and independently match this snapshot."""
        if isinstance(value, dict):
            if set(value) == {"repository", "commit", "path", "sha256"}:
                self.bind(value)
            else:
                for item in value.values():
                    self.references_in(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.references_in(item)

    def open(self, reference, schema):
        ref = self.bind(artifact_reference(reference))
        token = (encoded(ref), schema)
        if token not in self.values:
            custody = ResearchArtifacts(self.store).read(ref, include_data=False)
            self.bind(BlobRef(ref["repository"], ref["commit"],
                ref["path"].rsplit("/", 1)[0] + "/content.bin", custody["metadata"]["sha256"]))
            value = (watch_history.open_segment(ref, self.store) if schema == "segment" else
                     watch_history.event_record(ref, self.store) if schema == ACTIVITY_RECORD else
                     ConditionalOdds(self.store)._open(ref, schema))
            self.values[token] = value
        return deepcopy(self.values[token])

    def research_context(self, reference):
        ref = self.bind(research_reference(reference))
        token = encoded(ref)
        if token not in self.research:
            view = self.store.read_revision(BlobRef(**ref))
            record, payload = view["record"], view["record"]["input"]
            self.bind(BlobRef(ref["repository"], ref["commit"], record["body"]["path"], record["body"]["sha256"]))
            current = self.store.current(payload["case_id"], snapshot=self.snapshot)
            current = self.bind(current) if current else None
            context = {"reference": ref, "research_key": _key(ref), "case_id": payload["case_id"],
                "revision_id": payload["revision_id"], "kind": payload["kind"], "mandate": payload["mandate"],
                "method": payload["method"], "stored_at": record["stored_at"],
                "time_basis": "storage-time-not-research-or-market-cutoff",
                "recorded_unresolved": payload["unresolved"], "predecessor": payload["predecessor"],
                "at_snapshot": {"status": "current" if current and _key(current) == _key(ref) else
                    "historical" if current else "pointer-absent-unknown", "reference": current},
                "human_acceptance": record["human_acceptance"], "body_sha256": record["body"]["sha256"]}
            self.references_in(context)
            self.research[token] = context
        return deepcopy(self.research[token])

    def spec(self, reference):
        value = self.open(reference, SPEC_RECORD)
        require(isinstance(value, dict) and value.get("schema") == "frozen-valuation-spec-v1",
                "ODDS_BOOK_SPEC_INVALID")
        spec = validate_spec(value["spec"])
        research = self.research_context(spec["research_ref"])
        require(value["spec_sha256"] == digest(encoded(spec))
                and value["research_body_sha256"] == research["body_sha256"], "ODDS_BOOK_SPEC_BINDING_MISMATCH")
        return value, research

    def assessment(self, reference):
        value = self.open(reference, ASSESSMENT_RECORD)
        require(isinstance(value, dict) and value.get("schema") == "frozen-odds-assessment-v1",
                "ODDS_BOOK_ASSESSMENT_INVALID")
        spec, research = self.spec(value["assessment"]["spec_ref"])
        assessment = validate_assessment(value["assessment"], spec["spec"])
        self.bind(value["research_ref"])
        require(value["assessment_sha256"] == digest(encoded(assessment))
                and value["spec_sha256"] == spec["spec_sha256"]
                and _key(value["research_ref"]) == research["research_key"]
                and value["research_body_sha256"] == research["body_sha256"],
                "ODDS_BOOK_ASSESSMENT_BINDING_MISMATCH")
        return value, research

    def watch(self, reference):
        value = self.open(reference, WATCH_RECORD)
        require(isinstance(value, dict) and "watch" in value, "ODDS_BOOK_WATCH_INVALID")
        watch = validate_watch(value["watch"])
        require(value.get("schema") == "frozen-" + watch["schema"]
                and value["watch_sha256"] == digest(encoded(watch)), "ODDS_BOOK_WATCH_BINDING_MISMATCH")
        research = self.research_context(value["research_ref"])
        basis = watch["basis"]
        if basis["kind"] == "research":
            self.bind(basis["reference"])
            require(_key(basis["reference"]) == research["research_key"], "ODDS_BOOK_WATCH_BINDING_MISMATCH")
        else:
            original, related = (self.assessment(basis["reference"]) if basis["kind"] == "probability"
                                 else self.spec(basis["reference"]))
            require(related["research_key"] == research["research_key"]
                    and original["spec_sha256"] == value["spec_sha256"], "ODDS_BOOK_WATCH_BINDING_MISMATCH")
            if basis["kind"] == "probability":
                require(original["assessment_sha256"] == value["assessment_sha256"],
                        "ODDS_BOOK_WATCH_BINDING_MISMATCH")
        require(value["case_id"] == research["case_id"]
                and value["research_body_sha256"] == research["body_sha256"], "ODDS_BOOK_WATCH_BINDING_MISMATCH")
        tracking = watch.get("tracking", {}).get("odds_result")
        if tracking is not None:
            result = self.open(tracking["reference"], RESULT_RECORD if tracking["kind"] == "conditional" else PROBABILITY_RECORD)
            self.bind(result["research_ref"])
            require(_key(result["research_ref"]) == research["research_key"]
                    and result["spec_sha256"] == value["spec_sha256"], "ODDS_BOOK_WATCH_TRACKING_MISMATCH")
        return value, research

    def item(self, path, kind, row):
        ref = self.reference(path)
        row.update(reference=ref, identity=_identity(ref), open_operation=OPERATIONS.get(kind),
                   open_payload={"reference": ref} if kind in OPERATIONS else None)
        spec_ref = None
        if kind == "research":
            research = self.research_context(ref)
            recorded = {"kind": research["kind"], "recorded_unresolved": research["recorded_unresolved"]}
        elif kind == "research-pointer":
            current = self.store.current(CASE_POINTER.fullmatch(path)[1], snapshot=self.snapshot)
            require(current is not None, "ODDS_BOOK_RESEARCH_POINTER_MISSING")
            research = self.research_context(current)
            recorded = {"selected_research": asdict(current)}
            row["open_payload"] = {"reference": asdict(current)}
        elif kind == SPEC_RECORD:
            value, research = self.spec(ref)
            spec_ref, recorded = ref, value["spec"]
        elif kind == ASSESSMENT_RECORD:
            value, research = self.assessment(ref)
            spec_ref, recorded = value["assessment"]["spec_ref"], value["assessment"]
        elif kind in (RESULT_RECORD, PROBABILITY_RECORD):
            value = self.open(ref, kind)
            require(isinstance(value, dict) and "spec_ref" in value and "price" in value,
                    "ODDS_BOOK_RESULT_INVALID")
            if kind == PROBABILITY_RECORD:
                assessment, research = self.assessment(value["assessment_ref"])
                require(value.get("schema") == "probability-odds-v1"
                        and value["assessment_sha256"] == assessment["assessment_sha256"]
                        and _key(value["spec_ref"]) == _key(assessment["assessment"]["spec_ref"]),
                        "ODDS_BOOK_RESULT_BINDING_MISMATCH")
            else:
                require(value.get("schema") in ("conditional-odds-v1", "conditional-odds-retained-close-v1"),
                        "ODDS_BOOK_RESULT_INVALID")
            spec, research = self.spec(value["spec_ref"])
            self.bind(value["research_ref"])
            require(value["spec_sha256"] == spec["spec_sha256"]
                    and _key(value["research_ref"]) == research["research_key"]
                    and value["research_body_sha256"] == research["body_sha256"],
                    "ODDS_BOOK_RESULT_BINDING_MISMATCH")
            _result_shape(value, spec["spec"], assessment["assessment"] if kind == PROBABILITY_RECORD else None)
            spec_ref, recorded = value["spec_ref"], value
        elif kind == WATCH_RECORD:
            value, research = self.watch(ref)
            recorded = value["watch"]
            basis = recorded["basis"]
            if basis["kind"] == "conditional": spec_ref = basis["reference"]
            if basis["kind"] == "probability": spec_ref = self.assessment(basis["reference"])[0]["assessment"]["spec_ref"]
            pointer = ACTIVITY_PREFIX + value["watch_sha256"] + "/current.json"
            try:
                self.reads.read(self.snapshot, pointer)
                pointer_ref = self.reference(pointer)
            except MissingObject:
                pointer_ref = None
            row["activity_selection"] = {"status": "pointer-present-not-replayed" if pointer_ref else "pointer-absent-unknown",
                "pointer_ref": pointer_ref, "open_operation": "current-odds-watch-activity",
                "open_payload": {"reference": ref}, "history_validation": "not-requested"}
        elif kind in (CHECK_RECORD, ACTIVITY_RECORD, "activity-pointer", "segment"):
            selected = ref
            if kind == "activity-pointer":
                raw = self.reads.resolve(BlobRef(**ref))
                pointer = json.loads(raw)
                shape(pointer, {"schema", "watch_key", "event_path"})
                require(encoded(pointer) == raw and pointer["schema"] == "watch-activity-pointer-v1"
                        and pointer["watch_key"] == POINTER.fullmatch(path)[1]
                        and isinstance(pointer["event_path"], str)
                        and pointer["event_path"].startswith("research/artifacts/odds-watch-activity-"),
                        "ODDS_BOOK_ACTIVITY_POINTER_INVALID")
                selected = self.reference(pointer["event_path"])
                require(selected["commit"] == ref["commit"], "ODDS_BOOK_ACTIVITY_POINTER_NOT_ATOMIC")
                row["open_payload"] = {"reference": selected}
            value = self.open(selected, ACTIVITY_RECORD if kind == "activity-pointer" else kind)
            if kind == CHECK_RECORD:
                require(value.get("schema") in ("odds-watch-check-v1", "odds-watch-check-v2", "odds-watch-check-v3"),
                        "ODDS_BOOK_CHECK_INVALID")
                request = validate_request(value["request"])
                watch_ref = request["watch_ref"]
                recorded = {"recorded_result": value}
            elif kind == "segment":
                watch_ref = value["watch_ref"]
                recorded = {k: value[k] for k in ("watch_key", "segment_index", "kind", "sealed_because", "previous_segment", "entry_event")}
                recorded.update(event_count=len(value["events"]), first_event_ref=value["events"][0],
                                last_event_ref=value["events"][-1], history_validation="not-requested")
            else:
                request = value["command"]["input"]
                watch_ref = request["watch_ref"]
                require(value["state"] in ("paused", "active", "retired")
                        and isinstance(value["summary"], dict), "ODDS_BOOK_ACTIVITY_INVALID")
                recorded = {"event_ref": selected, "watch_key": value["watch_key"],
                    "at": request.get("at", request.get("checked_at")), "action": request.get("action", "check"),
                    "stored_state": value["state"], "recorded_summary": value["summary"],
                    "recorded_result": value["result"], "previous_event": request["expected_event"],
                    "history_validation": "not-requested"}
                if kind == "activity-pointer":
                    require(value["watch_key"] == pointer["watch_key"], "ODDS_BOOK_ACTIVITY_POINTER_KEY_MISMATCH")
            frozen, research = self.watch(watch_ref)
            if kind == CHECK_RECORD:
                _check_shape(value, frozen["watch"], watch_ref)
            elif kind in (ACTIVITY_RECORD, "activity-pointer") and value["result"] is not None:
                _check_shape(value["result"], frozen["watch"], watch_ref)
            if kind != CHECK_RECORD:
                require(value["watch_key"] == frozen["watch_sha256"], "ODDS_BOOK_WATCH_BINDING_MISMATCH")
            row["watch_ref"] = self.bind(watch_ref)
        else:
            raise RecordError("ODDS_BOOK_RECORD_UNSUPPORTED")
        self.references_in(recorded)
        row.update(status="readable", research=research, research_key=research["research_key"],
                   recorded=recorded, custody="verified", semantic_replay="not-requested",
                   qualification="stored-declarations-only", current_validity="not-certified")
        if spec_ref is not None:
            row.update(spec_ref=self.bind(spec_ref), spec_key=_key(spec_ref))
        return row


def _candidates(backend, snapshot):
    rows = set()
    research_paths = backend.paths(snapshot, "research/cases/")
    records = {path if path.endswith("/record.json") else path.rsplit("/", 1)[0] + "/record.json"
               for path in research_paths if path.endswith(("/record.json", "/research.md"))}
    records = {path for path in records if RESEARCH_PATH.fullmatch(path)}
    cases_with_revision = {RESEARCH_PATH.fullmatch(path)[1] for path in records}
    rows.update((path, "research") for path in records)
    for path in research_paths:
        if (match := CASE_POINTER.fullmatch(path)) and match[1] not in cases_with_revision:
            rows.add((path, "research-pointer"))
    layouts = sorted(((prefix, schema) for schema, (prefix, _) in RECORD_LAYOUTS.items()),
                     key=lambda row: len(row[0]), reverse=True)
    for path in backend.paths(snapshot, "research/artifacts/"):
        if path.endswith("/content.bin"):
            path = path.rsplit("/", 1)[0] + "/manifest.json"
        if ARTIFACT_PATH.fullmatch(path) and path.split("/")[2].startswith("odds-"):
            identifier = path.split("/")[2]
            kind = "segment" if identifier.startswith(SEGMENT_PREFIX) else next(
                (schema for prefix, schema in layouts if identifier.startswith(prefix)), "unsupported")
            rows.add((path, kind))
    rows.update((path, "activity-pointer") for path in backend.paths(snapshot, ACTIVITY_PREFIX) if POINTER.fullmatch(path))
    require(len(rows) <= MAX_CANDIDATES, "ODDS_BOOK_CANDIDATE_LIMIT")
    return sorted(rows)


def _relations(items, single_scan):
    groups = {}
    for row in items:
        if row["status"] != "readable": continue
        research, key = row["research"], row["research_key"]
        group = groups.setdefault(key, {"research_key": key, "research": research,
            "original_references": [], "objects": [], "spec_groups": {},
            "coverage": "this-page-only", "saved_odds": "not-observed-in-this-page"})
        if research["reference"] not in group["original_references"]:
            group["original_references"].append(research["reference"])
        group["objects"].append({"kind": row["kind"], "reference": row["reference"],
                                 "open_operation": row["open_operation"], "open_payload": row["open_payload"]})
        if row["kind"] in (RESULT_RECORD, PROBABILITY_RECORD):
            group["saved_odds"] = "present-in-this-page-not-current-certification"
        if "spec_key" in row:
            specs = group["spec_groups"]
            spec = specs.setdefault(row["spec_key"], {"spec_key": row["spec_key"], "original_references": [], "results": []})
            if row["spec_ref"] not in spec["original_references"]: spec["original_references"].append(row["spec_ref"])
            if row["kind"] in (RESULT_RECORD, PROBABILITY_RECORD):
                result = row["recorded"]
                spec["results"].append({"reference": row["reference"], "kind": row["kind"], "price": result["price"],
                    "evaluated_at": (result.get("calculation_input") or {}).get("evaluated_at"),
                    "comparison_scope": "same-spec-saved-results-in-this-page-not-latest-price"})
    for group in groups.values():
        group["spec_groups"] = list(group["spec_groups"].values())
        if single_scan and group["saved_odds"] == "not-observed-in-this-page":
            group["saved_odds"] = "not-found-in-single-scan-of-known-layouts"
    return list(groups.values())


def read_book(store, payload, registry=None):
    """Project one bounded stored page; registry is never a source-fetch trigger."""
    plan = validate_plan(payload)
    backend = store.backend
    cursor = plan["cursor"]
    require(cursor is None or cursor["repository"] == backend.repository, "ODDS_BOOK_CURSOR_REPOSITORY_CHANGED")
    snapshot = plan["snapshot"] or backend.head()
    require(isinstance(snapshot, str) and SHA.fullmatch(snapshot) is not None, "ODDS_BOOK_SNAPSHOT_INVALID")
    start = cursor["offset"] if cursor else 0
    with _scope(backend, snapshot) as (reader, reads):
        page = _Page(reader, reads, snapshot, backend)
        selected = page.research_context(plan["research_ref"]) if plan["research_ref"] is not None else None
        candidates = _candidates(reads, snapshot)
        require(start <= len(candidates), "ODDS_BOOK_CURSOR_OUT_OF_BOUNDS")
        items, end, failures = [], start, 0
        for index in range(start, min(start + plan["scan_limit"], len(candidates))):
            path, kind = candidates[index]
            row = {"path": path, "kind": kind, "reference": None,
                   "open_operation": OPERATIONS.get(kind), "open_payload": None}
            try:
                row = page.item(path, kind, row)
                reads.check()
                matches = selected is None or row["research_key"] == selected["research_key"]
            except (RecordError, KeyError, TypeError, ValueError, RecursionError) as error:
                reads.check()
                if isinstance(error, RecordError) and operation_failure(error): raise
                row.update(status="missing" if isinstance(error, MissingObject) else "invalid",
                           error="ODDS_BOOK_OBJECT_MISSING" if isinstance(error, MissingObject) else "ODDS_BOOK_OBJECT_INVALID",
                           filter_match="unknown", semantic_replay="not-requested", current_validity="not-certified")
                if isinstance(error, RecordError) and re.fullmatch(r"[A-Z0-9_]{1,96}", str(error)):
                    row["reason"] = str(error)
                failures += 1
                matches = True  # A broken, unattributable candidate cannot disappear behind a filter.
            end = index + 1
            if matches:
                if len(encoded(row)) > MAX_CARD_BYTES:
                    row = {key: row[key] for key in ("path", "kind", "reference", "open_operation", "open_payload")}
                    row.update(status="not-included", error="ODDS_BOOK_CARD_LIMIT",
                               semantic_replay="not-requested", current_validity="not-certified")
                    failures += 1
                items.append(row)
                if len(items) == plan["limit"]: break
        single_scan = start == 0 and end == len(candidates) and failures == 0
        result = {"schema": "odds-book-v1", "repository": backend.repository, "snapshot": snapshot,
            "coverage": "this-page-only", "order": "stored-path-not-priority-or-recency",
            "scan": {"start": start, "end": end, "total_candidates": len(candidates), "at_end": end == len(candidates)},
            "status": "partial-page" if failures else "readable-page" if items else
                "not-found-in-single-scan-of-known-layouts" if single_scan else "no-match-in-this-page",
            "items": items, "research_relations": _relations(items, single_scan),
            "next_cursor": None if end == len(candidates) else {"schema": CURSOR_SCHEMA,
                "repository": backend.repository, "snapshot": snapshot, "offset": end, "research_ref": plan["research_ref"]},
            "semantic_replay": "not-requested", "current_validity": "not-certified",
            "human_acceptance": "not-established-by-read", "investment_authority": "none",
            "persistence": "not-written", "source_acquisition": "not-requested"}
        if len(encoded(result)) > MAX_VIEW_BYTES:
            raise StoreUnavailable("ODDS_BOOK_VIEW_LIMIT")
        reads.check()
    return result
