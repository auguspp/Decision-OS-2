"""Main CLI and original native room consumers, using retained synthetic Git data."""
from copy import copy, deepcopy
from dataclasses import asdict
from html import unescape
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError

import pytest

from decision_kernel.application import close_structure as component
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_close_structure import AS_OF, REGISTRY, plan, retain, storage
from test_market_snapshot import local_fetch
from test_reading_scope import retained, transport
from test_research_runtime import CODE, inputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native_structure(retained, monkeypatch):
    model, seed, _, revision = retained
    model.git.binding = model.binding
    ref = retain((seed, lambda: VersionedStore(copy(model.git))), [10, 15, 12, 17, 14, 18])
    fetches = []
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch",
                        local_fetch(model, fetches))
    api = transport(model)
    model.requests.clear()
    return model, seed, ref, revision, api, fetches


def room_plan(ref, revision=None):
    result = {"as_of": AS_OF, "structure": plan(ref, limit=1)}
    if revision is not None:
        result["reference"] = asdict(revision)
    return result


def test_original_native_private_room_restores_exact_structure_with_default_trusted_registry(native_structure, tmp_path):
    model, seed, ref, revision, api, fetches = native_structure
    value = room_plan(ref, revision)
    before = model.git.head()
    expected = reading_commands.perform(seed, {"plan": value}, public=False)
    out = tmp_path / "native-private"
    report = research_runtime.run_command(model.binding, api=api,
        inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=out)
    assert report["status"] == "completed" and report["repository_visibility"] == "private"
    assert report["result"] == expected
    assert report["result"]["structure"]["view"]["scope"]["scope_count"] == 2
    assert report["result"]["structure"]["view"]["scope"]["incomplete_count"] == 1
    assert report["result"]["readings"][0]["body"] == "Original synthetic finding."
    assert report["write"] == "not-requested" and model.writes == 0 and model.git.head() == before
    assert len(fetches) == 1 and fetches[0]["api"] is api and 0 < api._requests < 512
    html = (out / "reading-room.html").read_text()
    assert html == render_page(expected) and "收盘区间与右侧确认" in html
    assert json.loads((out / "inputs.json").read_bytes())["operation"] == "reading-room"
    assert "read-close-structure" not in research_runtime.READ_OPERATIONS
    assert encoded(report["result"]["structure"]["open_request"]["payload"]) == encoded(ref)


@pytest.mark.parametrize("grant", [False, "true", None])
def test_public_source_rights_rejected_before_input_artifact_or_source_reads(native_structure, tmp_path, grant):
    model, _, ref, revision, api, fetches = native_structure
    model.meta["private"] = False
    payload = {"plan": room_plan(ref, revision), "public_research_custody_confirmed": True,
               "public_sources_custody_confirmed": grant}
    out = tmp_path / "public-not-authorized"
    with pytest.raises(RecordError, match="ROOM_PUBLIC_STRUCTURE_NOT_CONFIRMED"):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs("reading-room", payload), code_sha=CODE, output=out)
    assert not out.exists() and fetches == [] and model.writes == 0
    assert all(method == "GET" for method, _, _ in model.requests)
    # Native visibility also verifies its existing store marker and ancestry.
    # It may read those metadata, but no research or source body before rights.
    marker = model.git._run("rev-parse", model.binding.anchor + ":store.json").stdout.decode().strip()
    bodies = [path for _, path, _ in model.requests if "/git/blobs/" in path]
    assert bodies == ["/repos/" + model.binding.repository + "/git/blobs/" + marker]


def test_explicit_public_rights_are_separate_from_private_custody(native_structure, tmp_path):
    model, _, ref, revision, api, fetches = native_structure
    model.meta["private"] = False
    payload = {"plan": room_plan(ref, revision), "public_research_custody_confirmed": True,
               "public_sources_custody_confirmed": True}
    report = research_runtime.run_command(model.binding, api=api,
        inputs=inputs("reading-room", payload), code_sha=CODE, output=tmp_path / "authorized")
    assert report["repository_visibility"] == "public" and report["result"]["structure"]["partial"] is True
    assert len(fetches) == 1 and model.writes == 0
    assert "public_sources_custody_confirmed" not in report["result"]["structure"]["open_request"]["payload"]


@pytest.mark.parametrize("phase", ["component", "before-export"])
def test_privacy_reversal_releases_no_input_body_prices_or_html(native_structure, tmp_path, monkeypatch, phase):
    model, _, ref, revision, api, _ = native_structure
    owner, name = (component, "open_structure") if phase == "component" else (reading_commands, "perform")
    original = getattr(owner, name)
    def reversed_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        model.meta["private"] = False
        return result
    monkeypatch.setattr(owner, name, reversed_after_read)
    out = tmp_path / "reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs("reading-room", {"plan": room_plan(ref, revision)}), code_sha=CODE, output=out)
    report = json.loads((out / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report and model.writes == 0
    assert sorted(p.name for p in out.iterdir()) == ["report.json"]


@pytest.mark.parametrize("status,reason", [(403, "REMOTE_NOT_AUTHORIZED"), (429, "REMOTE_RATE_LIMIT")])
def test_one_native_http_denial_stops_reads_and_exports_in_original_host(native_structure, tmp_path, monkeypatch, status, reason):
    model, _, ref, revision, api, _ = native_structure
    original_component, original_wire = component.open_structure, api._opener.open
    state, after = {"armed": False, "failed": False}, []
    def arm(*args, **kwargs):
        state["armed"] = True
        return original_component(*args, **kwargs)
    def denied_once(request, timeout):
        if state["armed"] and not state["failed"]:
            state["failed"] = True
            raise HTTPError(request.full_url, status, "Synthetic denied", {}, None)
        if state["failed"]:
            after.append(request.full_url)
        return original_wire(request, timeout)
    monkeypatch.setattr(component, "open_structure", arm)
    monkeypatch.setattr(api._opener, "open", denied_once)
    out = tmp_path / "denied-once"
    with pytest.raises(StoreUnavailable, match="^" + reason + "$"):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs("reading-room", {"plan": room_plan(ref, revision)}), code_sha=CODE, output=out)
    assert state["failed"] and after == [] and model.writes == 0
    assert sorted(p.name for p in out.iterdir()) == ["report.json"]
    report = json.loads((out / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report


def test_original_api_budget_exhaustion_is_fatal_not_a_partial_panel(native_structure, tmp_path, monkeypatch):
    model, _, ref, revision, api, _ = native_structure
    original = component.open_structure
    def exhausted(*args, **kwargs):
        api._requests = 512
        return original(*args, **kwargs)
    monkeypatch.setattr(component, "open_structure", exhausted)
    out = tmp_path / "budget-exhausted"
    with pytest.raises(StoreUnavailable, match="^REMOTE_REQUEST_LIMIT$"):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs("reading-room", {"plan": room_plan(ref, revision)}), code_sha=CODE, output=out)
    assert api._requests == 513 and model.writes == 0
    assert sorted(p.name for p in out.iterdir()) == ["report.json"]
    assert "result" not in json.loads((out / "report.json").read_bytes())


def test_exact_reference_outside_native_scope_stops_original_host(native_structure, tmp_path):
    model, _, ref, revision, api, _ = native_structure
    value = room_plan(ref, revision)
    value["structure"]["reference"] = deepcopy(ref)
    value["structure"]["reference"]["manifest"]["commit"] = "0" * 40
    out = tmp_path / "outside-scope"
    with pytest.raises(StoreUnavailable, match="^COMMIT_OUTSIDE_BINDING$"):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=out)
    assert sorted(p.name for p in out.iterdir()) == ["report.json"] and model.writes == 0
    assert "result" not in json.loads((out / "report.json").read_bytes())


def test_content_integrity_failure_remains_local_without_fallback(native_structure, tmp_path):
    model, _, ref, revision, api, _ = native_structure
    value = room_plan(ref, revision)
    value["structure"]["reference"] = deepcopy(ref)
    value["structure"]["reference"]["manifest"]["sha256"] = "f" * 64
    report = research_runtime.run_command(model.binding, api=api,
        inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=tmp_path / "bad-content")
    result = report["result"]
    assert result["status"] == "partial" and result["readings"][0]["body"] == "Original synthetic finding."
    assert result["structure"]["view"]["scope"]["scope_count"] is None
    assert result["structure"]["view"]["scope"]["reason_code"] == "BLOB_DIGEST_MISMATCH"
    assert encoded(result["structure"]["reference"]) == encoded(value["structure"]["reference"])
    assert model.writes == 0


def test_no_structure_plan_preserves_old_model_and_performs_no_structure_reads(storage, monkeypatch):
    store = storage[0]
    before = ReadingRoom(store).build({"as_of": AS_OF})
    monkeypatch.setattr(component, "open_structure", lambda *a, **k: pytest.fail("unexpected structure read"))
    assert ReadingRoom(store).build({"as_of": AS_OF, "structure": None}) == before
    assert "structure" not in before and "structure" not in before["plan"]
    assert 'id="structure"' not in render_page(before)


def test_selection_permissions_and_reading_clock_are_checked_before_store_reads(storage):
    ref = retain(storage, [10, 15, 12, 17, 14, 18])
    value = room_plan(ref)
    value["structure"]["reference"] = deepcopy(ref)
    value["structure"]["reference"]["selection"].update(kind="lines", start=1, end=1)
    with pytest.raises(RecordError, match="WHOLE_SOURCE_REQUIRED"):
        reading_commands.preflight("reading-room", {"plan": value}, public=False)
    with pytest.raises(RecordError, match="ROOM_PUBLIC_STRUCTURE_NOT_CONFIRMED"):
        ReadingRoom(storage[0]).build(room_plan(ref), market_export=False)
    with pytest.raises(RecordError, match="ROOM_STRUCTURE_CLOCK_AFTER_READING"):
        ReadingRoom(storage[0]).build({**room_plan(ref), "as_of": "2026-10-19T12:04:00Z"})


def test_display_bound_preserves_exact_request_and_does_not_leak_partial_rows(storage, monkeypatch):
    ref = retain(storage, [10, 15, 12, 17, 14, 18])
    monkeypatch.setattr(component, "MAX_DISPLAY_BYTES", 1)
    result = ReadingRoom(storage[0], market_registry=REGISTRY).build(room_plan(ref))
    part = result["structure"]
    assert result["status"] == "partial" and part["status"] == "not-included" and "view" not in part
    assert encoded(part["open_request"]) == encoded({"operation": "read-source", "payload": ref})


def test_page_is_literal_and_exports_only_original_room_or_source_requests(storage):
    hostile = '</script><img src=x onerror="alert(1)">'
    ref = retain(storage, [10, 15, 12, 17, 14, 18], name=hostile)
    result = ReadingRoom(storage[0], market_registry=REGISTRY).build(room_plan(ref))
    page = render_page(result)
    assert hostile not in page and "\\u003c/script\\u003e" in page
    assert "roomRequest({...D.plan,structure:p})" in page
    assert "{operation:'read-source',payload:row.source_reference}" in page
    assert "connect-src 'none'" in unescape(page) and "尚无右侧收盘，未确认" in page


def test_main_cli_and_original_room_subprocess_read_from_same_exact_native_git(tmp_path):
    root = tmp_path / "retained.git"
    backend = GitStore.init_synthetic(root)
    store = VersionedStore(backend)
    ref = retain((store, lambda: VersionedStore(GitStore(root))), [10, 15, 12, 17, 14, 18])
    head = backend.head()
    outside = tmp_path / "outside"
    outside.mkdir()
    cli_plan, room_input = outside / "structure.json", outside / "room.json"
    cli_plan.write_bytes(encoded(plan(ref)))
    room_input.write_bytes(encoded(room_plan(ref)))
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT / "src")}
    # Execute the real main command parser in a fresh process, outside the repo.
    # Distribution metadata is installed by the existing suite's environment.
    entry = [sys.executable, "-c", "from decision_kernel.interfaces.cli import main; raise SystemExit(main())"]
    shared = ["store", "--path", str(root)]
    registry = ["--market-registry", str(ROOT / "docs/sources/relay-registry.json")]
    direct = subprocess.run(entry + shared + ["read-close-structure", "--input", str(cli_plan)] + registry,
        cwd=outside, env=env, capture_output=True, text=True, timeout=60)
    assert direct.returncode == 0, direct.stderr
    outputs = []
    for index in range(2):
        html = outside / f"room-{index}.html"
        process = subprocess.run(entry + shared + ["research-room", "--input", str(room_input), "--out", str(html)] + registry,
            cwd=outside, env=env, capture_output=True, text=True, timeout=60)
        assert process.returncode == 0, process.stderr
        outputs.append(json.loads(process.stdout))
        assert "收盘区间与右侧确认" in html.read_text()
    assert outputs[0] == outputs[1] and backend.head() == head
    direct_view, room_view = json.loads(direct.stdout)["view"], outputs[0]["structure"]["view"]
    assert direct_view["view_sha256"] == room_view["view_sha256"]
    assert direct_view["scope"] == room_view["scope"]
