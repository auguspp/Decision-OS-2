"""Original-judgment, clock, source, denominator and no-write consumer regressions."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Inexact, ROUND_UP, localcontext
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from decision_kernel.application import outcome_review as review, versioned_store
from decision_kernel.application.market_discovery import MarketDiscovery
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.outcome_review import BASIS_FIELDS, compare_values, horizon_change
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_market_custody import MemoryBackend
from test_market_discovery import DAYS, REGISTRY, request, retain, rising, save_sequence, storage

AS_OF = "2026-08-01T10:00:00+08:00"
BASIS = {"subject": "synthetic-issuer", "metric": "revenue", "period": "2026-H1",
    "period_start": "2026-01-01", "period_end": "2026-06-30", "period_kind": "period-flow",
    "basis": "consolidated-gaap", "currency": "CNY", "unit": "CNY", "share_basis": "not-per-share"}


@pytest.fixture(autouse=True)
def reading_clock(monkeypatch):
    monkeypatch.setattr(review, "_now", lambda: datetime.fromisoformat("2026-10-21T10:00:00+08:00"))


def span(text, word):
    start = text.index(word)
    return {"start": start, "end": start + len(word)}


def prepare(store, monkeypatch, *, actual="110", forecast="100", ranged=False):
    body = "Synthetic forecast.\nIssued 2026-04-01\n" + ("Lower 90\nUpper 110\n" if ranged else "Value " + forecast + "\n")
    body += "\n".join(key + " " + value for key, value in BASIS.items()) + "\n"
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-04-01T08:00:00+08:00")
    src = store.put_source("original-source", SourceInput("synthetic-original", "content", "Synthetic evidence",
        "synthetic:original", "Evidence limits remain.", published_at="2026-03-30", acquired_at="2026-04-01"))
    ref = store.submit_revision("original-research", RevisionInput("synthetic", "v1", "full", body,
        "Synthetic engineering mandate", "synthetic-method", (src,), unresolved=("Cash conversion unverified",)))
    forecast_plan = {"research": {"reference": asdict(ref), "start": 1, "end": len(body.splitlines())},
        "quote": body, "role": "research-forecast", "issued_on": "2026-04-01", "basis": deepcopy(BASIS),
        "issued_on_locator": span(body, "2026-04-01"),
        "value_locator": None if ranged else span(body, forecast),
        "range": {"lower_locator": span(body, "90"), "upper_locator": span(body, "110")} if ranged else None,
        "context_bindings": {key: span(body, value) for key, value in BASIS.items()}}
    table = io.StringIO(newline="")
    writer = csv.DictWriter(table, fieldnames=[*BASIS_FIELDS, "value"])
    writer.writeheader(); writer.writerow({**BASIS, "value": actual})
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-07-21T08:00:00+08:00")
    actual_ref = store.put_source("actual", SourceInput("synthetic-actual", "dataset", "Synthetic actual",
        "synthetic:actual", table.getvalue(), published_at="2026-07-20", acquired_at="2026-07-21",
        report_period="2026-H1"))
    actual_plan = {"reference": asdict(actual_ref), "value_locator": {"row": 1, "column": "value"},
        "basis": deepcopy(BASIS), "reported_on": "2026-07-20",
        "context_bindings": {key: {"row": 1, "column": key} for key in BASIS_FIELDS}}
    plan = {"schema": review.SCHEMA, "as_of": AS_OF,
        "economic": [{"forecast_id": "original", "forecast": forecast_plan,
                      "actual": actual_plan, "missing_reason": None}], "market": None}
    return plan


@pytest.fixture
def economy(monkeypatch):
    store = VersionedStore(MemoryBackend())
    plan = prepare(store, monkeypatch)
    return store, plan


def test_original_forecast_literal_basis_and_independent_read_without_research_rewrite(economy):
    store, plan = economy
    head = store.backend.head()
    first = review.open_outcome_review(store, plan)
    fresh = VersionedStore(MemoryBackend(store.backend.shared))
    assert review.open_outcome_review(fresh, plan) == first
    row = first["economic"]["rows"][0]
    assert row["status"] == "compared-literal-basis"
    assert row["comparison"]["difference"] == row["comparison"]["absolute_difference"] == "10"
    assert row["comparison"]["relative_to_absolute_forecast"] == "0.1"
    assert row["forecast"]["original_unresolved"] == ["Cash conversion unverified"]
    assert row["forecast"]["original_excerpt"]["text"] == plan["economic"][0]["forecast"]["quote"]
    assert first["writes"] == first["source_requests"] == 0
    assert first["original_judgment_modified"] is first["automatic_method_change"] is False
    assert row["ai_forecast_error"] is row["probability_score"] is row["economic_verdict"] is None
    assert store.backend.head() == head


@pytest.mark.parametrize("actual,expected", [("89", "below"), ("90", "within"), ("110", "within"), ("111", "above")])
def test_retained_original_range_endpoints_are_inclusive(monkeypatch, actual, expected):
    store = VersionedStore(MemoryBackend())
    plan = prepare(store, monkeypatch, actual=actual, ranged=True)
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["comparison"]["range_position"] == expected
    assert row["forecast"]["value"] == "100"
    assert row["forecast"]["lower"] == "90" and row["forecast"]["upper"] == "110"


def test_zero_negative_forecast_and_fixed_decimal_context():
    assert compare_values("0", "3")["relative_to_absolute_forecast"] is None
    assert compare_values("0", "3")["relative_denominator_status"] == "zero-forecast-undefined"
    assert compare_values("-10", "-5")["relative_to_absolute_forecast"] == "0.5"
    expected = compare_values("3", "4")
    with localcontext() as ctx:
        ctx.prec = 2; ctx.rounding = ROUND_UP; ctx.traps[Inexact] = True
        assert compare_values("3", "4") == expected
        assert ctx.prec == 2 and ctx.traps[Inexact] is True


@pytest.mark.parametrize("field,value", [("subject", "other"), ("metric", "profit"), ("period", "2026-Q1"),
    ("period_kind", "ytd-flow"), ("basis", "parent-only-gaap"), ("currency", "USD"),
    ("unit", "CNY_100M"), ("share_basis", "diluted-per-share")])
def test_incompatible_units_metric_subject_period_and_scope_do_not_calculate(economy, field, value):
    store, plan = economy
    plan["economic"][0]["actual"]["basis"][field] = value
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "incomparable" and field in row["mismatched_fields"]
    assert row["actual"] is row["comparison"] is None


def test_declared_context_cannot_impersonate_literal_source_checked_basis(economy):
    store, plan = economy
    plan["economic"][0]["forecast"]["context_bindings"] = {}
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "insufficient-basis" and row["comparison"] is None
    assert row["forecast"]["value"] == "100" and row["actual"]["value"] == "110"
    assert set(row["forecast"]["basis_binding"]["declared_fields"]) == set(BASIS_FIELDS)
    plan["economic"][0]["actual"]["context_bindings"]["metric"] = {"row": 1, "column": "subject"}
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "actual-unavailable" and row["comparison"] is None
    assert row["reason_code"] == "OUTCOME_SOURCE_BASIS_MISMATCH:metric"
    assert row["forecast"]["original_unresolved"] == ["Cash conversion unverified"]


def test_relabeling_both_original_numbers_cannot_qualify_another_metric_or_scale(economy):
    store, plan = economy
    for point in (plan["economic"][0]["forecast"], plan["economic"][0]["actual"]):
        point["context_bindings"] = {}
        point["basis"].update(metric="profit", unit="CNY_100M")
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "insufficient-basis" and row["comparison"] is None
    assert row["forecast"]["value"] == "100" and row["actual"]["value"] == "110"


def test_author_date_cannot_be_backfilled_independently_of_original_text(economy):
    store, plan = economy
    plan["economic"][0]["forecast"]["issued_on"] = "2026-03-01"
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "unavailable" and row["reason_code"] == "OUTCOME_FORECAST_DATE_BINDING"
    assert row["comparison"] is None


def test_unreadable_original_does_not_shrink_declared_event_denominator(economy):
    store, plan = economy
    original_scope = review.open_outcome_review(store, plan)["economic"]["scope"]
    plan["economic"][0]["forecast"]["quote"] += "Invented outcome\n"
    result = review.open_outcome_review(store, plan)
    assert result["economic"]["scope"]["declared_forecasts"] == original_scope["declared_forecasts"] == 1
    assert result["economic"]["scope"]["declared_event_keys"] == original_scope["declared_event_keys"] == 1
    assert result["economic"]["rows"][0]["comparison"] is None


def test_later_retention_does_not_certify_original_text_date_as_prior_capture(economy, monkeypatch):
    store, plan = economy
    forecast = plan["economic"][0]["forecast"]
    original = store.read_revision(review.BlobRef(**forecast["research"]["reference"]))
    source = review.SourceRef.from_dict(original["record"]["input"]["sources"][0])
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-07-22T08:00:00+08:00")
    ref = store.submit_revision("late-retained", RevisionInput("late-synthetic", "v1", "full",
        forecast["quote"], "Retain original author statement", "synthetic-method", (source,),
        unresolved=("Cash conversion unverified",)))
    forecast["research"]["reference"] = asdict(ref)
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "compared-literal-basis"
    assert row["forecast"]["issued_on"] == "2026-04-01"
    assert row["forecast"]["research_stored_at"] == "2026-07-22T08:00:00+08:00"
    assert row["knowledge"] == "later-review-not-ex-ante-capture-proof"
    assert row["independent_forecast_sample"] is False


def test_future_cutoff_is_rejected_before_any_store_access(economy, monkeypatch):
    store, plan = economy
    plan["as_of"] = "2026-11-01T00:00:00+00:00"
    def forbidden(*args):
        pytest.fail("future cutoff must not read records")
    monkeypatch.setattr(MemoryBackend, "head", forbidden)
    with pytest.raises(RecordError, match="OUTCOME_FUTURE_CUTOFF"):
        review.open_outcome_review(store, plan)


def test_actual_retained_after_cutoff_cannot_backdate_its_availability(economy, monkeypatch):
    store, plan = economy
    actual = plan["economic"][0]["actual"]
    view = store.read_source(review.SourceRef.from_dict(actual["reference"]))
    table = io.StringIO(newline="")
    writer = csv.DictWriter(table, fieldnames=list(view["data"][0]))
    writer.writeheader(); writer.writerows(view["data"])
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-09-01T08:00:00+08:00")
    later = store.put_source("later-retained-actual", SourceInput(**{
        **view["manifest"]["input"], "source_id": "later-retained-actual", "text": table.getvalue()}))
    actual["reference"] = asdict(later)
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == "actual-unavailable" and row["comparison"] is None
    assert row["reason_code"] == "OUTCOME_ACTUAL_RETAINED_AFTER_CUTOFF"
    assert row["forecast"]["original_unresolved"] == ["Cash conversion unverified"]


@pytest.mark.parametrize("cutoff,status", [("2026-06-01T09:00:00+08:00", "not-mature"),
    ("2026-06-30T23:00:00+08:00", "not-mature"),
    ("2026-07-01T09:00:00+08:00", "not-published-at-cutoff"),
    ("2026-07-20T09:00:00+08:00", "actual-unavailable")])
def test_future_period_publication_and_late_acquisition_never_return_actual_values(economy, cutoff, status):
    store, plan = economy; plan["as_of"] = cutoff
    row = review.open_outcome_review(store, plan)["economic"]["rows"][0]
    assert row["status"] == status and row["actual"] is row["comparison"] is None


def test_missing_actual_and_invalid_original_keep_declared_denominator(economy):
    store, plan = economy
    other = deepcopy(plan["economic"][0]); other["forecast_id"] = "missing"
    other["forecast"]["basis"]["metric"] = "operating-profit"
    other["forecast"]["context_bindings"].pop("metric")
    other.update(actual=None, missing_reason="Exact comparable actual not obtained")
    plan["economic"].append(other)
    result = review.open_outcome_review(store, plan)
    assert result["economic"]["scope"]["declared_forecasts"] == 2
    assert result["economic"]["scope"]["declared_event_keys"] == 1
    assert result["economic"]["scope"]["independent_sample_count"] is None
    assert result["economic"]["rows"][1]["status"] == "actual-not-retained"
    plan["economic"][0]["forecast"]["quote"] += "Later invented number 999\n"
    result = review.open_outcome_review(store, plan)
    assert result["economic"]["scope"]["declared_forecasts"] == 2
    assert result["economic"]["rows"][0]["reason_code"] == "OUTCOME_ORIGINAL_QUOTE_MISMATCH"


def test_same_original_forecast_cannot_be_renamed_to_inflate_count(economy):
    store, plan = economy
    other = deepcopy(plan["economic"][0]); other["forecast_id"] = "renamed"
    plan["economic"].append(other)
    with pytest.raises(RecordError, match="OUTCOME_DUPLICATE_SELECTION"):
        review.open_outcome_review(store, plan)


@pytest.mark.parametrize("failure", [StoreUnavailable("REMOTE_REQUEST_LIMIT"),
    RecordError("PRIVATE_UPGRADE_REVERSED"), RecordError("OPERATING_SOURCE_SCOPE_BOUND")])
def test_access_and_budget_failure_are_terminal_for_whole_operation(economy, monkeypatch, failure):
    store, plan = economy
    original = MemoryBackend.resolve
    ref = plan["economic"][0]["actual"]["reference"]["manifest"]
    calls = []
    def resolve(self, value):
        if value.path == ref["path"]:
            calls.append(value.path)
            raise failure
        return original(self, value)
    monkeypatch.setattr(MemoryBackend, "resolve", resolve)
    with pytest.raises(RecordError, match=str(failure)):
        review.open_outcome_review(store, plan)
    assert calls == [ref["path"]]


def test_byte_budget_and_final_scope_exit_cannot_release_partial_export(economy, monkeypatch):
    from contextlib import contextmanager
    store, plan = economy
    monkeypatch.setattr(review, "MAX_READ_BYTES", 1)
    with pytest.raises(RecordError, match="OUTCOME_READING_BOUND"):
        review.open_outcome_review(store, plan)
    monkeypatch.setattr(review, "MAX_READ_BYTES", 8 * 1024 * 1024)
    @contextmanager
    def denied(backend):
        yield backend
        raise StoreUnavailable("PRIVATE_ACCESS_REVOKED")
    monkeypatch.setattr(review, "reading_scope", denied)
    with pytest.raises(StoreUnavailable, match="PRIVATE_ACCESS_REVOKED"):
        review.open_outcome_review(store, plan)


def market_plan(before, after, **page):
    return {"schema": review.SCHEMA, "as_of": "2026-10-20T10:00:00+08:00", "economic": [],
            "market": {"before": before, "after": after, **page}}


def test_bad_market_reference_preserves_valid_economic_panel_but_access_failure_is_terminal(economy, monkeypatch):
    store, plan = economy
    both = (store, lambda: VersionedStore(MemoryBackend(store.backend.shared)))
    saved = save_sequence(both, [rising(30)])[0]
    ref = deepcopy(saved["reference"])
    ref["sha256"] = "0" * 64
    plan["as_of"] = "2026-10-20T10:00:00+08:00"
    plan["market"] = {"before": ref, "after": saved["reference"]}
    result = review.open_outcome_review(store, plan, registry=REGISTRY)
    assert result["economic"]["rows"][0]["comparison"]["difference"] == "10"
    assert result["market"]["status"] == "unavailable" and result["market"]["scope"] is None
    calls = []
    def stopped(*args):
        calls.append(1)
        raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
    monkeypatch.setattr(review, "_market", stopped)
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        review.open_outcome_review(store, plan, registry=REGISTRY)
    assert calls == [1]


def test_saved_market_events_and_original_full_denominator_survive_paging(storage):
    first, last = save_sequence(storage, [rising(30), rising(55)])
    before = storage[0].backend.head()
    plan = market_plan(first["reference"], last["reference"], limit=1)
    result = review.open_outcome_review(storage[0], plan, registry=REGISTRY)["market"]
    assert result["page"] == {"offset": 0, "limit": 1, "total": 2, "returned": 1, "remaining": 1}
    row = result["rows"][0]
    assert row["original_event"] == first["result"]["events"][row["original_state"]["origin_event"]]
    assert [h["target_date"] for h in row["horizons"]] == [DAYS[i][:4] + "-" + DAYS[i][4:6] + "-" + DAYS[i][6:] for i in (34, 49)]
    assert all(h["status"] == "observed" for h in row["horizons"])
    assert result["scope"]["sector"]["original_scope_count"] == 2
    assert result["scope"]["sector"]["horizons"]["5"] == {"observed": 1, "original-unqualified": 1}
    plan["market"]["offset"] = 1
    second = review.open_outcome_review(storage[1](), plan, registry=REGISTRY)["market"]
    assert second["scope"] == result["scope"] and second["whole_rows_sha256"] == result["whole_rows_sha256"]
    assert second["rows"][0]["original_gap"]["reason_code"] == "DISCOVERY_NOT_REQUESTED"
    assert second["win_rate"] is second["method_effectiveness"] is None
    assert storage[0].backend.head() == before


def test_maturing_one_horizon_does_not_promote_unobserved_twenty_sessions(storage):
    first, last = save_sequence(storage, [rising(30), rising(35)])
    result = review.open_outcome_review(storage[0], market_plan(first["reference"], last["reference"]), registry=REGISTRY)
    a, b = result["market"]["rows"][0]["horizons"]
    assert a["status"] == "observed" and b["status"] == "not-mature-in-retained-window"
    assert b["observed_subsequent_sessions"] == 5 and b["target_date"] is b["return_pct"] is None
    assert b["maturity_at_later_calendar_dates"] == "unknown-needs-later-retained-calendar"


def test_missing_session_close_never_moves_endpoint_to_a_later_bar(storage):
    first = save_sequence(storage, [rising(30)])[0]
    later_source = retain(storage, rising(55), missing=(DAYS[32],))
    last = MarketDiscovery(storage[0], REGISTRY).save("later-gap", request(later_source))
    result = review.open_outcome_review(storage[0], market_plan(first["reference"], last["reference"]), registry=REGISTRY)
    row = result["market"]["rows"][0]["horizons"][0]
    assert row["target_date"] == "2026-07-05"
    assert row["missing_sessions"] == ["2026-07-03"]
    assert row["status"] == "source-needed" and row["return_pct"] is None


def test_gap_after_the_fixed_window_does_not_block_an_already_complete_horizon(storage):
    first = save_sequence(storage, [rising(30)])[0]
    later_source = retain(storage, rising(55), missing=(DAYS[53],))
    last = MarketDiscovery(storage[0], REGISTRY).save("later-outside-window-gap", request(later_source))
    assert last["result"]["surfaces"]["sector"]["states"][0]["state"] == "unknown"
    result = review.open_outcome_review(storage[0], market_plan(first["reference"], last["reference"]), registry=REGISTRY)
    assert all(h["status"] == "observed" for h in result["market"]["rows"][0]["horizons"])


def test_prices_diagnostically_retained_after_observation_cutoff_do_not_gain_source_qualification(storage):
    from decision_kernel.application.market_custody import retain_market_reading
    from test_market_custody import capture_plan, RelayHTTP, RetryPolicy, SyntheticOpener
    from test_market_discovery import CODE, group_payload
    first = save_sequence(storage, [rising(30)])[0]
    payload = group_payload(rising(55))
    fields = ["ts_code", "trade_date", "close"]
    rows = [[CODE, day, str(value)] for day, value in zip(DAYS, rising(55))]
    query = {"api": "ths_daily", "stage": "baseline", "purpose": "synthetic later acquisition",
        "parameters": {"ts_code": CODE, "start_date": DAYS[0], "end_date": DAYS[54],
            "fields": ",".join(fields), "limit": "100"}}
    payload["index_results"][CODE] = list(capture_plan({"schema": "research-source-plan-v1",
        "case_id": "synthetic-outcome", "requests": [query]}, REGISTRY,
        client=RelayHTTP(opener=SyntheticOpener("ths_daily", fields, rows), credential="synthetic-key-only"),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: "2026-10-20T00:00:00+00:00"))[0]
    reference = retain_market_reading(payload, REGISTRY, storage[0], fresh_reader=storage[1],
        public_rights_confirmed=True)["reference"]
    last = MarketDiscovery(storage[0], REGISTRY).save("later-unqualified-clock", request(reference))
    assert last["result"]["surfaces"]["sector"]["states"][0]["reason_code"] == "DISCOVERY_SOURCE_AFTER_OBSERVATION"
    result = review.open_outcome_review(storage[0], market_plan(first["reference"], last["reference"]), registry=REGISTRY)
    for row in result["market"]["rows"][0]["horizons"]:
        assert row["status"] == "source-needed" and row["return_pct"] is None
        assert row["reason_code"] == "OUTCOME_LATER_SOURCE_UNQUALIFIED"
        assert row["later_observation_reason"] == "DISCOVERY_SOURCE_AFTER_OBSERVATION"


@pytest.mark.parametrize("kind", ["price-revision", "contract-change"])
def test_later_historical_revision_or_changed_identity_never_overwrites_original(storage, kind):
    first = save_sequence(storage, [rising(30)])[0]
    values = rising(55)
    changes = {}
    if kind == "price-revision":
        values[22] = 123
    else:
        changes["name"] = "Synthetic changed classification"
    last = MarketDiscovery(storage[0], REGISTRY).save("later-revised", request(retain(storage, values, **changes)))
    result = review.open_outcome_review(storage[0], market_plan(first["reference"], last["reference"]), registry=REGISTRY)
    out = result["market"]["rows"][0]
    assert all(x["status"] == "incomparable" and x["return_pct"] is None for x in out["horizons"])
    assert out["original_state"] == first["result"]["surfaces"]["sector"]["states"][0]


def test_sessions_are_not_weekdays_or_available_bar_positions():
    days = ["2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09", "2026-10-12"]
    original = {"contract": "qualified-context", "sessions": days[:1], "prices": {days[0]: "10"}}
    later = {"contract": "qualified-context", "sessions": days, "prices": {d: str(10 + i) for i, d in enumerate(days)}}
    out = horizon_change(original, later, days[0], 5)
    assert out["target_date"] == "2026-10-12" and out["return_pct"] == "50.0"
    del later["prices"][days[2]]
    out = horizon_change(original, later, days[0], 5)
    assert out["target_date"] == "2026-10-12" and out["status"] == "source-needed"


def test_ftshare_original_missing_master_coverage_cannot_become_zero_or_disappear():
    from test_ftshare_collection import adopt_kernel, kernel_adoption_fixture
    fixture = kernel_adoption_fixture()
    retained = adopt_kernel(fixture)
    plan = {"schema": "market-discovery-plan-v1", "as_of": "2026-10-19T12:05:00+00:00",
        "surfaces": {"stock": {"reference": retained["reference"], "adjustment": "raw"}}}
    saved = MarketDiscovery(fixture["store"], REGISTRY).save("outcome-ftshare", plan)
    head = fixture["store"].backend.head()
    result = review.open_outcome_review(fixture["fresh"](), market_plan(saved["reference"], saved["reference"]), registry=REGISTRY)
    stock = result["market"]["scope"]["stock"]
    assert stock["original_scope_count"] == len(fixture["context"]["scope_symbols"])
    assert sum(stock["horizons"]["5"].values()) == stock["original_scope_count"]
    assert any(row.get("original_gap", {}).get("reason_code") == "DISCOVERY_REFERENCE_QUARANTINED"
               for row in result["market"]["rows"])
    assert fixture["store"].backend.head() == head


def test_independent_cli_read_on_real_git_matches_same_originals(monkeypatch, tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "records.git"))
    plan = prepare(store, monkeypatch)
    payload = tmp_path / "plan.json"; payload.write_bytes(encoded(plan))
    head = store.backend.head()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.outcome_review_cli",
        "--path", str(store.backend.root), "--input", str(payload),
        "--registry", str(root / "docs/sources/relay-registry.json")],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0 and result.stderr == ""
    assert encoded(json.loads(result.stdout)) == encoded(review.open_outcome_review(store, plan))
    assert store.backend.head() == head


def test_room_panel_next_plan_preserves_fixed_generation_and_denominator(storage):
    from decision_kernel.application.reading_outcome import open_outcome
    first, last = save_sequence(storage, [rising(30), rising(55)])
    plan = market_plan(first["reference"], last["reference"], limit=1)
    panel = open_outcome(storage[0], plan, registry=REGISTRY)
    assert panel["status"] == "readable" and panel["next_plan"]["market"]["offset"] == 1
    assert panel["next_plan"]["market"]["before"] == plan["market"]["before"]
    assert panel["next_plan"]["market"]["after"] == plan["market"]["after"]
    second = open_outcome(storage[1](), panel["next_plan"], registry=REGISTRY)
    assert second["next_plan"] is None
    assert second["view"]["market"]["scope"] == panel["view"]["market"]["scope"]


def test_host_export_declarations_are_checked_before_record_access(economy):
    from decision_kernel.interfaces.outcome_review_commands import perform
    store, plan = economy
    for flags, error in [({}, "OUTCOME_PUBLIC_RESEARCH_NOT_CONFIRMED"),
        ({"public_research_custody_confirmed": True}, "OUTCOME_PUBLIC_SOURCES_NOT_CONFIRMED"),
        ({"public_research_custody_confirmed": "true"}, "OUTCOME_PUBLIC_RESEARCH_NOT_CONFIRMED"),
        ({"public_research_custody_confirmed": True, "public_sources_custody_confirmed": "true"}, "OUTCOME_PUBLIC_SOURCES_NOT_CONFIRMED")]:
        with pytest.raises(RecordError, match=error):
            perform(None, {"plan": plan, **flags})
    assert perform(store, {"plan": plan}, public=False) == review.open_outcome_review(store, plan)
    assert perform(store, {"plan": plan, "public_research_custody_confirmed": True,
        "public_sources_custody_confirmed": True}) == review.open_outcome_review(store, plan)


def test_optional_room_renderer_executes_literal_economic_and_market_rows(economy):
    from decision_kernel.application.reading_outcome import open_outcome
    from decision_kernel.interfaces.reading_outcome_page import JS
    node = shutil.which("node")
    if node is None:
        pytest.skip("optional Room script execution requires the existing Node runtime")
    store, plan = economy
    both = (store, lambda: VersionedStore(MemoryBackend(store.backend.shared)))
    first, last = save_sequence(both, [rising(30), rising(55)])
    plan["as_of"] = "2026-10-20T10:00:00+08:00"
    plan["market"] = {"before": first["reference"], "after": last["reference"]}
    panel = open_outcome(store, plan, registry=REGISTRY)
    literal = "</script><script>globalThis.pwned=1</script>"
    panel["view"]["economic"]["rows"][0]["forecast"]["original_excerpt"]["text"] = literal
    prefix = r'''
const D=JSON.parse(require('fs').readFileSync(0,'utf8'));
class E{constructor(tag,text,cls){this.tag=tag;this.textContent=text===undefined?'':String(text);this.children=[];this.className=cls;}append(...n){this.children.push(...n);}}
const pane=new E('div'),calls=[],buttons=[],$=id=>pane;
const el=(tag,text,cls)=>new E(tag,text,cls),details=(title,value)=>el('details',JSON.stringify(value));
const button=(text,fn)=>{const b=el('button',text);b.run=fn;buttons.push(b);return b;};
const request=(name,value)=>calls.push(value),roomRequest=plan=>({operation:'reading-room',payload:{plan}}),label=x=>x,save=()=>{};
'''
    suffix = r'''
buttons[0].run();
buttons.find(b=>b.textContent==='导出实际来源读取请求').run();
const flatten=e=>e.textContent+e.children.map(flatten).join('\n');
process.stdout.write(JSON.stringify({text:flatten(pane),calls,pwned:globalThis.pwned===1}));
'''
    result = subprocess.run([node, "-e", prefix + JS + suffix],
        input=encoded({"plan": {"outcome": plan}, "outcome": panel}).decode(),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stderr == ""
    rendered = json.loads(result.stdout)
    assert literal in rendered["text"] and rendered["pwned"] is False
    assert "原预测 100；实际 110" in rendered["text"] and "T+5" in rendered["text"]
    assert "原范围内未合格" in rendered["text"]
    assert encoded(rendered["calls"]) == encoded([
        {"operation": "reading-room", "payload": {"plan": {"outcome": plan}}},
        {"operation": "read-source", "payload": plan["economic"][0]["actual"]["reference"]}])
