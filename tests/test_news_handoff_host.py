"""Completed capture handoff over the real Store, CAS and bounded HTTP client.

Only the native HTTP wire and the original archive download are synthetic.
No source collection, production credentials or alternative writer is used.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, UnknownWrite, encoded
from decision_kernel.infrastructure import runtime_publication as policy
from decision_kernel.infrastructure.news_capture import capture_newsnow
from decision_kernel.interfaces import news_handoff_cli as host
from decision_kernel.interfaces.news_custody import source_from_archive
from test_github_store import GitAPI
from test_news_live_custody import fixture_archive
from test_news_read_budget import transport
from test_news_windows import newsnow

CODE = "a" * 40
END = "2026-10-16T12:00:00Z"


class NativeModel(GitAPI):
    def __init__(self, root):
        super().__init__(root)
        self.main_sha = CODE
        self.run = {"id": 9000, "head_sha": CODE, "head_branch": "main",
            "event": "workflow_dispatch", "run_attempt": 1, "status": "in_progress",
            "path": host.WORKFLOW, "name": "decision-os-news-handoff",
            "repository": {"id": host.REPOSITORY_ID}, "head_repository": {"id": host.REPOSITORY_ID},
            "created_at": "2026-10-16T12:01:00Z"}
        self.ci = {**self.run, "id": 8000, "event": "push", "status": "completed",
            "path": ".github/workflows/ci.yml", "name": "decision-os-ci", "conclusion": "success"}
        self.ci_page = {"total_count": 1, "workflow_runs": [self.ci]}
        reading = capture_newsnow(sources=("cls",), observed_at="2026-10-16T10:00:00Z", fetcher=lambda _: newsnow())
        self.raw, self.source_run, self.artifact, self.selection, self.reading = fixture_archive(reading)
        self.archive_calls = []
        self.native_hook = None

    def request(self, method, path, payload=None):
        value = None
        if method == "GET":
            if path == host.PREFIX + "/git/ref/heads/main":
                value = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main_sha}}
            elif path == host.PREFIX + "/actions/runs/9000":
                value = self.run
            elif path.startswith(host.PREFIX + "/actions/workflows/ci.yml/runs?"):
                value = self.ci_page
            elif path == host.PREFIX + "/actions/runs/123":
                value = self.source_run
            elif path == host.PREFIX + "/actions/artifacts/456":
                value = self.artifact
        if value is None:
            return super().request(method, path, payload)
        self.requests.append((method, path, deepcopy(payload)))
        value = deepcopy(value)
        return self.native_hook(path, value) if self.native_hook else value

    def archive(self, repository, artifact_id):
        assert repository == host.REPOSITORY and artifact_id == 456
        self.archive_calls.append((repository, artifact_id))
        return self.raw


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    model = NativeModel(tmp_path / "handoff.git")
    model.binding = replace(model.binding, repository=host.REPOSITORY,
        repository_id=host.REPOSITORY_ID, branch="records", store_id=policy.STORE_ID,
        visibility="public", write_enabled=False)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta.update(id=host.REPOSITORY_ID, full_name=host.REPOSITORY, private=False)
    model.git.repository = host.REPOSITORY
    # The real publication policy still verifies the exact synthetic anchor.
    monkeypatch.setattr(policy, "ANCHOR", anchor)
    grant = {"schema": "public-runtime-authorization-v1", "enabled": True,
        "repository": host.REPOSITORY, "repository_id": host.REPOSITORY_ID,
        "branch": "records", "anchor": anchor, "store_id": policy.STORE_ID,
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
        "scopes": ["sources", "brief", "reading"]}
    grant_path = tmp_path / "public-runtime.json"
    grant_path.write_bytes(encoded(grant))
    monkeypatch.setattr(policy, "AUTHORIZATION_FILE", grant_path)
    model.requests.clear()
    return model, grant, grant_path


def inputs(model, *, operation="deliver"):
    plan = {**model.selection, "as_of": END, "predecessor": None} if operation == "deliver" else {"source_run": "123"}
    env = {"GITHUB_REPOSITORY": host.REPOSITORY, "GITHUB_REPOSITORY_ID": str(host.REPOSITORY_ID),
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": host.REPOSITORY + "/" + host.WORKFLOW + "@refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "9000", "GITHUB_SHA": CODE,
        "DOS2_NEWS_HANDOFF_MODE": "write" if operation == "deliver" else "read"}
    event = {"inputs": {"operation": operation, "code_sha": CODE, "plan": encoded(plan).decode(),
        "expected_head": model.git.head() if operation == "deliver" else ""}}
    return event, env


def execute(model, out, *, operation="deliver", selected=None):
    api = transport(model)
    start = len(model.requests)
    selected = selected or host.settings(*inputs(model, operation=operation))
    result = host.execute(selected, model.binding, api, out, archive_loader=model.archive)
    assert api._requests == len(model.requests) - start < 512
    return result, json.loads((out / "handoff-report.json").read_bytes())


@pytest.mark.parametrize("already_saved", [False, True])
def test_complete_handoff_and_fresh_reconciliation_preserve_original_refs(scenario, tmp_path, already_saved):
    model, _, _ = scenario
    source = source_from_archive(model.raw, model.source_run, model.artifact, model.selection)
    original = VersionedStore(model.git).put_source("news-window-123", source) if already_saved else None
    result, report = execute(model, tmp_path / "delivered")
    assert result == 0 and report["status"] == "completed"
    assert report["confirmed_writes"] == model.writes == (2 if already_saved else 3)
    assert report["source_http_requests"] == 0 and report["archive_downloads"] == 1
    assert len(model.archive_calls) == 1 and report["observed_versions"] == 1
    assert report["research_executed"] is False and report["edition_saved"] is False
    assert report["continuous_capture_verified"] is False and report["complete_news_coverage"] is False
    assert report["human_acceptance"] == "not-confirmed" and report["binding_rechecked"]
    doc = json.loads((tmp_path / "delivered/edition.json").read_bytes())
    assert doc["plan"]["baseline"] is None and doc["plan"]["attention"] == []
    assert "research" not in doc and "industry" not in doc
    if original:
        assert report["batch_reference"] == json.loads(encoded(asdict(original)))
        assert report["stages"]["batch-reconcile"]["write"] == "reconciled-without-write"
        assert report["stages"]["batch-reconcile"]["expected_head"] == ""
    VersionedStore(model.git).put_source("later", SourceInput("later", "content", "Later", "synthetic:later", text="later"))
    head = model.git.head()
    result, recovered = execute(model, tmp_path / "reconciled", operation="reconcile")
    assert result == 0 and recovered["status"] == "completed" and recovered["confirmed_writes"] == 0
    assert recovered["archive_downloads"] == 0 and len(model.archive_calls) == 1
    assert model.git.head() == head and model.writes == (2 if already_saved else 3)
    for key in ("intent_reference", "batch_reference", "window_reference", "edition_sha256", "html_sha256"):
        assert recovered[key] == report[key]
    for name in ("edition.json", "news-desk.html", "news-brief.md", "reading.json"):
        assert (tmp_path / "delivered" / name).read_bytes() == (tmp_path / "reconciled" / name).read_bytes()


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_lost_write_ack_stops_then_reconciles_only_existing_stages(scenario, tmp_path, stage):
    model, _, _ = scenario
    def lose_ack(value):
        if model.writes == stage:
            raise TimeoutError("synthetic lost acknowledgement")
        return value
    model.after_write = lose_ack
    with pytest.raises(UnknownWrite):
        execute(model, tmp_path / "unknown")
    original = json.loads((tmp_path / "unknown/handoff-report.json").read_bytes())
    assert original["status"] == "unknown-write" and model.writes == stage
    assert original["confirmed_writes"] == stage - 1
    assert not (tmp_path / "unknown/news-desk.html").exists()
    model.after_write = None
    before = model.git.head()
    result, report = execute(model, tmp_path / "reconcile", operation="reconcile")
    assert model.writes == stage and model.git.head() == before and len(model.archive_calls) == 1
    assert report["confirmed_writes"] == 0 and report["archive_downloads"] == 0
    if stage < 3:
        assert result == 2 and report["status"] == "reconciliation-incomplete"
        assert report["missing_stage"] == ("batch" if stage == 1 else "window")
        assert not (tmp_path / "reconcile/news-desk.html").exists()
    else:
        assert result == 0 and report["status"] == "completed"
        assert (tmp_path / "reconcile/news-desk.html").is_file()


def test_missing_intent_readonly_reconciliation_never_downloads_or_writes(scenario, tmp_path):
    model, _, _ = scenario
    before = model.git.head()
    result, report = execute(model, tmp_path / "absent", operation="reconcile")
    assert result == 2 and report["missing_stage"] == "intent"
    assert model.writes == 0 and model.archive_calls == [] and model.git.head() == before


def test_second_deliver_refuses_before_archive_download_or_writes(scenario, tmp_path):
    model, _, _ = scenario
    execute(model, tmp_path / "first")
    before = model.git.head()
    with pytest.raises(RecordError, match="ALREADY_STARTED_USE_RECONCILE"):
        execute(model, tmp_path / "duplicate")
    assert model.writes == 3 and len(model.archive_calls) == 1 and model.git.head() == before


@pytest.mark.parametrize("mode", ["main-moved", "latest-ci-failed", "ci-page-incomplete", "attempt-two", "grant-revoked"])
def test_native_identity_and_latest_ci_gate_before_any_archive_or_write(scenario, tmp_path, mode):
    model, grant, path = scenario
    if mode == "main-moved": model.main_sha = "b" * 40
    if mode == "latest-ci-failed":
        model.ci_page = {"total_count": 2, "workflow_runs": [model.ci, {**model.ci, "id": 8001, "conclusion": "failure"}]}
    if mode == "ci-page-incomplete": model.ci_page["total_count"] = 2
    if mode == "attempt-two": model.run["run_attempt"] = 2
    if mode == "grant-revoked": path.write_bytes(encoded({**grant, "enabled": False}))
    with pytest.raises(RecordError): execute(model, tmp_path / "blocked")
    assert model.writes == 0 and model.archive_calls == []


@pytest.mark.parametrize("fault", ["bad-native", "bad-archive", "stale-head"])
def test_native_capture_and_archive_mismatch_cannot_start_intent(scenario, tmp_path, fault):
    model, _, _ = scenario
    selected = host.settings(*inputs(model))
    if fault == "bad-native": model.source_run["event"] = "pull_request"
    if fault == "bad-archive": model.raw = model.raw[:-1] + b"!"
    if fault == "stale-head":
        VersionedStore(model.git).put_source("other", SourceInput("other", "content", "Other", "synthetic:other", text="other"))
    with pytest.raises(RecordError): execute(model, tmp_path / "blocked", selected=selected)
    assert model.writes == 0
    assert len(model.archive_calls) == (1 if fault == "bad-archive" else 0)


def test_between_stage_head_change_does_not_refresh_expected_head(scenario, tmp_path, monkeypatch):
    model, _, _ = scenario
    original = host.run_command
    def move_after_intent(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs["output"].name == "intent-read":
            VersionedStore(model.git).put_source("concurrent", SourceInput("concurrent", "content", "Concurrent", "synthetic:concurrent", text="concurrent"))
        return value
    monkeypatch.setattr(host, "run_command", move_after_intent)
    with pytest.raises(RecordError, match="PLANNED_HEAD_CHANGED"):
        execute(model, tmp_path / "changed")
    report = json.loads((tmp_path / "changed/handoff-report.json").read_bytes())
    assert model.writes == 1 and report["confirmed_writes"] == 1
    assert not (tmp_path / "changed/news-desk.html").exists()


@pytest.mark.parametrize("change", ["private", "revoked", "main-moved"])
def test_final_revalidation_blocks_page_export(scenario, tmp_path, monkeypatch, change):
    model, grant, path = scenario
    original = host.build_edition
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "private": model.meta["private"] = True
        if change == "revoked": path.write_bytes(encoded({**grant, "enabled": False}))
        if change == "main-moved": model.main_sha = "b" * 40
        return result
    monkeypatch.setattr(host, "build_edition", changed)
    with pytest.raises(RecordError): execute(model, tmp_path / "blocked")
    assert model.writes == 3 and not (tmp_path / "blocked/news-desk.html").exists()
    assert not (tmp_path / "blocked/edition.json").exists()


@pytest.mark.parametrize("field,value", [("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_RUN_ATTEMPT", "2"),
    ("GITHUB_REPOSITORY_ID", "9"), ("GITHUB_SHA", "b" * 40), ("DOS2_NEWS_HANDOFF_MODE", "read")])
def test_entry_rejects_context_mismatch(scenario, field, value):
    event, env = inputs(scenario[0])
    with pytest.raises(RecordError): host.settings(event, {**env, field: value})


def test_reconcile_forbids_new_cutoff_predecessor_and_write_head(scenario):
    event, env = inputs(scenario[0], operation="reconcile")
    for edits in ({"expected_head": CODE}, {"plan": encoded({"source_run": "123", "as_of": END}).decode()}):
        with pytest.raises(RecordError): host.settings({"inputs": {**event["inputs"], **edits}}, env)


def test_workflow_is_manual_and_readonly_reconcile_has_no_write_credentials():
    import yaml
    flow = yaml.load((Path(__file__).parents[1] / host.WORKFLOW).read_text(), Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"} and flow["permissions"] == {}
    assert flow["concurrency"]["cancel-in-progress"] == "false"
    assert set(flow["jobs"]) == {"deliver", "reconcile"}
    for name, mode in (("deliver", "write"), ("reconcile", "read")):
        job = flow["jobs"][name]
        assert job["permissions"] == {"contents": mode, "actions": "read"}
        assert "github.run_attempt == 1" in job["if"] and "refs/heads/main" in job["if"]
        assert any(step.get("env", {}).get("DOS2_NEWS_HANDOFF_MODE") == mode for step in job["steps"])
        assert all("secrets." not in str(step) for step in job["steps"])
        upload = job["steps"][-1]
        assert upload["if"] == "always()" and upload["with"]["name"] == "dos2-news-handoff-${{ github.run_id }}-${{ github.run_attempt }}"


def test_fixed_predecessor_is_inherited_and_reconciled_without_latest_scan(scenario, tmp_path, monkeypatch):
    from decision_kernel.application import news_rolling_index as index
    from decision_kernel.domain.records import SourceRef

    model, _, _ = scenario
    store = VersionedStore(model.git)
    prior_reading = capture_newsnow(sources=("cls",), observed_at="2026-10-16T09:00:00Z",
        fetcher=lambda _: newsnow(title="合成前驱新闻", item_id="prior"))
    prior_batch = store.put_source("prior-batch", SourceInput("prior-batch", "content",
        "Synthetic predecessor", "synthetic:handoff-predecessor",
        text=encoded(prior_reading).decode(), custody="provided"))
    prior_index = index.rolling_index(store.read_source, [prior_batch], as_of="2026-10-16T10:00:00Z")
    prior_window = store.put_source("prior-window", index.index_source(prior_index, "prior-window"))
    prior_ref = json.loads(encoded(asdict(prior_window)))
    selected = host.settings(*inputs(model))
    selected["plan"]["predecessor"] = prior_ref
    monkeypatch.setattr(host.GitHubStore, "paths", lambda *a, **k: pytest.fail("no latest discovery"))

    result, delivered = execute(model, tmp_path / "with-prior", selected=selected)
    assert result == 0 and delivered["observed_versions"] == 2 and model.writes == 3
    view = json.loads((tmp_path / "with-prior/view.json").read_bytes())
    assert view["predecessor"] == {"status": "restored-exact-reference", "reference": prior_ref}
    assert json.loads(encoded(asdict(prior_batch))) in view["batch_references"]
    assert delivered["batch_reference"] in view["batch_references"]
    assert delivered["as_of"] == END and view["as_of"] == "2026-10-16T12:00:00+00:00"

    # A later retained window must not become the original handoff's predecessor
    # or cutoff when another invocation reconstructs the original page.
    later_index = index.rolling_index(store.read_source,
        [SourceRef.from_dict(delivered["batch_reference"])], as_of="2026-10-16T13:00:00Z")
    store.put_source("later-window", index.index_source(later_index, "later-window"))
    head = model.git.head()
    result, recovered = execute(model, tmp_path / "prior-reconciled", operation="reconcile")
    assert result == 0 and recovered["as_of"] == END and model.git.head() == head
    assert recovered["confirmed_writes"] == 0 and model.writes == 3 and len(model.archive_calls) == 1
    assert recovered["window_reference"] == delivered["window_reference"]
    for name in ("view.json", "reading.json", "desk-plan.json", "edition.json", "news-desk.html"):
        assert (tmp_path / "with-prior" / name).read_bytes() == (tmp_path / "prior-reconciled" / name).read_bytes()


@pytest.mark.parametrize("fault,error_code", [(403, "REMOTE_NOT_AUTHORIZED"),
    (429, "REMOTE_RATE_LIMIT"), ("budget", "REMOTE_REQUEST_LIMIT")])
def test_rolling_native_failure_is_terminal_before_window_write_or_export(
        scenario, tmp_path, monkeypatch, fault, error_code):
    from io import BytesIO
    from urllib.error import HTTPError
    from decision_kernel.domain.records import MissingObject, StoreUnavailable
    from decision_kernel.infrastructure.github_store import MAX_REQUESTS

    model, _, _ = scenario
    api = transport(model)
    selected = host.settings(*inputs(model))
    original_roll, original_wire = host.rolling_index, api._opener.open
    armed, failed_wire_calls = False, []

    def enter_rolling(*args, **kwargs):
        nonlocal armed
        armed = True
        if fault == "budget":
            api._requests = MAX_REQUESTS
        return original_roll(*args, **kwargs)

    def wire(request, timeout):
        if armed:
            failed_wire_calls.append(request.method)
            assert request.method == "GET"
            raise HTTPError(request.full_url, fault, "synthetic read failure", {}, BytesIO(b"synthetic"))
        return original_wire(request, timeout)

    monkeypatch.setattr(host, "rolling_index", enter_rolling)
    monkeypatch.setattr(api._opener, "open", wire)
    out = tmp_path / "rolling-blocked"
    with pytest.raises(StoreUnavailable, match=error_code):
        host.execute(selected, model.binding, api, out, archive_loader=model.archive)
    report = json.loads((out / "handoff-report.json").read_bytes())
    assert report["status"] == "failed" and report["error_code"] == error_code
    assert report["confirmed_writes"] == model.writes == 2
    assert report["source_http_requests"] == 0 and len(model.archive_calls) == 1
    assert not (out / "window-write").exists() and not (out / "news-desk.html").exists()
    assert not (out / "view.json").exists() and not (out / "edition.json").exists()
    with pytest.raises(MissingObject):
        model.git.read(model.git.head(), "submissions/news-handoff-123-window.json")
    # The same real bounded client classifies the wire error, and the source
    # reader's terminal latch prevents a swallowed gap from becoming a write.
    assert failed_wire_calls == ([] if fault == "budget" else ["GET"])
    if fault == "budget":
        assert api._requests == MAX_REQUESTS + 1 == report["storage_api_requests"]


@pytest.mark.parametrize("all_failed", [False, True])
def test_partial_and_all_failed_capture_keep_failure_coverage_without_recapture(scenario, tmp_path, all_failed):
    model, _, _ = scenario
    source_calls = []

    def captured(source):
        source_calls.append(source)
        if all_failed or source == "jin10":
            raise RecordError("NEWSNOW_HTTP_503")
        return newsnow(source)

    reading = capture_newsnow(sources=("cls", "jin10"), observed_at="2026-10-16T10:00:00Z", fetcher=captured)
    model.raw, model.source_run, model.artifact, model.selection, model.reading = fixture_archive(reading)
    result, report = execute(model, tmp_path / "failed-sources")
    expected_failed = 2 if all_failed else 1
    assert result == 0 and report["status"] == "completed" and model.writes == 3
    assert report["observed_versions"] == (0 if all_failed else 1)
    assert report["coverage"]["failed_source_windows"] == expected_failed
    assert report["coverage"]["complete_news_coverage"] is False
    page = json.loads((tmp_path / "failed-sources/reading.json").read_bytes())
    outcomes = [row["outcome"] for row in page["source_outcomes"]]
    assert len(outcomes) == 2 and sum(row["status"] == "unavailable" for row in outcomes) == expected_failed
    assert all(row["capture"]["error_code"] == "NEWSNOW_HTTP_503" for row in outcomes
               if row["status"] == "unavailable")
    assert page["coverage"]["unlocated_source_windows"] == 0
    assert page["selected_count"] == (0 if all_failed else 1)
    assert source_calls == ["cls", "jin10"] and report["source_http_requests"] == 0
    assert len(model.archive_calls) == 1 and (tmp_path / "failed-sources/news-desk.html").is_file()


@pytest.mark.parametrize("kind", ["legacy-without-receipt", "relay-with-receipt"])
def test_handoff_refuses_non_native_newsnow_capture_before_intent_write(scenario, tmp_path, kind):
    from decision_kernel.infrastructure.news_capture import capture_relay_news
    from test_news_windows import relay

    model, _, _ = scenario
    if kind == "legacy-without-receipt":
        fixture = fixture_archive()
        assert "capture" not in fixture[-1]["source_outcomes"][0]
    else:
        class SyntheticRelay:
            last_response_bytes = relay()

            def _get_json(self, *args, **kwargs):
                return json.loads(self.last_response_bytes)

        reading = capture_relay_news("news", source="cls", start="2026-10-16 09:00:00",
            end="2026-10-16 10:00:00", observed_at="2026-10-16T10:00:00Z", transport=SyntheticRelay())
        assert reading["source_outcomes"][0]["capture"]["request"]["family"] == "relay"
        fixture = fixture_archive(reading)
    model.raw, model.source_run, model.artifact, model.selection, model.reading = fixture
    head = model.git.head()
    out = tmp_path / "capture-rejected"
    with pytest.raises(RecordError, match="NEWS_HANDOFF_NEWSNOW_ONLY"):
        execute(model, out)
    assert model.writes == 0 and model.git.head() == head and len(model.archive_calls) == 1
    assert not (out / "intent-write").exists() and not (out / "news-desk.html").exists()
