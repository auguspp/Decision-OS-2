"""Normal Room, native transport and installed CLI over retained synthetic originals."""
from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import csv
import io
import json
import shutil
import subprocess

import pytest

from decision_kernel.application import outcome_review, reading_outcome, versioned_store
from decision_kernel.application.market_discovery import MarketDiscovery
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, SourceInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces import reading_commands, reading_outcome_page, research_runtime
from decision_kernel.interfaces.cli import main
from decision_kernel.interfaces.reading_page import render_page
from test_market_discovery import REGISTRY, request as discovery_request, retain, rising, save_sequence
from test_market_relay_inputs import REGISTRY_PATH
from test_market_snapshot import local_fetch
from test_news_read_budget import transport
from test_outcome_review import BASIS, market_plan, prepare, span
from test_research_runtime import CODE, host, inputs


@pytest.fixture(autouse=True)
def reading_clock(monkeypatch):
    monkeypatch.setattr(outcome_review, "_now", lambda: datetime.fromisoformat("2026-10-21T10:00:00+08:00"))


def retained_plan(store, fresh, monkeypatch, *, market=False):
    value = prepare(store, monkeypatch)
    if market:
        monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-19T12:00:00+00:00")
        before, after = save_sequence((store, fresh), [rising(30), rising(55)])
        value = {**market_plan(before["reference"], after["reference"], limit=1),
                 "economic": value["economic"]}
    return {"as_of": value["as_of"], "reference": value["economic"][0]["forecast"]["research"]["reference"],
            "outcome": value}


def native_plan(host, monkeypatch, *, private=True, market=False):
    if private:
        host.binding = replace(host.binding, private_upgrade_allowed=True)
        host.meta["private"] = True
    if market:
        host.binding = replace(host.binding, branch="records")
        anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
        host.binding = replace(host.binding, anchor=anchor)
    # Seed real synthetic Git records; consumers below independently restore
    # their native marker, exact history, source-only snapshot and byte proofs.
    host.git.repository, host.git.binding = host.binding.repository, host.binding
    store = VersionedStore(host.git)
    fresh = lambda: VersionedStore(copy(host.git))
    value = retained_plan(store, fresh, monkeypatch)
    if market:
        monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-19T12:00:00+00:00")
        saved = []
        for index, numbers in enumerate((rising(30), rising(55))):
            source = retain((store, fresh), numbers)
            wire = transport(host)  # One existing budget for this one native save.
            writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), wire))
            source_store = VersionedStore(GitMarketReader(host.binding, wire, fetch=local_fetch(host, [])))
            result = MarketDiscovery(writer, REGISTRY, source_store=source_store).save(
                "native-outcome-generation-" + str(index),
                discovery_request(source, previous=saved[-1]["reference"] if saved else None))
            assert result["status"] == "stored-and-verified" and wire._requests < 512
            saved.append(result)
        outcome = {**market_plan(saved[0]["reference"], saved[1]["reference"], limit=1),
                   "economic": value["outcome"]["economic"]}
        value.update(as_of=outcome["as_of"], outcome=outcome)
    return value


def test_native_private_room_recovers_original_forecast_and_saved_discovery_across_pages(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch, market=True)
    fetches = []
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch", local_fetch(host, fetches))
    before, writes = host.git.head(), host.writes
    wire = transport(host)  # Real 512-request transport, synthetic GitHub responses only.
    report = research_runtime.run_command(host.binding, api=wire,
        inputs=inputs("reading-room", {"plan": value}), code_sha=CODE,
        output=tmp_path / "first", run_id="123", attempt="1")
    assert report["status"] == "completed" and report["repository_visibility"] == "private"
    assert report["write"] == "not-requested" and 0 < wire._requests < 512
    assert len(fetches) == 1 and fetches[0]["api"] is wire
    room, view = report["result"], report["result"]["outcome"]["view"]
    assert room["status"] == "partial" and room["outcome"]["partial"] is True
    economic, = view["economic"]["rows"]
    assert economic["status"] == "compared-literal-basis" and economic["comparison"]["difference"] == "10"
    assert economic["forecast"]["original_unresolved"] == ["Cash conversion unverified"]
    assert economic["actual"]["stored_at"] == "2026-07-21T08:00:00+08:00"
    market = view["market"]
    assert market["page"] == {"offset": 0, "limit": 1, "total": 2, "returned": 1, "remaining": 1}
    assert [h["status"] for h in market["rows"][0]["horizons"]] == ["observed", "observed"]
    assert [h["target_date"] for h in market["rows"][0]["horizons"]] == ["2026-07-05", "2026-07-20"]
    assert market["scope"]["sector"]["horizons"]["5"] == {"observed": 1, "original-unqualified": 1}
    assert view["writes"] == view["source_requests"] == market["writes"] == market["source_requests"] == 0
    page = (tmp_path / "first/reading-room.html").read_text()
    assert page == render_page(room) and 'id="outcome-content"' in page
    embedded = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded) == room
    assert json.loads((tmp_path / "first/report.json").read_bytes())["result"] == room
    assert room["human_acceptance"] == "not-established" and room["persistence"] == "not-written"

    next_value = {**value, "outcome": room["outcome"]["next_plan"]}
    next_wire = transport(host)  # A new command gets one new budget; no reset within either read.
    last = research_runtime.run_command(host.binding, api=next_wire,
        inputs=inputs("reading-room", {"plan": next_value}), code_sha=CODE, output=tmp_path / "second")["result"]
    remaining = last["outcome"]["view"]["market"]
    assert remaining["scope"] == market["scope"]
    assert remaining["whole_rows_sha256"] == market["whole_rows_sha256"]
    assert remaining["rows"][0]["original_gap"]["reason_code"] == "DISCOVERY_NOT_REQUESTED"
    assert remaining["page"]["offset"] == 1 and last["outcome"]["next_plan"] is None
    assert last["outcome"]["partial"] is True and 0 < next_wire._requests < 512
    assert len(fetches) == 2 and fetches[1]["api"] is next_wire
    assert host.writes == writes and host.git.head() == before


@pytest.mark.parametrize("permission,error", [
    ("public_research_custody_confirmed", "ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED"),
    ("public_sources_custody_confirmed", "ROOM_PUBLIC_OUTCOME_NOT_CONFIRMED"),
])
@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_public_outcome_permissions_precede_native_reads_and_output(host, tmp_path, monkeypatch, permission, error, flag):
    value = native_plan(host, monkeypatch, private=False)
    payload = {"plan": value, "public_research_custody_confirmed": True,
               "public_sources_custody_confirmed": True, permission: flag}
    wire = transport(host)
    calls, writes, before = len(host.requests), host.writes, host.git.head()
    with pytest.raises(RecordError, match=error):
        research_runtime.run_command(host.binding, api=wire, inputs=inputs("reading-room", payload),
                                     code_sha=CODE, output=tmp_path / "denied")
    assert wire._requests == 0 and not (tmp_path / "denied").exists()
    assert (len(host.requests), host.writes, host.git.head()) == (calls, writes, before)


def test_public_origin_upgrade_option_still_checks_actual_visibility_and_source_rights(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch, private=False)
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    calls = len(host.requests)
    payload = {"plan": value, "public_research_custody_confirmed": True}
    with pytest.raises(RecordError, match="ROOM_PUBLIC_OUTCOME_NOT_CONFIRMED"):
        research_runtime.run_command(host.binding, api=transport(host), inputs=inputs("reading-room", payload),
                                     code_sha=CODE, output=tmp_path / "denied")
    marker = host.git._run("rev-parse", host.binding.anchor + ":store.json").stdout.decode().strip()
    checked = host.requests[calls:]
    assert checked and all(method == "GET" for method, _, _ in checked)
    assert all(path.endswith(marker) for _, path, _ in checked if "/git/blobs/" in path)
    assert not (tmp_path / "denied").exists()


def test_public_room_with_both_permissions_recovers_same_original_economics(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch, private=False)
    before, writes = host.git.head(), host.writes
    payload = {"plan": value, "public_research_custody_confirmed": True,
               "public_sources_custody_confirmed": True}
    report = research_runtime.run_command(host.binding, api=transport(host), inputs=inputs("reading-room", payload),
                                         code_sha=CODE, output=tmp_path / "public")
    assert report["repository_visibility"] == "public"
    panel = report["result"]["outcome"]
    assert panel["partial"] is False and panel["view"]["economic"]["rows"][0]["comparison"]["difference"] == "10"
    assert report["result"]["status"] == "readable"
    assert host.git.head() == before and host.writes == writes


def assert_no_private_content(path):
    report = json.loads((path / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report
    assert not (path / "reading-room.html").exists() and not (path / "inputs.json").exists()


def test_final_native_revocation_withholds_outcome_json_html_and_input_refs(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch)
    actual = reading_outcome.open_outcome
    def reverse(*args, **kwargs):
        result = actual(*args, **kwargs)
        host.meta["private"] = False
        return result
    monkeypatch.setattr(reading_outcome, "open_outcome", reverse)
    before, writes = host.git.head(), host.writes
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=tmp_path / "revoked")
    assert_no_private_content(tmp_path / "revoked")
    assert host.git.head() == before and host.writes == writes


def test_outcome_source_budget_is_terminal_for_the_whole_room(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch)
    monkeypatch.setattr(outcome_review, "MAX_READ_BYTES", 1)
    before, writes = host.git.head(), host.writes
    with pytest.raises(RecordError, match="OUTCOME_READING_BOUND"):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=tmp_path / "bounded")
    assert_no_private_content(tmp_path / "bounded")
    assert host.git.head() == before and host.writes == writes


def test_existing_api_budget_is_shared_with_outcome_and_not_restarted(host, tmp_path, monkeypatch):
    value = native_plan(host, monkeypatch)
    wire, entered = transport(host), []
    actual = reading_outcome.open_outcome
    def exhausted(store, plan, **kwargs):
        assert store.backend.api is wire
        entered.append(wire._requests)
        # Prior work on this same transport consumed the existing 512 requests.
        # The next native access must fail; no new adapter may restart the budget.
        wire._requests = 512
        return actual(store, plan, **kwargs)
    monkeypatch.setattr(reading_outcome, "open_outcome", exhausted)
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        research_runtime.run_command(host.binding, api=wire,
            inputs=inputs("reading-room", {"plan": value}), code_sha=CODE, output=tmp_path / "exhausted")
    assert entered and entered[0] > 0 and wire._requests > 512
    assert_no_private_content(tmp_path / "exhausted")


def test_primary_store_cli_reopens_both_outcomes_and_offline_room(tmp_path, monkeypatch, capsys):
    path = tmp_path / "records.git"
    store = VersionedStore(GitStore.init_synthetic(path))
    value = retained_plan(store, lambda: VersionedStore(GitStore(path)), monkeypatch, market=True)
    before = store.backend.head()
    plan_file = tmp_path / "outcome.json"
    plan_file.write_bytes(encoded(value["outcome"]))
    argv = ["store", "--path", str(path), "research-outcome-review", "--input", str(plan_file),
            "--market-registry", str(REGISTRY_PATH)]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["economic"]["rows"][0]["comparison"]["difference"] == "10"
    assert result["market"]["scope"]["sector"]["original_scope_count"] == 2
    assert result["market"]["rows"][0]["horizons"][1]["status"] == "observed"
    room_file, html_file = tmp_path / "room.json", tmp_path / "room.html"
    room_file.write_bytes(encoded(value))
    assert main(["store", "--path", str(path), "research-room", "--input", str(room_file),
                 "--out", str(html_file), "--market-registry", str(REGISTRY_PATH)]) == 0
    room = json.loads(capsys.readouterr().out)
    assert encoded(room["outcome"]["view"]) == encoded(result)
    embedded = html_file.read_text().split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded) == room and 'id="outcome-content"' in html_file.read_text()
    assert VersionedStore(GitStore(path)).backend.head() == before


def execute_outcome_buttons(model):
    node = shutil.which("node")
    if node is None:
        pytest.skip("offline DOM exercise uses the existing optional Node runtime")
    prefix = r'''
const D=JSON.parse(require('fs').readFileSync(0,'utf8'));
class E{constructor(tag,text,cls){this.tag=tag;this.textContent=text===undefined?'':String(text);this.children=[];this.className=cls;}append(...n){this.children.push(...n);}}
const pane=new E('div'),calls=[],buttons=[],$=id=>id==='outcome-content'?pane:null;
const el=(tag,text,cls)=>new E(tag,text,cls),details=(title,value)=>el('details',JSON.stringify(value));
const button=(text,fn)=>{const b=el('button',text);b.run=fn;buttons.push(b);return b;};
const request=(name,value)=>calls.push(value),roomRequest=plan=>({operation:'reading-room',payload:{plan}}),label=x=>x,save=()=>{};
'''
    suffix = r'''
for(const b of buttons)if(!b.disabled)b.run();
const flatten=e=>e.textContent+e.children.map(flatten).join('\n');
process.stdout.write(JSON.stringify({text:flatten(pane),calls,pwned:globalThis.pwned===1}));
'''
    run = subprocess.run([node, "-e", prefix + reading_outcome_page.JS + suffix],
                         input=encoded(model).decode(), text=True, capture_output=True, timeout=20)
    assert run.returncode == 0 and run.stderr == ""
    return json.loads(run.stdout)


def test_exported_room_pages_and_naked_source_refs_are_actual_readable_inputs(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "export.git"))
    value = retained_plan(store, lambda: VersionedStore(GitStore(store.backend.root)), monkeypatch, market=True)
    model = reading_commands.perform(store, {"plan": value}, public=False, market_registry=REGISTRY)
    exported = execute_outcome_buttons(model)
    assert len(exported["calls"]) == 3
    first, source, next_page = exported["calls"]
    assert first["operation"] == next_page["operation"] == "reading-room"
    for request in (first, next_page):
        assert set(request["payload"]) == {"plan"}  # No inherited public rights assertion.
        reading_commands.preflight(request["operation"], request["payload"], public=False)
        research_runtime.parse_inputs(inputs(request["operation"], request["payload"]))
    assert source["operation"] == "read-source"
    assert encoded(source["payload"]) == encoded(value["outcome"]["economic"][0]["actual"]["reference"])
    restored = store.read_source(SourceRef.from_dict(source["payload"]))
    assert restored["data"][0]["value"] == "110"
    assert research_runtime.parse_inputs(inputs("read-source", source["payload"]))["payload"] == source["payload"]
    page = reading_commands.perform(store, next_page["payload"], public=False, market_registry=REGISTRY)
    assert page["outcome"]["view"]["market"]["scope"] == model["outcome"]["view"]["market"]["scope"]
    assert page["outcome"]["view"]["market"]["rows"][0]["original_gap"]["reason_code"] == "DISCOVERY_NOT_REQUESTED"


def test_literal_actual_metadata_stays_data_and_old_room_has_no_panel(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "literal.git"))
    value = retained_plan(store, lambda: store, monkeypatch)
    original = store.read_source(SourceRef.from_dict(value["outcome"]["economic"][0]["actual"]["reference"]))
    literal = "</script><script>globalThis.pwned=1</script>"
    ref = SourceRef.from_dict(value["outcome"]["economic"][0]["actual"]["reference"])
    rep = original["manifest"]["representation"]
    raw = store.backend.resolve(BlobRef(ref.manifest.repository, ref.manifest.commit, rep["path"], rep["sha256"]))
    source = SourceInput.from_dict({**original["manifest"]["input"], "text": raw.decode()})
    ref = store.put_source("literal-title", replace(source, title=literal))
    value["outcome"]["economic"][0]["actual"]["reference"] = asdict(ref)
    model = ReadingRoom(store).build(value)
    page = render_page(model)
    assert literal not in page
    payload = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(payload) == model
    assert model["outcome"]["view"]["economic"]["rows"][0]["actual"]["source"]["title"] == literal
    exported = execute_outcome_buttons(model)
    assert literal in exported["text"] and exported["pwned"] is False
    monkeypatch.setattr(reading_outcome, "open_outcome", lambda *a, **k: pytest.fail("unselected reader"))
    plain = ReadingRoom(store).build({"as_of": value["as_of"]})
    assert ReadingRoom(store).build({"as_of": value["as_of"], "outcome": None}) == plain
    assert "outcome" not in plain and 'id="outcome-content"' not in render_page(plain)
    assert 'href="#outcome"' not in render_page(plain)
    subprocess.run([shutil.which("node"), "-e", "const D={},$=()=>null;\n" + reading_outcome_page.JS],
                   check=True, capture_output=True, text=True, timeout=20)


def test_display_bound_preserves_original_read_plan_but_no_outcome_view(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "display.git"))
    value = retained_plan(store, lambda: store, monkeypatch)
    monkeypatch.setattr(outcome_review, "MAX_RESULT_BYTES", 1)
    result = ReadingRoom(store).build(value)
    assert result["status"] == "partial"
    assert encoded(result["outcome"]) == encoded({"plan": value["outcome"], "status": "not-included", "error": "OUTCOME_RESULT_BOUND"})
    assert result["readings"][0]["status"] == "readable"


def exercise_installed_outcome(command, outside, store):
    """Extend the original single cold wheel; no second install or source collection."""
    body = "Synthetic statement retained for later review.\nIssued 2026-04-01\nValue 100\n"
    body += "\n".join(key + " " + value for key, value in BASIS.items()) + "\n"
    path = outside / "outcome-installed.json"
    path.write_bytes(encoded({"case_id": "installed-outcome", "revision_id": "r1", "kind": "full",
        "body": body, "mandate": "Synthetic original statement", "method": "synthetic",
        "unresolved": ["Original uncertainty remains"]}))
    research = command("submit-revision", "--submission-id", "outcome-original", "--input", str(path))
    table = io.StringIO(newline="")
    writer = csv.DictWriter(table, fieldnames=[*BASIS, "value"])
    writer.writeheader()
    writer.writerow({**BASIS, "value": "110"})
    path.write_bytes(encoded({"source_id": "installed-outcome-actual", "kind": "dataset", "title": "Synthetic actual",
        "origin": "synthetic:installed-outcome", "text": table.getvalue(), "published_at": "2026-07-20",
        "acquired_at": "2026-07-21", "report_period": "2026-H1"}))
    actual = command("put-source", "--submission-id", "outcome-actual", "--input", str(path))
    plan = {"schema": "outcome-review-plan-v1", "as_of": datetime.now(timezone.utc).isoformat(), "market": None,
        "economic": [{"forecast_id": "original", "forecast": {
            "research": {"reference": research, "start": 1, "end": len(body.splitlines())}, "quote": body,
            "role": "research-forecast", "issued_on": "2026-04-01", "issued_on_locator": span(body, "2026-04-01"),
            "basis": deepcopy(BASIS), "value_locator": span(body, "100"), "range": None,
            "context_bindings": {key: span(body, value) for key, value in BASIS.items()}},
            "actual": {"reference": actual, "value_locator": {"row": 1, "column": "value"},
                "basis": deepcopy(BASIS), "reported_on": "2026-07-20",
                "context_bindings": {key: {"row": 1, "column": key} for key in BASIS}}, "missing_reason": None}]}
    path.write_bytes(encoded(plan))
    before = GitStore(store).head()
    original = command("research-outcome-review", "--input", str(path))
    restored = command("research-outcome-review", "--input", str(path))
    assert original == restored and original["writes"] == original["source_requests"] == 0
    row, = original["economic"]["rows"]
    assert row["status"] == "compared-literal-basis" and row["comparison"]["difference"] == "10"
    assert row["forecast"]["original_unresolved"] == ["Original uncertainty remains"]
    assert row["knowledge"] == "later-review-not-ex-ante-capture-proof"
    path.write_bytes(encoded({"as_of": plan["as_of"], "reference": research, "outcome": plan}))
    page = outside / "outcome-installed.html"
    room = command("research-room", "--input", str(path), "--out", str(page))
    assert room["outcome"]["view"] == original and 'id="outcome-content"' in page.read_text()
    assert GitStore(store).head() == before
