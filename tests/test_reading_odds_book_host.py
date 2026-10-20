"""Original native Room, one API budget, composite publication and final release."""
from dataclasses import replace
import argparse
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application import odds_book
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, StoreUnavailable, digest, encoded
from decision_kernel.infrastructure.github_reading import _ReadingScope, reading_scope
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore, MAX_REQUESTS, _SourceReadScope
from decision_kernel.interfaces import research_runtime as runtime, store_cli
from test_hosted_watch_runtime import public_host
from test_news_read_budget import transport
from test_odds_book_host import SCOPES, seed
from test_reading_odds_book import AT, html_model
from test_reading_scope import retained
from test_market_room import private_native_room
from test_research_runtime import CODE, host, inputs


def payload(query=None):
    return {"plan": {"as_of": AT, "odds_book": {} if query is None else query}}


def run(model, api, output, value=None):
    return runtime.run_command(model.binding, api=api, inputs=inputs("reading-room", payload() if value is None else value),
                               code_sha=CODE, output=output, run_id="123", attempt="1")


def failed(output):
    report = json.loads((output / "report.json").read_bytes())
    assert report["status"] == "failed" and report["write"] == "not-requested" and "result" not in report
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert "SYNTHETIC_BOOK_RESEARCH_BODY" not in (output / "report.json").read_text()
    return report


@pytest.mark.parametrize("private", [False, True], ids=["authorized-public", "native-private"])
def test_native_room_keeps_exact_original_reader_api_and_deferred_outputs(public_host, tmp_path, monkeypatch, private):
    model, grant, path = public_host
    seed(model)
    if private:
        model.binding = replace(model.binding, private_upgrade_allowed=True)
        model.meta["private"] = True
        grant["enabled"] = False;path.write_bytes(encoded(grant))
    api, output = transport(model), tmp_path / "room"
    original, seen = odds_book.read_book, []
    def read(store, query, registry=None):
        assert type(store.backend) is _ReadingScope and store.backend.api is api
        assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
        seen.append(store.backend);return original(store, query, registry=registry)
    monkeypatch.setattr(odds_book, "read_book", read)
    before, writes = model.git.head(), model.writes
    value = payload({"limit": 8, "scan_limit": 32})
    report = run(model, api, output, value)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["repository_visibility"] == ("private" if private else "public")
    assert report["payload_sha256"] == digest(encoded(value))
    assert json.loads((output / "inputs.json").read_bytes()) == inputs("reading-room", value)
    assert html_model((output / "reading-room.html").read_text()) == report["result"]
    assert len(seen) == 1 and seen[0].active is False
    assert report["result"]["odds_book"]["view"]["snapshot"] == report["result"]["catalogue"]["snapshot"]
    assert 0 < api._requests == len(model.requests) < MAX_REQUESTS == 512
    assert all(method == "GET" for method, _, _ in model.requests)
    assert model.git.head() == before and model.writes == writes


@pytest.mark.parametrize("scope", SCOPES)
def test_empty_query_requires_all_five_scopes_before_room_or_input_export(public_host, tmp_path, monkeypatch, scope):
    model, grant, path = public_host
    grant["scopes"].remove(scope);path.write_bytes(encoded(grant))
    monkeypatch.setattr(ReadingRoom, "build", lambda *a, **k: pytest.fail("read before five-scope admission"))
    value = {**payload(), **{name: True for name in ("public_research_custody_confirmed",
        "public_feedback_custody_confirmed", "public_material_custody_confirmed", "public_sources_custody_confirmed")}}
    output = tmp_path / "denied"
    with pytest.raises(RecordError, match="ODDS_BOOK_PUBLICATION_NOT_CONFIRMED"):
        run(model, transport(model), output, value)
    assert not output.exists()


@pytest.mark.parametrize("scope", SCOPES)
def test_revocation_after_all_components_releases_no_inputs_html_or_result(public_host, tmp_path, monkeypatch, scope):
    model, grant, path = public_host
    seed(model)
    original, output = runtime._perform, tmp_path / "revoked"
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
        grant["scopes"].remove(scope);path.write_bytes(encoded(grant));return result
    monkeypatch.setattr(runtime, "_perform", revoke)
    with pytest.raises(RecordError, match="PUBLICATION_NOT_CONFIRMED|AUTHORIZATION_REVOKED"):
        run(model, transport(model), output)
    failed(output)


def test_private_reversal_uses_same_native_latch_and_releases_nothing(public_host, tmp_path, monkeypatch):
    model, _, _ = public_host
    seed(model)
    model.binding = replace(model.binding, private_upgrade_allowed=True);model.meta["private"] = True
    original = runtime._perform
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs);model.meta["private"] = False;return result
    monkeypatch.setattr(runtime, "_perform", reverse)
    output = tmp_path / "reverse"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        run(model, transport(model), output)
    failed(output)


@pytest.mark.parametrize("phase,status", [("catalogue", 403), ("odds", 403), ("odds", 429)])
def test_once_native_http_denial_stops_all_later_reads_and_exports(public_host, tmp_path, monkeypatch, phase, status):
    model, _, _ = public_host
    seed(model)
    api = transport(model);wire = api._opener
    state = {"armed": False, "failed": False, "after": [], "original_error": None}
    class Once:
        def open(self, request, timeout):
            if state["failed"]:state["after"].append(request.full_url)
            if state["armed"] and not state["failed"] and "/git/blobs/" in request.full_url:
                state["failed"] = True
                raise HTTPError(request.full_url, status, "Synthetic single refusal", {}, None)
            return wire.open(request, timeout)
    api._opener = Once()
    native_request = api.request
    def observed_request(*args, **kwargs):
        try:
            return native_request(*args, **kwargs)
        except StoreUnavailable as error:
            if state["original_error"] is None:
                state["original_error"] = error
            raise
    monkeypatch.setattr(api, "request", observed_request)
    owner, name = (ReadingRoom, "_catalogue") if phase == "catalogue" else (odds_book, "read_book")
    original = getattr(owner, name)
    def arm(*args, **kwargs):
        state["armed"] = True;return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, arm)
    output = tmp_path / "refused"
    with pytest.raises(StoreUnavailable, match="REMOTE_NOT_AUTHORIZED|REMOTE_RATE_LIMIT") as caught:
        run(model, api, output)
    assert caught.value is state["original_error"]
    assert state["failed"] and state["after"] == []
    failed(output)


def test_original_api_exhaustion_is_not_retried_by_later_room_components(public_host, tmp_path, monkeypatch):
    model, _, _ = public_host
    seed(model)
    api, original = transport(model), ReadingRoom._catalogue
    def exhausted(*args, **kwargs):
        api._requests = MAX_REQUESTS;return original(*args, **kwargs)
    monkeypatch.setattr(ReadingRoom, "_catalogue", exhausted)
    output = tmp_path / "exhausted"
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        run(model, api, output)
    assert api._requests == MAX_REQUESTS + 1
    failed(output)


def test_report_size_gate_runs_before_any_room_body_or_input_release(public_host, tmp_path, monkeypatch):
    model, _, _ = public_host
    seed(model)
    original = runtime._perform
    def too_large(*args, **kwargs):
        result = original(*args, **kwargs);result["synthetic_extra"] = "x" * runtime.MAX_RESULT_BYTES;return result
    monkeypatch.setattr(runtime, "_perform", too_large)
    output = tmp_path / "oversize"
    with pytest.raises(RecordError, match="COMMAND_RESULT_TOO_LARGE"):
        run(model, transport(model), output)
    failed(output)


def test_final_five_scope_gate_follows_complete_pure_html_render(public_host, tmp_path, monkeypatch):
    from decision_kernel.interfaces import reading_page
    model, grant, path = public_host
    seed(model)
    original, render = runtime._perform, reading_page.render_page
    state = {"finished": False, "revoked": False}
    def performed(*args, **kwargs):
        result = original(*args, **kwargs);state["finished"] = True;return result
    def revoke_after_render(*args, **kwargs):
        html = render(*args, **kwargs)
        if state["finished"]:
            grant["scopes"].remove("market");path.write_bytes(encoded(grant));state["revoked"] = True
        return html
    monkeypatch.setattr(runtime, "_perform", performed)
    monkeypatch.setattr(reading_page, "render_page", revoke_after_render)
    output = tmp_path / "after-render"
    with pytest.raises(RecordError, match="ODDS_BOOK_PUBLICATION_NOT_CONFIRMED"):
        run(model, transport(model), output)
    assert state["revoked"]
    failed(output)


def test_bound_main_cli_checks_publication_again_before_html_or_stdout(public_host, tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    model, grant, path = public_host
    seed(model)
    api, original = transport(model), odds_book.read_book
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs);grant["scopes"].remove("market");path.write_bytes(encoded(grant));return result
    monkeypatch.setattr(odds_book, "read_book", revoke)
    monkeypatch.setattr(GitHubAPI, "from_environment", lambda: api)
    binding, source, output = tmp_path / "binding.json", tmp_path / "room.json", tmp_path / "room.html"
    binding.write_bytes(encoded(asdict(model.binding)));source.write_bytes(encoded(payload()["plan"]))
    parser = argparse.ArgumentParser();store_cli.configure(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["store", "--binding", str(binding), "research-room", "--input", str(source), "--out", str(output)])
    assert store_cli.execute(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "PUBLICATION_NOT_CONFIRMED" in captured.err and not output.exists()


@pytest.mark.parametrize("violation", ["foreign", "path"])
def test_terminal_scope_preserves_original_error_before_cached_reads_and_exit(retained, violation):
    model, _, ref, _ = retained
    api = transport(model)
    owner = GitHubStore(model.binding, api)
    with pytest.raises(RecordError) as final:
        with reading_scope(owner, fail_closed=True) as backend:
            assert type(backend) is _ReadingScope and isinstance(backend, GitHubStore)
            assert backend.owner is owner and backend.api is api
            with backend.source_read_scope(ref.manifest.commit) as child:
                assert type(child) is _SourceReadScope and child.owner is backend
                assert child.snapshot == ref.manifest.commit and child.active
                assert child.resolve(ref.manifest) == backend.resolve(ref.manifest)
            assert not child.active
            original = backend.resolve(ref.manifest)
            with pytest.raises(MissingObject):
                backend.read(ref.manifest.commit, "source-views/missing/" + "0" * 64 + "/manifest.json")
            assert backend.resolve(ref.manifest) == original  # An ordinary gap is still local.
            with pytest.raises(RecordError) as first:
                if violation == "foreign":
                    backend.resolve(replace(ref.manifest, repository="other/repository"))
                else:
                    backend.read(ref.manifest.commit, "outside/objects.txt")
            assert str(first.value) == ("FOREIGN_REPOSITORY" if violation == "foreign" else "PATH_OUTSIDE_STORE")
            stopped = api._requests
            for action in (backend.head, lambda: backend.resolve(ref.manifest),
                           lambda: backend.paths(ref.manifest.commit, "source-views/"),
                           lambda: backend.reference(ref.manifest.commit, ref.manifest.path),
                           lambda: backend.last_change(ref.manifest.commit, ref.manifest.path),
                           backend.live_visibility):
                with pytest.raises(RecordError) as again:
                    action()
                assert again.value is first.value
            assert api._requests == stopped
    assert final.value is first.value and api._requests == stopped and not backend.active


def test_odds_room_collection_preserves_original_snapshot_reader_and_single_api(private_native_room, tmp_path):
    from test_market_room import plan
    model, api, reference, fetches = private_native_room
    before, writes = model.git.head(), model.writes
    value = {"plan": {**plan(reference, limit=1), "odds_book": {}}}
    output = tmp_path / "collection-and-book"
    report = run(model, api, output, value)
    market = report["result"]["market"]
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert market["view"]["status"] == "restored" and market["view"]["source_requests"] == 0
    assert market["view"]["view"]["schema"] == "relay-ashare-collection-view-v1"
    assert market["view"]["view"]["scope_count"] == 4 and market["reference"] == reference
    assert report["result"]["odds_book"]["view"]["snapshot"] == report["result"]["catalogue"]["snapshot"]
    assert len(fetches) == 1 and fetches[0]["api"] is api and not fetches[0]["destination"].exists()
    assert 0 < api._requests == len(model.requests) < 128 < MAX_REQUESTS
    assert html_model((output / "reading-room.html").read_text()) == report["result"]
    assert model.git.head() == before and model.writes == writes


def test_original_industry_source_scope_retains_32_commit_bound(retained):
    from decision_kernel.application.industry_operations import industry_source_scope
    from test_github_store import source
    model, local, _, _ = retained
    references = [local.put_source("scope-" + str(i), source("Synthetic scope " + str(i), source_id="scope-" + str(i)))
                  for i in range(33)]
    api = transport(model)
    owner = GitHubStore(model.binding, api)
    before, writes = model.git.head(), model.writes
    with reading_scope(owner, fail_closed=True) as backend:
        with industry_source_scope(VersionedStore(backend)) as reader:
            for index, ref in enumerate(references[:32]):
                assert reader.read_source(ref)["data"] == "Synthetic scope " + str(index)
            stopped = api._requests
            with pytest.raises(RecordError, match="^OPERATING_SOURCE_SCOPE_BOUND$"):
                reader.read_source(references[32])
            assert api._requests == stopped
    assert 0 < api._requests < MAX_REQUESTS == 512
    assert model.git.head() == before and model.writes == writes


@pytest.mark.parametrize("change", ["normal", "missing", "foreign"])
def test_native_industry_child_preflight_keeps_terminal_and_ordinary_gap_distinct(host, tmp_path, monkeypatch, change):
    from test_industry_reading import prepare, packet
    from test_industry_room import plan
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    reference, original = prepare(writer)
    if change != "normal":
        manifest = original["series"][0]["observations"][0]["manifest"]
        if change == "foreign":
            manifest["repository"] = "other/repository"
        else:
            manifest["path"] = "source-views/missing/" + "0" * 64 + "/manifest.json"
        reference = packet(writer, original)
    api = transport(host)
    before, writes = host.git.head(), host.writes
    state = {"error": None, "stopped": None, "after_child": 0, "odds_calls": 0}
    resolve, book = _SourceReadScope.resolve, odds_book.read_book
    def observed_child(self, ref):
        if state["error"] is not None:
            state["after_child"] += 1
        try:
            return resolve(self, ref)
        except RecordError as error:
            if str(error) == "FOREIGN_REPOSITORY" and state["error"] is None:
                state["error"], state["stopped"] = error, api._requests
            raise
    def observed_book(*args, **kwargs):
        state["odds_calls"] += 1
        return book(*args, **kwargs)
    monkeypatch.setattr(_SourceReadScope, "resolve", observed_child)
    monkeypatch.setattr(odds_book, "read_book", observed_book)
    value = {"plan": {**plan(reference), "odds_book": {}}}
    output = tmp_path / "industry-child"
    if change == "foreign":
        with pytest.raises(RecordError, match="^FOREIGN_REPOSITORY$") as caught:
            run(host, api, output, value)
        assert caught.value is state["error"] and api._requests == state["stopped"]
        assert state["after_child"] == state["odds_calls"] == 0
        failed(output)
    else:
        result = run(host, api, output, value)
        assert result["status"] == "completed" and state["odds_calls"] == 1
        series = result["result"]["industry"]["view"]["series"]
        points = [point for item in series for point in item["observations"]]
        assert points[-1]["status"] == "read"
        assert points[0]["status"] == ("unavailable" if change == "missing" else "read")
        if change == "missing":
            assert points[0]["reason_code"] == "OBJECT_MISSING"
        assert html_model((output / "reading-room.html").read_text()) == result["result"]
    assert host.git.head() == before and host.writes == writes
