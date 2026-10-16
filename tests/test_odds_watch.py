"""Version-scoped Watch comparisons, finite replay and no hidden side effects."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import localcontext, ROUND_UP
import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch, _Sources, verify_fresh
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.odds_watch import CHECK_RECORD, validate_watch
from decision_kernel.domain.records import Conflict, RecordError, RevisionInput, SourceInput
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, spec_payload, price_payload
from test_probability_odds import ctx, calculation, REGISTRY

AT = "2026-01-03T04:00:00+00:00"


def watch_payload(reference, kind="conditional", **changes):
    return {"schema": "odds-watch-v1", "watch_id": "synthetic-watch", "basis": {"kind": kind, "reference": reference},
        "condition": {"metric": "research_version" if kind == "research" else "price", "world_id": None,
                      "comparison": "changed" if kind == "research" else "lte", "threshold": None if kind == "research" else "100",
                      "basis_note": "Explicit synthetic condition, no human investment boundary"},
        "activation": {"enabled": True, "as_of": "2026-01-03T03:30:00Z", "basis_note": "Synthetic engineering use only"},
        "window": {"starts_at": "2026-01-03T03:30:00Z", "ends_at": "2030-01-01T00:00:00Z",
                   "minimum_interval_seconds": 0, "end_after_trigger": False},
        "data_policy": {"price_kind": "declared-reference", "max_price_age_days": 3, "max_capital_age_days": 30} if kind == "conditional" else None,
        "effect": "record-only", "limitations": ["Synthetic, not investment research"], **changes}


def request(store, ref, *, at=AT, price=None, previous=None, reasons=None, snapshot=None):
    return {"watch_ref": ref, "checked_at": at, "records_snapshot": snapshot or store.backend.head(),
            "previous_check": previous, "input": price, "review_reasons": reasons or []}


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
    backend = GitStore.init_synthetic(tmp_path / "watch.git")
    store = VersionedStore(backend)
    src = store.put_source("s", SourceInput("s", "content", "Synthetic", "synthetic:watch", "Synthetic research basis"))
    ref = store.submit_revision("r", RevisionInput("case", "r1", "full", "Synthetic research", "Synthetic", "synthetic", (src,)))
    spec = ConditionalOdds(store).freeze(authorized("spec", spec_payload(asdict(ref))), submission_id="s1")["reference"]
    service = OddsWatch(store)
    w = service.freeze(authorized("watch", watch_payload(spec)), submission_id="w1")["reference"]
    return store, ref, spec, service, w


def save(service, payload, key):
    receipt = service.save(authorized("check", payload), submission_id=key)
    return receipt["reference"], service.read_check(receipt["reference"])["result"]


def test_price_refresh_trigger_continuation_gap_and_reentry(case):
    store, research, _, service, w = case
    previous = None
    for i, (price, expected) in enumerate([("110", "not-met"), ("100", "first-trigger"), ("90", "continued-trigger"),
                                          (None, "cannot-determine"), ("95", "continued-trigger"), ("101", "not-met"), ("99", "first-trigger")]):
        q = request(store, w, at=f"2026-01-03T04:{i:02}:00Z", price=price_payload(price=price, price_id=f"p{i}") if price else None, previous=previous)
        previous, result = save(service, q, f"check-{i}")
        assert result["status"] == expected
        if i == 4: assert result["trigger_continuity"] == "intervening-checks-unknown"
        assert not any(result["effects"][k] for k in ("notification_sent", "research_started", "scheduler_created"))
    assert store.current("case") == research
    assert result["observation"]["calculation"]["spec_ref"] == case[2]


@pytest.mark.parametrize("mode", ["disabled", "before-start", "ended", "interval", "end-after-trigger"])
def test_lifecycle_does_not_turn_calendar_into_evidence(case, mode):
    store, _, spec, service, _ = case
    watch = watch_payload(spec)
    if mode == "disabled": watch["activation"]["enabled"] = False
    if mode == "before-start": watch["window"]["starts_at"] = "2026-01-04T00:00:00Z"
    if mode == "ended": watch["window"]["ends_at"] = "2026-01-03T04:00:00Z"
    if mode == "interval": watch["window"]["minimum_interval_seconds"] = 300
    if mode == "end-after-trigger": watch["window"]["end_after_trigger"] = True
    w = service.freeze(authorized("watch", watch), submission_id="other-watch")["reference"]
    prev = None
    if mode in ("interval", "end-after-trigger"):
        prev, _ = save(service, request(store, w, price=price_payload()), "previous")
    out = service.check(request(store, w, at="2026-01-03T04:01:00Z", previous=prev))
    assert out["status"] == {"disabled": "not-enabled", "before-start": "not-due", "ended": "ended",
                             "interval": "not-due", "end-after-trigger": "ended"}[mode]
    assert out["condition_met"] is None and not out["check_performed"]


def test_not_due_calls_do_not_postpone_due_time(case):
    store, _, spec, service, _ = case
    w = watch_payload(spec); w["window"]["minimum_interval_seconds"] = 300
    ref = service.freeze(authorized("watch", w), submission_id="interval")["reference"]
    a, _ = save(service, request(store, ref, price=price_payload()), "a")
    b, out = save(service, request(store, ref, at="2026-01-03T04:04:00Z", previous=a), "b")
    assert out["status"] == "not-due"
    out = service.check(request(store, ref, at="2026-01-03T04:05:00Z", previous=b, price=price_payload()))
    assert out["status"] == "continued-trigger"


def test_new_research_blocks_price_only_but_old_check_replays(case):
    store, old, _, service, w = case
    snapshot = store.backend.head()
    ref, before = save(service, request(store, w, price=price_payload(), snapshot=snapshot), "check")
    new = store.submit_revision("r2", RevisionInput("case", "r2", "full", "New evidence", "Synthetic", "synthetic", predecessor=old, unresolved=("Synthetic evidence not verified",)))
    out = service.check(request(store, w, at="2026-01-03T04:01:00Z", price=price_payload(), previous=ref))
    assert out["status"] == "review-required" and out["condition_met"] is None and out["observation"]["calculation"] is None
    read = service.read_check(ref)
    assert read["result"] == before and read["current_use"]["status"] == "historical-research-not-current"
    assert read["current_use"]["research"] == asdict(new)


def test_research_version_condition_requires_no_price_or_registry(case, monkeypatch):
    store, old, _, service, _ = case
    w = service.freeze(authorized("watch", watch_payload(asdict(old), "research")), submission_id="research-watch")["reference"]
    monkeypatch.setattr(service, "_registry", lambda: pytest.fail("No Registry needed for research-only check"))
    ref, out = save(service, request(store, w), "same")
    assert out["status"] == "not-met"
    store.submit_revision("r2", RevisionInput("case", "r2", "full", "Changed", "Synthetic", "synthetic", predecessor=old, unresolved=("Synthetic evidence not verified",)))
    out = service.check(request(store, w, at="2026-01-03T04:01:00Z", previous=ref))
    assert out["status"] == "first-trigger" and out["observation"]["calculation"] is None
    with pytest.raises(RecordError, match="DOES_NOT_USE_PRICE"): service.check(request(store, w, price=price_payload()))


@pytest.mark.parametrize("mode", ["stale-price", "future-price", "capital-change", "capital-stale", "cash-passed", "target-passed", "flag", "missing", "wrong-qualification"])
def test_failed_refresh_is_never_not_met(case, mode):
    store, _, _, service, w = case
    at, price, reasons = AT, price_payload(), []
    if mode == "stale-price": at = "2026-01-10T00:00:00Z"
    if mode == "future-price": price["as_of"] = "2026-01-05T00:00:00Z"
    if mode == "capital-change": price["share_count"] = "101"
    if mode == "capital-stale": at = "2026-02-10T00:00:00Z"; price["as_of"] = at
    if mode == "cash-passed": at = "2027-06-01T08:00:00Z"
    if mode == "target-passed": at = "2028-12-31T08:00:00Z"
    if mode == "flag": reasons = ["A new capital plan requires review; caller statement"]
    if mode == "missing": price = None
    if mode == "wrong-qualification": price["kind"] = "qualified-market"
    service.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    out = service.check(request(store, w, at=at, price=price, reasons=reasons))
    assert out["status"] in ("cannot-determine", "review-required", "ended") and out["condition_met"] is None


def test_decimal_result_not_restricted_to_input_precision(case):
    store, _, spec, service, _ = case
    w = watch_payload(spec); w["condition"].update(metric="undiscounted_holding_period_return", world_id="one", comparison="gte", threshold="0.2")
    ref = service.freeze(authorized("watch", w), submission_id="return-watch")["reference"]
    with localcontext() as c:
        c.prec = 3; c.rounding = ROUND_UP
        out = service.check(request(store, ref, price=price_payload(price="101")))
    assert out["status"] == "first-trigger" and len(out["observation"]["value"]) > 48


@pytest.mark.parametrize("change", ["effect", "threshold", "window", "activation", "policy", "metric"])
def test_bad_watch_input_refuses_before_store_write(case, change):
    store, _, spec, service, _ = case; w = watch_payload(spec); head = store.backend.head()
    if change == "effect": w["effect"] = "send-notification"
    if change == "threshold": w["condition"]["threshold"] = 100
    if change == "window": w["window"]["ends_at"] = w["window"]["starts_at"]
    if change == "activation": w["activation"]["enabled"] = "true"
    if change == "policy": w["data_policy"]["max_price_age_days"] = 0
    if change == "metric": w["condition"]["metric"] = "expected_holding_period_return"
    with pytest.raises(RecordError): service.freeze(authorized("watch", w), submission_id="bad")
    assert store.backend.head() == head


def test_present_value_cannot_be_future_return_watch(case):
    store, research, _, service, _ = case
    p = spec_payload(asdict(research)); p.update(value_kind="current-fair-value", method="equity-value-distance-v1", target_date=None, cash_policy="included-in-fair-value")
    p["worlds"][0]["distributions"] = []
    s = ConditionalOdds(store).freeze(authorized("spec", p), submission_id="pv")["reference"]
    w = watch_payload(s); w["condition"].update(metric="undiscounted_holding_period_return", world_id="one", threshold="0.2")
    with pytest.raises(RecordError, match="CURRENT_VALUE_IS_NOT_FUTURE_RETURN"): service.freeze(authorized("watch", w), submission_id="bad")


def test_same_intent_replays_and_altered_intent_conflicts(case):
    store, _, _, service, w = case
    payload = authorized("check", request(store, w, price=price_payload()))
    a = service.save(payload, submission_id="original")
    assert service.save(payload, submission_id="original") == a
    payload["check"]["input"]["price"] = "80"
    with pytest.raises(Conflict): service.save(payload, submission_id="original")


def test_hash_valid_tamper_still_refused(case):
    store, _, _, service, w = case
    out = service.check(request(store, w, price=price_payload()))
    out["status"] = "not-met"; out["condition_met"] = False
    bad = service.custody._put("forged", CHECK_RECORD, out, "Synthetic adversarial fixture")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"): service.read_check(asdict(bad))
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"):
        service.check(request(store, w, at="2026-01-03T04:01:00Z", previous=asdict(bad)))


def test_cross_watch_previous_or_clock_reversal_refused(case):
    store, _, spec, service, w = case
    ref, _ = save(service, request(store, w, price=price_payload()), "old")
    with pytest.raises(RecordError, match="CLOCK_NOT_ADVANCING"): service.check(request(store, w, price=price_payload(), previous=ref))
    other = watch_payload(spec); other["watch_id"] = "other"
    other = service.freeze(authorized("watch", other), submission_id="other")["reference"]
    with pytest.raises(RecordError, match="DIFFERENT_WATCH"):
        service.check(request(store, other, at="2026-01-03T04:01:00Z", previous=ref))


def test_source_memo_is_exact_defensive_and_per_operation(case):
    store, research, _, _, _ = case
    source = store.read_revision(research)["record"]["input"]["sources"][0]
    from decision_kernel.domain.records import SourceRef
    ref = SourceRef.from_dict(source); reader = _Sources(store.backend)
    a = reader.read_source(ref); a["data"] = "tampered"
    assert reader.read_source(ref)["data"] != "tampered"
    assert _Sources(store.backend).memo == {}


def test_probability_watch_uses_retained_close_and_same_assessment(ctx):
    store, probability, _, _, market = ctx
    calc = calculation(ctx); service = OddsWatch(store, REGISTRY)
    w = watch_payload(calc["assessment_ref"], "probability")
    w["condition"].update(metric="expected_holding_period_return", threshold="0.1", comparison="gte")
    ref = service.freeze(authorized("watch", w), submission_id="watch")["reference"]
    out = service.check(request(store, ref, price=market))
    assert out["status"] == "not-met" and out["observation"]["value"] == "-0.05"
    assert out["observation"]["calculation"]["assessment_ref"] == calc["assessment_ref"]
    later = service.check(request(store, ref, at="2026-01-10T04:00:00Z", price=market))
    assert later["status"] == "cannot-determine" and later["condition_met"] is None


def test_history_bound_fails_explicitly_without_truncating_or_writing(case, monkeypatch):
    store, _, _, service, w = case
    a, _ = save(service, request(store, w, price=price_payload()), "a")
    b, _ = save(service, request(store, w, at="2026-01-03T04:01:00Z", price=price_payload(), previous=a), "b")
    monkeypatch.setattr("decision_kernel.application.odds_watch.MAX_HISTORY", 1)
    head = store.backend.head()
    with pytest.raises(RecordError, match="HISTORY_LIMIT"):
        service.save(authorized("check", request(store, w, at="2026-01-03T04:02:00Z", previous=b)), submission_id="too-long")
    assert store.backend.head() == head


def test_unknown_failure_does_not_become_a_false_condition(case, monkeypatch):
    store, _, _, service, w = case
    from decision_kernel.domain.records import MissingObject
    monkeypatch.setattr(service, "_observe_supported", lambda *args: (_ for _ in ()).throw(MissingObject("SIMULATED_SOURCE_UNREADABLE")))
    with pytest.raises(MissingObject): service.check(request(store, w, price=price_payload()))


def test_immutable_read_memo_bounds_and_failed_reads(case):
    from decision_kernel.application.odds_watch import _Reads
    store, research, *_ = case
    proxy = _Reads(store.backend)
    raw = proxy.resolve(research); assert proxy.resolve(research) is raw
    other = deepcopy(research)
    from dataclasses import replace
    other = replace(other, sha256="a" * 64)
    count = len(proxy.memo)
    for _ in range(2):
        with pytest.raises(RecordError): proxy.resolve(other)
    assert len(proxy.memo) == count
    proxy.size = 8 * 1024 * 1024
    proxy.read(research.commit, research.path)
    assert len(proxy.memo) == count
    with pytest.raises(RecordError, match="READ_ONLY"): proxy.commit({}, expected=research.commit)


def test_cached_replay_still_checks_mutable_binding_before_return(case, monkeypatch):
    store, _, _, service, w = case
    original = service._replay
    def change_after_read(*args):
        result = original(*args)
        def unavailable(): raise RecordError("SIMULATED_VISIBILITY_CHANGED")
        monkeypatch.setattr(store.backend, "head", unavailable)
        return result
    monkeypatch.setattr(service, "_replay", change_after_read)
    with pytest.raises(RecordError, match="VISIBILITY_CHANGED"):
        service.check(request(store, w, price=price_payload()))


def test_watch_check_has_explicit_clock_and_snapshot(case):
    store, _, _, service, w = case
    payload = request(store, w, at="2999-01-01T00:00:00Z")
    with pytest.raises(RecordError, match="IN_FUTURE"): service.check(payload)
    payload["checked_at"] = AT; payload["records_snapshot"] = "main"
    with pytest.raises(RecordError, match="EXACT_RECORDS_SNAPSHOT"): service.check(payload)


def test_fresh_readback_failure_keeps_known_receipt(case):
    store, _, _, service, w = case
    receipt = service.save(authorized("check", request(store, w, price=price_payload())), submission_id="known")
    def fail(): raise RecordError("SIMULATED_READER_UNAVAILABLE")
    restored = verify_fresh(receipt, "save-odds-watch-check", fail)
    assert restored["reference"] == receipt["reference"] and restored["readback"]["status"] == "failed"


@pytest.mark.parametrize("when,expected", [("2027-05-31T16:00:00Z", "review-required"), ("2028-12-30T16:00:00Z", "ended")])
def test_sh_sz_date_boundaries_do_not_depend_on_check_offset(case, when, expected):
    store, _, _, service, w = case
    service.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    out = service.check(request(store, w, at=when, price=price_payload()))
    assert out["status"] == expected
