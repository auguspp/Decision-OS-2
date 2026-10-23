"""Book discovery on real stored artifacts: identity, coverage and terminal risk."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from decision_kernel.application import odds_book as book
from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD, SPEC_RECORD
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity, pointer_path
from decision_kernel.domain.odds_watch import CHECK_RECORD
from decision_kernel.domain.records import RecordError, RevisionInput, StoreUnavailable
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, context, price_payload, spec_payload
from test_github_store import GitAPI
from test_odds_watch import case
from test_probability_odds import calculation, ctx
from test_watch_activity import apply, change


def collect(store, plan=None):
    result, pages = book.read_book(store, plan or {"limit": 8, "scan_limit": 32}), []
    while True:
        pages.append(result)
        if result["next_cursor"] is None: return pages
        result = book.read_book(store, {"cursor": result["next_cursor"], "limit": 8, "scan_limit": 32})


def saved_pair(context):
    store, research = context
    service = ConditionalOdds(store, allow_declared_only=True)
    spec = service.freeze(authorized("spec", spec_payload(asdict(research))), submission_id="book-spec")["reference"]
    first = service.save(authorized("calculation", {"spec_ref": spec, "price": price_payload()}),
                         submission_id="book-first")["reference"]
    # Same immutable spec bytes at a later reachable commit are the same object,
    # while both original references must survive navigation and comparison.
    alias = {**spec, "commit": store.backend.head()}
    second = service.save(authorized("calculation", {"spec_ref": alias, "price": price_payload(price="80", price_id="p2")}),
                          submission_id="book-second")["reference"]
    return store, research, spec, alias, first, second


def test_real_multi_case_versions_same_spec_prices_and_native_reopen(context, monkeypatch):
    store, research, spec, alias, first, second = saved_pair(context)
    newer = store.submit_revision("new-full", RevisionInput("case", "r2", "full", "New synthetic Full",
        "Same commission", "synthetic-method", predecessor=research, unresolved=("New unresolved evidence",)))
    other = store.submit_revision("other-full", RevisionInput("other", "r1", "full", "Other synthetic Full",
        "Separate commission", "synthetic-method", unresolved=("No saved valuation",)))
    before = store.backend.head()
    originals = {ref["path"]: ConditionalOdds(store).read(ref)["result"] for ref in (first, second)}
    with monkeypatch.context() as patch:
        patch.setattr(ConditionalOdds, "compute", lambda *a, **k: pytest.fail("Book must not calculate"))
        patch.setattr(OddsWatch, "read_watch", lambda *a, **k: pytest.fail("Book must not replay a Watch"))
        result = book.read_book(store, {"limit": 8, "scan_limit": 32})
    assert result["snapshot"] == before and store.backend.head() == before
    assert result["coverage"] == "this-page-only" and result["semantic_replay"] == "not-requested"
    assert result["current_validity"] == "not-certified" and result["persistence"] == "not-written"
    assert all(row["status"] == "readable" for row in result["items"])
    groups = {row["research"]["revision_id"] + row["research"]["case_id"]: row for row in result["research_relations"]}
    historical = groups["r1case"]
    assert historical["research"]["at_snapshot"] == {"status": "historical", "reference": asdict(newer)}
    assert historical["saved_odds"].startswith("present-in-this-page")
    same = historical["spec_groups"][0]
    assert {tuple(sorted(ref.items())) for ref in same["original_references"]} == {
        tuple(sorted(spec.items())), tuple(sorted(alias.items()))}
    assert {row["price"]["price"] for row in same["results"]} == {"80", "100"}
    assert groups["r2case"]["saved_odds"] == "not-found-in-single-scan-of-known-layouts"
    assert groups["r1other"]["research"]["reference"] == asdict(other)
    assert groups["r1other"]["saved_odds"] == "not-found-in-single-scan-of-known-layouts"
    for row in result["items"]:
        if row["kind"] == RESULT_RECORD:
            assert row["recorded"] == originals[row["reference"]["path"]]
            assert row["open_operation"] == "read-odds" and row["open_payload"] == {"reference": row["reference"]}
            assert ConditionalOdds(store).read(row["open_payload"]["reference"])["recalculation"] == "matches"


def test_old_cursor_retains_research_pointer_and_inventory_after_new_full(context):
    store, research, *_ = saved_pair(context)
    first = book.read_book(store, {"limit": 1, "scan_limit": 1})
    old_snapshot = first["snapshot"]
    new = store.submit_revision("later", RevisionInput("case", "r2", "full", "Later Full", "Same commission",
        "synthetic", predecessor=research, unresolved=("Later evidence",)))
    store.submit_revision("new-case", RevisionInput("aaa", "r1", "quick", "New quick", "New commission",
        "synthetic", unresolved=("No original",)))
    pages = collect(store, {"cursor": first["next_cursor"], "limit": 8, "scan_limit": 32})
    assert all(page["snapshot"] == old_snapshot for page in pages)
    rows = first["items"] + [row for page in pages for row in page["items"]]
    assert len({row["reference"]["path"] for row in rows}) == first["scan"]["total_candidates"]
    assert all(row["research"]["at_snapshot"] == {"status": "current", "reference": asdict(research)} for row in rows)
    assert all(row["research"]["reference"] != asdict(new) for row in rows)
    assert all(page["coverage"] == "this-page-only" for page in pages)


def test_tail_cursor_and_later_page_do_not_prove_earlier_read_or_global_absence(context):
    store, _ = context
    store.submit_revision("other", RevisionInput("z", "r1", "full", "Other", "Other", "synthetic", unresolved=("Unknown",)))
    first = book.read_book(store, {"limit": 1})
    tail = book.read_book(store, {"cursor": first["next_cursor"]})
    assert tail["scan"]["at_end"] and tail["scan"]["start"] > 0
    assert tail["research_relations"][0]["saved_odds"] == "not-observed-in-this-page"
    forged = {**first["next_cursor"], "offset": first["scan"]["total_candidates"]}
    empty = book.read_book(store, {"cursor": forged})
    assert empty["scan"]["at_end"] and empty["status"] == "no-match-in-this-page"
    assert "complete" not in empty["scan"] and "all_seen" not in empty["scan"]


def test_filter_still_accounts_for_scanned_nonmatches_and_preserves_corruption(context):
    store, research, spec, _, first, _ = saved_pair(context)
    other = store.submit_revision("other", RevisionInput("other", "r1", "full", "Other", "Other", "synthetic", unresolved=("Unknown",)))
    bounded = book.read_book(store, {"research_ref": asdict(other), "scan_limit": 1, "limit": 8})
    assert not bounded["items"] and bounded["status"] == "no-match-in-this-page"
    assert bounded["scan"]["end"] == 1 and bounded["next_cursor"] is not None
    # Original artifact remains valid in history, but its snapshot content was changed.
    content = first["path"].rsplit("/", 1)[0] + "/content.bin"
    store.backend.commit({content: b"corrupt latest snapshot bytes"}, expected=store.backend.head())
    result = book.read_book(store, {"research_ref": asdict(other), "limit": 8, "scan_limit": 32})
    failed = [row for row in result["items"] if row["status"] == "invalid"]
    assert len(failed) == 1 and failed[0]["reference"] == first
    assert failed[0]["filter_match"] == "unknown" and result["status"] == "partial-page"
    assert result["research_relations"][0]["saved_odds"] == "not-observed-in-this-page"
    assert ConditionalOdds(store).read(first)["recalculation"] == "matches"


def test_changed_research_body_at_snapshot_does_not_reuse_old_good_body(context):
    store, research, *_ = saved_pair(context)
    path = research.path.rsplit("/", 1)[0] + "/research.md"
    store.backend.commit({path: b"changed outside original record"}, expected=store.backend.head())
    result = book.read_book(store, {"limit": 8, "scan_limit": 32})
    assert result["status"] == "partial-page"
    assert all(row["status"] == "invalid" for row in result["items"])
    assert not result["research_relations"]


@pytest.mark.parametrize("damage", ["price", "worlds", "world-id", "acceptance"])
def test_hash_valid_but_malformed_typed_result_is_not_a_readable_odds_row(context, damage):
    store, _, _, _, first, _ = saved_pair(context)
    service = ConditionalOdds(store)
    value = service.read(first)["result"]
    if damage == "price": value["price"] = "100"
    elif damage == "worlds": del value["world_results"]
    elif damage == "world-id": value["world_results"][0]["world_id"] = "unrelated"
    else: value["human_acceptance"] = "confirmed"
    bad = asdict(service._put("malformed", RESULT_RECORD, value, "Synthetic malformed fixture"))
    result = book.read_book(store, {"limit": 8, "scan_limit": 32})
    bad_row = next(row for row in result["items"] if row["reference"] == bad)
    assert bad_row["status"] == "invalid" and result["status"] == "partial-page"
    assert any(row["reference"] == first and row["status"] == "readable" for row in result["items"])


def test_orphan_companions_remain_missing_candidates_not_an_empty_success(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "orphan.git"))
    orphan = "research/artifacts/odds-result-" + "a" * 64 + "/" + "b" * 64 + "/content.bin"
    store.backend.commit({orphan: b"missing manifest", "research/cases/orphan/revisions/r1/research.md": b"missing record"},
                         expected=store.backend.head())
    result = book.read_book(store, {"limit": 8})
    assert result["scan"]["total_candidates"] == 2 and result["status"] == "partial-page"
    assert all(row["status"] == "missing" for row in result["items"])
    assert all(row["reference"] is None and row["open_payload"] is None for row in result["items"])


def test_valid_eighty_digit_saved_output_is_preserved_without_input_precision_gate(context):
    store, research = context
    service = ConditionalOdds(store, allow_declared_only=True)
    spec = service.freeze(authorized("spec", spec_payload(asdict(research))), submission_id="spec")["reference"]
    saved = service.save(authorized("calculation", {"spec_ref": spec, "price": price_payload(price="7")}),
                         submission_id="long-result")["reference"]
    result = book.read_book(store, {"limit": 8})
    row = next(row for row in result["items"] if row["reference"] == saved)
    number = row["recorded"]["world_results"][0]["undiscounted_holding_period_return"]
    assert row["status"] == "readable" and len(number) > 48
    assert number == service.read(saved)["result"]["world_results"][0]["undiscounted_holding_period_return"]


def test_ref_after_snapshot_is_not_admitted_even_with_same_path_and_bytes(context):
    store, research = context
    selected = store.backend.head()
    store.submit_revision("unrelated", RevisionInput("other", "r1", "full", "Other", "Other", "synthetic", unresolved=("Unknown",)))
    future_alias = {**asdict(research), "commit": store.backend.head()}
    with pytest.raises(RecordError, match="REFERENCE_AFTER_SNAPSHOT"):
        book.read_book(store, {"snapshot": selected, "research_ref": future_alias})


def test_watch_retirement_is_recorded_not_verified_history_and_filter_follows_watch(case):
    store, research, _, _, watch = case
    svc = WatchActivity(store)
    registered = apply(svc, change(watch), "register")
    retired = apply(svc, change(watch, registered, "retire", "2026-01-03T04:01:00Z"), "retire")
    before = store.backend.head()
    pages = collect(store, {"research_ref": asdict(research), "limit": 8, "scan_limit": 32})
    rows = [row for page in pages for row in page["items"]]
    assert all(row["status"] == "readable" for row in rows)
    pointer = next(row for row in rows if row["kind"] == "activity-pointer")
    assert pointer["recorded"]["stored_state"] == "retired"
    assert pointer["recorded"]["history_validation"] == "not-requested"
    assert pointer["recorded"]["event_ref"] == retired
    assert pointer["open_payload"] == {"reference": retired}
    assert all(row["research_key"] == pointer["research_key"] for row in rows)
    assert store.backend.head() == before and svc.read(retired)["recalculation"] == "matches"


def test_absent_activity_pointer_is_unknown_not_unregistered(case):
    store, _, _, _, watch = case
    rows = collect(store)[0]["items"]
    row = next(item for item in rows if item["reference"] == watch)
    assert row["activity_selection"]["status"] == "pointer-absent-unknown"
    assert row["activity_selection"]["history_validation"] == "not-requested"


@pytest.mark.parametrize("kind", [CHECK_RECORD, ACTIVITY_RECORD])
@pytest.mark.parametrize("damage", ["status", "condition-status"])
def test_watch_check_statuses_are_typed_in_both_original_record_paths(case, kind, damage):
    from test_watch_activity import query
    from test_watch_conditions import v2_input, watch_v2
    store, _, spec, service, _ = case
    watch = service.freeze(authorized("watch", watch_v2(spec)), submission_id="book-v2")["reference"]
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "book-register")
    active = apply(activity, change(watch, registered, "resume", "2026-01-03T04:01:00Z"), "book-resume")
    saved = activity.save_check(authorized("check", query(store, watch, active,
        price=v2_input(price_payload()))), submission_id="book-check")["reference"]
    event = activity.read(saved)["event"]
    if damage == "status": event["result"]["status"] = "HUMAN_APPROVED_BUY"
    else: event["result"]["condition_results"][0]["status"] = "UNDECLARED_CONDITION_STATUS"
    value = event if kind == ACTIVITY_RECORD else event["result"]
    bad = asdict(ConditionalOdds(store)._put("malformed-check", kind, value, "Synthetic malformed fixture"))
    pages = collect(store)
    rows = [row for page in pages for row in page["items"]]
    rejected = next(row for row in rows if row["reference"] == bad)
    assert rejected["status"] == "invalid" and rejected["reason"] == "ODDS_BOOK_CHECK_STATUS_INVALID"
    assert next(row for row in rows if row["reference"] == saved)["status"] == "readable"
    assert any(page["status"] == "partial-page" for page in pages)


def test_full_v3_activity_result_keeps_ordered_stored_controls_without_replay(case, monkeypatch):
    from test_full_watch import full_watch, saved_odds
    from test_watch_activity import query
    from test_watch_conditions import v2_input
    store, _, spec, service, _ = case
    watch = service.freeze(authorized("watch", full_watch(spec, saved_odds(store, spec))),
                           submission_id="book-full-v3")["reference"]
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "book-register")
    active = apply(activity, change(watch, registered, "resume", "2026-01-03T04:01:00Z"), "book-resume")
    saved = activity.save_check(authorized("check", query(store, watch, active,
        price=v2_input(price_payload()))), submission_id="book-check")["reference"]
    original = activity.read(saved)["event"]["result"]
    before = store.backend.head()
    with monkeypatch.context() as patch:
        patch.setattr(WatchActivity, "read", lambda *a, **k: pytest.fail("No activity history replay"))
        patch.setattr(OddsWatch, "read_check", lambda *a, **k: pytest.fail("No check truth replay"))
        rows = [row for page in collect(store) for row in page["items"]]
    row = next(row for row in rows if row["reference"] == saved)
    assert row["status"] == "readable" and row["recorded"]["recorded_result"] == original
    assert original["schema"] == "odds-watch-check-v3"
    assert [result["condition_id"] for result in original["condition_results"]] == ["first", "second"]
    assert [result["status"] for result in original["condition_results"]] == ["first-trigger", "not-met"]
    assert row["recorded"]["history_validation"] == "not-requested" and store.backend.head() == before


def test_probability_projection_keeps_declared_policy_and_saved_market_clock(ctx, monkeypatch):
    store, service, *_ = ctx
    saved = service.save(authorized("calculation", calculation(ctx)), submission_id="prob-result")["reference"]
    original = service.read(saved)["result"]
    with monkeypatch.context() as patch:
        patch.setattr(type(service), "compute", lambda *a, **k: pytest.fail("No probability replay on Book scan"))
        result = book.read_book(store, {"limit": 8, "scan_limit": 32})
    row = next(item for item in result["items"] if item["reference"] == saved)
    assert row["status"] == "readable" and row["recorded"] == original
    assert row["recorded"]["calculation_input"]["evaluated_at"] == "2026-01-03T04:00:00+00:00"
    assert row["qualification"] == "stored-declarations-only" and row["current_validity"] == "not-certified"


@pytest.mark.parametrize("damage", ["aggregate", "probability", "clock", "zone"])
def test_probability_shape_does_not_turn_arbitrary_stored_fields_into_qualified_rows(ctx, damage):
    from decision_kernel.domain.probability_odds import RESULT_RECORD as PROBABILITY_RECORD
    store, service, *_ = ctx
    saved = service.save(authorized("calculation", calculation(ctx)), submission_id="prob-result")["reference"]
    value = service.read(saved)["result"]
    if damage == "aggregate": value["aggregate"] = {}
    elif damage == "probability": value["world_results"][0]["probability"] = "NaN"
    elif damage == "clock": del value["calculation_input"]
    else: value["participation_zone"] = "HUMAN_APPROVED_BUY"
    bad = asdict(ConditionalOdds(store)._put("malformed-prob", PROBABILITY_RECORD, value, "Synthetic malformed fixture"))
    result = book.read_book(store, {"limit": 8, "scan_limit": 32})
    assert next(row for row in result["items"] if row["reference"] == bad)["status"] == "invalid"
    assert next(row for row in result["items"] if row["reference"] == saved)["status"] == "readable"
    assert result["status"] == "partial-page"


def test_saved_ftshare_close_and_full_price_watch_keep_original_clocks_without_replay(monkeypatch):
    from test_ftshare_odds import build_case, conditional_input
    from test_full_watch import full_watch
    from test_watch_conditions import numeric
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-10-19T12:03:00+00:00")
    case = build_case()
    saved = case["conditional"].save(authorized("calculation", conditional_input(case)), submission_id="retained")["reference"]
    watch = full_watch(case["spec_ref"], saved, conditions=[numeric("price")])
    watch["activation"]["as_of"] = watch["window"]["starts_at"] = "2026-10-19T13:10:00+00:00"
    watch["data_policy"] = {"price_kind": "retained-ftshare-raw-close", "market_policy": case["policy"]}
    frozen = OddsWatch(case["store"]).freeze(authorized("watch", watch), submission_id="full-price-watch")["reference"]
    before = case["store"].backend.head()
    with monkeypatch.context() as patch:
        patch.setattr(ConditionalOdds, "compute", lambda *a, **k: pytest.fail("No market requalification"))
        patch.setattr(OddsWatch, "read_watch", lambda *a, **k: pytest.fail("No Full Watch semantic replay"))
        rows = [row for page in collect(case["store"]) for row in page["items"]]
    result = next(row for row in rows if row["reference"] == saved)
    tracking = next(row for row in rows if row["reference"] == frozen)
    assert result["status"] == tracking["status"] == "readable"
    assert result["recorded"]["price"]["kind"] == "retained-ftshare-raw-close"
    assert result["recorded"]["calculation_input"]["evaluated_at"] == "2026-10-19T13:00:00+00:00"
    assert tracking["recorded"]["tracking"]["odds_result"]["reference"] == saved
    assert case["store"].backend.head() == before


@pytest.mark.parametrize("payload", [None, [], {"unexpected": 1}, {"limit": True}, {"limit": 0},
    {"limit": 9}, {"scan_limit": 0}, {"scan_limit": 33}, {"snapshot": "main"}, {"cursor": {}},
    {"research_ref": {"repository": "x", "commit": "a" * 40, "path": "research/cases/x/current.json", "sha256": "b" * 64}}])
def test_preflight_rejects_unbounded_or_ambiguous_inputs_without_io(payload):
    with pytest.raises(RecordError): book.validate_plan(payload)


def test_card_budget_does_not_remove_candidate_or_claim_absence(context, monkeypatch):
    store, _ = context
    monkeypatch.setattr(book, "MAX_CARD_BYTES", 20)
    result = book.read_book(store, {})
    assert result["status"] == "partial-page" and result["scan"]["at_end"]
    assert result["items"][0]["status"] == "not-included"
    assert result["items"][0]["reference"] and result["items"][0]["open_payload"]
    assert result["research_relations"] == []


@pytest.mark.parametrize("code", ["REMOTE_RATE_LIMIT", "REMOTE_REQUEST_LIMIT", "REMOTE_NOT_AUTHORIZED", "REMOTE_NOT_FOUND_OR_NOT_AUTHORIZED"])
def test_original_terminal_error_stops_page_before_any_later_candidate(context, monkeypatch, code):
    store, *_ = saved_pair(context)
    original = store.backend.resolve
    calls, failure = [], StoreUnavailable(code)
    def fail(ref):
        calls.append(ref.path)
        if ref.path.endswith("manifest.json"): raise failure
        return original(ref)
    monkeypatch.setattr(store.backend, "resolve", fail)
    with pytest.raises(StoreUnavailable) as raised: book.read_book(store, {"limit": 8})
    assert raised.value is failure and len(calls) == 1


def test_terminal_latch_cannot_be_swallowed_by_nested_reader(context, monkeypatch):
    store, _ = context
    original = book._Page.item
    failure = StoreUnavailable("REMOTE_RATE_LIMIT")
    native_read = store.backend.read
    def fail(commit, path):
        if path == "source-views/synthetic-fault/identity.json": raise failure
        return native_read(commit, path)
    monkeypatch.setattr(store.backend, "read", fail)
    def swallowing(self, path, kind, row):
        try: self.reads.read(self.snapshot, "source-views/synthetic-fault/identity.json")
        except StoreUnavailable: pass
        return original(self, path, kind, row)
    monkeypatch.setattr(book._Page, "item", swallowing)
    with pytest.raises(StoreUnavailable) as raised: book.read_book(store, {})
    assert raised.value is failure


def test_github_paging_reuses_owner_budget_and_checks_final_visibility(tmp_path):
    api = GitAPI(tmp_path / "native.git")
    backend = GitHubStore(api.binding, api)
    store = VersionedStore(backend)
    old = store.submit_revision("one", RevisionInput("a", "r1", "full", "Old", "Synthetic", "synthetic", unresolved=("Unknown",)))
    store.submit_revision("two", RevisionInput("b", "r1", "full", "Other", "Synthetic", "synthetic", unresolved=("Unknown",)))
    first = book.read_book(store, {"limit": 1})
    store.submit_revision("new", RevisionInput("a", "r2", "full", "New", "Synthetic", "synthetic", predecessor=old, unresolved=("Unknown",)))
    before = len(api.requests)
    second = book.read_book(store, {"cursor": first["next_cursor"]})
    assert second["snapshot"] == first["snapshot"] and second["scan"]["total_candidates"] == 2
    assert all(method == "GET" for method, _, _ in api.requests[before:])
    assert len(api.requests) - before < 64
    count = 0
    def reverse(path, value):
        nonlocal count
        if path == "/repos/" + backend.repository:
            count += 1
            if count >= 2: return {**value, "private": False}
        return value
    api.corrupt_response = reverse
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        book.read_book(store, {"snapshot": first["snapshot"]})
