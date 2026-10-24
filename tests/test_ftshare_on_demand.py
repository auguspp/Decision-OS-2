"""复用真实 Store/恢复链和既有合成来源 fixture；不访问供应商。"""
from copy import deepcopy
import socket

import pytest

from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application import ftshare_on_demand as od
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
from decision_kernel.domain.records import Conflict, RecordError, SourceRef, StoreUnavailable, UnknownWrite
from test_ftshare_collection import (
    REGISTRY, _new_calendars, _retain, plan_single, settle_single, single_collection_fixture,
)


STAMP = "2026-10-20T12:00:00+00:00"
LATER = "2026-10-21T12:00:00+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("按需计划测试禁止联网")
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)


def ready(**options):
    fixture = single_collection_fixture(count=5)
    single = fixture["single"]
    if options:
        single = plan_single(fixture, **options)
    root = _retain(single, fixture["store"], fixture["fresh"])
    _, root = settle_single(fixture, single, root)
    fixture["base"] = root["reference"]
    return fixture


def request(fixture, **options):
    return {"schema": od.REQUEST_SCHEMA, "id": "research-one", "base_reference": fixture["base"],
        "symbol": "600001.SH", "adjust_kind": "none", "reason": "补齐本次研究所需日线", **options}


def plan(fixture, value=None, **options):
    return od.plan_ftshare_on_demand(value if value is not None else request(fixture),
        REGISTRY, fixture["fresh"](), created_at=STAMP, **options)


def read_only(monkeypatch, fixture):
    def forbidden(*args, **kwargs):
        pytest.fail("计划或恢复不得写 store、保管来源或开始派发")
    monkeypatch.setattr(type(fixture["backend"]), "commit", forbidden)
    for name in ("retain_ftshare_collection", "retain_ftshare_single", "begin_ftshare_slot"):
        monkeypatch.setattr(ft, name, forbidden)


@pytest.mark.parametrize("kind", ["none", "forward"])
def test_new_plan_uses_only_existing_planner_one_target_and_frozen_frontier(monkeypatch, kind):
    fixture = ready(completion_policy=EOD_COMPLETION_POLICY)
    prior = ft.restore_ftshare_collection(fixture["base"], REGISTRY, fixture["fresh"]())["collection"]
    original = ft.plan_ftshare_single_collection
    calls = []
    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(ft, "plan_ftshare_single_collection", tracked)
    read_only(monkeypatch, fixture)
    before = fixture["backend"].head()
    value = request(fixture, adjust_kind=kind)
    untouched = deepcopy(value)
    result = plan(fixture, value)
    current, frozen = result["collection"], prior["plan"]
    assert result["status"] == "planned-ftshare-on-demand"
    assert result["reference"] is None and result["source_requests"] == 0
    assert len(calls) == 1 and calls[0][0][0] == fixture["base"]
    assert calls[0][1]["targets"] == [{"symbol": "600001.SH", "adjust_kind": kind}]
    assert calls[0][1]["source_budget"] == current["plan"]["source_budget"] == 1
    for field in ("start", "end", "calendar_end", "calendar_references", "master_references", "completion_policy"):
        assert current["plan"][field] == frozen[field]
    slot = kind + ":600001.SH"
    assert current["plan"]["new_slots"] == [slot]
    assert current["units"][slot] == {"status": "pending", "request_count": 0,
        "reference": None, "reason_code": None}
    assert current["reserved_requests"] == 0
    assert current["plan"]["prior_generation_reserved_requests"] == (
        frozen["prior_generation_reserved_requests"] + prior["reserved_requests"])
    for old_slot, unit in prior["units"].items():
        assert current["plan"]["requests"][old_slot] == frozen["requests"][old_slot]
        assert current["units"][old_slot] == {**unit, "request_count": 0}
    assert value == untouched and fixture["backend"].head() == before


def test_unretained_plan_has_stable_intent_generation_independent_of_clock_and_key_order():
    fixture = ready()
    value = request(fixture)
    first = plan(fixture, value)["collection"]
    second = od.plan_ftshare_on_demand(dict(reversed(list(value.items()))), REGISTRY,
        fixture["fresh"](), created_at=LATER)["collection"]
    assert first["plan"]["generation"] == second["plan"]["generation"]
    assert first["plan"]["created_at"] == STAMP and second["plan"]["created_at"] == LATER
    assert first["plan"]["generation"] != plan(fixture, request(fixture, id="another"))["collection"]["plan"]["generation"]


@pytest.mark.parametrize("state", ["pending", "in-flight", "failed", "complete", "held", "complete-held"])
def test_same_intent_restores_exact_plan_clock_reference_and_never_reopens(monkeypatch, state):
    fixture = ready()
    value = request(fixture)
    current = plan(fixture, value)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    slot = current["plan"]["new_slots"][0]
    if state == "in-flight":
        current = ft.begin_ftshare_slot(current, slot, at=STAMP)
        root = _retain(current, fixture["store"], fixture["fresh"], root["reference"])
    elif state not in ("pending", "in-flight"):
        current, root = settle_single(fixture, current, root,
            reason_code="FTSHARE_HTTP_503" if state == "failed" else
                "FTSHARE_HTTP_403" if state == "held" else None,
            source_hold="FTSHARE_HTTP_403" if state == "held" else
                "FTSHARE_HTTP_429" if state == "complete-held" else None)
    def forbidden(*args, **kwargs):
        pytest.fail("已有 intent 只恢复，不重新规划或要求当前写前沿")
    monkeypatch.setattr(ft, "plan_ftshare_single_collection", forbidden)
    monkeypatch.setattr(ft, "_single_write_frontier", forbidden)
    monkeypatch.setattr(ft, "_expansion_successors", forbidden)
    read_only(monkeypatch, fixture)
    before = fixture["backend"].head()
    result = od.plan_ftshare_on_demand(value, REGISTRY, fixture["fresh"](), created_at=LATER)
    assert result == {"status": "restored-ftshare-on-demand", "collection": current,
        "reference": root["reference"], "source_requests": 0}
    assert result["collection"]["plan"]["created_at"] == STAMP
    assert fixture["backend"].head() == before


def test_reentry_after_later_generation_restores_original_not_latest_frontier(monkeypatch):
    fixture = ready()
    value = request(fixture)
    current = plan(fixture, value)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    current, root = settle_single(fixture, current, root)
    later = plan(fixture, request(fixture, id="two", symbol="600002.SH"))["collection"]
    assert later["plan"]["base_reference"] == root["reference"]
    later_root = _retain(later, fixture["store"], fixture["fresh"])
    settle_single(fixture, later, later_root, reason_code="FTSHARE_HTTP_503")
    read_only(monkeypatch, fixture)
    result = plan(fixture, value)
    assert result["reference"] == root["reference"] and result["collection"] == current


@pytest.mark.parametrize("change", ["reason", "symbol", "adjust_kind", "base_reference"])
def test_same_id_changed_content_conflicts_even_while_existing_pending(monkeypatch, change):
    fixture = ready()
    value = request(fixture)
    current = plan(fixture, value)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    changed = {**value, change: {"reason": "另一个用途", "symbol": "600002.SH",
        "adjust_kind": "forward", "base_reference": fixture["adopted"]["reference"]}[change]}
    read_only(monkeypatch, fixture)
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="^FTSHARE_ON_DEMAND_INTENT_CONFLICT$"):
        plan(fixture, changed)
    assert ft.latest_ftshare_reference(fixture["fresh"](), REGISTRY,
        collection_id=current["plan"]["collection_id"], generation=current["plan"]["generation"]) == root["reference"]
    assert fixture["backend"].head() == before


@pytest.mark.parametrize("state,reason", [
    ("pending", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("in-flight", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("failed", "FTSHARE_EXPANSION_FAILED"),
    ("held", "FTSHARE_COLLECTION_SOURCE_HOLD"),
])
def test_another_intent_cannot_bypass_unfinished_or_failed_frontier(monkeypatch, state, reason):
    fixture = ready()
    current = plan(fixture)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    if state == "in-flight":
        current = ft.begin_ftshare_slot(current, current["plan"]["new_slots"][0], at=STAMP)
        _retain(current, fixture["store"], fixture["fresh"], root["reference"])
    elif state in ("failed", "held"):
        settle_single(fixture, current, root, reason_code="FTSHARE_HTTP_503" if state == "failed" else "FTSHARE_HTTP_403",
            source_hold="FTSHARE_HTTP_403" if state == "held" else None)
    read_only(monkeypatch, fixture)
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture, request(fixture, id="different", symbol="600002.SH"))


@pytest.mark.parametrize("state,reason", [
    ("pending", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("in-flight", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("failed", "FTSHARE_EXPANSION_FAILED"),
    ("held", "FTSHARE_COLLECTION_SOURCE_HOLD"),
])
def test_explicit_base_itself_must_be_fully_successful(state, reason):
    fixture = single_collection_fixture(count=5)
    current = fixture["single"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    if state == "in-flight":
        current = ft.begin_ftshare_slot(current, current["plan"]["new_slots"][0], at=current["updated_at"])
        root = _retain(current, fixture["store"], fixture["fresh"], root["reference"])
    elif state in ("failed", "held"):
        _, root = settle_single(fixture, current, root,
            reason_code="FTSHARE_HTTP_503" if state == "failed" else "FTSHARE_HTTP_403",
            source_hold="FTSHARE_HTTP_403" if state == "held" else None)
    fixture["base"] = root["reference"]
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture)


@pytest.mark.parametrize("symbol,kind,reason", [
    ("600000.SH", "none", "FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED"),
    ("600000.SH", "forward", "FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED"),
    ("600004.SH", "none", "FTSHARE_SINGLE_TARGET_OUTSIDE_ELIGIBLE_SCOPE"),
    ("699999.SH", "forward", "FTSHARE_SINGLE_TARGET_OUTSIDE_ELIGIBLE_SCOPE"),
])
def test_duplicate_quarantined_and_outside_scope_are_rejected(symbol, kind, reason):
    fixture = ready()
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture, request(fixture, symbol=symbol, adjust_kind=kind))


@pytest.mark.parametrize("protection", [["600001.SH"], ("600001.SH",)])
def test_protected_symbols_is_caller_bound_and_conservatively_blocks_both_adjustments(protection):
    fixture = ready()
    for kind in ("none", "forward"):
        with pytest.raises(RecordError, match="^FTSHARE_EXPANSION_PROTECTED_SYMBOL$"):
            plan(fixture, request(fixture, adjust_kind=kind), protected_symbols=protection)
    assert plan(fixture, protected_symbols=["000001.SZ", "600000.SH"])["source_requests"] == 0
    # 没有内置 daily 配置；采用根已用 none，仍可补尚未存在的 forward。
    adopted = single_collection_fixture(count=5)
    adopted["base"] = adopted["adopted"]["reference"]
    assert plan(adopted, request(adopted, symbol="600000.SH", adjust_kind="forward"))["source_requests"] == 0


@pytest.mark.parametrize("protection", [None, "600001.SH", {"600001.SH"}, ["bad"], [3],
    ["600001.SH", "600001.SH"], ["600001.SH"] * (ft.MAX_REQUESTS + 1)])
def test_invalid_protected_symbols(protection):
    fixture = ready()
    with pytest.raises(RecordError, match="^FTSHARE_EXPANSION_PROTECTED_SYMBOLS$"):
        plan(fixture, protected_symbols=protection)


@pytest.mark.parametrize("extra", ["source_budget", "start", "end", "calendar_references",
    "master_references", "completion_policy", "generation", "run_id", "created_at", "targets",
    "protected_symbols", "eligible_symbols", "source_requests", "calendar_end"])
def test_exact_request_keys_refuse_caller_budget_dates_or_execution_identity(extra):
    fixture = ready()
    with pytest.raises(RecordError, match="^FTSHARE_ON_DEMAND_REQUEST$"):
        plan(fixture, request(fixture, **{extra: 1}))


@pytest.mark.parametrize("missing", ["schema", "id", "base_reference", "symbol", "adjust_kind", "reason"])
def test_missing_request_keys(missing):
    fixture = ready()
    value = request(fixture)
    del value[missing]
    with pytest.raises(RecordError, match="^FTSHARE_ON_DEMAND_REQUEST$"):
        plan(fixture, value)


@pytest.mark.parametrize("change,reason", [
    ({"schema": "unknown"}, "FTSHARE_ON_DEMAND_REQUEST"),
    ({"id": ""}, "INVALID_ID"), ({"id": 1}, "INVALID_ID"),
    ({"reason": " "}, "INVALID_TEXT"), ({"reason": None}, "INVALID_TEXT"),
    ({"reason": "x" * 1025}, "INVALID_TEXT"), ({"reason": "a\x00b"}, "INVALID_TEXT"),
    ({"symbol": ["600001.SH"]}, "FTSHARE_SINGLE_TARGETS"),
    ({"symbol": "600001"}, "FTSHARE_SINGLE_TARGETS"),
    ({"adjust_kind": "backward"}, "FTSHARE_SINGLE_TARGETS"),
    ({"adjust_kind": ["none", "forward"]}, "FTSHARE_SINGLE_TARGETS"),
    ({"base_reference": {}}, "INVALID_SOURCE_REF"),
])
def test_invalid_request_values(change, reason):
    fixture = ready()
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture, request(fixture, **change))


@pytest.mark.parametrize("value", [None, [], "request"])
def test_non_dict_request(value):
    with pytest.raises(RecordError, match="^FTSHARE_ON_DEMAND_REQUEST$"):
        od.plan_ftshare_on_demand(value, REGISTRY, None, created_at=STAMP)


def test_reference_must_be_exact_local_and_original():
    fixture = ready()
    for mutation, reason in [
        (lambda ref: ref.update(selection={"kind": "lines", "start": 1, "end": 2}),
            "FTSHARE_COLLECTION_EXACT_LOCAL_REFERENCE"),
        (lambda ref: ref["manifest"].update(repository="foreign/repository"),
            "FTSHARE_COLLECTION_EXACT_LOCAL_REFERENCE"),
        (lambda ref: ref["manifest"].update(sha256="0" * 64), "DIGEST_MISMATCH"),
    ]:
        reference = deepcopy(fixture["base"])
        mutation(reference)
        with pytest.raises(RecordError, match=reason):
            plan(fixture, request(fixture, base_reference=reference))


def test_source_missing_from_successful_predecessor_is_not_accepted():
    fixture = ready()
    collection = ft.restore_ftshare_collection(fixture["base"], REGISTRY, fixture["fresh"]())["collection"]
    ref = SourceRef.from_dict(collection["units"]["forward:600000.SH"]["reference"])
    fixture["backend"].shared["missing"].add((ref.manifest.commit, ref.manifest.path))
    with pytest.raises(RecordError):
        plan(fixture)


def test_old_revision_of_base_is_not_upgraded_to_its_new_tip():
    fixture = single_collection_fixture(count=5)
    root = _retain(fixture["single"], fixture["store"], fixture["fresh"])
    fixture["base"] = root["reference"]
    settle_single(fixture, fixture["single"], root)
    with pytest.raises(RecordError, match="^FTSHARE_SINGLE_STALE_BASE$"):
        plan(fixture)


def test_competing_original_genesis_children_refuse_new_intent():
    fixture = ready()
    first = plan_single(fixture, base=fixture["base"], generation="competing-one",
        targets=[{"symbol": "600002.SH", "adjust_kind": "none"}])
    second = plan_single(fixture, base=fixture["base"], generation="competing-two",
        targets=[{"symbol": "600003.SH", "adjust_kind": "none"}])
    _retain(first, fixture["store"], fixture["fresh"])
    # 与原有前沿测试相同，插入规范来源作为分叉损坏证据。
    document = {"schema": ft.SCHEMA, "registry": deepcopy(REGISTRY.snapshot),
        "predecessor": deepcopy(fixture["base"]), "collection": second}
    fixture["store"].put_source("synthetic-conflicting-genesis", ft._source(document))
    with pytest.raises(RecordError, match="^FTSHARE_EXPANSION_FORK$"):
        plan(fixture)


@pytest.mark.parametrize("barrier", ["before-put", "before-commit"])
def test_checked_snapshot_refuses_competing_reason_without_extra_sources_or_fork(monkeypatch, barrier):
    fixture = ready()
    value = request(fixture, reason="用途 A")
    current = plan(fixture, value)["collection"]
    competing = plan(fixture, request(fixture, reason="用途 B"))["collection"]
    assert current["plan"]["generation"] != competing["plan"]["generation"]
    snapshot = fixture["backend"].head()
    peer_store = fixture["fresh"]()
    original = fixture["store"].put_source if barrier == "before-put" else fixture["backend"].commit
    saved = {}

    def write_after_peer(*args, **kwargs):
        # 确定性 barrier：A 已完成 tip/frontier 核验，B 完整落库后才放行 A。
        assert not saved and fixture["backend"].head() == snapshot
        root = _retain(competing, peer_store, fixture["fresh"])
        _, root = settle_single({**fixture, "store": peer_store}, competing, root)
        saved.update(reference=root["reference"], shared=deepcopy(fixture["backend"].shared))
        return original(*args, **kwargs)

    target = fixture["store"] if barrier == "before-put" else fixture["backend"]
    name = "put_source" if barrier == "before-put" else "commit"
    # B 使用独立写实例，避免 barrier 截获 B 自己的提交。
    monkeypatch.setattr(target, name, write_after_peer)
    with pytest.raises(Conflict, match="^HEAD_CHANGED$"):
        _retain(current, fixture["store"], fixture["fresh"])
    assert saved and fixture["backend"].shared == saved["shared"]
    frontier = ft.resolve_ftshare_expansion_frontier(fixture["base"], REGISTRY, fixture["fresh"]())
    assert frontier["reference"] == saved["reference"] and frontier["reason_code"] is None
    assert len(frontier["successor_references"]) == 1 and frontier["source_requests"] == 0
    assert ft.latest_ftshare_reference(fixture["fresh"](), REGISTRY,
        collection_id=current["plan"]["collection_id"], generation=current["plan"]["generation"]) is None


def test_same_intent_at_write_barrier_reconciles_original_reference_after_peer_completion(monkeypatch):
    fixture = ready()
    current = plan(fixture)["collection"]
    snapshot = fixture["backend"].head()
    original = fixture["store"].put_source
    peer_store = fixture["fresh"]()
    saved = {}

    def write_after_peer(*args, **kwargs):
        assert not saved and fixture["backend"].head() == snapshot
        root = _retain(current, peer_store, fixture["fresh"])
        saved["reference"] = root["reference"]
        settle_single({**fixture, "store": peer_store}, current, root)
        saved["shared"] = deepcopy(fixture["backend"].shared)
        return original(*args, **kwargs)

    monkeypatch.setattr(fixture["store"], "put_source", write_after_peer)
    replay = _retain(current, fixture["store"], fixture["fresh"])
    assert replay["reference"] == saved["reference"]
    assert replay["restored"]["collection"] == current
    assert fixture["backend"].shared == saved["shared"]


@pytest.mark.parametrize("replay_stage", ["genesis", "reservation"])
def test_unknown_control_replay_precedes_new_tip_and_frontier_checks(monkeypatch, replay_stage):
    fixture = ready()
    current = plan(fixture)["collection"]
    original = fixture["backend"].commit

    def unknown(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_RESPONSE_LOST")

    monkeypatch.setattr(fixture["backend"], "commit", unknown)
    with pytest.raises(UnknownWrite, match="^SYNTHETIC_RESPONSE_LOST$"):
        _retain(current, fixture["store"], fixture["fresh"])
    monkeypatch.setattr(fixture["backend"], "commit", original)
    root = _retain(current, fixture["store"], fixture["fresh"])
    _, final = settle_single(fixture, current, root)
    replay_state, replay_ref, predecessor = current, root["reference"], None
    if replay_stage == "reservation":
        replay_state = ft.begin_ftshare_slot(current, current["plan"]["new_slots"][0], at=STAMP)
        predecessor = root["reference"]
        replay_ref = _retain(replay_state, fixture["store"], fixture["fresh"], predecessor)["reference"]
    later = plan(fixture, request(fixture, id="later", symbol="600002.SH"))["collection"]
    _retain(later, fixture["store"], fixture["fresh"])
    before = deepcopy(fixture["backend"].shared)

    def forbidden(*args, **kwargs):
        pytest.fail("已存 journal 意图必须先于当前 tip/frontier 对账，且不得再写")

    monkeypatch.setattr(ft, "_tip", forbidden)
    monkeypatch.setattr(ft, "_single_write_frontier", forbidden)
    monkeypatch.setattr(fixture["backend"], "commit", forbidden)
    replay = _retain(replay_state, fixture["store"], fixture["fresh"], predecessor)
    assert replay_ref != final["reference"]
    assert replay["reference"] == replay_ref and replay["restored"]["collection"] == replay_state
    assert fixture["backend"].shared == before


def test_semantically_equivalent_exact_reference_restores_same_intent():
    fixture = ready()
    value = request(fixture)
    current = plan(fixture, value)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    equivalent = deepcopy(value)
    equivalent["base_reference"].pop("selection")
    restored = plan(fixture, equivalent)
    assert restored["reference"] == root["reference"] and restored["collection"] == current


def test_retained_success_is_not_returned_when_its_original_capture_is_missing():
    fixture = ready()
    current = plan(fixture)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    current, _ = settle_single(fixture, current, root)
    ref = SourceRef.from_dict(current["units"][current["plan"]["new_slots"][0]]["reference"])
    fixture["backend"].shared["missing"].add((ref.manifest.commit, ref.manifest.path))
    with pytest.raises(RecordError):
        plan(fixture)


def test_read_failure_does_not_fall_back_to_new_plan(monkeypatch):
    fixture = ready()
    def unavailable(*args, **kwargs):
        raise StoreUnavailable("SYNTHETIC_READ_DENIED")
    monkeypatch.setattr(ft, "latest_ftshare_reference", unavailable)
    read_only(monkeypatch, fixture)
    with pytest.raises(StoreUnavailable, match="SYNTHETIC_READ_DENIED"):
        plan(fixture)


@pytest.mark.parametrize("change,reason", [
    ("calendar", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("window", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("master", "FTSHARE_EXPANSION_MASTER_CHANGED"),
    ("completion", "FTSHARE_EXPANSION_COMPLETION_CHANGED"),
    ("daily", "FTSHARE_EXPANSION_DAILY_GENERATION"),
    ("protected", "FTSHARE_EXPANSION_PROTECTED_SYMBOL"),
])
def test_changed_or_protected_successor_is_not_silently_used_as_expansion_frontier(change, reason):
    fixture = ready()
    options = {"generation": "manual-existing", "created_at": STAMP,
        "targets": [{"symbol": "600002.SH", "adjust_kind": "none"}]}
    if change in ("calendar", "window"):
        options["calendar_references"] = _new_calendars(fixture["store"], fixture["fresh"],
            through="20261019" if change == "window" else "20261017")
    if change == "window":
        options["end"] = "20261019"
    elif change == "master":
        from test_ftshare_master_adoption import new_masters
        options["master_references"], _ = new_masters(fixture)
    elif change == "completion":
        options["completion_policy"] = EOD_COMPLETION_POLICY
    elif change == "daily":
        options["generation"] = "daily-20261019"
    current = plan_single(fixture, base=fixture["base"], **options)
    root = _retain(current, fixture["store"], fixture["fresh"])
    settle_single(fixture, current, root)
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture, protected_symbols=["600002.SH"] if change == "protected" else [])
    assert fixture["backend"].head() == before


def test_current_anchor_freezes_its_own_new_calendar_and_completion_not_original_adoption():
    fixture = single_collection_fixture(count=5)
    calendars = _new_calendars(fixture["store"], fixture["fresh"], through="20261019")
    current = plan_single(fixture, end="20261019", calendar_references=calendars,
        completion_policy=EOD_COMPLETION_POLICY, created_at=STAMP)
    root = _retain(current, fixture["store"], fixture["fresh"])
    current, root = settle_single(fixture, current, root)
    fixture["base"] = root["reference"]
    planned = plan(fixture)["collection"]["plan"]
    assert planned["base_reference"] == root["reference"]
    for field in ("start", "end", "calendar_end", "calendar_references", "master_references", "completion_policy"):
        assert planned[field] == current["plan"][field]


def test_new_intent_cannot_repeat_a_slot_added_by_existing_completed_successor():
    fixture = ready()
    current = plan(fixture)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    settle_single(fixture, current, root)
    with pytest.raises(RecordError, match="^FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED$"):
        plan(fixture, request(fixture, id="new-name"))


def test_existing_intent_is_also_refused_when_its_symbol_becomes_protected():
    fixture = ready()
    current = plan(fixture)["collection"]
    _retain(current, fixture["store"], fixture["fresh"])
    with pytest.raises(RecordError, match="^FTSHARE_EXPANSION_PROTECTED_SYMBOL$"):
        plan(fixture, protected_symbols=["600001.SH"])


@pytest.mark.parametrize("mutation,reason", [
    ("target", "FTSHARE_ON_DEMAND_INTENT_CONFLICT"),
    ("budget", "FTSHARE_ON_DEMAND_INTENT_CONFLICT"),
    ("window", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("calendar", "FTSHARE_EXPANSION_WINDOW_CHANGED"),
    ("master", "FTSHARE_EXPANSION_MASTER_CHANGED"),
    ("completion", "FTSHARE_EXPANSION_COMPLETION_CHANGED"),
])
def test_generation_name_alone_cannot_replace_original_request_semantics(mutation, reason):
    fixture = ready()
    generation = plan(fixture)["collection"]["plan"]["generation"]
    options = {"generation": generation, "created_at": STAMP,
        "targets": [{"symbol": "600002.SH" if mutation == "target" else "600001.SH", "adjust_kind": "none"}]}
    if mutation == "window":
        options.update(end="20261019",
            calendar_references=_new_calendars(fixture["store"], fixture["fresh"], through="20261019"))
    elif mutation == "calendar":
        options["calendar_references"] = _new_calendars(fixture["store"], fixture["fresh"])
    elif mutation == "master":
        from test_ftshare_master_adoption import new_masters
        options["master_references"], _ = new_masters(fixture)
    elif mutation == "completion":
        options["completion_policy"] = EOD_COMPLETION_POLICY
    elif mutation == "budget":
        options["targets"].append({"symbol": "600002.SH", "adjust_kind": "forward"})
        options["source_budget"] = 2
    substituted = plan_single(fixture, base=fixture["base"], **options)
    _retain(substituted, fixture["store"], fixture["fresh"])
    with pytest.raises(RecordError, match="^" + reason + "$"):
        plan(fixture)


def test_new_plan_clock_and_registry_keep_existing_validation():
    fixture = ready()
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_CLOCK"):
        od.plan_ftshare_on_demand(request(fixture), REGISTRY, fixture["fresh"](), created_at="invalid")
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_CLOCK"):
        od.plan_ftshare_on_demand(request(fixture), REGISTRY, fixture["fresh"](),
            created_at="2026-10-01T12:00:00+00:00")
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_REGISTRY_REQUIRED"):
        od.plan_ftshare_on_demand(request(fixture), None, fixture["fresh"](), created_at=STAMP)


@pytest.mark.parametrize("kind", ["none", "forward"])
def test_shenzhen_target_has_one_slot_and_preserves_frozen_eligibility(monkeypatch, kind):
    from test_ftshare_master_adoption import new_masters
    fixture = single_collection_fixture(count=5)
    def add_target(rows):
        rows["stock_basic", "SZSE"].append({**rows["stock_basic", "SZSE"][0],
            "ts_code": "000002.SZ", "symbol": "000002"})
    masters, _ = new_masters(fixture, edit=add_target)
    current = plan_single(fixture, master_references=masters, created_at=STAMP)
    root = _retain(current, fixture["store"], fixture["fresh"])
    _, root = settle_single(fixture, current, root)
    fixture["base"] = root["reference"]
    prior = ft.restore_ftshare_collection(fixture["base"], REGISTRY, fixture["fresh"]())["collection"]
    assert "000002.SZ" in prior["plan"]["eligible_symbols"]
    value = request(fixture, symbol="000002.SZ", adjust_kind=kind)
    untouched = deepcopy(value)
    read_only(monkeypatch, fixture)
    before = deepcopy(fixture["backend"].shared)
    result = plan(fixture, value, protected_symbols=["600000.SH"])
    current = result["collection"]
    assert set(result) == {"status", "collection", "reference", "source_requests"}
    assert result["reference"] is None and result["source_requests"] == 0
    assert current["plan"]["targets"] == [{"symbol": "000002.SZ", "adjust_kind": kind}]
    assert current["plan"]["new_slots"] == [kind + ":000002.SZ"]
    assert current["plan"]["source_budget"] == 1 and current["reserved_requests"] == 0
    for field in ("symbols", "eligible_symbols", "quarantined_symbols", "sessions"):
        assert current["plan"][field] == prior["plan"][field]
    assert value == untouched and fixture["backend"].shared == before


@pytest.mark.parametrize("state,reason", [
    ("pending", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("in-flight", "FTSHARE_SINGLE_BASE_NOT_TERMINAL"),
    ("failed", "FTSHARE_EXPANSION_FAILED"),
    ("complete-held", "FTSHARE_COLLECTION_SOURCE_HOLD"),
    ("complete", None),
])
def test_every_predecessor_slot_must_succeed_without_hold(monkeypatch, state, reason):
    fixture = single_collection_fixture(count=5)
    current = plan_single(fixture, source_budget=2, targets=[
        {"symbol": "600002.SH", "adjust_kind": "none"},
        {"symbol": "600003.SH", "adjust_kind": "forward"}])
    root = _retain(current, fixture["store"], fixture["fresh"])
    current, root = settle_single(fixture, current, root, current["plan"]["new_slots"][0])
    remaining = current["plan"]["new_slots"][1]
    if state == "in-flight":
        current = ft.begin_ftshare_slot(current, remaining, at=current["updated_at"])
        root = _retain(current, fixture["store"], fixture["fresh"], root["reference"])
    elif state in ("failed", "complete-held", "complete"):
        current, root = settle_single(fixture, current, root, remaining,
            reason_code="FTSHARE_HTTP_503" if state == "failed" else None,
            source_hold="FTSHARE_HTTP_429" if state == "complete-held" else None)
    fixture["base"] = root["reference"]
    read_only(monkeypatch, fixture)
    before = deepcopy(fixture["backend"].shared)
    if reason is not None:
        with pytest.raises(RecordError, match="^" + reason + "$"):
            plan(fixture)
    else:
        planned = plan(fixture)["collection"]["plan"]
        assert planned["base_reference"] == root["reference"]
        assert planned["source_budget"] == 1
    assert fixture["backend"].shared == before


@pytest.mark.parametrize("clock", [LATER, "invalid", None])
def test_restore_keeps_original_clock_and_accepts_canonical_reference_forms(monkeypatch, clock):
    fixture = ready(completion_policy=EOD_COMPLETION_POLICY)
    value = request(fixture)
    current = plan(fixture, value)["collection"]
    root = _retain(current, fixture["store"], fixture["fresh"])
    equivalent = deepcopy(value)
    equivalent["base_reference"] = SourceRef.from_dict(value["base_reference"])
    read_only(monkeypatch, fixture)
    before = deepcopy(fixture["backend"].shared)
    result = od.plan_ftshare_on_demand(equivalent, REGISTRY, fixture["fresh"](), created_at=clock)
    assert result == {"status": "restored-ftshare-on-demand", "collection": current,
        "reference": root["reference"], "source_requests": 0}
    assert result["collection"]["plan"]["created_at"] == STAMP
    assert result["collection"]["plan"]["completion_policy"] == EOD_COMPLETION_POLICY
    assert fixture["backend"].shared == before


@pytest.mark.parametrize("field", ["schema", "symbol", "reason"])
def test_invalid_request_never_reads_store(field):
    value = {"schema": od.REQUEST_SCHEMA, "id": "invalid-input", "base_reference": {},
        "symbol": "600001.SH", "adjust_kind": "none", "reason": "合成测试"}
    value[field] = None
    with pytest.raises(RecordError):
        od.plan_ftshare_on_demand(value, REGISTRY, None, created_at=STAMP)
