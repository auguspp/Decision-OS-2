"""The local console may only ever run reads, and only with a read-only credential."""
import base64
from collections import Counter
from dataclasses import asdict, replace
from http.client import IncompleteRead
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_kernel.application import hosted_handoff
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_catalogue import ResearchCatalogue
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, StoreUnavailable, digest
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces import (
    auction_commands, auction_next_session_commands, close_structure_commands, delivery_commands,
    decision_preparation_commands, local_read, odds_commands, outcome_review_commands,
    reading_commands, reading_page, research_commands, store_cli,
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
    assert record["tree_inventory"] == {"schema": "local-git-tree-v1", "answered": 0,
                                        "declined": 0, "mirror": "absent"}
    assert record["blob_content"] == {"schema": "local-git-blob-v1", "answered": 0,
                                      "declined": 0, "mirror": "absent"}


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


@pytest.fixture
def catalogue_records(console, records):
    """Four independent cards exercise repeated live reads, without a scale fixture."""
    store = VersionedStore(console.api.git)
    references = {"case": records.research}
    for index in range(1, 4):
        case = "case-" + str(index)
        source = store.put_source(case + "-source", SourceInput(
            case + "-source", "content", "Synthetic catalogue evidence " + str(index),
            "synthetic:catalogue", "Independent synthetic evidence " + str(index)))
        references[case] = store.submit_revision(case + "-research", RevisionInput(
            case, "r1", "full", "Synthetic catalogue research " + str(index),
            "Engineering scope fixture", "synthetic", (source,)))
    console.api.requests.clear()
    return SimpleNamespace(store=store, references=references, snapshot=console.api.git.head())


def request_kinds(requests, repository):
    prefix = "/repos/" + repository
    kinds = Counter()
    for method, path, _ in requests:
        assert method == "GET"
        if path == prefix:
            kinds["repository"] += 1
        else:
            kind = next((label for label, suffix in (
                ("ref", "/git/ref/heads/"), ("commit", "/git/commits/"),
                ("tree", "/git/trees/"), ("blob", "/git/blobs/"),
                ("compare", "/compare/")) if path.startswith(prefix + suffix)), "other")
            kinds[kind] += 1
    return kinds


def test_catalogue_scope_preserves_four_cards_and_reduces_repeated_live_requests(
        console, catalogue_records, capsys):
    # The baseline is the existing unscoped service, using a separate operation
    # budget and the same native transport/records. No mirror is used here, so
    # fewer calls prove scope reuse independently of blob acceleration.
    selected = replace(console.api.binding, private_upgrade_allowed=False)
    baseline_wire = transport(console.api)
    baseline_api = store_cli._PublicReadAPI(baseline_wire, selected)
    baseline_backend = GitHubStore(selected, baseline_api)
    expected = ResearchCatalogue(VersionedStore(baseline_backend)).page(limit=4)
    assert baseline_backend.live_visibility() == "public"
    baseline = request_kinds(console.api.requests, selected.repository)
    assert baseline_wire._requests == sum(baseline.values())
    console.api.requests.clear()

    assert run_console(console, "research-catalogue", "--limit", "4") == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == json.loads(json.dumps(expected))
    assert result["returned"] == 4 and result["snapshot"] == catalogue_records.snapshot
    assert all(row["status"] == "readable" for row in result["items"])
    scoped = request_kinds(console.api.requests, selected.repository)
    assert console.wire._requests == sum(scoped.values()) < sum(baseline.values())
    assert 3 <= scoped["repository"] < baseline["repository"]
    assert 2 <= scoped["ref"] < baseline["ref"]
    # Immutability does not mean skipping the bodies or changing case selection.
    assert scoped["blob"] == baseline["blob"] > 0
    assert json.loads(captured.err)["local_read_receipt"]["blob_content"]["mirror"] == "absent"
    assert console.api.git.head() == catalogue_records.snapshot and console.api.writes == 0


@pytest.mark.parametrize("selection", ["flag", "environment"])
def test_console_selects_the_same_mirror_for_exact_trees_and_blobs_and_reports_actual_use(
        console, records, tmp_path, monkeypatch, capsys, selection):
    mirror = str(console.api.git.root)
    args = ["research-catalogue", "--limit", "4"]
    if selection == "flag":
        args += ["--git-mirror", mirror]
    else:
        monkeypatch.setenv(local_read.MIRROR, mirror)
    target = tmp_path / "execution-receipt.json"
    monkeypatch.setenv("DECISION_OS_READ_RECEIPT", str(target))
    original = console.wire._opener.open

    def broken_tree_response(request, timeout):
        if "/git/trees/" in request.full_url or "/git/blobs/" in request.full_url:
            raise IncompleteRead(b"synthetic incomplete object", 100)
        return original(request, timeout)

    monkeypatch.setattr(console.wire._opener, "open", broken_tree_response)
    assert run_console(console, *args) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["snapshot"] == records.snapshot
    assert any(item["reference"] == asdict(records.research) for item in result["items"])
    record = json.loads(captured.err)["local_read_receipt"]
    assert json.loads(target.read_text(encoding="utf-8")) == record
    assert record["tree_inventory"]["schema"] == "local-git-tree-v1"
    assert record["tree_inventory"]["mirror"] == record["ancestry"]["mirror"] == "local-git-graph"
    assert record["tree_inventory"]["answered"] > 0
    assert record["tree_inventory"]["declined"] == 0
    assert record["blob_content"]["schema"] == "local-git-blob-v1"
    assert record["blob_content"]["mirror"] == record["tree_inventory"]["mirror"]
    assert record["blob_content"]["answered"] > 0 and record["blob_content"]["declined"] == 0
    assert record["ancestry"]["answered"] > 0 and record["exit_code"] == 0
    assert not any("/git/trees/" in path for _, path, _ in console.api.requests)
    assert not any("/git/blobs/" in path for _, path, _ in console.api.requests)
    assert console.api.git.head() == records.snapshot and console.api.writes == 0
    assert local_read.STORE_TOKEN not in os.environ


def assert_denied(capsys, code):
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [json.loads(line) for line in captured.err.splitlines()]
    assert lines[0] == {"error": code}
    receipt = lines[1]["local_read_receipt"]
    assert receipt["exit_code"] == 2 and receipt["identity"]["write"] == "not-requested"
    return receipt


@pytest.mark.parametrize("phase", ["entry", "exit", "export"])
def test_catalogue_scope_keeps_native_admission_exit_and_export_checks(
        console, catalogue_records, monkeypatch, capsys, phase):
    execute, state = research_commands.execute, {"complete": False, "metadata": 0, "after": 0}

    def completed(*args, **kwargs):
        result = execute(*args, **kwargs)
        state["complete"] = True
        return result

    def change_visibility(path, value):
        if path == "/repos/" + console.api.binding.repository:
            state["metadata"] += 1
            if state["complete"]:
                state["after"] += 1
            # Initial Store construction has its own admission. The scope's
            # entry is the next native check; exit and export follow reading.
            refuse = (phase == "entry" and state["metadata"] == 2
                      or phase == "exit" and state["complete"] and state["after"] == 1
                      or phase == "export" and state["complete"] and state["after"] == 2)
            if refuse:
                return {**value, "private": True}
        return value

    monkeypatch.setattr(research_commands, "execute", completed)
    console.api.corrupt_response = change_visibility
    assert run_console(console, "research-catalogue", "--limit", "4") == 2
    assert state["complete"] is (phase != "entry")
    assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    assert state["metadata"] == {"entry": 2, "exit": 3, "export": 4}[phase]
    assert console.api.git.head() == catalogue_records.snapshot and console.api.writes == 0


def _revise_catalogue_case(records, case_id):
    return records.store.submit_revision(case_id + "-successor", RevisionInput(
        case_id, "r2", "full", "New synthetic research after this read began.",
        "Engineering scope fixture", "synthetic", (), records.references[case_id],
        ("Synthetic successor deliberately supplies no new evidence.",)))


def test_catalogue_normal_head_advance_and_later_cursor_keep_the_original_snapshot(
        console, catalogue_records, tmp_path, monkeypatch, capsys):
    original, state = ResearchCatalogue._item, {"advanced": False}

    def advance_after_first(self, case_id, snapshot):
        result = original(self, case_id, snapshot)
        if not state["advanced"]:
            state["advanced"] = True
            _revise_catalogue_case(catalogue_records, "case-1")
        return result

    monkeypatch.setattr(ResearchCatalogue, "_item", advance_after_first)
    mirror = str(console.api.git.root)
    assert run_console(console, "research-catalogue", "--limit", "2", "--git-mirror", mirror) == 0
    captured = capsys.readouterr()
    first = json.loads(captured.out)
    assert state["advanced"] and first["snapshot"] == catalogue_records.snapshot
    assert [row["reference"] for row in first["items"]] == [
        asdict(catalogue_records.references[case]) for case in ("case", "case-1")]
    assert console.api.git.head() != catalogue_records.snapshot
    assert json.loads(captured.err)["local_read_receipt"]["blob_content"]["answered"] > 0

    # A later call pins the original cursor even after another current pointer
    # advances. New read admission cannot silently replace it with live HEAD.
    _revise_catalogue_case(catalogue_records, "case-3")
    cursor = tmp_path / "catalogue-next.json"
    cursor.write_text(json.dumps(first["next_cursor"]), encoding="utf-8")
    assert run_console(console, "research-catalogue", "--cursor", str(cursor),
                       "--limit", "2", "--git-mirror", mirror) == 0
    captured = capsys.readouterr()
    second = json.loads(captured.out)
    assert second["snapshot"] == catalogue_records.snapshot and second["next_cursor"] is None
    assert [row["reference"] for row in second["items"]] == [
        asdict(catalogue_records.references[case]) for case in ("case-2", "case-3")]
    assert json.loads(captured.err)["local_read_receipt"]["exit_code"] == 0
    assert console.api.writes == 0


@pytest.mark.parametrize("failure", ["rewritten-branch", "permission-denied"])
def test_catalogue_scope_never_exports_after_branch_rewrite_or_native_permission_denial(
        console, catalogue_records, monkeypatch, capsys, failure):
    original, state = ResearchCatalogue._item, {"changed": False, "reads": 0}

    def invalidate_after_first(self, case_id, snapshot):
        result = original(self, case_id, snapshot)
        state["reads"] += 1
        if not state["changed"]:
            state["changed"] = True
            if failure == "rewritten-branch":
                # Only this test's synthetic remote branch is rewritten. Its
                # former snapshot must fail the final reachability check.
                console.api.git._run("update-ref", "refs/heads/records", console.api.binding.anchor,
                                    catalogue_records.snapshot)
            else:
                def deny(path, value):
                    raise StoreUnavailable("REMOTE_NOT_AUTHORIZED")
                console.api.corrupt_response = deny
        return result

    monkeypatch.setattr(ResearchCatalogue, "_item", invalidate_after_first)
    assert run_console(console, "research-catalogue", "--limit", "4",
                       "--git-mirror", str(console.api.git.root)) == 2
    assert state["changed"] and state["reads"] >= 1
    receipt = assert_denied(capsys, "COMMIT_OUTSIDE_BINDING" if failure == "rewritten-branch"
                            else "REMOTE_NOT_AUTHORIZED")
    assert receipt["blob_content"]["answered"] > 0
    assert console.api.writes == 0


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
        # A read scope may copy the frozen binding while retaining the exact
        # preflight identity. Reopening the now-invalid file would still fail.
        assert getattr(store.backend, "owner", store.backend).binding is selected[0]
        assert store.backend.binding == selected[0]
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
    command = ["research-room", "--input", str(plan), "--out", str(output)]
    mirrored = include_ai and phase == "after-render"
    if mirrored:
        command += ["--git-mirror", str(console.api.git.root)]
    assert run_console(console, *command) == 2
    assert state["read"] and not output.exists()
    receipt = assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    if mirrored:
        assert receipt["blob_content"]["answered"] > 0
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
    assert run_console(console, "read-artifact", "--ref", str(path),
                       "--git-mirror", str(console.api.git.root)) == (2 if revoke else 0)
    if revoke:
        receipt = assert_denied(capsys, "LOCAL_READ_PUBLIC_STORE_ONLY")
    else:
        captured = capsys.readouterr()
        assert base64.b64decode(json.loads(captured.out)["data_base64"]) == records.attachment
        receipt = json.loads(captured.err)["local_read_receipt"]
    assert receipt["blob_content"]["answered"] > 0
    assert not any("/git/blobs/" in request for _, request, _ in console.api.requests)
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


def test_shared_native_adapter_still_permits_its_opted_in_private_upgrade(console):
    console.api.meta["private"] = True
    original = GitHubStore(console.api.binding, console.wire)
    assert original.live_visibility() == "private"
    assert original.binding.private_upgrade_allowed
