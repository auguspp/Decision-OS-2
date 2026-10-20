"""Original native news host/API/Store over real temporary Git; only wire is synthetic."""
import json
from copy import deepcopy
from pathlib import Path
import re
from urllib.error import HTTPError

import pytest

from decision_kernel.application import news_desk_navigation as nav
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded
from decision_kernel.infrastructure import github_store as remote, runtime_publication as publication
from decision_kernel.interfaces import news_rolling_cli as host, news_delivery_cli as delivery
from test_github_transport import Response
from test_news_read_budget import sources, setup, transport
from test_news_desk_navigation import retained_window, retained_edition, retained_legacy_window
from test_news_rolling import save

CODE = "a" * 40
FILES = ("news-intake.yml", "rss-intake.yml", "news-custody.yml", "news-rolling.yml")


class NewsWire:
    def __init__(self, original, model):
        self.original, self.model = original, model
        self.calls = []
        self.main = CODE

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        prefix = remote.API_ROOT + "/repos/" + host.REPOSITORY
        path = request.full_url.removeprefix(prefix)
        if path == "/git/ref/heads/main":
            return Response(encoded({"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main}}), request.full_url)
        if path.startswith("/actions/workflows/"):
            filename = path[len("/actions/workflows/"):].split("/")[0]
            assert filename in FILES and request.method == "GET"
            wid = FILES.index(filename) + 1
            if path.endswith(filename):
                value = {"id": wid, "path": ".github/workflows/" + filename, "state": "active"}
            else:
                assert path.endswith("/runs?branch=main&per_page=20")
                def run(number, at, conclusion):
                    return {"id": number, "run_attempt": 1, "workflow_id": wid,
                        "head_sha": CODE, "head_branch": "main", "path": ".github/workflows/" + filename,
                        "event": "workflow_dispatch", "status": "completed", "conclusion": conclusion,
                        "created_at": at, "run_started_at": at, "updated_at": at,
                        "head_repository": {"id": 1420041744, "full_name": host.REPOSITORY}}
                value = {"total_count": 2, "workflow_runs": [
                    run(wid * 10 + 2, "2026-10-19T10:00:00Z", "failure"),
                    run(wid * 10 + 1, "2026-10-18T10:00:00Z", "success")]}
            return Response(encoded(value), request.full_url)
        return self.original.open(request, timeout)


@pytest.fixture
def native(sources, monkeypatch, tmp_path):
    model, store, _, _, _ = sources
    ref, batch, index = retained_window(store)
    edition, doc = retained_edition(store, ref)
    api, inputs = setup(monkeypatch, tmp_path, model, {}, saving=False, operation="latest-desk")
    inputs.update(code_sha="", plan="", public_metadata_custody_confirmed="true")
    (tmp_path / "event.json").write_bytes(encoded({"inputs": inputs}))
    wire = NewsWire(api._opener, model)
    api._opener = wire
    monkeypatch.setattr(delivery, "run_command", lambda *a, **k: pytest.fail("latest-desk cannot write or dispatch"))
    return model, store, api, inputs, wire, ref, batch, edition, doc


def event(tmp_path, inputs):
    (tmp_path / "event.json").write_bytes(encoded({"inputs": inputs}))


def only_failure(out):
    assert {p.name for p in out.iterdir()} == {"desk-report.json"}
    report = json.loads((out / "desk-report.json").read_bytes())
    assert report["status"] == "failed-no-content-exported" and report["write"] == "not-requested"
    return report


def test_normal_entry_uses_native_code_and_original_readers_with_same_html_json_summary(native, tmp_path):
    model, store, api, inputs, wire, _, _, _, doc = native
    before = store.backend.head()
    out = tmp_path / "latest"
    assert host.main(["build", "--out", str(out)]) == 0
    report = json.loads((out / "desk-report.json").read_bytes())
    navigation = json.loads((out / "navigation.json").read_bytes())
    html = (out / "news-desk.html").read_text()
    embedded = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert embedded["navigation"] == navigation and embedded["edition"] == doc
    assert json.loads((out / "edition.json").read_bytes()) == doc
    assert "older-than-24h" in (out / "news-summary.md").read_text()
    assert report["code_sha"] == CODE and inputs["code_sha"] == ""
    assert report["source_http_requests"] == model.writes == 0 and store.backend.head() == before
    assert api._requests == len(wire.calls) < remote.MAX_REQUESTS == 512
    runs = navigation["native_runs"]["workflows"]
    assert len(runs) == 4 and all(row["latest_observed_attempt"]["conclusion"] == "failure" for row in runs)
    assert all(row["business_content_verified"] is False for row in runs)
    assert report["source_reads"]["reused_source_reads"] > 0


@pytest.mark.parametrize("changes", [{"save": "true"}, {"submission_id": "new-key"},
    {"expected_head": "b" * 40}, {"code_sha": "b" * 40}, {"plan": '{"window":{}}'}])
def test_latest_entry_refuses_write_or_foreign_plan_before_any_native_request(native, tmp_path, changes):
    model, _, api, inputs, _, *_ = native
    event(tmp_path, {**inputs, **changes})
    out = tmp_path / "invalid"
    assert host.main(["build", "--out", str(out)]) == 2
    assert not out.exists() and api._requests == model.writes == 0


def test_unconfirmed_metadata_does_not_start_reading_or_export(native, tmp_path):
    _, _, api, inputs, _, *_ = native
    event(tmp_path, {**inputs, "public_metadata_custody_confirmed": "false"})
    out = tmp_path / "no-rights"
    assert host.main(["build", "--out", str(out)]) == 2
    assert api._requests == 0
    assert only_failure(out)["error_code"] == "NEWS_DESK_METADATA_PUBLICATION_NOT_CONFIRMED"


@pytest.mark.parametrize("location", ["batch-replay", "edition-discovery"])
@pytest.mark.parametrize("http_status,code", [(403, "REMOTE_NOT_AUTHORIZED"), (429, "REMOTE_RATE_LIMIT")])
def test_native_http_fatal_remains_original_exception_and_stops_all_later_reads(native, tmp_path, location, http_status, code):
    model, _, api, _, wire, _, batch, edition, _ = native
    ref, suffix = (batch.manifest, "content.txt") if location == "batch-replay" else (edition, "content.bin")
    blob = model.git._run("rev-parse", ref.commit + ":" + ref.path.replace("manifest.json", suffix)).stdout.decode().strip()
    original_open = wire.open
    failed, after, errors = [], [], []
    def once(request, timeout):
        if failed:
            after.append(request.full_url)
        if not failed and request.full_url.endswith("/git/blobs/" + blob):
            failed.append(request.full_url)
            raise HTTPError(request.full_url, http_status, "Synthetic native denial", {}, None)
        return original_open(request, timeout)
    wire.open = once
    original_request = api.request
    def observed(*args, **kwargs):
        try:
            return original_request(*args, **kwargs)
        except StoreUnavailable as error:
            errors.append(error)
            raise
    api.request = observed
    out = tmp_path / "denied"
    with pytest.raises(StoreUnavailable, match=code) as actual:
        host.build_on_host(out)
    assert actual.value is errors[0] and len(errors) == 1
    assert failed and after == [] and model.writes == 0
    assert only_failure(out)["error_code"] == code


def test_original_512_budget_is_not_reset_for_discovery_replay_or_final_check(native, tmp_path, monkeypatch):
    model, _, api, _, wire, *_ = native
    original = nav.news_rolling_index.restore_index
    at_limit = []
    def consumed(*args, **kwargs):
        api._requests = remote.MAX_REQUESTS
        at_limit.append(len(wire.calls))
        return original(*args, **kwargs)
    monkeypatch.setattr(nav.news_rolling_index, "restore_index", consumed)
    out = tmp_path / "budget"
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        host.build_on_host(out)
    assert api._requests == 513 and len(wire.calls) == at_limit[0] and model.writes == 0
    assert only_failure(out)["error_code"] == "REMOTE_REQUEST_LIMIT"


def test_native_latch_precedes_cache_hit_and_preserves_original_error(native):
    model, _, _, _, _, first, second, _, _ = native
    api = transport(model)
    store = VersionedStore(remote.GitHubStore(model.binding, api))
    reads = host._SourceReads(store.read_source, fail_closed=True)
    good = reads(first)
    assert reads(first) == good
    original = api._opener.open
    calls = []
    def denied(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 403, "Synthetic one-time denial", {}, None)
        return original(request, timeout)
    api._opener.open = denied
    with pytest.raises(StoreUnavailable) as first_error:
        reads(second)
    count = api._requests
    with pytest.raises(StoreUnavailable) as cached_error:
        reads(first)
    assert cached_error.value is first_error.value and api._requests == count and len(calls) == 1


def test_exact_foreign_dependency_is_terminal_before_later_sources_or_final_native_reads(native, tmp_path, monkeypatch):
    model, store, api, _, wire, _, _, _, _ = native
    _, _, index = retained_window(store, "foreign-original")
    value = deepcopy(index)
    for ref in value["batch_references"]:
        ref["manifest"]["repository"] = "example/foreign"
    for row in value["source_outcomes"]:
        row["reference"]["manifest"]["repository"] = "example/foreign"
    value.pop("index_sha256")
    value["index_sha256"] = digest(encoded(value))
    # This real retained candidate wins the explicit equal-time path tie. Its
    # original index validator passes; exact source custody must reject it.
    save(store, "news-roll-000-foreign", value)
    nav.news_rolling_index.verify_index(value)
    original = VersionedStore.read_source
    failures = []
    def observe(self, ref):
        try:
            return original(self, ref)
        except RecordError as error:
            if str(error) == "FOREIGN_REPOSITORY":
                failures.append((error, len(wire.calls), api._requests))
            raise
    monkeypatch.setattr(VersionedStore, "read_source", observe)
    out = tmp_path / "foreign"
    with pytest.raises(RecordError, match="FOREIGN_REPOSITORY") as actual:
        host.build_on_host(out)
    assert len(failures) == 1 and actual.value is failures[0][0]
    assert len(wire.calls) == failures[0][1] and api._requests == failures[0][2]
    assert only_failure(out)["error_code"] == "FOREIGN_REPOSITORY" and model.writes == 0


@pytest.mark.parametrize("change", ["visibility", "main", "marker"])
def test_final_original_backend_and_code_checks_block_output_after_revocation(native, tmp_path, monkeypatch, change):
    model, _, _, _, wire, *_ = native
    original = delivery.render_page
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "visibility":
            model.meta["private"] = True
        elif change == "main":
            wire.main = "b" * 40
        else:
            model.git.commit({"store.json": b'{}'}, expected=model.git.head())
        return result
    monkeypatch.setattr(delivery, "render_page", changed)
    out = tmp_path / "revoked"
    assert host.main(["build", "--out", str(out)]) == 2
    only_failure(out)
    assert model.writes == 0


def test_existing_publication_predicate_grants_entry_and_revocation_prevents_release(native, tmp_path, monkeypatch):
    model, _, _, inputs, _, *_ = native
    # Synthetic policy names this actual synthetic Git anchor; no fake production Ref.
    monkeypatch.setattr(publication, "ANCHOR", model.binding.anchor)
    monkeypatch.setattr(publication, "STORE_ID", model.binding.store_id)
    policy_file = tmp_path / "config/public-runtime.json"
    policy = {"schema": "public-runtime-authorization-v1", "enabled": True,
        "repository": host.REPOSITORY, "repository_id": 1420041744, "branch": "records",
        "anchor": model.binding.anchor, "store_id": model.binding.store_id,
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
        "scopes": sorted(publication.SCOPES)}
    policy_file.write_bytes(encoded(policy))
    event(tmp_path, {**inputs, "public_metadata_custody_confirmed": "false"})
    good = tmp_path / "granted"
    assert host.main(["build", "--out", str(good)]) == 0
    assert json.loads((good / "desk-report.json").read_bytes())["publication_authorization"]["reading"] == policy["authorization_ref"]
    original = delivery.render_page
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        policy_file.write_bytes(encoded({**policy, "enabled": False}))
        return result
    monkeypatch.setattr(delivery, "render_page", revoked)
    bad = tmp_path / "grant-revoked"
    assert host.main(["build", "--out", str(bad)]) == 2
    assert only_failure(bad)["error_code"] == "NEWS_DESK_METADATA_PUBLICATION_NOT_CONFIRMED"


def test_latest_requires_feedback_permission_for_retained_choices_and_exports_no_unapproved_body(native, tmp_path):
    _, store, _, _, _, ref, *_ = native
    retained_edition(store, ref, "selected-feedback", cutoff="2026-10-19T12:00:00Z", attention=True)
    out = tmp_path / "feedback-blocked"
    assert host.main(["build", "--out", str(out)]) == 0
    navigation = json.loads((out / "navigation.json").read_bytes())
    assert navigation["status"] == "partial" and not (out / "edition.json").exists()
    assert "合成选择理由" not in (out / "news-desk.html").read_text()
    assert navigation["latest_saved_edition"]["status"] == "selected-edition-unavailable"


def test_workflow_keeps_original_default_and_advanced_contract_with_readonly_latest_entry():
    text = (Path(__file__).parents[1] / ".github/workflows/news-rolling.yml").read_text()
    assert "options: [build, read, desk, read-desk, latest-desk]" in text and "default: build" in text
    assert "inputs.operation != 'latest-desk'" in text and "schedule:" not in text
    assert 'test "$SAVE_REQUESTED" = false' in text
    assert 'test "$GITHUB_SHA" = "$EXPECTED_CODE"' in text
    assert "r['run_attempt']==1" in text and "r['event']=='push'" in text
    assert '"$GITHUB_STEP_SUMMARY"' in text


@pytest.mark.parametrize("selected", ["index", "legacy"])
def test_native_mixed_representations_keep_scope_and_only_export_replayed_index(native, tmp_path, selected):
    model, store, api, inputs, wire, *_ = native
    event(tmp_path, {**inputs, "plan": "{}"})
    first, _, _ = retained_legacy_window(store, "legacy-first", as_of="2026-10-16T11:00:00Z")
    retained_legacy_window(store, "legacy-second", as_of="2026-10-16T12:00:00Z", predecessor=first)
    newest, _, index = retained_window(store, "new-index", as_of="2026-10-19T12:00:00Z",
                                       observed="2026-10-19T10:00:00Z", capture=True)
    if selected == "legacy":
        newest, _, _ = retained_legacy_window(store, "newest-inline", as_of="2026-10-19T13:00:00Z",
                                             observed="2026-10-19T11:00:00Z")
    before = store.backend.head()
    out = tmp_path / "mixed-representations"
    assert host.main(["build", "--out", str(out)]) == 0
    report = json.loads((out / "desk-report.json").read_bytes())
    navigation = json.loads((out / "navigation.json").read_bytes())
    html = (out / "news-desk.html").read_text()
    embedded = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert embedded["navigation"] == navigation
    windows = 4 if selected == "index" else 5
    assert navigation["latest_window"]["total_candidates"] == navigation["latest_window"]["valid_candidates"] == windows
    assert navigation["history"]["total_candidates"] == windows + 1 and navigation["history"]["invalid_candidates"] == 0
    assert navigation["latest_window"]["reference"]["manifest"]["path"] == newest.manifest.path
    old = [r for r in navigation["history"]["items"] if r.get("read_status") == "legacy-offline-reader-only"]
    assert len(old) == (2 if selected == "index" else 3) and all(r["open_request"] is None for r in old)
    assert "原离线入口可读" in html
    files = {p.name for p in out.iterdir()}
    base = {"desk-report.json", "plan.json", "navigation.json", "news-desk.html", "news-summary.md"}
    if selected == "index":
        assert report["content_status"] == "readable"
        assert navigation["display"]["status"] == "retained-window-preview-not-saved"
        assert files == base | {"edition.json", "news-brief.md", "quick-candidates.json"}
        edition = json.loads((out / "edition.json").read_bytes())
        assert edition == embedded["edition"] and embedded["saved_reference"] is None
        assert edition["attention_state"] == edition["plan"]["attention"] == [] and edition["plan"]["baseline"] is None
        assert edition["plan"]["cutoff"] == index["as_of"] and edition["window"]["index_sha256"] == index["index_sha256"]
        assert (out / "news-brief.md").read_bytes() == edition["brief_markdown"].encode()
    else:
        assert report["content_status"] == "partial" and navigation["latest_window"]["status"] == "selected-not-readable"
        assert files == base and embedded["edition"] is None and embedded["articles"] == []
        assert embedded["saved_reference"] is None and navigation["display"]["reference"] is None
    assert report["write"] == "not-requested" and report["source_http_requests"] == model.writes == 0
    assert store.backend.head() == before and report["binding_rechecked"] is True
    assert report["source_reads"]["byte_limit"] == host.MAX_SOURCE_CACHE_BYTES == 8388608
    assert report["source_reads"]["serialized_bytes"] <= 8388608
    if selected == "index":
        assert report["source_reads"]["reused_source_reads"] > 0
    # A selected legacy window has no Desk replay; zero cache hits is valid.
    assert report["storage_api_requests"] == api._requests == len(wire.calls) <= remote.MAX_REQUESTS == 512


def test_legacy_discovery_native_403_is_terminal_after_prior_verified_cache(native, tmp_path, monkeypatch):
    model, store, api, _, wire, *_ = native
    legacy, _, _ = retained_legacy_window(store, "zzz-legacy")
    blob = model.git._run("rev-parse", legacy.manifest.commit + ":" +
                         legacy.manifest.path.replace("manifest.json", "content.txt")).stdout.decode().strip()
    original_open, original_request, original_reader = wire.open, api.request, host._SourceReads
    failed, after, errors, readers = [], [], [], []
    def reader(*args, **kwargs):
        value = original_reader(*args, **kwargs)
        readers.append(value)
        return value
    def once(request, timeout):
        if failed:
            after.append(request.full_url)
        if not failed and request.full_url.endswith("/git/blobs/" + blob):
            failed.append(request.full_url)
            raise HTTPError(request.full_url, 403, "Synthetic native denial", {}, None)
        return original_open(request, timeout)
    def observed(*args, **kwargs):
        try:
            return original_request(*args, **kwargs)
        except StoreUnavailable as error:
            errors.append(error)
            raise
    monkeypatch.setattr(host, "_SourceReads", reader)
    wire.open, api.request = once, observed
    out = tmp_path / "legacy-denied"
    with pytest.raises(StoreUnavailable, match="REMOTE_NOT_AUTHORIZED") as actual:
        host.build_on_host(out)
    assert actual.value is errors[0] and len(errors) == len(readers) == 1
    reads = readers[0]
    assert reads.usage()["cached_references"] > 0
    previous = api._requests
    cached_ref = json.loads(next(iter(reads._cache)))
    with pytest.raises(StoreUnavailable) as cached:
        reads(cached_ref)
    assert cached.value is actual.value and api._requests == previous
    assert failed and after == [] and model.writes == 0
    assert only_failure(out)["error_code"] == "REMOTE_NOT_AUTHORIZED"
