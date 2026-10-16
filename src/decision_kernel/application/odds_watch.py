"""On-demand version-bound Watch using existing Odds, market and artifact owners.

Snapshots and predecessors are explicit inputs. Replaying an old check never
consults today's research pointer as if it were the originally observed one.
"""
from dataclasses import asdict
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import json

from decision_kernel.application.conditional_odds import ConditionalOdds, publication
from decision_kernel.application.probability_odds import ProbabilityOdds
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.conditional_odds import day, number, timestamp
from decision_kernel.domain.odds_watch import (
    AGGREGATE_METRICS, WORLD_METRICS, CHECK_RECORD, WATCH_RECORD, MAX_HISTORY,
    gate, project, validate_request, validate_watch,
)
from decision_kernel.domain.records import BlobRef, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference


class _Reads:
    """Operation-local immutable reads; all first reads use the original backend.

    No mutable head, permission or failure is cached. The caller rechecks head
    before returning; publication still uses the original uncached writer/CAS.
    """
    def __init__(self, backend):
        self.original, self.repository = backend, backend.repository
        self.memo, self.size = {}, 0

    def _get(self, key, load):
        key = encoded(key)
        if key in self.memo: return self.memo[key]
        value = load()
        if self.size + len(key) + len(value) <= 8 * 1024 * 1024:
            self.memo[key] = value; self.size += len(key) + len(value)
        return value

    def head(self): return self.original.head()
    def check_commit(self, commit): return self.original.check_commit(commit)
    def resolve(self, ref):
        return self._get(["resolve", asdict(ref)], lambda: self.original.resolve(ref))
    def read(self, commit, path):
        return self._get(["read", commit, path], lambda: self.original.read(commit, path))
    def last_change(self, head, path):
        return self._get(["last-change", head, path], lambda: self.original.last_change(head, path).encode()).decode()
    def reference(self, commit, path):
        raw = self._get(["reference", commit, path], lambda: encoded(asdict(self.original.reference(commit, path))))
        return BlobRef(**json.loads(raw))
    def commit(self, files, *, expected):
        raise RecordError("WATCH_REPLAY_READER_IS_READ_ONLY")


class _Sources(VersionedStore):
    """One evaluation's exact-source memo, not another store or cross-call cache."""
    def __init__(self, backend):
        super().__init__(_Reads(backend))
        self.memo, self.size = {}, 0

    def read_source(self, ref):
        key = encoded(asdict(ref))
        if key in self.memo: return json.loads(self.memo[key])
        view = super().read_source(ref)
        raw = encoded(view)
        if self.size + len(key) + len(raw) <= 8 * 1024 * 1024:
            self.memo[key] = raw; self.size += len(key) + len(raw)
        return json.loads(raw)


class OddsWatch:
    def __init__(self, store, registry=None, *, clock=None):
        self.store, self.registry = store, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.custody = ConditionalOdds(store)

    def _frozen(self, payload, reader):
        watch = validate_watch(payload)
        basis = watch["basis"]
        spec, assessment = None, None
        if basis["kind"] == "research":
            ref = BlobRef(**basis["reference"])
        else:
            if basis["kind"] == "probability":
                assessment = ProbabilityOdds(reader).read_assessment(basis["reference"])
                spec_ref = assessment["assessment"]["spec_ref"]
            else:
                spec_ref = basis["reference"]
            spec = ConditionalOdds(reader).read_spec(spec_ref)
            ref = BlobRef(**spec["spec"]["research_ref"])
            condition = watch["condition"]
            if condition["metric"] in WORLD_METRICS:
                require(condition["world_id"] in {w["id"] for w in spec["spec"]["worlds"]}, "WATCH_WORLD_NOT_IN_SPEC")
                expected = "value_to_price_distance" if spec["spec"]["value_kind"] == "current-fair-value" else "undiscounted_holding_period_return"
                require(condition["metric"] == expected, "WATCH_CURRENT_VALUE_IS_NOT_FUTURE_RETURN")
        research = reader.read_revision(ref)
        require(timestamp(research["record"]["stored_at"]) <= timestamp(watch["activation"]["as_of"]),
                "WATCH_ACTIVATION_PRECEDES_RESEARCH")
        if assessment:
            require(timestamp(assessment["assessment"]["as_of"]) <= timestamp(watch["activation"]["as_of"]),
                    "WATCH_ACTIVATION_PRECEDES_ASSESSMENT")
        return {"schema": "frozen-odds-watch-v1", "watch": watch, "watch_sha256": digest(encoded(watch)),
            "research_ref": asdict(ref), "case_id": research["record"]["input"]["case_id"],
            "research_body_sha256": research["record"]["body"]["sha256"],
            "research_unresolved": research["record"]["input"]["unresolved"],
            "spec_sha256": spec["spec_sha256"] if spec else None,
            "assessment_sha256": assessment["assessment_sha256"] if assessment else None}

    def freeze(self, payload, *, submission_id):
        frozen = self._frozen(publication(payload, "watch"), self.store)
        require(timestamp(frozen["watch"]["activation"]["as_of"]) <= self.clock(), "WATCH_ACTIVATION_IN_FUTURE")
        ref = self.custody._put(submission_id, WATCH_RECORD, frozen, payload["rights_basis"])
        return {"reference": asdict(ref), "watch_sha256": frozen["watch_sha256"], "research_ref": frozen["research_ref"]}

    def read_watch(self, reference, *, reader=None):
        frozen = ConditionalOdds(reader or self.store)._open(reference, WATCH_RECORD)
        require(isinstance(frozen, dict) and "watch" in frozen, "WATCH_RECORD_INVALID")
        require(encoded(self._frozen(frozen["watch"], reader or self.store)) == encoded(frozen), "WATCH_RECORD_CHANGED")
        return frozen

    def _registry(self):
        if self.registry is None:
            from decision_kernel.application.relay_registry import RelayRegistry
            from pathlib import Path
            return RelayRegistry(Path("docs/sources/relay-registry.json"))
        return self.registry

    def _observe(self, frozen, request, previous, reader, *, known_dates_first=False):
        try:
            if known_dates_first:
                return self._observe_supported(frozen, request, previous, reader, known_dates_first=True)
            return self._observe_supported(frozen, request, previous, reader)
        except RecordError as exc:
            code = str(exc)
            review = {"ODDS_CAPITAL_CHANGE_REQUIRES_NEW_SPEC", "ODDS_CAPITAL_ASSUMPTION_STALE",
                      "ODDS_PAST_DISTRIBUTION_REQUIRES_NEW_SPEC", "ODDS_TARGET_NOT_AFTER_PRICE",
                      "ODDS_POLICY_HORIZON_NOT_COVERED"}
            unavailable = {"ODDS_CLOSE_DAY_NOT_FINISHED_OR_STALE", "ODDS_EXACT_CLOSE_MISSING",
                           "ODDS_SECURITY_MASTER_STALE", "ODDS_SECURITY_MASTER_MISSING",
                           "ODDS_CLOSE_NOT_TRADING_DAY", "ODDS_MARKET_CAPTURE_AFTER_EVALUATION",
                           "ODDS_CLOSE_CAPTURE_DAY_NOT_FINISHED", "WATCH_PRICE_QUALIFICATION_MISMATCH"}
            if code not in review | unavailable:
                raise  # Transport, custody corruption and malformed payloads are not a false comparison.
            return {"status": "review-required" if code in review else "cannot-determine",
                    "condition_met": None, "research_at_snapshot": None, "calculation": None,
                    "value": None, "reason": code}

    def _observe_supported(self, frozen, request, previous, reader, *, known_dates_first=False):
        watch = frozen["watch"]
        gated = gate(watch, request, previous)
        out = {"status": gated, "condition_met": None, "research_at_snapshot": None,
               "calculation": None, "value": None, "reason": None}
        if gated: return out
        current = reader.current(frozen["case_id"], snapshot=request["records_snapshot"])
        if current is None:
            return {**out, "status": "cannot-determine", "reason": "WATCH_RESEARCH_POINTER_MISSING"}
        # Resolve rather than merely compare an unchecked current-pointer path/hash.
        reader.read_revision(current)
        out["research_at_snapshot"] = asdict(current)
        changed = current != BlobRef(**frozen["research_ref"])
        if watch["basis"]["kind"] == "research":
            require(request["input"] is None, "WATCH_RESEARCH_CHECK_DOES_NOT_USE_PRICE")
            return {**out, "status": "evaluated", "condition_met": changed,
                    "reason": "VERSION_COMPARISON_ONLY_NOT_ECONOMIC_REVIEW"}
        if changed or request["review_reasons"]:
            return {**out, "status": "review-required", "reason": "WATCH_RESEARCH_OR_ASSUMPTIONS_CHANGED"}
        if request["input"] is None and not known_dates_first:
            # Preserve the v1 standalone-check projection during historical replay.
            return {**out, "status": "cannot-determine", "reason": "WATCH_NEW_PRICE_INPUT_MISSING"}
        conditional = ConditionalOdds(reader)
        if watch["basis"]["kind"] == "conditional":
            spec = conditional.read_spec(watch["basis"]["reference"])["spec"]
        else:
            a = ProbabilityOdds(reader).read_assessment(watch["basis"]["reference"])
            spec = conditional.read_spec(a["assessment"]["spec_ref"])["spec"]
        # The evaluation clock, not merely an old quote, determines elapsed payouts.
        at = timestamp(request["checked_at"])
        calendar_at = at.astimezone(timezone(timedelta(hours=8))) if spec["security"]["code"].endswith((".SH", ".SZ")) else at
        if spec["target_date"] and calendar_at.date() >= day(spec["target_date"]):
            return {**out, "status": "ended", "reason": "WATCH_FIXED_HORIZON_REACHED"}
        if any(day(flow["date"]) <= calendar_at.date() for w in spec["worlds"] for flow in w["distributions"]):
            return {**out, "status": "review-required", "reason": "WATCH_ELAPSED_CASH_REQUIRES_NEW_SPEC"}
        if request["input"] is None:
            return {**out, "status": "cannot-determine", "reason": "WATCH_NEW_PRICE_INPUT_MISSING"}
        if watch["basis"]["kind"] == "probability":
            calculation = ProbabilityOdds(reader, self._registry(), clock=self.clock).compute({
                "assessment_ref": watch["basis"]["reference"], "market": request["input"], "evaluated_at": request["checked_at"]})
        else:
            price = request["input"]
            p = watch["data_policy"]
            require(price.get("kind") == p["price_kind"], "WATCH_PRICE_QUALIFICATION_MISMATCH")
            # Existing Odds validates shape, security, shares, evidence, cash and method.
            calculation = conditional.compute({"spec_ref": watch["basis"]["reference"], "price": price})
            age = (at - timestamp(price["as_of"])).total_seconds()
            if not 0 <= age <= p["max_price_age_days"] * 86400:
                return {**out, "status": "cannot-determine", "reason": "WATCH_PRICE_FUTURE_OR_STALE"}
            if not 0 <= (calendar_at.date() - day(spec["capital_structure"]["as_of"])).days <= p["max_capital_age_days"]:
                return {**out, "status": "review-required", "reason": "WATCH_CAPITAL_ASSUMPTION_STALE"}
        c = watch["condition"]
        if c["metric"] == "price": value = calculation["price"]["price"]
        elif c["metric"] in AGGREGATE_METRICS: value = calculation["aggregate"][c["metric"]]
        else: value = next(w[c["metric"]] for w in calculation["world_results"] if w["world_id"] == c["world_id"])
        matched = Decimal(value) <= number(c["threshold"]) if c["comparison"] == "lte" else Decimal(value) >= number(c["threshold"])
        return {**out, "status": "evaluated", "condition_met": matched, "calculation": calculation, "value": value}

    def _replay(self, request, reader=None):
        """Iterative bounded predecessor replay. No user-supplied state is trusted."""
        reader = reader or _Sources(self.store.backend)
        frozen = self.read_watch(request["watch_ref"], reader=reader)
        custody = ConditionalOdds(reader)
        records, seen = [], set()
        ref = request["previous_check"]
        while ref is not None:
            key = encoded(ref)
            require(key not in seen and len(records) < MAX_HISTORY, "WATCH_HISTORY_LIMIT_OR_CYCLE")
            seen.add(key)
            value = custody._open(ref, CHECK_RECORD)
            require(isinstance(value, dict) and "request" in value, "WATCH_CHECK_RECORD_INVALID")
            old = validate_request(value["request"])
            require(old["watch_ref"] == request["watch_ref"], "WATCH_PREDECESSOR_DIFFERENT_WATCH")
            records.append(value); ref = old["previous_check"]
        previous = None
        for value in reversed(records):
            old = value["request"]
            observation = self._observe(frozen, old, previous, reader)
            rebuilt = project(frozen["watch"], old, observation, previous)
            require(encoded(rebuilt) == encoded(value), "WATCH_CHECK_RECALCULATION_MISMATCH")
            previous = rebuilt
        observation = self._observe(frozen, request, previous, reader)
        return project(frozen["watch"], request, observation, previous)

    def check(self, payload, *, reader=None):
        request = validate_request(payload)
        require(timestamp(request["checked_at"]) <= self.clock(), "WATCH_CHECK_IN_FUTURE")
        # Recheck mutable visibility/binding even when exact sources were memoized.
        self.store.backend.head()
        result = self._replay(request, reader)
        self.store.backend.head()
        return json.loads(encoded(result))

    def save(self, payload, *, submission_id):
        from decision_kernel.application.watch_activity import assert_unmanaged
        reader = _Sources(self.store.backend)
        result = self.check(publication(payload, "check"), reader=reader)
        ref = self.custody._put(submission_id, CHECK_RECORD, result, payload["rights_basis"],
            before_new_write=lambda head: assert_unmanaged(reader, result["request"]["watch_ref"], snapshot=head))
        return {"reference": asdict(ref), "check_sha256": digest(encoded(result)), "status": result["status"],
                "watch_ref": result["request"]["watch_ref"]}

    def read_check(self, reference, *, include_current_use=True):
        reader = _Sources(self.store.backend)
        result = ConditionalOdds(reader)._open(reference, CHECK_RECORD)
        require(isinstance(result, dict) and "request" in result, "WATCH_CHECK_RECORD_INVALID")
        require(encoded(self.check(result["request"], reader=reader)) == encoded(result), "WATCH_CHECK_RECALCULATION_MISMATCH")
        output = {"reference": asdict(artifact_reference(reference)), "result": result, "recalculation": "matches"}
        if include_current_use:
            frozen = self.read_watch(result["request"]["watch_ref"], reader=reader)
            output["current_use"] = self._hint(frozen)
        return output

    def _hint(self, frozen):
        hint = {"status": "unknown", "research": None,
                "meaning": "explicit-snapshot-check-not-live-monitoring-or-refreshed-market"}
        try:
            current = self.store.current(frozen["case_id"])
            hint.update(research=asdict(current) if current else None,
                        status="same-revision" if current == BlobRef(**frozen["research_ref"])
                        else "historical-research-not-current")
        except RecordError:
            hint["error"] = "WATCH_CURRENT_RESEARCH_UNAVAILABLE"
        return hint

    def inspect(self, payload):
        from decision_kernel.application.watch_activity import assert_unmanaged
        reader = _Sources(self.store.backend)
        assert_unmanaged(reader, validate_request(payload)["watch_ref"])
        result = self.check(payload, reader=reader)
        frozen = self.read_watch(result["request"]["watch_ref"], reader=reader)
        return {"result": result, "current_use": self._hint(frozen)}


def verify_fresh(receipt, operation, reader_factory, registry=None):
    try:
        reader = OddsWatch(reader_factory(), registry)
        if operation in ("freeze-odds-watch", "reconcile-odds-watch"):
            value = reader.read_watch(receipt["reference"])
            require(value["watch_sha256"] == receipt["watch_sha256"], "WATCH_FRESH_READBACK_MISMATCH")
        else:
            value = reader.read_check(receipt["reference"], include_current_use=False)
            require(digest(encoded(value["result"])) == receipt["check_sha256"], "WATCH_FRESH_READBACK_MISMATCH")
        state = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    except RecordError:
        state = {"status": "failed", "new_reader": True, "error": "WATCH_FRESH_READBACK_FAILED"}
    return {**receipt, "readback": state}
