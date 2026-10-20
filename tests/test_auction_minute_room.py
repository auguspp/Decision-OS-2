"""The normal Room/CLI, original whole-cohort model and failure boundaries."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_kernel.application import auction_minute_custody as custody, reading_auction_minute
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.reading_market import open_market
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_auction_inputs import auction_payload
from test_auction_minute_inputs import ROOT, REGISTRY, seed_payload, minute_payload
from test_auction_minute_custody import saved_fixture, root_document
from test_global_room import source_query

CLOCK = "2026-10-21T12:00:00+08:00"


def room_plan(reference, **navigation):
    return {"as_of": CLOCK, "auction_minute": {"reference": reference, **navigation}}


def test_normal_room_pages_keep_whole_five_groups_and_existing_auction():
    store, fresh, seed, _, saved = saved_fixture()
    plan = {**room_plan(saved["reference"], limit=1), "auction": {"reference": seed["reference"]}}
    room = ReadingRoom(store, market_registry=REGISTRY).build(plan)
    part = room["auction_minute"]
    assert room["auction"]["view"]["view"]["cohort_denominator"] == 9
    expected = read_stored_market(saved["reference"], REGISTRY, fresh(), limit=100)
    assert encoded(part["view"]) == encoded(expected) and part["page"]["returned"] == 1
    assert part["open_request"] == {"operation": "read-source", "payload": json.loads(encoded(saved["reference"]))}
    assert part["writes"] == part["source_requests"] == 0
    next_ = ReadingRoom(fresh(), market_registry=REGISTRY).build({"as_of": CLOCK, "auction_minute": part["next_plan"]})
    selected = ReadingRoom(fresh(), market_registry=REGISTRY).build(room_plan(saved["reference"], entity_id="830001.BJ"))
    assert next_["auction_minute"]["view"] == selected["auction_minute"]["view"] == part["view"]
    assert selected["auction_minute"]["page"]["entries"][0]["entity_id"] == "830001.BJ"
    assert "id=\"auction-minute-content\"" in render_page(room)
    with pytest.raises(RecordError):
        open_market(store, {"reference": saved["reference"]}, registry=REGISTRY)


def test_optional_seed_close_missing_does_not_change_original_minute_members_or_groups():
    payload = seed_payload()
    payload["close_result"] = auction_payload()["close_result"]
    store, fresh, seed, _, saved = saved_fixture(seed=payload)
    close, _ = source_query(store, seed["reference"], api="daily")
    store.backend.shared["missing"].add((close.manifest.commit, close.manifest.path))
    restored = read_stored_market(saved["reference"], REGISTRY, fresh())
    assert restored["status"] == "restored" and restored["view"] == saved["restored"]["view"]


def test_no_plan_has_no_component_io_and_scope_permission_is_preflighted(monkeypatch):
    store, _, seed, _, saved = saved_fixture()
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected source/registry read")
    monkeypatch.setattr(reading_auction_minute, "RelayRegistry", forbidden)
    monkeypatch.setattr(VersionedStore, "read_source", forbidden)
    room = ReadingRoom(store).build({"as_of": CLOCK}, market_export=False)
    assert "auction_minute" not in room and "id=\"auction-minute-content\"" not in render_page(room)
    monkeypatch.setattr(store.backend, "head", forbidden)
    for changes in ({"limit": True}, {"entity_id": "BTC-USD"}, {"url": "https://invalid"}, {"offset": -1}):
        with pytest.raises(RecordError):
            ReadingRoom(store).build(room_plan(saved["reference"], **changes))
    with pytest.raises(RecordError, match="ROOM_PUBLIC_MINUTE_NOT_CONFIRMED"):
        ReadingRoom(store).build(room_plan(saved["reference"]), market_export=False)
    with pytest.raises(RecordError, match="ROOM_PUBLIC_MINUTE_NOT_CONFIRMED"):
        reading_commands.perform(store, {"plan": room_plan(saved["reference"]), "public_research_custody_confirmed": True})


@pytest.mark.parametrize("failure", [StoreUnavailable("REMOTE_REQUEST_LIMIT"), RecordError("READING_SCOPE_CLOSED"),
    RecordError("PUBLIC_BINDING_MISMATCH"), RecordError("COMMIT_NOT_REACHABLE")], ids=str)
def test_original_terminal_object_reaches_caller_and_no_backend_method_continues(monkeypatch, failure):
    store, _, _, _, saved = saved_fixture()
    root = root_document(store, saved["reference"])
    bad = SourceRef.from_dict(root["sources"][0]["reference"]).manifest
    original, head, calls, failed = store.backend.resolve, store.backend.head, [], False
    def resolve(ref):
        nonlocal failed
        calls.append(("resolve", ref, failed))
        if ref == bad and not failed:
            failed = True
            raise failure
        return original(ref)
    def checked_head():
        calls.append(("head", None, failed))
        return head()
    monkeypatch.setattr(store.backend, "resolve", resolve)
    monkeypatch.setattr(store.backend, "head", checked_head)
    with pytest.raises(type(failure)) as caught:
        ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved["reference"]))
    assert caught.value is failure and failed and calls[-1] == ("resolve", bad, False)
    assert not any(after for _, _, after in calls)


def test_missing_original_local_and_final_scope_failure_prevents_html_even_when_over_display_bound(tmp_path, monkeypatch):
    store, _, _, _, saved = saved_fixture()
    source = SourceRef.from_dict(root_document(store, saved["reference"])["sources"][0]["reference"])
    store.backend.shared["missing"].add((source.manifest.commit, source.manifest.path))
    result = ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved["reference"]))
    assert result["auction_minute"]["status"] == "partial"
    assert result["auction_minute"]["view"]["view"]["cohort_denominator"] == 9
    actual, error, completed = custody.market_read_scope, RecordError("PUBLIC_BINDING_MISMATCH"), []
    @contextmanager
    def scope(owner):
        with actual(owner) as reader:
            yield reader
            completed.append(True)
            raise error
    monkeypatch.setattr(custody, "market_read_scope", scope)
    monkeypatch.setattr(reading_auction_minute, "MAX_MINUTE_BYTES", 1)
    args = SimpleNamespace(input=tmp_path / "plan.json", out=tmp_path / "must-not-exist.html",
                           market_registry=ROOT / "docs/sources/relay-registry.json")
    with pytest.raises(RecordError) as caught:
        reading_commands.execute(args, store, lambda _: room_plan(saved["reference"]))
    assert caught.value is error and completed == [True] and not args.out.exists()


def test_normal_custody_cli_and_main_room_reopen_without_original_input_files(tmp_path):
    path = tmp_path / "synthetic.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    seed = seed_payload()
    auction = retain_market_reading(seed, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    capture = tmp_path / "minute.json"
    capture.write_bytes(encoded(minute_payload(read_market_payload(seed, REGISTRY), auction["reference"])))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    base = [sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli"]
    run = subprocess.run([*base, "save", "--path", str(path), "--registry", str(ROOT / "docs/sources/relay-registry.json"),
        "--input", str(capture), "--custody-authorized"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    reference = json.loads(run.stdout)["reference"]
    capture.unlink()
    plan = tmp_path / "plan.json"; plan.write_bytes(encoded(room_plan(reference, limit=1)))
    ref = tmp_path / "ref.json"; ref.write_bytes(encoded(reference))
    before, values = store.backend.head(), []
    for name in ("first", "reopen"):
        run = subprocess.run([sys.executable, "-m", "decision_kernel", "store", "--path", str(path), "research-room",
            "--input", str(plan), "--out", str(tmp_path / (name + ".html")), "--market-registry",
            str(ROOT / "docs/sources/relay-registry.json")], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
        values.append(json.loads(run.stdout))
    direct = subprocess.run([*base, "read", "--path", str(path), "--registry", str(ROOT / "docs/sources/relay-registry.json"),
        "--input", str(ref), "--limit", "100"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert values[0] == values[1] and json.loads(direct.stdout) == values[0]["auction_minute"]["view"]
    assert (tmp_path / "first.html").read_bytes() == (tmp_path / "reopen.html").read_bytes()
    assert store.backend.head() == before


def exercise_installed_auction_minute_room(outside, python, env, store):
    """Only extend the existing one-wheel consumer; root runs that one gate."""
    directory = outside / "auction-minute-room"; directory.mkdir()
    registry_dir = directory / "registry"; registry_dir.mkdir()
    registry_source = ROOT / "docs/sources/relay-registry.json"
    metadata = json.loads(registry_source.read_text())
    for relative in ("relay-registry.json", metadata["manual"]["path"], metadata["live_catalogue"]["path"],
                     metadata["live_catalogue"]["receipt_path"]):
        destination = registry_dir / relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(registry_source.parent / relative, destination)
    registry = registry_dir / "relay-registry.json"
    seed_file = directory / "seed.json"; seed_file.write_bytes(encoded(seed_payload()))
    minute_file = directory / "minute.json"; minute_file.write_bytes(encoded(minute_payload()))
    plan = directory / "plan.json"
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import encoded
store,registry,seed,minute,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(store))
r=RelayRegistry(registry)
seedref=retain_market_reading(json.loads(seed.read_bytes()),r,fresh(),fresh_reader=fresh,custody_authorized=True)['reference']
payload=json.loads(minute.read_bytes());payload['auction_reference']=seedref
ref=retain_market_reading(payload,r,fresh(),fresh_reader=fresh,custody_authorized=True)['reference']
output.write_bytes(encoded({'as_of':'2026-10-21T12:00:00+08:00','auction_minute':{'reference':ref,'limit':1}}))
'''
    subprocess.run([str(python), "-I", "-c", script, str(store), str(registry), str(seed_file), str(minute_file), str(plan)],
                   cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    seed_file.unlink(); minute_file.unlink()
    before, values = GitStore(store).head(), []
    for name in ("first", "reopen"):
        run = subprocess.run([str(python), "-I", "-m", "decision_kernel", "store", "--path", str(store), "research-room",
            "--input", str(plan), "--out", str(directory / (name + ".html")), "--market-registry", str(registry)],
            cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
        values.append(json.loads(run.stdout))
    assert values[0] == values[1] and values[0]["catalogue"]["total_cases"] > 0
    part = values[0]["auction_minute"]
    assert part["view"]["view"]["cohort_denominator"] == 9 and part["view"]["view"]["observed_0940"] == 8
    assert part["page"]["returned"] == 1 and part["next_plan"] and part["view"]["view"]["cross_provider_return"] == "NOT_COMPUTED"
    assert (directory / "first.html").read_bytes() == (directory / "reopen.html").read_bytes()
    assert GitStore(store).head() == before
