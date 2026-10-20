"""Four supplied-original families on the ordinary room, with fixed roots."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.application.global_public_custody import read_public_root
from decision_kernel.application.market_custody import read_stored_market, retain_market_reading
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_global_public_inputs import public_input, replace_body
from test_global_room import CLOCK, room_plan

COUNTS = {"treasury": 8, "fx": 4, "commodities": 3, "crypto": 2}


def public_inputs():
    return {name: public_input(name, as_of={"treasury": "2026-09-25", "fx": "2026-09-24",
            "commodities": "2026-09-23", "crypto": "2026-09-26"}[name]) for name in COUNTS}


def retain_public(store, fresh, values=None):
    return {name: json.loads(encoded(retain_market_reading(value, None, store,
        fresh_reader=fresh, public_rights_confirmed=True)["reference"]))
        for name, value in (public_inputs() if values is None else values).items()}


@pytest.fixture
def public_saved(tmp_path):
    path = tmp_path / "global-originals.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    return store, fresh, retain_public(store, fresh)


def test_room_keeps_whole_models_dates_and_denominators_across_navigation(public_saved):
    store, fresh, refs = public_saved
    before = store.backend.head()
    plan = room_plan(refs, **{name: {"limit": 1} for name in refs})
    first = ReadingRoom(fresh()).build(plan)
    assert first["status"] == first["global_context"]["status"] == "readable"
    next_plan = deepcopy(plan)
    filtered_plan = deepcopy(plan)
    for name, part in first["global_context"]["families"].items():
        expected = read_stored_market(refs[name], None, fresh(), limit=100)
        assert encoded(part["view"]) == encoded(expected)
        assert part["view"]["view"]["scope_count"] == len(part["view"]["view"]["entries"]) == COUNTS[name]
        assert part["view"]["view"]["as_of"] == public_inputs()[name]["as_of_date"] != CLOCK[:10]
        assert part["page"]["returned"] == 1 and part["next_plan"]["reference"] == refs[name]
        next_plan["global_context"][name] = part["next_plan"]
        filtered_plan["global_context"][name]["entity_id"] = part["view"]["view"]["declared_entities"][-1]
    second = ReadingRoom(fresh()).build(next_plan)
    filtered = ReadingRoom(fresh()).build(filtered_plan)
    for name in refs:
        a, b, c = [r["global_context"]["families"][name] for r in (first, second, filtered)]
        assert a["view"] == b["view"] == c["view"]
        assert a["page"]["entries"][0] != b["page"]["entries"][0]
        assert c["page"]["matched_count"] == 1 and c["next_plan"] is None
        assert c["open_request"] == {"operation": "read-source", "payload": refs[name]}
    assert store.backend.head() == before


def test_family_slot_does_not_promote_another_root_or_hide_unselected_null(public_saved):
    store, fresh, refs = public_saved
    wrong = {**refs, "treasury": refs["fx"]}
    result = ReadingRoom(fresh()).build(room_plan(wrong))
    assert result["global_context"]["families"]["treasury"]["error"] == "ROOM_OBJECT_INVALID"
    assert result["global_context"]["families"]["crypto"]["status"] == "readable"
    value = public_input("commodities")
    raw = json.loads(value["records"][0]["body"])
    raw["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] = None
    replace_body(value, 0, json.dumps(raw))
    latest = retain_public(store, fresh, {"commodities": value})
    result = ReadingRoom(fresh()).build(room_plan(latest, commodities={"entity_id": "CL=F", "limit": 1}))
    part = result["global_context"]["families"]["commodities"]
    assert part["page"]["entries"][0]["value"] == "101"
    assert part["view"]["view"]["entries"][0]["value"] is None
    assert part["view"]["view"]["available_values"] == 2 and part["view"]["view"]["scope_count"] == 3
    assert part["partial"] and result["status"] == "partial"


def test_lost_treasury_month_does_not_promote_a_surviving_older_month():
    from test_global_public_custody import save
    from test_market_custody import MemoryBackend
    backend = MemoryBackend()
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    value = public_input("treasury", started="2026-09-02T11:00:00Z", as_of="2026-09-01")
    saved = save((store, fresh), value)
    _, root = read_public_root(store, saved["reference"])
    assert len(root["sources"]) == 2
    target = SourceRef.from_dict(root["sources"][1]["reference"]).manifest
    backend.shared["missing"].add((target.commit, target.path))
    view = read_stored_market(saved["reference"], None, fresh())["view"]
    assert view["scope_count"] == view["missing_values"] == 8 and view["available_values"] == 0
    assert all(row["value"] is None and row["source_date"] is None for row in view["entries"])


@pytest.mark.parametrize("changes,error_code", [
    ({}, None),
    ({"as_of_date": "2026-02-30"}, "GP_DATE"),
    ({"started_at": "0001-01-01T00:00:00+00:01"}, "GP_CLOCK_INVALID"),
    ({"started_at": "0001-01-01T00:00:00Z"}, "GP_DATE"),
], ids=["normal", "invalid-calendar-day", "utc-offset-underflow", "recent-window-underflow"])
def test_public_root_date_errors_are_local_with_peer_readable(public_saved, changes, error_code):
    from decision_kernel.application.global_public_custody import _root_source
    from decision_kernel.application.reading_global import open_global
    from decision_kernel.domain.records import RecordError
    store, fresh, refs = public_saved
    reference = refs["treasury"]
    if changes:
        _, root = read_public_root(store, reference)
        root["reading"].update(changes)
        reference = asdict(store.put_source("synthetic-invalid-global-date", _root_source(root)))
        with pytest.raises(RecordError, match="^" + error_code + "$"):
            read_public_root(fresh(), reference)
    before = store.backend.head()
    result = open_global(fresh(), {"treasury": {"reference": reference}, "fx": {"reference": refs["fx"]}})
    assert result["status"] == ("partial" if changes else "readable")
    assert result["families"]["treasury"]["status"] == ("invalid" if changes else "readable")
    assert result["families"]["fx"]["status"] == "readable"
    assert encoded(result["families"]["fx"]["view"]) == encoded(read_stored_market(refs["fx"], None, fresh(), limit=100))
    assert store.backend.head() == before


def exercise_installed_global_public_room(outside, python, env, store):
    """Extend the existing unique cold wheel; no extra installation or source I/O."""
    directory = outside / "global-public-room"; directory.mkdir()
    source = directory / "supplied-originals.json"; source.write_bytes(encoded(public_inputs()))
    plan = directory / "plan.json"
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import encoded
store,source,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(store))
parts={name:{'reference':retain_market_reading(value,None,fresh(),fresh_reader=fresh,public_rights_confirmed=True)['reference'],'limit':1}
       for name,value in json.loads(source.read_bytes()).items()}
output.write_bytes(encoded({'as_of':'2026-10-20T12:00:00+08:00','global_context':parts}))
'''
    subprocess.run([str(python), "-I", "-c", script, str(store), str(source), str(plan)],
                   cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    source.unlink()
    before, results = GitStore(store).head(), []
    for label in ("first", "reopen"):
        run = subprocess.run([str(python), "-I", "-m", "decision_kernel", "store", "--path", str(store),
            "research-room", "--input", str(plan), "--out", str(directory / (label + ".html"))],
            cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
        results.append(json.loads(run.stdout))
    assert results[0] == results[1]
    for name, count in COUNTS.items():
        part = results[0]["global_context"]["families"][name]
        assert part["view"]["view"]["scope_count"] == len(part["view"]["view"]["entries"]) == count
        assert part["page"]["returned"] == 1 and part["next_plan"] is not None
    assert results[0]["catalogue"]["total_cases"] > 0 and GitStore(store).head() == before
    assert (directory / "first.html").read_bytes() == (directory / "reopen.html").read_bytes()
