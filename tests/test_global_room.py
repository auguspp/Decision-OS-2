"""Two exact global roots through the existing room; synthetic records only."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from decision_kernel.application import reading_global
from decision_kernel.application.market_custody import read_stored_market, retain_market_reading
from decision_kernel.application.reading_market import open_market
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MissingObject, RecordError, RevisionInput, SourceInput, SourceRef, StoreUnavailable, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH
from test_us_index_custody import payload as us_payload
from test_shibor_context import ROWS, captured, payload as shibor_payload

CLOCK = "2026-10-20T12:00:00+08:00"


def global_inputs():
    us = us_payload()
    us["horizons"] = [1, 3]  # The existing short synthetic calendar supports these exact windows.
    return {"us_indices": us, "shibor": shibor_payload()}


def retain_global(store, fresh, payloads=None):
    return {name: json.loads(encoded(retain_market_reading(value, REGISTRY, store,
        fresh_reader=fresh, public_rights_confirmed=True)["reference"]))
        for name, value in (global_inputs() if payloads is None else payloads).items()}


def room_plan(refs, **changes):
    return {"as_of": CLOCK, "global_context": {
        name: {"reference": reference, **changes.get(name, {})} for name, reference in refs.items()}}


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / "records.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    refs = retain_global(store, fresh)
    return store, fresh, refs


def build(saved, **changes):
    store, _, refs = saved
    return ReadingRoom(store, market_registry=REGISTRY).build(room_plan(refs, **changes))


def source_query(store, reference, *, code=None, api=None):
    root = json.loads(store.read_source(SourceRef.from_dict(reference))["data"])
    for ref in root["queries"].values():
        query = json.loads(store.read_source(SourceRef.from_dict(ref))["data"])
        if (api is None or query["use"]["api"] == api) and query["use"]["parameters"].get("ts_code") == code:
            return SourceRef.from_dict(ref), query
    raise AssertionError("synthetic query not found")


def test_complete_original_models_and_independent_clocks_survive_room(saved):
    store, fresh, refs = saved
    before = store.backend.head()
    room = build(saved, us_indices={"limit": 1}, shibor={"entity_id": "3m", "limit": 1})
    context = room["global_context"]
    assert context["status"] == room["status"] == "readable"
    for name, count in (("us_indices", 4), ("shibor", 8)):
        family = context["families"][name]
        expected = read_stored_market(refs[name], REGISTRY, fresh(), limit=100)
        assert encoded(family["view"]) == encoded(expected)
        assert family["view"]["view"]["scope_count"] == count
        assert len(family["view"]["view"]["entries"]) == count
        assert family["page"]["returned"] == 1
        assert family["view"]["view"]["as_of"] == "2026-10-16" != CLOCK[:10]
        assert family["open_request"] == {"operation": "read-source", "payload": refs[name]}
    assert context["families"]["us_indices"]["view"]["view"]["currency"] is None
    assert context["families"]["shibor"]["view"]["view"]["currency"] == "CNY"
    assert context["source_requests"] == context["writes"] == 0
    assert store.backend.head() == before


def test_paging_filtering_and_new_versions_leave_whole_models_fixed(saved):
    store, fresh, refs = saved
    first = build(saved, us_indices={"limit": 1}, shibor={"limit": 1})
    payloads = global_inputs(); payloads["us_indices"]["horizons"] = [1]
    newer = retain_global(store, fresh, payloads)
    assert newer["us_indices"] != refs["us_indices"]
    second_plan = room_plan(refs)
    for name, part in first["global_context"]["families"].items():
        second_plan["global_context"][name] = part["next_plan"]
    second = ReadingRoom(fresh(), market_registry=REGISTRY).build(second_plan)
    filtered = build(saved, us_indices={"entity_id": "SPX"}, shibor={"entity_id": "3m"})
    for name, entity in (("us_indices", "SPX"), ("shibor", "3m")):
        a, b, c = [model["global_context"]["families"][name] for model in (first, second, filtered)]
        assert a["view"] == b["view"] == c["view"]
        assert a["page"]["entries"][0]["entity_id"] != b["page"]["entries"][0]["entity_id"]
        assert [entry["entity_id"] for entry in c["page"]["entries"]] == [entity]
        assert c["page"]["matched_count"] == 1 and c["next_plan"] is None
        assert a["next_plan"]["reference"] == refs[name]


def test_unselected_index_gap_and_shibor_cell_gap_remain_in_whole_scope(saved):
    store, fresh, _ = saved
    payloads = global_inputs()
    payloads["us_indices"]["index_results"].pop("DJI")
    rows = deepcopy(ROWS); rows[-1][1] = None
    payloads["shibor"] = shibor_payload(captured(rows))
    refs = retain_global(store, fresh, payloads)
    room = build((store, fresh, refs), us_indices={"entity_id": "SPX"}, shibor={"entity_id": "3m"})
    us, shibor = [room["global_context"]["families"][name] for name in ("us_indices", "shibor")]
    assert us["page"]["entries"][0]["observation"]["status"] == "observed"
    assert shibor["page"]["entries"][0]["end_quote"]["status"] == "observed"
    assert us["partial"] and shibor["partial"] and room["status"] == "partial"
    assert us["view"]["view"]["scope_count"] == 4
    assert us["view"]["view"]["source_qualification_counts"]["not-supplied"] == 1
    assert shibor["view"]["view"]["end_quote_counts"]["missing-value"] == 1
    assert shibor["view"]["view"]["scope_count"] == 8


@pytest.mark.parametrize("missing", ["index-page", "calendar-query", "us-root"])
def test_exact_missing_dependency_stays_local_to_its_family(saved, monkeypatch, missing):
    store, _, refs = saved
    if missing == "us-root":
        bad = SourceRef.from_dict(refs["us_indices"]).manifest
    else:
        query_ref, query = source_query(store, refs["us_indices"], code="SPX" if missing == "index-page" else None)
        bad = (SourceRef.from_dict(query["pages"][0]["reference"]).manifest
               if missing == "index-page" else query_ref.manifest)
    original = store.backend.resolve
    def read(reference):
        if reference == bad:
            raise MissingObject("SYNTHETIC_EXACT_SOURCE_MISSING")
        return original(reference)
    monkeypatch.setattr(store.backend, "resolve", read)
    room = build(saved)
    us, shibor = [room["global_context"]["families"][name] for name in ("us_indices", "shibor")]
    assert room["status"] == "partial" and shibor["status"] == "readable" and not shibor["partial"]
    if missing == "us-root":
        assert us["status"] == "missing" and "view" not in us
    elif missing == "calendar-query":
        assert us["view"]["view"] is None and us["view"]["unavailable_queries"] == 1
    else:
        assert us["view"]["view"]["scope_count"] == 4 and us["view"]["unavailable_queries"] == 1
        assert us["view"]["view"]["source_qualification_counts"] == {
            "checked": 3, "checked-empty": 0, "rejected": 1, "not-supplied": 0}


def test_signed_zero_shibor_different_source_date_and_missing_baseline_remain_explicit(saved):
    from test_us_index_context import CALENDAR, index_rows, source
    store, fresh, _ = saved
    us = us_payload(); us.update(end="20261015", horizons=[1, 2])
    us["calendar_result"] = source("us_tradecal", CALENDAR[:-1], params={"end_date": "20261015"})
    us["index_results"] = {code: source("index_global", index_rows(code)[:-1], code=code,
        params={"end_date": "20261015"}) for code in us["declared_indices"]}
    rows = deepcopy(ROWS); rows[0][1] = "0"; rows[-1][1] = "-0.25"
    shibor = shibor_payload(captured(rows), baseline_dates=["20261012", "20261014"])
    refs = retain_global(store, fresh, {"us_indices": us, "shibor": shibor})
    room = build((store, fresh, refs))
    a, b = [room["global_context"]["families"][name]["view"]["view"] for name in ("us_indices", "shibor")]
    assert a["as_of"] == "2026-10-15" and b["as_of"] == "2026-10-16"
    assert a["unit"] == "index-points" and a["currency"] is None
    assert b["entries"][0]["end_quote"]["rate_pct"] == "-0.25"
    assert b["entries"][0]["comparisons"][0]["baseline"]["rate_pct"] == "0"
    assert b["entries"][0]["comparisons"][0]["change_bp"] == "-25.00"
    assert b["comparison_counts"]["2026-10-14"]["missing-quote"] == 8
    assert b["entries"][0]["comparisons"][1]["baseline"]["status"] == "date-not-returned"
    assert room["global_context"]["families"]["shibor"]["partial"]


def test_plan_rejection_and_source_permission_precede_all_io(saved, monkeypatch):
    store, _, refs = saved
    monkeypatch.setattr(store.backend, "head", lambda: pytest.fail("invalid plan/permission reached store"))
    plans = [{}, {"unknown-family": {"reference": refs["us_indices"]}}, {"us_indices": None}]
    for changes in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 51}, {"limit": True},
                    {"entity_id": "HSI"}, {"entity_id": []}, {"registry": "caller-path"}, {"url": "https://invalid"}):
        plans.append({"us_indices": {"reference": refs["us_indices"], **changes}})
    plans.append({"shibor": {"reference": refs["shibor"], "entity_id": "2y"}})
    selected = deepcopy(refs["us_indices"]); selected["selection"] = dict(kind="lines", start=1, end=1, columns=[])
    plans.append({"us_indices": {"reference": selected}})
    for value in plans:
        with pytest.raises(RecordError):
            ReadingRoom(store).build({"as_of": CLOCK, "global_context": value})
    for permission in (None, False, "true", 1):
        with pytest.raises(RecordError, match="PUBLIC_GLOBAL"):
            reading_commands.perform(store, {"plan": room_plan(refs),
                "public_research_custody_confirmed": True, "public_sources_custody_confirmed": permission})
        with pytest.raises(RecordError, match="PUBLIC_GLOBAL"):
            ReadingRoom(store).build(room_plan(refs), market_export=permission)


def test_wrong_family_and_count_receipt_stop_before_queries_and_ashare_guard_survives(saved, monkeypatch):
    store, _, refs = saved
    counts = store.put_source("global-counts-only", SourceInput("global-counts-only", "content",
        "Synthetic count receipt", "synthetic:global-counts", '{"source_refs_created":false,"observed_count":4}'))
    original, calls = VersionedStore.read_source, []
    def read(owner, ref):
        calls.append(ref)
        return original(owner, ref)
    monkeypatch.setattr(VersionedStore, "read_source", read)
    for name, reference in (("us_indices", refs["shibor"]), ("shibor", refs["us_indices"]),
                            ("fx", refs["us_indices"]),
                            ("us_indices", asdict(counts))):
        calls.clear()
        result = reading_global.open_global(store, {name: {"reference": reference}}, registry=REGISTRY)
        assert result["families"][name]["status"] == "invalid" and len(calls) == 1
    for reference in refs.values():
        calls.clear()
        with pytest.raises(RecordError, match="ASHARE_ROOT_REQUIRED"):
            open_market(store, {"reference": reference}, registry=REGISTRY)
        assert len(calls) == 1


def test_registry_drift_is_visible_and_no_global_plan_adds_no_source_work(saved, monkeypatch):
    store, _, refs = saved
    changed = deepcopy(REGISTRY); changed.snapshot["sha256"] = "0" * 64
    result = ReadingRoom(store, market_registry=changed).build(room_plan(refs))
    for part in result["global_context"]["families"].values():
        assert part["partial"] and part["view"]["view"] is None
        assert all(s["reason_code"] == "MARKET_CUSTODY_REGISTRY_CHANGED" for s in part["view"]["sources"])
    monkeypatch.setattr(reading_global, "RelayRegistry", lambda *a: pytest.fail("unrequested registry"))
    monkeypatch.setattr(VersionedStore, "read_source", lambda *a: pytest.fail("unrequested source"))
    old = ReadingRoom(store, market_registry=object()).build({"as_of": CLOCK}, market_export=False)
    assert "global_context" not in old and "global_context" not in old["plan"]
    assert 'id="global-content"' not in render_page(old)


@pytest.mark.parametrize("failure", [StoreUnavailable("REMOTE_REQUEST_LIMIT"),
    RecordError("READING_SCOPE_CLOSED"), RecordError("SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED"),
    RecordError("PUBLIC_BINDING_MISMATCH")], ids=lambda e: str(e))
def test_original_terminal_identity_and_no_backend_reads_after_failure(saved, monkeypatch, failure):
    store, _, refs = saved
    bad, _ = source_query(store, refs["us_indices"], code="DJI")
    original, calls, failed, after = store.backend.resolve, [], False, []
    def read(reference):
        nonlocal failed
        calls.append(reference)
        if failed:
            after.append(reference)
        if reference == bad.manifest and not failed:
            failed = True
            raise failure
        return original(reference)
    monkeypatch.setattr(store.backend, "resolve", read)
    with pytest.raises(type(failure)) as caught:
        build(saved)
    assert caught.value is failure and failed and after == []
    assert calls[-1] == bad.manifest
    assert SourceRef.from_dict(refs["shibor"]).manifest not in calls


def test_scope_exit_failure_overrides_finished_model_and_display_bound(saved, monkeypatch):
    store, _, refs = saved
    original, failure, completed = reading_global.market_read_scope, RecordError("PUBLIC_BINDING_MISMATCH"), []
    @contextmanager
    def scope(owner):
        with original(owner) as reader:
            yield reader
            completed.append(True)
            raise failure
    monkeypatch.setattr(reading_global, "market_read_scope", scope)
    monkeypatch.setattr(reading_global, "MAX_GLOBAL_BYTES", 1)
    with pytest.raises(RecordError) as caught:
        build(saved)
    assert caught.value is failure and completed == [True]
    monkeypatch.setattr(reading_global, "market_read_scope", original)
    result = build(saved)["global_context"]
    assert result["status"] == "not-included" and "families" not in result
    assert {name: p["reference"] for name, p in result["plan"].items()} == refs


def test_original_cli_reopens_only_refs_without_writing_records(saved, tmp_path):
    store, _, refs = saved
    path = tmp_path / "plan.json"; path.write_bytes(encoded(room_plan(refs)))
    env = {k: v for k, v in os.environ.items() if k not in (
        "GH_TOKEN", "GITHUB_TOKEN", "DECISION_OS_STORE_TOKEN", "TUSHARE_PROXY_API_KEY")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    before, results = store.backend.head(), []
    for slot in ("first", "reopen"):
        run = subprocess.run([sys.executable, "-m", "decision_kernel", "store", "--path", str(store.backend.root),
            "research-room", "--input", str(path), "--out", str(tmp_path / (slot + ".html")),
            "--market-registry", str(REGISTRY_PATH)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        results.append(json.loads(run.stdout))
    assert results[0] == results[1] and results[0]["global_context"]["status"] == "readable"
    assert (tmp_path / "first.html").read_bytes() == (tmp_path / "reopen.html").read_bytes()
    assert store.backend.head() == before


def exercise_installed_global_room(outside, python, env, store):
    """Use the existing single cold wheel and installed consumer outside checkout."""
    directory = outside / "global-room"; directory.mkdir()
    registry_dir = directory / "registry"; registry_dir.mkdir()
    metadata = json.loads(REGISTRY_PATH.read_text())
    for relative in ("relay-registry.json", metadata["manual"]["path"], metadata["live_catalogue"]["path"],
                     metadata["live_catalogue"]["receipt_path"]):
        dest = registry_dir / relative; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REGISTRY_PATH.parent / relative, dest)
    source = directory / "capture.json"; source.write_bytes(encoded(global_inputs()))
    plan = directory / "plan.json"; registry = registry_dir / "relay-registry.json"
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import encoded
store,registry,source,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(store))
trusted=RelayRegistry(registry)
parts={name:{'reference':retain_market_reading(value,trusted,fresh(),fresh_reader=fresh,public_rights_confirmed=True)['reference'],'limit':1}
       for name,value in json.loads(source.read_bytes()).items()}
output.write_bytes(encoded({'as_of':'2026-10-20T12:00:00+08:00','global_context':parts}))
'''
    subprocess.run([str(python), "-I", "-c", script, str(store), str(registry), str(source), str(plan)],
                   cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    source.unlink()
    before, results = GitStore(store).head(), []
    for slot in ("first", "reopen"):
        run = subprocess.run([str(python), "-I", "-m", "decision_kernel", "store", "--path", str(store),
            "research-room", "--input", str(plan), "--out", str(directory / (slot + ".html")),
            "--market-registry", str(registry)], cwd=outside, env=env, check=True,
            capture_output=True, text=True, timeout=60)
        results.append(json.loads(run.stdout))
    assert results[0] == results[1]
    parts = results[0]["global_context"]["families"]
    assert parts["us_indices"]["view"]["view"]["scope_count"] == 4
    assert parts["shibor"]["view"]["view"]["scope_count"] == 8
    assert all(part["page"]["returned"] == 1 and part["next_plan"] is not None for part in parts.values())
    assert (directory / "first.html").read_bytes() == (directory / "reopen.html").read_bytes()
    assert results[0]["catalogue"]["total_cases"] > 0 and GitStore(store).head() == before
