"""The local console may only ever run reads, and only with a read-only credential."""
import base64
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_kernel.application import hosted_handoff
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, StoreUnavailable, digest
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces import (
    auction_commands, auction_next_session_commands, close_structure_commands, delivery_commands,
    decision_preparation_commands, local_read, odds_commands, outcome_review_commands,
    reading_commands, reading_page, research_commands,
)
from test_conditional_odds import authorized, price_payload, spec_payload
from test_hosted_watch_runtime import public_host
from test_news_read_budget import transport
from test_reading_odds_book import html_model
from test_research_runtime import host

BINDING = {"repository": "auguspp/Decision-OS-2", "repository_id": 1420041744, "branch": "records",
           "anchor": "0" * 40, "store_id": "decision-os-runtime", "visibility": "public",
           "write_enabled": False, "private_upgrade_allowed": True}


def upstream_reads() -> set:
    research = set(research_commands.COMMANDS) - {"checkpoint-research"}
    delivery = set(delivery_commands.COMMANDS) - set(delivery_commands.WRITE_COMMANDS)
    return (research | delivery | set(odds_commands.READ_COMMANDS)
            | {"resume-research", "current", "search", "read-source", "read-revision"}
            | {outcome_review_commands.COMMAND, close_structure_commands.COMMAND,
               auction_commands.COMMAND, auction_next_session_commands.COMMAND})


def writes() -> set:
    return (set(odds_commands.WRITE_COMMANDS) | set(odds_commands.RECONCILE_COMMANDS)
            | set(decision_preparation_commands.WRITES) | set(decision_preparation_commands.RECONCILES)
            | set(delivery_commands.WRITE_COMMANDS)
            | {"put-source", "submit-revision", "checkpoint-research"})


def binding(tmp_path, **changes) -> Path:
    data = dict(BINDING)
    data.update(changes)
    path = tmp_path / "store-binding.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_every_local_command_is_exactly_the_current_read_set():
    assert set(local_read.READ_ONLY) == upstream_reads()
    assert len(local_read.READ_ONLY) == len(set(local_read.READ_ONLY))


def test_no_local_command_can_write_or_reconcile():
    assert set(local_read.READ_ONLY) & writes() == set()


def test_private_decision_preparation_never_runs_locally():
    assert "prepare-decision" not in local_read.READ_ONLY
    assert "read-decision-preparation" not in local_read.READ_ONLY


def test_a_write_command_is_refused_before_any_binding_or_token_is_read(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("save-odds", binding(tmp_path), {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_REFUSES_WRITE_COMMAND"


def test_private_custody_reads_are_refused_with_their_own_reason(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("prepare-decision", binding(tmp_path), {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_PRIVATE_CUSTODY_REQUIRES_GITHUB"


def test_a_local_read_requires_a_binding(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", None, {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_REQUIRES_A_BINDING"


def test_a_local_read_requires_its_own_token(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path), {})
    assert str(refused.value) == "LOCAL_READ_TOKEN_REQUIRED"


def test_a_store_token_in_the_environment_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path),
                         {local_read.READ_TOKEN: "read", local_read.STORE_TOKEN: "write"})
    assert str(refused.value) == "LOCAL_READ_REFUSES_STORE_TOKEN"


def test_a_write_enabled_binding_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path, write_enabled=True),
                         {local_read.READ_TOKEN: "read"})
    assert str(refused.value) == "LOCAL_READ_REQUIRES_READ_ONLY_BINDING"


def test_a_private_binding_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue",
                         binding(tmp_path, visibility="private", private_upgrade_allowed=False),
                         {local_read.READ_TOKEN: "read"})
    assert str(refused.value) == "LOCAL_READ_PUBLIC_STORE_ONLY"


def test_an_allowed_read_returns_the_binding_it_will_read(tmp_path):
    found = local_read.check("read-odds-book", binding(tmp_path), {local_read.READ_TOKEN: "read"})
    assert isinstance(found, GitHubBinding)
    assert found.branch == "records" and found.write_enabled is False


def test_the_receipt_states_the_store_and_that_no_write_was_requested():
    record = local_read.receipt("research-catalogue", GitHubBinding.from_dict(BINDING),
                                started="a", finished="b", duration_ms=12, code=0)
    assert record["schema"] == "local-read-receipt-v1"
    assert record["identity"] == {"token": "read-only-required", "write": "not-requested",
                                  "authority": "local-read-only-console"}
    assert record["store"]["branch"] == "records" and record["duration_ms"] == 12
    assert record["runner"] == "local"


@pytest.fixture
def console(public_host, tmp_path, monkeypatch):
    api, _, _ = public_host
    api.binding = replace(api.binding, private_upgrade_allowed=True)
    path = tmp_path / "local-binding.json"
    path.write_text(json.dumps(asdict(api.binding)), encoding="utf-8")
    wire = transport(api)
    monkeypatch.setattr(GitHubAPI, "from_environment", lambda: wire)
    monkeypatch.setenv(local_read.READ_TOKEN, "synthetic-read-only")
    monkeypatch.delenv(local_read.STORE_TOKEN, raising=False)
    monkeypatch.delenv(local_read.MIRROR, raising=False)
    monkeypatch.delenv("DECISION_OS_READ_RECEIPT", raising=False)
    return SimpleNamespace(api=api, wire=wire, binding=path)


@pytest.fixture
def records(console, monkeypatch):
    # Real local Git supplies synthetic immutable records; the console later
    # reads them through the original bounded GitHubAPI wire model.
    api = console.api
    api.git.repository = api.binding.repository
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T01:00:00Z")
    store = VersionedStore(api.git)
    source = store.put_source("source", SourceInput("source", "content", "Synthetic source",
        "synthetic:local-console", "Synthetic retained evidence, not a provider response."))
    research = store.submit_revision("research", RevisionInput("case", "r1", "full",
        "Synthetic retained research body.", "Engineering fixture", "synthetic", (source,)))
    odds = ConditionalOdds(store, allow_declared_only=True)
    spec = odds.freeze(authorized("spec", spec_payload(asdict(research))), submission_id="spec")
    result = odds.save(authorized("calculation", {"spec_ref": spec["reference"],
        "price": price_payload()}), submission_id="odds")
    doc = {"schema": hosted_handoff.BOUND_SCHEMA, "kind": "brief", "execution_date": "2026-01-03",
        "cutoff": "2026-01-03T02:00:00Z", "scope": "Synthetic local consumer", "gaps": [],
        "body": "Synthetic author Brief, with one retained research adoption.", "baseline": None,
        "watch_adoptions": [], "sources": [asdict(source)],
        "adoptions": [{"reference": asdict(research),
            "body_sha256": digest(store.read_revision(research)["body"].encode()),
            "used_change": "Keep the original synthetic evidence."}]}
    brief = hosted_handoff.save(store, doc, "brief")
    attachment = b"Synthetic original attachment.\x00"
    artifact = ResearchArtifacts(store).put("attachment", ArtifactInput.from_bytes(
        "attachment", "original.bin", "application/octet-stream", attachment))
    api.requests.clear()
    return SimpleNamespace(research=research, odds=result, brief=brief, document=doc,
                           artifact=artifact, attachment=attachment, snapshot=api.git.head())


def run_console(console, *args):
    return local_read.main(["--binding", str(console.binding), *args])


def assert_denied(capsys, code):
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [json.loads(line) for line in captured.err.splitlines()]
    assert lines[0] == {"error": code}
    receipt = lines[1]["local_read_receipt"]
    assert receipt["exit_code"] == 2 and receipt["identity"]["write"] == "not-requested"
    return receipt


def test_local_native_private_refuses_before_any_records_request(console, capsys):
    console.api.meta["private"] = True
    assert run_console(console, "research-catalogue") == 2
    assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    assert console.api.requests == [("GET", "/repos/" + console.api.binding.repository, None)]
    assert console.api.writes == 0


def test_prechecked_binding_is_executed_without_reopening_file_or_resetting_api(console, records,
                                                                            monkeypatch, capsys):
    check, execute, selected = local_read.check, research_commands.execute, []
    def preflight(*args):
        found = check(*args)
        selected.append(found)
        console.binding.write_text("{}", encoding="utf-8")
        return found
    def consume(args, store, load, **kwargs):
        assert store.backend.binding is selected[0]
        api = store.backend.api
        assert isinstance(api, GitHubAPI) and api.rate_limits is console.wire.rate_limits
        assert api._token == console.wire._token
        result = execute(args, store, load, **kwargs)
        assert api._requests == console.wire._requests > 0
        return result
    monkeypatch.setattr(local_read, "check", preflight)
    monkeypatch.setattr(research_commands, "execute", consume)
    assert run_console(console, "research-catalogue") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["snapshot"] == records.snapshot
    assert json.loads(captured.err)["local_read_receipt"]["store"]["repository"] == console.api.binding.repository
    assert not selected[0].private_upgrade_allowed and console.api.binding.private_upgrade_allowed


def test_prechecked_binding_keeps_shared_duplicate_key_refusal(console, capsys):
    raw = console.binding.read_text(encoding="utf-8")
    console.binding.write_text(raw[:-1] + ', "visibility": "public"}', encoding="utf-8")
    assert run_console(console, "research-catalogue") == 2
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err) == {"error": "DUPLICATE_JSON_KEY"}
    assert console.api.requests == []


def test_normal_local_room_keeps_exact_research_odds_brief_and_snapshot(console, records, tmp_path, capsys):
    plan = tmp_path / "room-plan.json"
    plan.write_text(json.dumps({"as_of": "2026-01-03T03:00:00Z", "catalogue": {"limit": 4},
        "ai_brief": {"select": "latest-ai-brief"},
        "odds_book_view": {"query": {"limit": 4, "scan_limit": 16}, "page_limit": 1}}))
    output = tmp_path / "room.html"
    assert run_console(console, "research-room", "--input", str(plan), "--out", str(output)) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert html_model(output.read_text(encoding="utf-8")) == result
    assert result["catalogue"]["snapshot"] == result["odds_book_view"]["view"]["snapshot"] == records.snapshot
    assert result["ai_brief"]["snapshot"] == records.snapshot
    assert result["ai_brief"]["view"]["document"] == json.loads(json.dumps(records.document))
    assert any(row["reference"] == asdict(records.research) for row in result["readings"])
    assert result["odds_book_view"]["status"] == "readable"
    assert console.api.git.head() == records.snapshot and console.api.writes == 0
    assert json.loads(captured.err)["local_read_receipt"]["exit_code"] == 0


@pytest.mark.parametrize("include_ai", [False, True])
@pytest.mark.parametrize("phase", ["after-read", "after-render"])
def test_local_room_rechecks_after_read_and_render_before_html_or_stdout(console, records, tmp_path,
                                                                       monkeypatch, capsys, include_ai, phase):
    plan = tmp_path / "room-plan.json"
    value = {"as_of": "2026-01-03T03:00:00Z"}
    if include_ai:
        value["ai_brief"] = {"select": "latest-ai-brief"}
    plan.write_text(json.dumps(value))
    output = tmp_path / "not-exported.html"
    perform, render, state = reading_commands.perform, reading_page.render_page, {"read": False}
    def completed(*args, **kwargs):
        result = perform(*args, **kwargs)
        state["read"] = True
        if phase == "after-read":
            console.api.meta["private"] = True
        return result
    def rendered(*args, **kwargs):
        result = render(*args, **kwargs)
        if state["read"] and phase == "after-render":
            console.api.meta["private"] = True
        return result
    monkeypatch.setattr(reading_commands, "perform", completed)
    monkeypatch.setattr(reading_page, "render_page", rendered)
    assert run_console(console, "research-room", "--input", str(plan), "--out", str(output)) == 2
    assert state["read"] and not output.exists()
    assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


@pytest.mark.parametrize("denial", ["private", "unauthorized"])
def test_source_gap_cannot_heal_a_native_denial_before_stdout(console, records, monkeypatch, capsys, denial):
    source, state = ResearchReader._source, {}
    def unavailable(*args):
        if denial == "private":
            console.api.meta["private"] = True
        else:
            def revoke(path, value):
                raise StoreUnavailable("REMOTE_NOT_AUTHORIZED")
            console.api.corrupt_response = revoke
        try:
            item = source(*args)
            assert item["view"] is None  # The existing consumer did catch the error.
            state["caught"] = len(console.api.requests)
            return item
        finally:
            console.api.meta["private"] = False
            console.api.corrupt_response = None
    monkeypatch.setattr(ResearchReader, "_source", unavailable)
    assert run_console(console, "resume-research", "--case-id", "case") == 2
    assert len(console.api.requests) == state["caught"]  # Sticky denial makes no further requests.
    assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY" if denial == "private" else "REMOTE_NOT_AUTHORIZED")


def test_new_child_reader_shares_terminal_native_denial(console, records, monkeypatch, capsys):
    execute, state = research_commands.execute, {}
    def child_read(args, store, load, **kwargs):
        result = execute(args, store, load, **kwargs)
        console.api.meta["private"] = True
        try:
            with pytest.raises(RecordError, match="LOCAL_READ_PUBLIC_STORE_ONLY"):
                GitHubStore(store.backend.binding, store.backend.api)
            state["caught"] = len(console.api.requests)
        finally:
            console.api.meta["private"] = False
        return result
    monkeypatch.setattr(research_commands, "execute", child_read)
    assert run_console(console, "research-catalogue") == 2
    assert len(console.api.requests) == state["caught"]
    assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")


@pytest.mark.parametrize("revoke", [False, True])
def test_local_attachment_bytes_follow_the_same_final_native_gate(console, records, tmp_path,
                                                                 monkeypatch, capsys, revoke):
    path = tmp_path / "artifact-ref.json"
    path.write_text(json.dumps(asdict(records.artifact)))
    read = ResearchArtifacts.read
    def restored(*args, **kwargs):
        result = read(*args, **kwargs)
        if revoke:
            console.api.meta["private"] = True
        return result
    monkeypatch.setattr(ResearchArtifacts, "read", restored)
    assert run_console(console, "read-artifact", "--ref", str(path)) == (2 if revoke else 0)
    if revoke:
        assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    else:
        captured = capsys.readouterr()
        assert base64.b64decode(json.loads(captured.out)["data_base64"]) == records.attachment
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


def test_shared_native_adapter_still_permits_its_opted_in_private_upgrade(console):
    console.api.meta["private"] = True
    original = GitHubStore(console.api.binding, console.wire)
    assert original.live_visibility() == "private"
    assert original.binding.private_upgrade_allowed
