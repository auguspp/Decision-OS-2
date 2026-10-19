"""State evidence consumed through the real room/CLI, without writes or providers."""
from copy import copy, deepcopy
from dataclasses import replace
import json
import subprocess

import pytest

from decision_kernel.application.market_discovery import MarketDiscovery
from decision_kernel.application.reading_discovery import discovery_plan, open_discovery
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import market_custody_cli, market_discovery_cli
from decision_kernel.interfaces.reading_commands import perform, preflight
from decision_kernel.interfaces.reading_page import render_page
from decision_kernel.interfaces.research_payload import pack_payload
import test_market_custody as custody_fixture
from test_market_discovery import AS_OF, REGISTRY, retain, rising, save_sequence, storage
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport


def test_native_room_reads_replayed_state_and_full_scope_without_writes(native, monkeypatch):
    model, seed = native
    inputs = seed, lambda: VersionedStore(copy(model.git))
    saved = save_sequence(inputs, [rising(25), rising(26)])[-1]
    api, fetches = transport(model), []
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch",
                        local_fetch(model, fetches))
    store = VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), api))
    before = model.git.head()
    plan = {"as_of": AS_OF, "discovery": {"reference": saved["reference"],
        "kind": "sector", "entity_id": "ths-a-sector:881001.TI", "limit": 1}}
    result = perform(store, {"plan": plan}, public=False, market_registry=REGISTRY)
    part = result["discovery"]
    assert part["status"] == "readable" and result["status"] == "partial"
    view = part["view"]
    assert view["surfaces"]["sector"]["scope_count"] == 2
    assert view["surfaces"]["sector"]["unknown_count"] == 1
    assert view["surfaces"]["stock"]["status"] == "not-supplied"
    row, = view["states"]
    assert row["transition"] == "entered"
    assert view["events"][row["origin_event"]]["type"] == "entry"
    assert view["proofs"] and view["persistence"] == "saved-exact-version"
    assert model.git.head() == before and model.writes == 0 and 0 < api._requests < 512
    assert len(fetches) == 1 and fetches[0]["api"] is api
    html = render_page(result)
    assert "状态发现" in html and "20 日涨幅的 5 日变化" in html
    assert "首次观察基线和历史重算" in html


def test_preview_is_not_saved_and_missing_entity_keeps_denominator(storage):
    ref = retain(storage, rising(30))
    plan = {"build": {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
        "surfaces": {"sector": {"reference": ref}}}, "kind": "sector",
        "entity_id": "ths-a-sector:881002.TI"}
    before = storage[0].backend.head()
    result = open_discovery(storage[0], plan, registry=REGISTRY)
    assert result["view"]["persistence"] == "not-written" and result["reference"] is None
    assert result["view"]["states"] == [] and result["view"]["gap_query_count"] == 1
    assert result["view"]["surfaces"]["sector"]["scope_count"] == 2
    assert storage[0].backend.head() == before


def test_invalid_artifact_is_isolated_without_falling_back_to_other_saved_state(storage):
    saved = save_sequence(storage, [rising(30)])[0]
    broken = deepcopy(saved["reference"])
    broken["sha256"] = "f" * 64
    before = storage[0].backend.head()
    result = ReadingRoom(storage[0], market_registry=REGISTRY).build(
        {"as_of": AS_OF, "discovery": {"reference": broken}})
    assert result["discovery"]["status"] == "invalid" and result["status"] == "partial"
    assert result["discovery"]["reference"] == broken
    assert storage[0].backend.head() == before


def test_public_room_requires_source_export_permission_before_opening_state(storage):
    saved = save_sequence(storage, [rising(30)])[0]
    payload = {"plan": {"as_of": AS_OF, "discovery": {"reference": saved["reference"]}},
               "public_research_custody_confirmed": True}
    with pytest.raises(RecordError, match="PUBLIC_DISCOVERY"):
        preflight("reading-room", payload)
    preflight("reading-room", payload, public=False)
    with pytest.raises(RecordError, match="ROOM_DISCOVERY_PLAN"):
        discovery_plan({"reference": saved["reference"], "build": saved["result"]["plan"]})


def test_explicit_discovery_save_denies_without_authorization_and_read_is_source_free(storage):
    source = retain(storage, rising(30))
    plan = {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
            "surfaces": {"sector": {"reference": source}}}
    before = storage[0].backend.head()
    with pytest.raises(RecordError, match="SAVE_NOT_AUTHORIZED"):
        market_discovery_cli.execute("save", plan, storage[0], registry=REGISTRY,
                                     submission_id="not-authorized")
    assert storage[0].backend.head() == before
    saved = market_discovery_cli.execute("save", plan, storage[0], registry=REGISTRY,
        submission_id="authorized-state", rights_confirmed=True)
    before = storage[0].backend.head()
    result = market_discovery_cli.execute("read", saved["reference"], storage[1](), registry=REGISTRY)
    assert result["view_sha256"] == saved["result"]["view_sha256"]
    assert result["source_requests"] == 0 and storage[0].backend.head() == before


def test_runtime_compressed_catalogue_still_projects_all_records_and_strips_diagnostics(tmp_path):
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    payload = custody_fixture.raw_payload()
    payload["stock_results"][custody_fixture.CODE]["capture"]["pages"][0]["attempts"] = [
        {"private": "x" * 90000}]
    event = fixture.event("save", payload)
    event["inputs"]["payload"] = pack_payload(encoded(payload))
    assert len(encoded(payload)) > 60000
    out = tmp_path / "compressed"
    def child(command, **kwargs):
        actual = json.loads((out / "input.json").read_bytes())
        assert "private" not in actual["stock_results"][custody_fixture.CODE]["capture"]["pages"][0]
        assert actual["declared_entities"] == payload["declared_entities"]
        assert "private" not in (out / "input.json").read_text()
        return subprocess.CompletedProcess(command, 0, '{}', '')
    assert fixture.invoke(event, fixture.env(tmp_path), out, child) == 0


def test_runtime_bad_compressed_digest_cannot_create_output_or_launch_child(tmp_path):
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    event = fixture.event()
    envelope = json.loads(pack_payload(encoded(fixture.ref)))
    envelope["sha256"] = "0" * 64
    event["inputs"]["payload"] = json.dumps(envelope)
    out = tmp_path / "bad-envelope"
    with pytest.raises(RecordError, match="PAYLOAD_DIGEST_MISMATCH"):
        fixture.invoke(event, fixture.env(tmp_path), out, lambda *a, **k: pytest.fail("child called"))
    assert not out.exists()


def test_discovery_runtime_selects_new_cli_and_has_no_source_credentials(storage, tmp_path):
    saved = save_sequence(storage, [rising(30)])[0]
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    event = fixture.event("discovery-save", saved["result"]["plan"])
    event["inputs"]["public_rights_confirmed"] = True
    out = tmp_path / "runtime"
    def child(command, **kwargs):
        assert command[2:4] == ["decision_kernel.interfaces.market_discovery_cli", "save"]
        assert "--public-rights-confirmed" in command and "--submission-id" in command
        assert "--offset" not in command and "TUSHARE_PROXY_API_KEY" not in kwargs["env"]
        assert json.loads((out / "binding.json").read_bytes())["write_enabled"] is True
        return subprocess.CompletedProcess(command, 0, '{}', '')
    assert fixture.invoke(event, fixture.env(tmp_path), out, child) == 0


def test_custody_release_rechecks_native_private_repository(native, monkeypatch, capsys):
    model, _ = native
    api = transport(model)
    monkeypatch.setattr(market_custody_cli, "_load", lambda _: model.binding.__dict__)
    monkeypatch.setattr(market_custody_cli.GitHubAPI, "from_environment", lambda: api)
    assert market_custody_cli.main(["release"]) == 0 and model.writes == 0
    model.meta["private"] = False
    assert market_custody_cli.main(["release"]) == 2 and model.writes == 0
    assert "PRIVATE_RELEASE_REQUIRED" in capsys.readouterr().err
