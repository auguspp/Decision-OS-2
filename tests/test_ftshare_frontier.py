"""Original finite FT chains and native Git reads; all supplier data is synthetic."""
from copy import copy, deepcopy
from dataclasses import replace

import pytest
import test_ftshare_collection as fixtures

from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from test_ftshare_collection import (
    REGISTRY, _new_calendars, _retain, plan_single, settle_single, single_collection_fixture,
)
from test_market_collection_inputs import CollectionOpener
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport


PROTECTED = ["000001.SZ", "600000.SH"]


def ready(*, storage=None, count=5):
    fixture = single_collection_fixture(storage=storage, count=count)
    initial = _retain(fixture["single"], fixture["store"], fixture["fresh"])
    _, root = settle_single(fixture, fixture["single"], initial)
    fixture["base"] = root["reference"]
    return fixture


def expand(fixture, *, base=None, generation="manual-one", symbol="600001.SH",
           kinds=("none",), state="complete", **options):
    base = base or fixture["base"]
    prior = ft.restore_ftshare_collection(base, REGISTRY, fixture["fresh"]())["collection"]["plan"]
    arguments = {"generation": generation, "start": prior["start"], "end": prior["end"],
        "completion_policy": prior.get("completion_policy"),
        "created_at": prior["created_at"],
        "targets": [{"symbol": symbol, "adjust_kind": kind} for kind in kinds],
        "source_budget": len(kinds)}
    current = plan_single(fixture, base=base, **{**arguments, **options})
    root = _retain(current, fixture["store"], fixture["fresh"])
    if state == "in-flight":
        current = ft.begin_ftshare_slot(current, current["plan"]["new_slots"][0], at=current["updated_at"])
        root = _retain(current, fixture["store"], fixture["fresh"], root["reference"])
    elif state != "pending":
        for slot in current["plan"]["new_slots"]:
            current, root = settle_single(fixture, current, root, slot,
                reason_code="FTSHARE_HTTP_503" if state == "failed" else
                    "FTSHARE_HTTP_403" if state == "held" else None,
                source_hold="FTSHARE_HTTP_403" if state == "held" else
                    "FTSHARE_HTTP_429" if state == "complete-held" else None)
    return current, root


def resolve(fixture, **kwargs):
    return ft.resolve_ftshare_expansion_frontier(fixture["base"], REGISTRY,
        fixture["fresh"](), protected_symbols=PROTECTED, **kwargs)


def test_complete_append_chain_keeps_every_owner_and_original_cumulative_budget():
    fixture = ready()
    original = ft.restore_ftshare_collection(fixture["base"], REGISTRY, fixture["fresh"]())["collection"]
    first, one = expand(fixture, kinds=("none", "forward"))
    final, two = expand(fixture, base=one["reference"], generation="manual-two", symbol="600002.SH")
    before = fixture["backend"].head()
    resolved = resolve(fixture)
    assert resolved["reference"] == two["reference"]
    assert resolved["successor_references"] == [one["reference"], two["reference"]]
    assert resolved["reason_code"] is resolved["blocking_reference"] is None
    assert resolved["source_requests"] == 0 and resolved["restored"]["batches_restored"] is True
    assert resolved["restored"]["collection"] == final
    for slot, unit in original["units"].items():
        assert final["plan"]["requests"][slot] == original["plan"]["requests"][slot]
        assert final["units"][slot] == {**unit, "request_count": 0}
    assert final["plan"]["prior_generation_reserved_requests"] == (
        original["plan"]["prior_generation_reserved_requests"]
        + original["reserved_requests"] + first["reserved_requests"])
    assert final["reserved_requests"] == final["plan"]["source_budget"] == 1
    assert fixture["backend"].head() == before


def test_adoption_owner_role_names_are_preserved_without_a_protected_list():
    fixture = ready()
    resolved = ft.resolve_ftshare_expansion_frontier(fixture["adopted"]["reference"],
        REGISTRY, fixture["fresh"]())
    assert resolved["reference"] == fixture["base"] and resolved["reason_code"] is None
    assert resolved["restored"]["collection"]["reserved_requests"] == 1


@pytest.mark.parametrize("state,reason", [
    ("pending", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("in-flight", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("failed", "FTSHARE_EXPANSION_FAILED"),
    ("held", "FTSHARE_COLLECTION_SOURCE_HOLD"),
    ("complete-held", "FTSHARE_COLLECTION_SOURCE_HOLD"),
])
def test_unresolved_or_failed_child_is_never_replaced_by_the_old_green_root(state, reason):
    fixture = ready()
    current, root = expand(fixture, state=state)
    before = fixture["backend"].head()
    resolved = resolve(fixture)
    assert resolved["reference"] == resolved["blocking_reference"] == root["reference"]
    assert resolved["reason_code"] == reason and resolved["source_requests"] == 0
    assert resolved["restored"]["collection"] == current
    assert fixture["backend"].head() == before


def test_later_structural_tip_keeps_the_first_failed_reference_and_reason():
    fixture = ready()
    failed, blocked = expand(fixture, state="failed")
    final, tip = expand(fixture, base=blocked["reference"], generation="manual-after-failure", symbol="600002.SH")
    resolved = resolve(fixture)
    assert resolved["reference"] == tip["reference"]
    assert resolved["blocking_reference"] == blocked["reference"]
    assert resolved["reason_code"] == "FTSHARE_EXPANSION_FAILED"
    assert resolved["restored"]["collection"] == final
    assert final["units"]["none:600001.SH"] == {
        **failed["units"]["none:600001.SH"], "request_count": 0}
    # The original generic FT restore contract still admits retained failure.
    assert ft.restore_ftshare_collection(blocked["reference"], REGISTRY,
        fixture["fresh"]())["collection"] == failed


def test_a_later_green_generation_cannot_clear_an_earlier_failure():
    fixture = ready()
    _, blocked = expand(fixture, state="failed")
    calendars = _new_calendars(fixture["store"], fixture["fresh"], through="20261019")
    final, tip = expand(fixture, base=blocked["reference"], generation="manual-new-window",
        end="20261019", calendar_references=calendars, created_at="2026-10-20T12:00:00+00:00")
    assert all(unit["status"] == "complete" for unit in final["units"].values())
    resolved = resolve(fixture)
    assert resolved["reference"] == tip["reference"]
    assert resolved["blocking_reference"] == blocked["reference"]
    assert resolved["reason_code"] == "FTSHARE_EXPANSION_FAILED"


def test_exact_bridge_stops_before_later_daily_generation_and_failed_descendants():
    fixture = ready()
    _, manual = expand(fixture)
    _, daily = expand(fixture, base=manual["reference"], generation="daily-20261019", symbol="600002.SH")
    _, failed = expand(fixture, base=daily["reference"], generation="manual-later", symbol="600003.SH", state="failed")
    historical = resolve(fixture, through_reference=manual["reference"])
    assert historical["reference"] == manual["reference"]
    assert historical["reason_code"] is historical["blocking_reference"] is None
    assert historical["successor_references"] == [manual["reference"]]
    current = resolve(fixture)
    assert current["reference"] == failed["reference"]
    assert current["blocking_reference"] == daily["reference"]
    assert current["reason_code"] == "FTSHARE_EXPANSION_DAILY_GENERATION"
    # A retained daily root is a legal anchor; the reserved-name rule applies to
    # a crossed child, never to that anchor or its zero-edge historical proof.
    zero = ft.resolve_ftshare_expansion_frontier(daily["reference"], REGISTRY,
        fixture["fresh"](), through_reference=daily["reference"])
    assert zero["reference"] == daily["reference"]
    assert zero["reason_code"] is None and zero["successor_references"] == []


def test_exact_bridge_does_not_consult_current_successor_inventory(monkeypatch):
    fixture = ready()
    _, manual = expand(fixture)
    def forbidden(*args, **kwargs):
        pytest.fail("historical endpoints do not require today's write frontier")
    monkeypatch.setattr(ft, "_expansion_successors", forbidden)
    monkeypatch.setattr(ft, "_single_write_frontier", forbidden)
    assert resolve(fixture, through_reference=manual["reference"])["reason_code"] is None


@pytest.mark.parametrize("change,reason", [
    ("end", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("start", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("calendar", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("completion", "FTSHARE_EXPANSION_COMPLETION_CHANGED"),
    ("reserved-generation", "FTSHARE_EXPANSION_DAILY_GENERATION"),
])
def test_original_single_generations_with_non_expansion_changes_are_only_diagnostics(change, reason):
    fixture = ready()
    options = {}
    if change in {"calendar", "end"}:
        options["calendar_references"] = _new_calendars(fixture["store"], fixture["fresh"],
            through="20261019" if change == "end" else "20261017")
    if change == "end":
        options.update(end="20261019", created_at="2026-10-20T12:00:00+00:00")
    elif change == "start":
        options["start"] = "20260702"
    elif change == "completion":
        options["completion_policy"] = EOD_COMPLETION_POLICY
    elif change == "reserved-generation":
        options["generation"] = "daily-20261019"
    current, root = expand(fixture, **options)
    resolved = resolve(fixture)
    assert resolved["reference"] == resolved["blocking_reference"] == root["reference"]
    assert resolved["reason_code"] == reason
    assert resolved["restored"]["collection"] == current


def test_protected_symbols_add_to_unconditional_existing_owner_preservation():
    fixture = ready()
    _, root = expand(fixture)
    resolved = ft.resolve_ftshare_expansion_frontier(fixture["base"], REGISTRY,
        fixture["fresh"](), protected_symbols=PROTECTED + ["600001.SH"])
    assert resolved["reference"] == root["reference"]
    assert resolved["reason_code"] == "FTSHARE_EXPANSION_PROTECTED_SYMBOL"


def test_foreign_or_non_descendant_endpoint_is_not_silently_replaced():
    fixture = ready()
    foreign = deepcopy(fixture["base"])
    foreign["manifest"]["repository"] = "other/repository"
    with pytest.raises(RecordError, match="EXACT_LOCAL_REFERENCE"):
        resolve(fixture, through_reference=foreign)
    with pytest.raises(RecordError, match="ENDPOINT_NOT_DESCENDANT"):
        resolve(fixture, through_reference=fixture["adopted"]["reference"])


def test_two_original_genesis_children_refuse_current_but_not_a_recorded_bridge():
    fixture = ready()
    competing = plan_single(fixture, base=fixture["base"], generation="manual-fork",
        targets=[{"symbol": "600002.SH", "adjust_kind": "none"}])
    _, root = expand(fixture)
    # Insert the canonical source for a competing genesis as corruption evidence;
    # the normal FT writer itself already rejects this second child.
    document = {"schema": ft.SCHEMA, "registry": deepcopy(REGISTRY.snapshot),
        "predecessor": deepcopy(fixture["base"]), "collection": competing}
    fixture["store"].put_source("synthetic-conflicting-genesis", ft._source(document))
    with pytest.raises(RecordError, match="FTSHARE_EXPANSION_FORK"):
        resolve(fixture)
    assert resolve(fixture, through_reference=root["reference"])["reference"] == root["reference"]


def test_missing_retained_price_is_not_accepted_from_a_complete_manifest():
    fixture = ready()
    current, root = expand(fixture)
    price = current["units"]["none:600001.SH"]["reference"]["manifest"]
    fixture["backend"].shared["missing"].add((price["commit"], price["path"]))
    # The manifest-only API deliberately remains a valid history read.
    assert ft.restore_ftshare_collection(root["reference"], REGISTRY,
        fixture["fresh"]())["collection"] == current
    with pytest.raises(RecordError):
        resolve(fixture)


def test_a_missing_original_revision_breaks_current_and_exact_restoration():
    fixture = ready()
    current, root = expand(fixture)
    prefix = "source-views/" + ft._source_id(current["plan"]["collection_id"],
        current["plan"]["generation"], 1) + "/"
    files = fixture["backend"].shared["trees"][fixture["backend"].head()]
    path = next(path for path in files if path.startswith(prefix) and path.endswith("/manifest.json"))
    del files[path]
    for options in ({}, {"through_reference": root["reference"]}):
        with pytest.raises(RecordError, match="FTSHARE_COLLECTION_CHAIN_GAP"):
            resolve(fixture, **options)


def test_same_source_body_referenced_at_a_later_commit_is_not_an_original_endpoint():
    fixture = ready()
    _, root = expand(fixture)
    fixture["store"].put_source("synthetic-later-record", SourceInput(source_id="synthetic-later-record",
        kind="content", title="Synthetic later record", origin="https://example.invalid/synthetic-record",
        custody="provided", text="Synthetic unrelated record, not a market observation."))
    changed = deepcopy(root["reference"])
    changed["manifest"]["commit"] = fixture["backend"].head()
    with pytest.raises(RecordError, match="REFERENCE_NOT_ORIGINAL"):
        resolve(fixture, through_reference=changed)


def test_operation_access_or_budget_failure_propagates_without_fallback(monkeypatch):
    fixture = ready()
    def unavailable(*args, **kwargs):
        raise StoreUnavailable("STORE_API_BUDGET_EXCEEDED")
    monkeypatch.setattr(ft, "_single_sources", unavailable)
    with pytest.raises(StoreUnavailable, match="STORE_API_BUDGET_EXCEEDED"):
        resolve(fixture)


def test_native_full_5226_master_six_stock_plus_one_frontier_restores_all_prices_under_admission(
        native, monkeypatch, record_property):
    model, store = native
    local_fresh = lambda: VersionedStore(copy(model.git))
    class FullMasterOpener(CollectionOpener):
        def __init__(self, *, count=2, **kwargs):
            super().__init__(count=count, **kwargs)
            template = self.rows["stock_basic", "SZSE"][0]
            self.rows["stock_basic", "SZSE"] = [{**template,
                "ts_code": f"{index + 1:06}.SZ", "symbol": f"{index + 1:06}"}
                for index in range(count)]
    # Supply complete synthetic original provider rows before custody. The old
    # fixture's count only grows SSE, so changing its read context is not proof.
    monkeypatch.setattr(fixtures, "CollectionOpener", FullMasterOpener)
    fixture = ready(storage=(model.git, store, local_fresh), count=2613)
    eligible = fixture["context"]["eligible_symbols"]
    protected = sorted([code for code in eligible if code.endswith(".SZ")][:3]
        + [code for code in eligible if code.endswith(".SH")][:3])
    additions = [code for code in protected if code not in PROTECTED]
    targets = [{"symbol": code, "adjust_kind": kind} for code in additions for kind in ("none", "forward")]
    six, six_root = expand(fixture, generation="manual-six-stock-fixture", targets=targets,
        source_budget=len(targets))
    assert len(protected) == 6 and len(six["units"]) == 12
    extra = next(code for code in eligible if code not in protected)
    final, root = expand(fixture, base=six_root["reference"], generation="manual-independent-one",
        symbol=extra, kinds=("none", "forward"))
    expected_refs = {encoded(unit["reference"]) for unit in final["units"].values()}
    assert len(expected_refs) == 14
    proof_refs = {encoded(ref) for ref in final["plan"]["adoption"]["capture_references"].values()}
    # Original qualification also retains the distinct repeat-response capture;
    # it remains provenance, not a fifteenth price owner or a new source attempt.
    expected_originals = expected_refs | proof_refs
    assert len(expected_originals) == 15
    assert len(final["plan"]["symbols"]) == 5226
    assert len(final["plan"]["eligible_symbols"]) == 5224
    assert len(final["plan"]["quarantined_symbols"]) == 2
    assert len(encoded(final)) < ft.MAX_ROOT_BYTES == 512 * 1024
    api, fetches, restored_prices = transport(model), [], set()
    reader = VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    original = ft.restore_single
    def remember(reference, *args, **kwargs):
        value = original(reference, *args, **kwargs)
        restored_prices.add(encoded(reference))
        return value
    monkeypatch.setattr(ft, "restore_single", remember)
    before = model.git.head()
    resolved = ft.resolve_ftshare_expansion_frontier(six_root["reference"], REGISTRY,
        reader, protected_symbols=protected)
    admission_requests = api._requests
    assert resolved["reference"] == root["reference"] and resolved["reason_code"] is None
    assert restored_prices == expected_originals
    assert len(fetches) == 1 and 0 < admission_requests < 240
    reading = read_ftshare_collection_version(resolved["reference"], REGISTRY, reader)
    assert reading["view"]["scope_count"] == 5226
    assert reading["ftshare_response_bodies_restored"] is True and reading["unavailable_queries"] == 0
    assert reading["source_requests"] == 0
    assert len(fetches) == 2 and api._requests == len(model.requests) < MAX_REQUESTS == 512
    assert all(call["api"] is api and not call["destination"].exists() for call in fetches)
    assert reader.backend._depth == 0 and reader.backend._reader is None
    assert model.git.head() == before and model.writes == 0
    record_property("native_admission_api_requests", admission_requests)
    record_property("native_complete_read_api_requests", api._requests)
    record_property("restored_original_price_versions", len(restored_prices))
    record_property("retained_price_owner_count", len(expected_refs))
    record_property("complete_collection_bytes", len(encoded(final)))
