"""Native admission and finite FTShare execution with real retained source controls."""
from copy import copy, deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from decision_kernel.application.ftshare_collection import latest_ftshare_reference, restore_ftshare_collection
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, digest, encoded
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubRateLimits
from decision_kernel.interfaces import ftshare_collection_cli as cli
from test_ftshare_collection import (
    _capture, _setup, adopt_kernel, kernel_adoption_fixture, single_capture, STAMP,
)
from test_market_collection_runtime import InventoriedBackend
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY

SHA = "a" * 40
STUDY_SHA = "b" * 40


def config():
    return {"schema": cli.SCHEMA, "collection_id": "ftshare-cn-eod", "generation": "g20261016",
        "legacy_reference": deepcopy(cli.BASE_REFERENCE), "qualification": {
            "run_id": 7001, "artifact_id": 9001, "sha256": "c" * 64, "code_sha": STUDY_SHA},
        "kernel_adoption": None}


def runtime_input(tmp_path):
    event = {"inputs": {"operation": "ftshare-sync", "code_sha": SHA,
        "selection": json.dumps({k: config()[k] for k in ("collection_id", "generation")}),
        "work_units": "2", "access_and_custody_confirmed": True, "public_rights_confirmed": False}}
    env = {"GITHUB_REPOSITORY": cli.REPO, "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_WORKFLOW_REF": cli.REPO + "/" + cli.WORKFLOW + "@refs/heads/main",
        "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "8001", "GITHUB_RUN_ATTEMPT": "1", "RUNNER_TEMP": str(tmp_path),
        "DECISION_OS_STORE_TOKEN": "synthetic-store-token", "FTSHARE_API_KEY": "synthetic-source-secret"}
    return event, env


class NativeAPI(GitHubAPI):
    def __init__(self, token="synthetic-store-token"):
        super().__init__(token)
        self._requests = 0
        self.rate_limits = GitHubRateLimits()
        self.calls = []
        self.meta = {"id": 1420041744, "full_name": cli.REPO, "private": True,
            "archived": False, "default_branch": "main"}
        self.main = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": SHA}}
        # Default: a main that no longer reaches this run's code. A test that
        # means a plain merge sets this to an ahead verdict instead.
        self.ancestry = {"status": "diverged", "merge_base_commit": {"sha": "b" * 40}}
        common = {"head_branch": "main", "event": "workflow_dispatch", "run_attempt": 1,
            "path": cli.WORKFLOW, "name": "decision-os-ashare-data",
            "repository": {"id": 1420041744}, "head_repository": {"id": 1420041744}}
        self.run = {**common, "id": 8001, "head_sha": SHA, "status": "in_progress"}
        self.study = {**common, "id": 7001, "head_sha": STUDY_SHA, "status": "completed",
            "conclusion": "success", "display_title": cli.QUALIFICATION_TITLE}
        self.ci = {"workflow_runs": [{"id": 6001, "head_sha": SHA, "head_branch": "main", "event": "push",
            "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success"}]}
        self.artifact = {"id": 9001, "name": "dos2-ftshare-check-continuation-7001-1", "expired": False,
            "digest": "sha256:" + "c" * 64,
            "workflow_run": {"id": 7001, "head_sha": STUDY_SHA, "head_branch": "main"}}

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None
        self.calls.append(path)
        self._requests += 1
        self.rate_limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": str(5000 - self._requests), "x-ratelimit-reset": "2000000000"})
        if path == cli.PREFIX:
            return deepcopy(self.meta)
        if path.endswith("/git/ref/heads/main"):
            return deepcopy(self.main)
        if "/compare/" in path:
            return deepcopy(self.ancestry)
        if path.endswith("/actions/runs/8001"):
            return deepcopy(self.run)
        if path.endswith("/actions/runs/7001"):
            return deepcopy(self.study)
        if "/actions/workflows/ci.yml/runs?" in path:
            return deepcopy(self.ci)
        if path.endswith("/actions/artifacts/9001"):
            return deepcopy(self.artifact)
        if hasattr(self, "store_model"):
            return self.store_model.request(method, path, payload)
        raise AssertionError(path)


@pytest.mark.parametrize("where,key,value", [
    ("env", "GITHUB_WORKFLOW_REF", "other/workflow@main"),
    ("env", "GITHUB_RUN_ATTEMPT", "2"),
    ("env", "GITHUB_SHA", "d" * 40),
    ("inputs", "operation", "sync"),
    ("inputs", "work_units", "33"),
    ("inputs", "access_and_custody_confirmed", 1),
    ("inputs", "public_rights_confirmed", 0),
    ("inputs", "repair_request", "{}"),
    ("inputs", "selection", '{"collection_id":"ftshare-cn-eod","generation":"g20261016","since":0}'),
])
def test_host_scope_and_input_cannot_override_the_fixed_collection(tmp_path, where, key, value):
    event, env = runtime_input(tmp_path)
    (env if where == "env" else event["inputs"])[key] = value
    with pytest.raises(RecordError):
        cli.settings(event, env, config())


@pytest.mark.parametrize("family,key,value", [
    ("study", "head_sha", SHA), ("study", "path", ".github/workflows/other.yml"),
    ("study", "conclusion", "failure"), ("study", "run_attempt", 2),
    ("study", "display_title", "FTShare daily check 20261018"),
    ("artifact", "digest", "sha256:" + "d" * 64), ("artifact", "expired", True),
    ("artifact", "name", "dos2-ftshare-check-7001-1"),
    ("artifact", "name", "copied-artifact"), ("run", "head_branch", "feature"),
    ("meta", "private", False),
])
def test_native_execution_and_original_study_must_match(tmp_path, family, key, value):
    event, env = runtime_input(tmp_path)
    api = NativeAPI()
    getattr(api, family)[key] = value
    with pytest.raises(RecordError):
        cli.native_ready(api, cli.settings(event, env, config()), config())


def test_native_admission_authenticates_exact_artifact_and_latest_main_ci(tmp_path):
    event, env = runtime_input(tmp_path)
    selected = cli.settings(event, env, config())
    api = NativeAPI()
    assert cli.native_ready(api, selected, config())["artifact_digest_verified"] is True
    api.ci["workflow_runs"][0]["conclusion"] = "failure"
    with pytest.raises(RecordError, match="MAIN_CI"):
        cli.native_ready(api, selected, config())


def test_native_status_does_not_fetch_the_expired_or_deleted_source_study(tmp_path):
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="ftshare-status", work_units="1", access_and_custody_confirmed=False)
    selected = cli.settings(event, env, config())
    api = NativeAPI()
    api.study = api.artifact = None
    proof = cli.native_ready(api, selected, config())
    assert proof == {"main_ci_run": 6001, "source_qualification": "not-required-for-status"}
    assert len(api.calls) == 4
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in api.calls)


@pytest.mark.parametrize("gate", ["private", "main", "current-run", "ci"])
def test_native_status_keeps_private_main_current_run_and_ci_gates(tmp_path, gate):
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="ftshare-status", work_units="1", access_and_custody_confirmed=False)
    api = NativeAPI()
    if gate == "private":
        api.meta["private"] = False
    elif gate == "main":
        api.main["object"]["sha"] = "d" * 40
    elif gate == "current-run":
        api.run["head_sha"] = "d" * 40
    else:
        api.ci["workflow_runs"][0]["conclusion"] = "failure"
    with pytest.raises(RecordError):
        cli.native_ready(api, cli.settings(event, env, config()), config())
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in api.calls)


def test_native_factory_shares_quota_and_uses_only_same_readonly_binding(tmp_path, monkeypatch):
    from test_github_store import GitAPI
    event, env = runtime_input(tmp_path)
    selected = cli.settings(event, env, config())
    model = GitAPI(tmp_path / "records.git")
    model.binding = replace(model.binding, repository=cli.REPO, repository_id=1420041744, branch="records")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta = NativeAPI().meta
    apis = []
    def factory(token):
        api = NativeAPI(token)
        api.store_model = model
        apis.append(api)
        return api
    monkeypatch.setattr(cli, "GitHubAPI", factory)
    binding = model.binding
    open_unit = cli._open_remote(binding, env[cli.TOKEN_ENV], selected, config())
    first, fresh, api = open_unit(True)
    second, again, other = open_unit(True)
    assert api is not other and api.rate_limits is other.rate_limits
    assert first.backend.api is api and fresh().backend.api is api
    assert second.backend.binding == binding and again().backend.binding == replace(binding, write_enabled=False)
    assert all(0 < item._requests < 240 for item in apis)
    status = cli._open_remote(binding, env[cli.TOKEN_ENV], {**selected, "operation": "ftshare-status"}, config())
    with pytest.raises(RecordError, match="FTSHARE_RUNTIME_OPERATION_BINDING"):
        status(True)
    assert len(apis) == 2
    readonly, _, status_api = status(False)
    assert readonly.backend.binding == replace(binding, write_enabled=False)
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in status_api.calls)


def test_calendar_selection_accepts_only_exact_same_store_references(tmp_path):
    event, env = runtime_input(tmp_path)
    selection = json.loads(event["inputs"]["selection"])
    selection["calendar_references"] = {v: deepcopy(cli.BASE_REFERENCE) for v in ("SSE", "SZSE")}
    event["inputs"]["selection"] = json.dumps(selection)
    assert cli.settings(event, env, config())["calendar_references"] == selection["calendar_references"]
    selection["calendar_references"]["SZSE"]["manifest"]["repository"] = "other/records"
    event["inputs"]["selection"] = json.dumps(selection)
    with pytest.raises(RecordError):
        cli.settings(event, env, config())
    bad = config()
    bad["qualification"]["run_id"] = True
    with pytest.raises(RecordError):
        cli.configuration(bad)


class Backend(InventoriedBackend):
    repository = MemoryBackend.repository


class Host:
    def __init__(self):
        original, _, _, self.old_ref, self.old, self.plan = _setup(quarantine=True)
        self.shared = original.shared
        self.apis = []
        self.after_commit = self.read_error = None
        self.commits = []
        self.limits = GitHubRateLimits()
        self.limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "5000", "x-ratelimit-reset": "2000000000"})
        self.initial_api_count = 0
        self.config = {k: self.plan["plan"][k] for k in ("collection_id", "generation", "legacy_reference")}

    def open(self, write):
        api = SimpleNamespace(_requests=self.initial_api_count, rate_limits=self.limits)
        self.apis.append(api)
        store = VersionedStore(Backend(self.shared, api, write=write, host=self))
        return store, lambda: VersionedStore(Backend(self.shared, api, host=self)), api

    def read(self):
        return VersionedStore(Backend(self.shared, SimpleNamespace(_requests=0), host=self))

    def latest(self):
        ref = latest_ftshare_reference(self.read(), REGISTRY, collection_id=self.config["collection_id"],
            generation=self.config["generation"])
        return None if ref is None else restore_ftshare_collection(ref, REGISTRY, self.read())


class Provider:
    def __init__(self, host, *, fail=False, observed_zero=False, after=None, transport=None):
        self.host, self.fail, self.observed_zero, self.after = host, fail, observed_zero, after
        self.transport = deepcopy(transport or {})
        self.requests = 0
        self.last_response_bytes = self.last_response_meta = self.last_error_meta = None
        self.last_retry_after = ("missing", None)
        self.arguments = []

    def validate_credential(self):
        return "synthetic-source-secret"

    def candlesticks_batch(self, symbols, *, since, until, adjust_kind):
        restored = self.host.latest()
        slot = adjust_kind + ":000"
        assert restored["collection"]["units"][slot]["status"] == "in-flight"
        assert restored["collection"]["reserved_requests"] >= self.requests + 1
        self.requests += 1
        self.arguments.append((symbols, since, until, adjust_kind))
        if self.fail:
            if isinstance(self.fail, BaseException):
                raise self.fail
            status = self.fail if type(self.fail) is int else 429
            self.last_error_meta = {"http_status": status,
                "classification": "auth-or-entitlement" if status in (401, 403) else "rate-limit",
                **self.transport}
            raise RecordError("FTSHARE_HTTP_" + str(status))
        capture = _capture(restored["collection"], slot)
        self.last_response_bytes = capture["body_utf8"].encode()
        self.last_response_meta = {"http_status": 200, "wire_bytes": len(self.last_response_bytes),
            "wire_sha256": capture["wire_sha256"], "rate_limits": {}, **self.transport}
        if self.observed_zero:
            self.last_response_meta["rate_limits"] = {"key_remaining": {"state": "valid", "remaining": 0}}
        self.last_retry_after = (self.last_response_meta.get("retry_after_state", "missing"),
                                 self.last_response_meta.get("retry_after_seconds"))
        if self.after:
            self.after()
        return json.loads(self.last_response_bytes)


def execute(host, provider=None, *, operation="ftshare-sync", **kwargs):
    return cli.run_collection(operation=operation, config=host.config, registry=REGISTRY,
        open_unit=host.open, access_confirmed=True, custody_authorized=True,
        client_factory=(lambda: provider) if provider is not None else
            (lambda: (_ for _ in ()).throw(AssertionError("source client must not be constructed"))),
        clock=lambda: STAMP, **{"sleep": lambda seconds: None, **kwargs})


def test_persisted_reservation_precedes_http_and_restart_keeps_budget_and_old_root():
    host = Host()
    first = Provider(host)
    partial = execute(host, first)
    assert partial["receipt"]["status"] == "partial" and first.requests == 1
    assert partial["receipt"]["reserved_requests"] == 1 and partial["receipt"]["next_run_can_resume"] is True
    assert first.arguments[0][-1] == "none"
    second = Provider(host)
    complete = execute(host, second)
    assert complete["receipt"]["status"] == "complete" and second.requests == 1
    assert complete["receipt"]["source_requests"] == 1 and complete["receipt"]["reserved_requests"] == 2
    assert second.arguments[0][-1] == "forward"
    assert restore_market_collection(host.old_ref, REGISTRY, host.read())["collection"] == host.old
    data = encoded(complete["receipt"])
    assert b"source-views/" not in data and b"600001.SH" not in data and b"ts_millis" not in data


def test_status_and_completed_sync_need_no_source_client_and_restore_normal_health():
    host = Host()
    execute(host, Provider(host), max_work_units=2)
    status = execute(host, operation="ftshare-status")
    assert status["receipt"]["status"] == "read" and status["receipt"]["source_requests"] == 0
    assert status["receipt"]["health"]["raw"]["horizon_counts"][2]["observed_count"] == 2
    assert status["receipt"]["health"]["qfq"]["missing_count"] == 2
    assert execute(host)["receipt"]["status"] == "already-complete"


@pytest.mark.parametrize("http_status", [401, 403, 429])
def test_observed_source_refusal_holds_remaining_slots_across_restart(http_status):
    host = Host()
    failed = Provider(host, fail=http_status)
    result = execute(host, failed, max_work_units=32)
    assert result["receipt"]["status"] == "failed" and failed.requests == 1
    assert result["receipt"]["slot_counts"] == {"pending": 1, "in-flight": 0, "complete": 0, "failed": 1}
    held = {"reason_code": "FTSHARE_HTTP_" + str(http_status), "set_at": STAMP}
    assert result["receipt"]["source_hold"] == held
    assert result["receipt"]["next_run_can_resume"] is False
    assert host.latest()["collection"]["source_hold"] == held
    before = host.read().backend.head()
    resumed = execute(host, max_work_units=32)
    assert resumed["receipt"]["status"] == "source-held" and resumed["receipt"]["source_requests"] == 0
    assert resumed["receipt"]["reserved_requests"] == 1
    assert host.read().backend.head() == before


@pytest.mark.parametrize("failure", [RecordError("FTSHARE_HTTP_UNAVAILABLE"),
                                   RecordError("FTSHARE_HTTP_403")])
def test_error_text_without_observed_transport_does_not_invent_account_hold_or_retry(failure):
    host = Host()
    failed = Provider(host, fail=failure)
    result = execute(host, failed, max_work_units=32)
    assert result["receipt"]["status"] == "failed" and failed.requests == 1
    assert result["receipt"]["source_hold"] is None
    assert host.latest()["collection"]["units"]["none:000"]["status"] == "failed"
    next_source = Provider(host)
    resumed = execute(host, next_source, max_work_units=32)
    assert resumed["receipt"]["status"] == "exhausted" and next_source.requests == 1
    assert next_source.arguments[0][-1] == "forward"


def test_ambiguous_reservation_remains_inflight_and_stops_restart_without_source(monkeypatch):
    host = Host()
    original = cli.retain_ftshare_collection
    def unconfirmed(collection, *args, **kwargs):
        result = original(collection, *args, **kwargs)
        if collection["revision"] == 1:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
        return result
    monkeypatch.setattr(cli, "retain_ftshare_collection", unconfirmed)
    provider = Provider(host)
    result = execute(host, provider)
    assert result["receipt"]["status"] == "persistence-unconfirmed" and provider.requests == 0
    assert result["receipt"]["next_run_can_resume"] is False
    assert host.latest()["collection"]["units"]["none:000"]["status"] == "in-flight"
    again = execute(host)
    assert again["receipt"]["reason_code"] == "FTSHARE_IN_FLIGHT_REQUIRES_RECONCILIATION"
    assert again["receipt"]["source_requests"] == 0 and again["receipt"]["reserved_requests"] == 1


def test_unknown_raw_retention_keeps_consumed_reservation_and_unrequested_slots(monkeypatch):
    host = Host()
    original = cli.retain_ftshare_batch
    def unconfirmed(*args, **kwargs):
        original(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    monkeypatch.setattr(cli, "retain_ftshare_batch", unconfirmed)
    result = execute(host, Provider(host), max_work_units=32)
    assert result["receipt"]["source_requests"] == 1 and result["receipt"]["status"] == "persistence-unconfirmed"
    assert result["receipt"]["slot_counts"] == {"pending": 1, "in-flight": 1, "complete": 0, "failed": 0}


@pytest.mark.parametrize("kind", ["capacity", "api-budget"])
def test_capacity_and_finite_api_admission_stop_before_reserving_or_source(kind):
    host = Host()
    if kind == "api-budget":
        host.initial_api_count = 240
    options = {"capacity": lambda store: {"admit_work_unit": False}} if kind == "capacity" else {}
    provider = Provider(host)
    before = host.read().backend.head()
    result = execute(host, provider, **options)
    assert result["receipt"]["status"] == "blocked" and provider.requests == 0
    assert host.read().backend.head() == before and result["receipt"]["reserved_requests"] == 0


def test_shared_native_quota_cannot_be_refilled_by_another_work_unit():
    host = Host()
    def consumed():
        host.limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "400", "x-ratelimit-reset": "2000000000"})
    provider = Provider(host, after=consumed)
    result = execute(host, provider, max_work_units=32)
    assert result["receipt"]["status"] == "paused" and provider.requests == 1
    assert len(host.apis) == 1 and result["receipt"]["reserved_requests"] == 1


@pytest.mark.parametrize("transport,reason", [
    ({"rate_limits": {"key_remaining": {"state": "valid", "remaining": 0}}},
     "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"),
    ({"rate_limits": {"ip_remaining": {"state": "valid", "remaining": 0}}},
     "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"),
    ({"retry_after_state": "valid", "retry_after_seconds": 90}, "FTSHARE_WAIT_EXCEEDS_CHECK_BOUND"),
    ({"retry_after_state": "invalid", "retry_after_seconds": None}, "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"),
    ({"retry_after_state": "withheld", "retry_after_seconds": None}, "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"),
])
def test_final_success_keeps_valid_batch_and_source_hold_survives_restart(transport, reason):
    host = Host()
    provider = Provider(host, transport=transport)
    result = execute(host, provider, max_work_units=1)
    assert provider.requests == 1 and result["receipt"]["reserved_requests"] == 1
    assert result["receipt"]["status"] == "source-held" and result["receipt"]["reason_code"] == reason
    assert result["receipt"]["next_run_can_resume"] is False
    collection = host.latest()["collection"]
    assert collection["source_hold"] == {"reason_code": reason, "set_at": STAMP}
    assert collection["units"]["none:000"]["status"] == "complete"
    assert collection["units"]["none:000"]["reference"] is not None
    assert collection["units"]["forward:000"]["status"] == "pending"
    status = execute(host, operation="ftshare-status")
    assert status["receipt"]["status"] == "read" and status["receipt"]["source_requests"] == 0
    assert status["receipt"]["source_hold"] == collection["source_hold"]
    assert status["receipt"]["next_run_can_resume"] is False
    assert status["receipt"]["health"]["raw"]["horizon_counts"][2]["observed_count"] == 2
    before = host.read().backend.head()
    resumed = execute(host, max_work_units=32)
    assert resumed["receipt"]["status"] == "source-held" and resumed["receipt"]["source_requests"] == 0
    assert resumed["receipt"]["reserved_requests"] == 1 and host.read().backend.head() == before


@pytest.mark.parametrize("seconds", [4, 60])
def test_short_wait_is_fulfilled_before_final_settle_and_new_client(seconds):
    host = Host()
    provider = Provider(host, transport={"retry_after_state": "valid", "retry_after_seconds": seconds})
    waits = []
    def sleep(delay):
        waiting = host.latest()["collection"]
        assert waiting["units"]["none:000"]["status"] == "in-flight"
        waits.append(delay)
    first = execute(host, provider, sleep=sleep)
    assert waits == [seconds] and first["receipt"]["source_hold"] is None
    assert first["receipt"]["next_run_can_resume"] is True
    second = Provider(host)
    resumed = execute(host, second)
    assert resumed["receipt"]["status"] == "complete" and second.requests == 1
    assert resumed["receipt"]["reserved_requests"] == 2


def test_failed_wait_preserves_valid_batch_and_holds_remaining_source_access():
    host = Host()
    provider = Provider(host, transport={"retry_after_state": "valid", "retry_after_seconds": 4})
    def unavailable(_seconds):
        raise TimeoutError("synthetic private wait detail")
    result = execute(host, provider, sleep=unavailable, max_work_units=32)
    assert provider.requests == 1 and result["receipt"]["status"] == "source-held"
    assert result["receipt"]["reason_code"] == "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"
    assert host.latest()["collection"]["units"]["none:000"]["status"] == "complete"
    assert b"synthetic private wait detail" not in encoded(result["receipt"])
    assert execute(host)["receipt"]["source_requests"] == 0


def test_only_the_observed_rate_buckets_can_establish_exhaustion():
    host = Host()
    provider = Provider(host, transport={"rate_limits": {
        "minute": {"state": "valid", "remaining": 0},
        "key_remaining": {"state": "missing", "remaining": None},
        "ip_remaining": {"state": "invalid", "remaining": None}}})
    result = execute(host, provider, max_work_units=2)
    assert result["receipt"]["status"] == "complete" and provider.requests == 2
    assert result["receipt"]["source_hold"] is None


def test_unknown_hold_settlement_cannot_reopen_source_on_restart(monkeypatch):
    host = Host()
    original = cli.retain_ftshare_collection
    def unconfirmed(collection, *args, **kwargs):
        result = original(collection, *args, **kwargs)
        if collection["source_hold"] is not None:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
        return result
    monkeypatch.setattr(cli, "retain_ftshare_collection", unconfirmed)
    provider = Provider(host, fail=403)
    first = execute(host, provider, max_work_units=32)
    assert first["receipt"]["status"] == "persistence-unconfirmed" and provider.requests == 1
    assert first["receipt"]["next_run_can_resume"] is False
    assert host.latest()["collection"]["source_hold"]["reason_code"] == "FTSHARE_HTTP_403"
    restarted = execute(host, max_work_units=32)
    assert restarted["receipt"]["status"] == "source-held" and restarted["receipt"]["source_requests"] == 0
    assert restarted["receipt"]["reserved_requests"] == 1


def test_resume_cannot_replace_calendar_references_or_reset_request_budget():
    host = Host()
    execute(host, Provider(host))
    bad = deepcopy(host.latest()["collection"]["plan"]["calendar_references"])
    bad["SSE"] = deepcopy(bad["SZSE"])
    provider = Provider(host)
    result = execute(host, provider, calendar_references=bad)
    assert result["receipt"]["reason_code"] == "FTSHARE_RUNTIME_RESUME_CALENDAR_CHANGED"
    assert provider.requests == 0 and result["receipt"]["reserved_requests"] == 1


def test_later_native_admission_failure_returns_prior_confirmed_reference():
    host = Host()
    original = host.open
    def opening(write):
        if host.apis:
            raise RecordError("FTSHARE_MAIN_MOVED")
        return original(write)
    host.open = opening
    result = execute(host, Provider(host), max_work_units=2)
    assert result["receipt"]["reason_code"] == "FTSHARE_MAIN_MOVED"
    assert result["reference"] == host.latest()["reference"]
    assert result["receipt"]["source_requests"] == result["receipt"]["reserved_requests"] == 1


def test_native_authority_loss_after_http_keeps_unknown_slot_and_stops_retention():
    host = Host()
    provider = Provider(host)
    def guard(api):
        if provider.requests:
            raise RecordError("FTSHARE_PRIVATE_REPOSITORY_REQUIRED")
    result = execute(host, provider, max_work_units=2, guard=guard)
    assert result["receipt"]["status"] == "persistence-unconfirmed"
    assert result["receipt"]["source_requests"] == 1 and result["receipt"]["next_run_can_resume"] is False
    assert result["receipt"]["slot_counts"] == {"pending": 1, "in-flight": 1, "complete": 0, "failed": 0}


def test_runtime_prints_counts_and_keeps_exact_reference_in_private_control(tmp_path, monkeypatch, capsys):
    event, env = runtime_input(tmp_path)
    config_path = tmp_path / "trusted-ftshare.json"
    config_path.write_bytes(encoded(config()))
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    ref = deepcopy(cli.BASE_REFERENCE)
    seen = []
    def execute(**kwargs):
        seen.append(kwargs)
        kwargs["progress"]({"status": "partial", "source_requests": 1})
        return {"receipt": {"status": "partial", "source_requests": 1}, "reference": ref}
    out = tmp_path / "result"
    assert cli.run_runtime(event, env, out, execute=execute) == 2
    printed = capsys.readouterr().out
    assert "source-views/" not in printed and env["FTSHARE_API_KEY"] not in printed
    assert json.loads((out / "control.json").read_bytes())["reading_request"]["reference"] == ref
    assert seen[0]["config"] == config() and seen[0]["max_work_units"] == 2


def test_pending_qualification_status_passes_native_gates_and_reads_existing_records(tmp_path):
    pending = config()
    pending["qualification"] = None
    assert cli.configuration(pending) == pending
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="ftshare-status", work_units="1", access_and_custody_confirmed=False)
    selected = cli.settings(event, env, pending)
    api = NativeAPI()
    api.study = api.artifact = None
    host = Host()
    execute(host, Provider(host), max_work_units=2)
    host.config["qualification"] = None
    original_open = host.open
    def open_status(write):
        assert write is False
        assert cli.native_ready(api, selected, pending)["source_qualification"] == "not-required-for-status"
        return original_open(write)
    host.open = open_status
    before = host.read().backend.head()
    result = execute(host, operation="ftshare-status")
    assert result["receipt"]["status"] == "read" and result["receipt"]["source_requests"] == 0
    assert result["receipt"]["health"]["raw"]["horizon_counts"][2]["observed_count"] == 2
    assert host.read().backend.head() == before and len(api.calls) == 4
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in api.calls)


def test_pending_qualification_sync_refuses_before_store_or_source_client(tmp_path, monkeypatch, capsys):
    from decision_kernel.infrastructure import ftshare_http
    pending = config()
    pending["qualification"] = None
    config_path = tmp_path / "pending-ftshare.json"
    config_path.write_bytes(encoded(pending))
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    api = NativeAPI()
    monkeypatch.setattr(cli, "GitHubAPI", lambda token: api)
    constructed = []
    def forbidden(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("pending qualification must not construct a Store or source client")
    monkeypatch.setattr(cli, "GitHubStore", forbidden)
    monkeypatch.setattr(cli, "GitMarketReader", forbidden)
    monkeypatch.setattr(ftshare_http, "FTShareHTTP", forbidden)
    event, env = runtime_input(tmp_path)
    out = tmp_path / "pending-sync"
    assert cli.run_runtime(event, env, out) == 2
    receipt = json.loads((out / "result.json").read_bytes())
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "FTSHARE_RUNTIME_QUALIFICATION_PENDING"
    assert receipt["source_requests"] == 0 and receipt["work_units"] == []
    assert constructed == [] and not (out / "control.json").exists()
    assert len(api.calls) == 4 and not any("/records" in path or "/artifacts/" in path for path in api.calls)
    assert env["FTSHARE_API_KEY"] not in capsys.readouterr().out


KERNEL_ARCHIVE = b"synthetic original archive for native admission only"


def kernel_config(raw=KERNEL_ARCHIVE):
    value = config()
    value["kernel_adoption"] = {**value["qualification"], "sha256": digest(raw)}
    value["qualification"] = None
    return value


def kernel_input(tmp_path):
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="ftshare-adopt-kernel", work_units="1")
    env.pop("FTSHARE_API_KEY")
    return event, env


class KernelNativeAPI(NativeAPI):
    def __init__(self, token="synthetic-store-token", *, raw=KERNEL_ARCHIVE):
        super().__init__(token)
        self.run.update(name=cli.ADOPTION_TITLE, display_title=cli.ADOPTION_TITLE)
        self.study.update(name=cli.KERNEL_TITLE, display_title=cli.KERNEL_TITLE)
        self.artifact.update(name="dos2-ftshare-check-kernel-7001-1", size_in_bytes=len(raw),
                             digest="sha256:" + digest(raw))
        self.artifact["workflow_run"].update(repository_id=1420041744, head_repository_id=1420041744)
        self.ci["total_count"] = 1
        self.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
            repository={"id": 1420041744}, head_repository={"id": 1420041744})


def test_kernel_adoption_uses_separate_config_without_unlocking_batch_sync(tmp_path):
    cfg = kernel_config()
    assert cli.configuration(cfg) == cfg and cfg["qualification"] is None
    event, env = kernel_input(tmp_path)
    selected = cli.settings(event, env, cfg)
    api = KernelNativeAPI()
    checked = cli.native_ready(api, selected, cfg)
    assert checked["kernel_adoption"] == {**cfg["kernel_adoption"], "run_attempt": 1,
                                          "size_in_bytes": len(KERNEL_ARCHIVE)}
    event["inputs"]["operation"] = "ftshare-sync"
    api.run.update(name="decision-os-ashare-data")
    with pytest.raises(RecordError, match="QUALIFICATION_PENDING"):
        cli.native_ready(api, cli.settings(event, env, cfg), cfg)


@pytest.mark.parametrize("key,value", [("work_units", "2"), ("access_and_custody_confirmed", False),
    ("public_rights_confirmed", True), ("repair_request", "{}"),
    ("selection", '{"collection_id":"another","generation":"g20261016"}')])
def test_kernel_adoption_scope_is_one_authorized_private_fixed_unit(tmp_path, key, value):
    event, env = kernel_input(tmp_path)
    event["inputs"][key] = value
    with pytest.raises(RecordError):
        cli.settings(event, env, kernel_config())


def test_kernel_adoption_rejects_calendar_override_and_extra_proof_fields(tmp_path):
    event, env = kernel_input(tmp_path)
    selection = json.loads(event["inputs"]["selection"])
    selection["calendar_references"] = {venue: deepcopy(cli.BASE_REFERENCE) for venue in ("SSE", "SZSE")}
    event["inputs"]["selection"] = json.dumps(selection)
    with pytest.raises(RecordError, match="CALENDAR_OVERRIDE"):
        cli.settings(event, env, kernel_config())
    bad = kernel_config()
    bad["kernel_adoption"]["archive_url"] = "https://example.invalid/other.zip"
    with pytest.raises(RecordError, match="QUALIFICATION_REQUIRED"):
        cli.configuration(bad)


@pytest.mark.parametrize("family,key,value", [
    ("study", "run_attempt", 2), ("study", "conclusion", "failure"),
    ("study", "head_sha", SHA), ("study", "display_title", cli.QUALIFICATION_TITLE),
    ("artifact", "name", "dos2-ftshare-check-continuation-7001-1"),
    ("artifact", "digest", "sha256:" + "d" * 64), ("artifact", "expired", True),
    ("artifact", "size_in_bytes", 0), ("run", "display_title", cli.KERNEL_TITLE),
    ("meta", "private", False),
])
def test_kernel_adoption_requires_this_native_run_and_original_kernel_artifact(tmp_path, family, key, value):
    event, env = kernel_input(tmp_path)
    api = KernelNativeAPI()
    getattr(api, family)[key] = value
    with pytest.raises(RecordError):
        cli.native_ready(api, cli.settings(event, env, kernel_config()), kernel_config())


@pytest.mark.parametrize("change", [{"run_attempt": 2}, {"conclusion": "failure"},
    {"repository": {"id": 99}}, {"head_repository": {"id": 99}}, {"event": "pull_request"}])
def test_kernel_adoption_cannot_reuse_a_newer_failed_or_wrong_main_ci(tmp_path, change):
    event, env = kernel_input(tmp_path)
    api = KernelNativeAPI()
    # An older successful first row cannot hide a newer run later in the response.
    api.ci["workflow_runs"].append({**api.ci["workflow_runs"][0], "id": 6002, **change})
    api.ci["total_count"] = 2
    with pytest.raises(RecordError, match="MAIN_CI"):
        cli.native_ready(api, cli.settings(event, env, kernel_config()), kernel_config())
    assert not any(path.endswith("/runs/7001") for path in api.calls)


def test_kernel_adoption_download_uses_native_id_and_checks_the_original_bytes():
    api, calls = KernelNativeAPI(), []
    def load(repo, artifact_id):
        calls.append((repo, artifact_id))
        return KERNEL_ARCHIVE
    original = cli.load_kernel_archive(api, kernel_config(), archive_loader=load)
    assert original["archive"] == KERNEL_ARCHIVE and calls == [(cli.REPO, 9001)]
    assert original["proof"] == {**kernel_config()["kernel_adoption"], "run_attempt": 1,
                                  "size_in_bytes": len(KERNEL_ARCHIVE)}
    with pytest.raises(RecordError, match="ARCHIVE_DIGEST"):
        cli.load_kernel_archive(api, kernel_config(), archive_loader=lambda *_: KERNEL_ARCHIVE + b"changed")
    api.artifact["workflow_run"]["repository_id"] = 99
    with pytest.raises(RecordError, match="KERNEL_ARTIFACT"):
        cli.load_kernel_archive(api, kernel_config(), archive_loader=lambda *_: pytest.fail("must not download"))


def test_kernel_adoption_runtime_supplies_only_store_token_and_normal_read_control(tmp_path, monkeypatch, capsys):
    from decision_kernel.infrastructure import ftshare_http
    event, values = kernel_input(tmp_path)
    class Environment(dict):
        def get(self, key, *args):
            assert key not in {"FTSHARE_API_KEY", "RELAY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
            return super().get(key, *args)
    env = Environment(values)
    config_path = tmp_path / "kernel-config.json"
    config_path.write_bytes(encoded(kernel_config()))
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(ftshare_http, "FTShareHTTP", lambda *a, **k: pytest.fail("must not construct a source client"))
    downloads = []
    def download(repo, artifact_id, *, token):
        downloads.append((repo, artifact_id, token))
        return KERNEL_ARCHIVE
    monkeypatch.setattr(cli, "download_archive", download)
    def execute(**kwargs):
        assert kwargs["operation"] == "ftshare-adopt-kernel" and kwargs["max_work_units"] == 1
        assert kwargs["client_factory"] is None
        original = kwargs["kernel_loader"](KernelNativeAPI())
        assert original["archive"] == KERNEL_ARCHIVE
        ref = deepcopy(cli.BASE_REFERENCE)
        return {"receipt": {"status": "complete", "source_requests": 0}, "reference": ref,
                "reading_request": {"reference": ref, "offset": 0, "limit": 50}}
    out = tmp_path / "adopted"
    assert cli.run_runtime(event, env, out, execute=execute) == 0
    assert downloads == [(cli.REPO, 9001, env[cli.TOKEN_ENV])]
    control = json.loads((out / "control.json").read_bytes())
    assert control["reading_request"]["reference"] == control["reference"] == cli.BASE_REFERENCE
    printed = capsys.readouterr().out
    assert "source-views/" not in printed and env[cli.TOKEN_ENV] not in printed


def test_missing_kernel_approval_blocks_before_store_or_archive_download(tmp_path, monkeypatch, capsys):
    event, env = kernel_input(tmp_path)
    pending = kernel_config()
    pending["kernel_adoption"] = None
    config_path = tmp_path / "pending-kernel.json"
    config_path.write_bytes(encoded(pending))
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    api = KernelNativeAPI()
    monkeypatch.setattr(cli, "GitHubAPI", lambda _token: api)
    monkeypatch.setattr(cli, "GitHubStore", lambda *a, **k: pytest.fail("must not construct a Store"))
    monkeypatch.setattr(cli, "download_archive", lambda *a, **k: pytest.fail("must not download an artifact"))
    out = tmp_path / "pending-kernel"
    assert cli.run_runtime(event, env, out) == 2
    receipt = json.loads((out / "result.json").read_bytes())
    assert receipt["reason_code"] == "FTSHARE_RUNTIME_KERNEL_ADOPTION_PENDING"
    assert receipt["source_requests"] == 0 and receipt["work_units"] == []
    assert not (out / "control.json").exists()


class KernelHost(Host):
    def __init__(self):
        self.fixture = kernel_adoption_fixture()
        self.shared = self.fixture["backend"].shared
        self.old_ref, self.old = self.fixture["legacy_reference"], self.fixture["old"]
        self.config = {key: self.fixture[key] for key in ("collection_id", "generation", "legacy_reference")}
        self.now = self.fixture["created_at"]
        self.apis, self.commits, self.archive_reads = [], [], []
        self.after_commit = self.read_error = None
        self.initial_api_count = 0
        self.limits = GitHubRateLimits()
        self.limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "5000", "x-ratelimit-reset": "2000000000"})

    def original(self, api):
        self.archive_reads.append(api)
        return {key: self.fixture[key] for key in ("archive", "proof")}


def adopt_in_runtime(host, **options):
    arguments = {"operation": "ftshare-adopt-kernel", "config": host.config, "registry": REGISTRY,
        "open_unit": host.open, "access_confirmed": True, "custody_authorized": True,
        "kernel_loader": host.original, "clock": lambda: host.now,
        "client_factory": lambda: pytest.fail("adoption must not construct a source client")}
    return cli.run_collection(**{**arguments, **options})


def test_runtime_adopts_originals_once_and_status_uses_only_the_retained_store():
    host = KernelHost()
    updates = []
    saved = adopt_in_runtime(host, progress=lambda receipt: updates.append(receipt))
    receipt = saved["receipt"]
    assert receipt["status"] == "complete", receipt
    assert receipt["source_requests"] == receipt["source_budget"] == receipt["reserved_requests"] == 0
    assert (receipt["historical_source_requests"], receipt["prior_source_requests"],
            receipt["cumulative_source_requests"]) == (4, 2, 6)
    assert receipt["retained_response_count"] == 4 and receipt["unique_series_count"] == 3
    assert receipt["revision"] == 0 and len(receipt["work_units"]) == 1
    assert receipt["capacity"]["admit_work_unit"] is True
    assert saved["reading_request"] == {"reference": saved["reference"], "offset": 0, "limit": 50}
    assert all(value["source_requests"] == 0 for value in updates)
    assert b"source-views/" not in encoded(receipt) and b"ts_millis" not in encoded(receipt)
    restored = host.latest()
    assert restored["reference"] == saved["reference"]
    assert restored["collection"]["plan"]["adoption"]["proof"] == host.fixture["proof"]
    assert len(restored["collection"]["units"]) == 3
    assert restore_market_collection(host.old_ref, REGISTRY, host.read())["collection"] == host.old

    before, commits = host.read().backend.head(), len(host.commits)
    host.now = "2026-10-20T12:00:00+00:00"
    repeated = adopt_in_runtime(host, capacity=lambda _: pytest.fail("existing adoption requires no new capacity"))
    assert repeated["receipt"]["status"] == "complete"
    assert repeated["reference"] == saved["reference"] and repeated["reading_request"] == saved["reading_request"]
    assert host.read().backend.head() == before and len(host.commits) == commits
    assert host.latest()["collection"]["plan"]["created_at"] == host.fixture["created_at"]

    reading = adopt_in_runtime(host, operation="ftshare-status", access_confirmed=False,
        custody_authorized=False, kernel_loader=lambda _: pytest.fail("normal status must not download an artifact"))
    assert reading["receipt"]["status"] == "read" and reading["receipt"]["source_requests"] == 0
    assert reading["receipt"]["health"]["raw"]["horizon_counts"][2]["observed_count"] == 2
    assert reading["receipt"]["health"]["qfq"]["horizon_counts"][2]["observed_count"] == 1
    assert reading["reference"] == saved["reference"] and len(host.archive_reads) == 2
    assert host.read().backend.head() == before and len(host.commits) == commits


@pytest.mark.parametrize("gate", ["capacity", "api-budget", "main-moved"])
def test_kernel_runtime_admission_failure_cannot_retain_or_create_a_source_client(gate):
    host = KernelHost()
    calls, options = [], {}
    if gate == "capacity":
        options["capacity"] = lambda _: {"admit_work_unit": False}
    elif gate == "api-budget":
        host.initial_api_count = 240
    else:
        def guard(api):
            calls.append(api)
            if len(calls) == 2:
                raise RecordError("FTSHARE_MAIN_MOVED")
        options["guard"] = guard
    before = host.read().backend.head()
    outcome = adopt_in_runtime(host, **options)
    assert outcome["receipt"]["status"] == "blocked"
    assert outcome["receipt"]["source_requests"] == outcome["receipt"]["reserved_requests"] == 0
    assert outcome["reference"] is None and host.read().backend.head() == before
    assert host.commits == [] and host.latest() is None


def test_kernel_runtime_reconstructs_archive_before_any_storage_admission_or_write():
    host = KernelHost()
    # A matching digest for corrupt bytes still cannot stand in for the original ZIP.
    host.fixture["archive"] = b"invalid archive with a self-consistent digest"
    host.fixture["proof"].update(sha256=digest(host.fixture["archive"]), size_in_bytes=len(host.fixture["archive"]))
    before = host.read().backend.head()
    outcome = adopt_in_runtime(host, capacity=lambda _: pytest.fail("malformed original must fail first"))
    assert outcome["receipt"]["status"] == "blocked"
    assert outcome["receipt"]["reason_code"] == "FTSHARE_ADOPTION_ARCHIVE_INVALID"
    assert outcome["receipt"]["source_requests"] == 0 and "historical_source_requests" not in outcome["receipt"]
    assert host.read().backend.head() == before and not host.commits


@pytest.mark.parametrize("uncertain_at", ["first-original", "published-root"])
def test_kernel_runtime_unknown_write_stops_and_explicit_reentry_reconciles(uncertain_at):
    host = KernelHost()
    failures = []
    def lose_receipt(backend, files):
        original = uncertain_at == "first-original" and len(host.commits) == 1
        root = uncertain_at == "published-root" and any(
            b"ftshare-collection-state-v1" in value for value in files.values())
        if original or root:
            failures.append(backend.head())
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_commit = lose_receipt
    first = adopt_in_runtime(host)
    assert first["receipt"]["status"] == "persistence-unconfirmed", first["receipt"]
    assert first["receipt"]["next_run_can_resume"] is False
    assert first["receipt"]["source_requests"] == first["receipt"]["reserved_requests"] == 0
    assert len(failures) == 1 and first["reference"] is None
    host.after_commit = None
    existing = host.latest()
    assert (existing is not None) == (uncertain_at == "published-root")
    if existing is not None:
        previous_head, previous_commits = host.read().backend.head(), len(host.commits)
    host.now = "2026-10-20T12:00:00+00:00"
    resumed = adopt_in_runtime(host)
    assert resumed["receipt"]["status"] == "complete", resumed["receipt"]
    assert resumed["receipt"]["source_requests"] == resumed["receipt"]["source_budget"] == 0
    assert resumed["receipt"]["historical_source_requests"] == 4
    if existing is not None:
        assert resumed["reference"] == existing["reference"]
        assert host.read().backend.head() == previous_head and len(host.commits) == previous_commits
    final_head, final_commits = host.read().backend.head(), len(host.commits)
    repeated = adopt_in_runtime(host)
    assert repeated["reference"] == resumed["reference"]
    assert host.read().backend.head() == final_head and len(host.commits) == final_commits
    assert restore_market_collection(host.old_ref, REGISTRY, host.read())["collection"] == host.old


def single_config():
    value = kernel_config()
    value.update(generation="g20261016-v1", kernel_sync={"base_reference": deepcopy(cli.BASE_REFERENCE),
        "start": "20260701", "end": "20261016",
        "targets": [{"symbol": "600000.SH", "adjust_kind": "forward"}], "source_budget": 1})
    return value


def single_input(tmp_path):
    event, env = runtime_input(tmp_path)
    event["inputs"].update(work_units="1", selection=json.dumps({
        key: single_config()[key] for key in ("collection_id", "generation")}))
    return event, env


class SingleNativeAPI(KernelNativeAPI):
    def __init__(self, token="synthetic-store-token"):
        super().__init__(token)
        self.run.update(name="decision-os-ashare-data", display_title="decision-os-ashare-data")
        # Success must not depend on either temporary study object still existing.
        self.study = self.artifact = None


def test_single_configuration_is_finite_and_keeps_the_batch_qualification_closed(tmp_path):
    value = single_config()
    assert cli.configuration(value) == value and value["qualification"] is None
    assert cli.configuration({**config(), "kernel_sync": None}) == {**config(), "kernel_sync": None}
    value["kernel_sync"]["end"] = "20261027"  # Exactly 119 natural Shanghai days.
    assert cli.configuration(value) == value
    value["kernel_sync"]["targets"] = [{"symbol": f"{number:06}.SH", "adjust_kind": "none"}
        for number in range(212)]
    value["kernel_sync"]["source_budget"] = 212
    assert cli.configuration(value) == value
    event, env = single_input(tmp_path)
    approved = cli.settings(event, env, single_config())
    api = SingleNativeAPI()
    assert cli.native_ready(api, approved, single_config()) == {
        "main_ci_run": 6001, "source_qualification": "required-from-retained-kernel-base"}
    assert len(api.calls) == 4
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in api.calls)


@pytest.mark.parametrize("change", ["old-generation", "missing-proof", "foreign-base", "extra-field",
    "invalid-date", "reversed-range", "overlong-range", "empty-targets", "duplicate-targets",
    "target-extra-field", "unsupported-symbol", "unsupported-adjustment", "bool-budget", "wrong-budget",
    "overlarge-pool"])
def test_single_configuration_rejects_unbounded_or_ambiguous_scope(change):
    value = single_config()
    sync = value["kernel_sync"]
    if change == "old-generation":
        value["generation"] = "g20261016"
    elif change == "missing-proof":
        value["kernel_adoption"] = None
    elif change == "foreign-base":
        sync["base_reference"]["manifest"]["repository"] = "other/records"
    elif change == "extra-field":
        sync["source_url"] = "https://example.invalid/data"
    elif change == "invalid-date":
        sync["start"] = "20260230"
    elif change == "reversed-range":
        sync["start"] = "20261017"
    elif change == "overlong-range":
        sync["end"] = "20261028"
    elif change == "empty-targets":
        sync.update(targets=[], source_budget=0)
    elif change == "duplicate-targets":
        sync.update(targets=sync["targets"] * 2, source_budget=2)
    elif change == "target-extra-field":
        sync["targets"][0]["limit"] = 100
    elif change == "unsupported-symbol":
        sync["targets"][0]["symbol"] = "430047.BJ"
    elif change == "unsupported-adjustment":
        sync["targets"][0]["adjust_kind"] = "backward"
    elif change == "bool-budget":
        sync["source_budget"] = True
    elif change == "wrong-budget":
        sync["source_budget"] = 2
    else:
        sync.update(targets=[{"symbol": f"{number:06}.SH", "adjust_kind": "none"}
            for number in range(213)], source_budget=213)
    with pytest.raises(ValueError):
        cli.configuration(value)


def test_single_generation_cannot_readopt_the_old_study_or_override_source_scope(tmp_path):
    event, env = single_input(tmp_path)
    event["inputs"]["operation"] = "ftshare-adopt-kernel"
    with pytest.raises(RecordError, match="ADOPTION_GENERATION_CLOSED"):
        cli.settings(event, env, single_config())
    with pytest.raises(RecordError, match="ADOPTION_GENERATION_CLOSED"):
        cli.run_collection(operation="ftshare-adopt-kernel", config=single_config(), registry=REGISTRY,
            open_unit=lambda _: pytest.fail("closed adoption must not open a Store"),
            access_confirmed=True, custody_authorized=True)
    event["inputs"]["operation"] = "ftshare-sync"
    selection = json.loads(event["inputs"]["selection"])
    selection["targets"] = [{"symbol": "000001.SZ", "adjust_kind": "none"}]
    event["inputs"]["selection"] = json.dumps(selection)
    with pytest.raises(RecordError, match="FIXED_SELECTION"):
        cli.settings(event, env, single_config())


@pytest.mark.parametrize("gate", ["private", "main", "run-title", "run-attempt", "latest-failed",
    "ci-attempt", "ci-identity", "incomplete-ci-list"])
def test_single_native_execution_and_latest_actual_main_ci_remain_required(tmp_path, gate):
    event, env = single_input(tmp_path)
    api = SingleNativeAPI()
    if gate == "private":
        api.meta["private"] = False
    elif gate == "main":
        api.main["object"]["sha"] = "d" * 40
    elif gate == "run-title":
        api.run["display_title"] = cli.KERNEL_TITLE
    elif gate == "run-attempt":
        api.run["run_attempt"] = 2
    elif gate == "incomplete-ci-list":
        api.ci["total_count"] = 6
    else:
        change = {"conclusion": "failure"} if gate == "latest-failed" else {
            "run_attempt": 2} if gate == "ci-attempt" else {"head_repository": {"id": 99}}
        api.ci["workflow_runs"].append({**api.ci["workflow_runs"][0], "id": 6002, **change})
        api.ci["total_count"] = 2
    with pytest.raises(RecordError):
        cli.native_ready(api, cli.settings(event, env, single_config()), single_config())
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for path in api.calls)


class SingleHost(KernelHost):
    def __init__(self):
        super().__init__()
        self.adopted = adopt_kernel(self.fixture)
        self.config.update(generation="g20261016-v1", qualification=None,
            kernel_adoption={key: self.fixture["proof"][key]
                for key in ("run_id", "artifact_id", "sha256", "code_sha")},
            kernel_sync={"base_reference": self.adopted["reference"], "start": "20260701", "end": "20261016",
                "targets": [{"symbol": "600000.SH", "adjust_kind": "forward"}], "source_budget": 1})
        self.now = "2026-10-19T12:03:00+00:00"


class SingleProvider(Provider):
    def candlesticks_batch(self, *args, **kwargs):
        pytest.fail("a configured v1 successor must not call the v2 batch route")

    def candlesticks(self, symbol, *, since, until, adjust_kind):
        collection = self.host.latest()["collection"]
        slot = adjust_kind + ":" + symbol
        assert slot in collection["plan"]["new_slots"]
        assert collection["units"][slot]["status"] == "in-flight"
        assert collection["reserved_requests"] >= self.requests + 1
        assert all(unit["request_count"] == 0 for unit in collection["plan"]["inherited_units"].values())
        self.requests += 1
        self.arguments.append((symbol, since, until, adjust_kind))
        if self.fail:
            if isinstance(self.fail, BaseException):
                raise self.fail
            status = self.fail if type(self.fail) is int else 429
            self.last_error_meta = {"http_status": status, "classification": "auth-or-entitlement"
                if status in (401, 403) else "rate-limit", **self.transport}
            raise RecordError("FTSHARE_HTTP_" + str(status))
        captured = single_capture(collection, slot, acquired_at=self.host.now)
        self.last_response_bytes = captured["body_utf8"].encode()
        self.last_response_meta = {"http_status": 200, "wire_bytes": len(self.last_response_bytes),
            "wire_sha256": captured["wire_sha256"], "rate_limits": {}, **self.transport}
        if self.observed_zero:
            self.last_response_meta["rate_limits"] = {"key_remaining": {"state": "valid", "remaining": 0}}
        if self.after:
            self.after()
        return json.loads(self.last_response_bytes)


def single_execute(host, provider=None, **options):
    arguments = {"operation": "ftshare-sync", "config": host.config, "registry": REGISTRY,
        "open_unit": host.open, "access_confirmed": True, "custody_authorized": True,
        "clock": lambda: host.now, "sleep": lambda _: None,
        "kernel_loader": lambda _: pytest.fail("v1 sync must not download a temporary study artifact"),
        "client_factory": (lambda: provider) if provider is not None else
            (lambda: pytest.fail("no new source client may be constructed"))}
    return cli.run_collection(**{**arguments, **options})


def test_single_runtime_inherits_three_owners_and_persists_one_truthful_v1_request():
    from decision_kernel.application.ftshare_single import ENDPOINT, restore_single
    host = SingleHost()
    original = restore_ftshare_collection(host.adopted["reference"], REGISTRY, host.read())
    provider = SingleProvider(host)
    result = single_execute(host, provider, max_work_units=32)
    receipt = result["receipt"]
    assert receipt["status"] == "complete", receipt
    assert receipt["source_requests"] == receipt["source_budget"] == receipt["reserved_requests"] == 1
    assert receipt["new_slot_counts"] == {"pending": 0, "in-flight": 0, "complete": 1, "failed": 0}
    assert receipt["slot_counts"] == {"pending": 0, "in-flight": 0, "complete": 4, "failed": 0}
    assert receipt["inherited_slot_count"] == 3 and receipt["prior_generation_reserved_requests"] == 0
    assert receipt["prior_study_source_requests"] == 6 and provider.requests == 1
    assert receipt["account_daily_limit_verified"] is False
    latest = host.latest()
    collection = latest["collection"]
    assert collection["revision"] == 2 and collection["plan"]["base_reference"] == host.adopted["reference"]
    assert collection["plan"]["adoption"]["proof"] == host.fixture["proof"]
    assert collection["plan"]["calendar_references"] == original["collection"]["plan"]["calendar_references"]
    for prior_slot, request in original["collection"]["plan"]["requests"].items():
        params = request["parameters"]
        slot = params["adjust_kind"] + ":" + params["symbol"]
        assert collection["plan"]["requests"][slot] == request
        assert collection["units"][slot] == original["collection"]["units"][prior_slot]
    slot = "forward:600000.SH"
    request = collection["plan"]["requests"][slot]
    captured = restore_single(collection["units"][slot]["reference"], host.read(), request=request)
    assert captured["request"]["url"] == ENDPOINT
    assert "symbol" in request["parameters"] and "symbols" not in request["parameters"]
    assert captured["body_utf8"].encode() == provider.last_response_bytes
    assert captured["wire_sha256"] == digest(provider.last_response_bytes)
    assert provider.arguments == [("600000.SH", request["parameters"]["since"],
        request["parameters"]["until"], "forward")]
    assert restore_market_collection(host.old_ref, REGISTRY, host.read())["collection"] == host.old
    before, commits = host.read().backend.head(), len(host.commits)
    host.now = "2026-10-20T12:00:00+00:00"
    repeated = single_execute(host, capacity=lambda _: pytest.fail("settled generation needs no admission"))
    assert repeated["receipt"]["status"] == "already-complete"
    assert repeated["reference"] == result["reference"] and repeated["receipt"]["source_requests"] == 0
    assert repeated["receipt"]["reserved_requests"] == 1
    status = single_execute(host, operation="ftshare-status", access_confirmed=False, custody_authorized=False)
    assert status["receipt"]["status"] == "read" and status["receipt"]["source_requests"] == 0
    assert status["receipt"]["health"]["raw"]["horizon_counts"][2]["observed_count"] == 2
    assert status["receipt"]["health"]["qfq"]["horizon_counts"][2]["observed_count"] == 2
    assert host.read().backend.head() == before and len(host.commits) == commits
    assert not host.archive_reads


@pytest.mark.parametrize("missing", ["archive", "capture", "trusted-proof", "unqualified-base"])
def test_single_runtime_restores_durable_qualification_before_capacity_or_source_client(missing):
    host = SingleHost()
    if missing in {"archive", "capture"}:
        reference = host.adopted["evidence_reference"] if missing == "archive" else (
            host.adopted["capture_references"]["raw-sz-repeat"])
        manifest = reference["manifest"]
        host.shared["missing"].add((manifest["commit"], manifest["path"]))
    elif missing == "trusted-proof":
        host.config["kernel_adoption"]["sha256"] = "d" * 64
    else:
        host.config["kernel_sync"]["base_reference"] = host.old_ref
    before = host.read().backend.head()
    result = single_execute(host, capacity=lambda _: pytest.fail("qualification must be restored first"))
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["source_requests"] == 0 and result["reference"] is None
    assert host.read().backend.head() == before and not host.commits


@pytest.mark.parametrize("gate", ["capacity", "api-budget"])
def test_single_runtime_storage_admission_stops_before_credential_or_reservation(gate):
    host = SingleHost()
    if gate == "api-budget":
        host.initial_api_count = 240
    options = {"capacity": lambda _: {"admit_work_unit": False}} if gate == "capacity" else {}
    before = host.read().backend.head()
    result = single_execute(host, **options)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["source_requests"] == result["receipt"]["reserved_requests"] == 0
    assert result["reference"] is None and host.read().backend.head() == before and not host.commits


@pytest.mark.parametrize("failure", [403, 429, RecordError("FTSHARE_HTTP_UNAVAILABLE")])
def test_single_runtime_spent_source_failure_is_not_retried_and_observed_hold_survives(failure):
    host = SingleHost()
    provider = SingleProvider(host, fail=failure)
    first = single_execute(host, provider, max_work_units=32)
    assert first["receipt"]["status"] == "failed" and first["receipt"]["source_requests"] == 1
    assert first["receipt"]["reserved_requests"] == 1 and provider.requests == 1
    assert host.latest()["collection"]["units"]["forward:600000.SH"]["status"] == "failed"
    before = host.read().backend.head()
    repeated = single_execute(host, max_work_units=32)
    assert repeated["receipt"]["status"] == ("exhausted" if isinstance(failure, BaseException) else "source-held")
    assert repeated["receipt"]["source_requests"] == 0 and repeated["receipt"]["reserved_requests"] == 1
    assert host.read().backend.head() == before


@pytest.mark.parametrize("change", ["base", "start", "end", "targets", "budget", "calendar"])
def test_single_reentry_cannot_change_the_retained_generation_scope(change):
    host = SingleHost()
    saved = single_execute(host, SingleProvider(host))
    assert saved["receipt"]["status"] == "complete"
    options = {}
    sync = host.config["kernel_sync"]
    if change == "base":
        sync["base_reference"] = host.old_ref
    elif change == "start":
        sync["start"] = "20260702"
    elif change == "end":
        sync["end"] = "20261015"
    elif change == "targets":
        sync["targets"] = [{"symbol": "000001.SZ", "adjust_kind": "none"}]
    elif change == "budget":
        sync["targets"].append({"symbol": "000001.SZ", "adjust_kind": "none"})
        sync["source_budget"] = 2
    else:
        options["calendar_references"] = {venue: host.old_ref for venue in ("SSE", "SZSE")}
    before = host.read().backend.head()
    result = single_execute(host, **options)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["reason_code"] == ("FTSHARE_RUNTIME_RESUME_CALENDAR_CHANGED"
        if change == "calendar" else "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED")
    assert result["receipt"]["source_requests"] == 0 and host.read().backend.head() == before


@pytest.mark.parametrize("uncertain_at", ["genesis", "reservation", "original", "settlement"])
def test_single_runtime_unknown_write_keeps_the_exact_durable_budget_and_never_resends(uncertain_at):
    host = SingleHost()
    stop = {"genesis": 1, "reservation": 2, "original": 3, "settlement": 4}[uncertain_at]
    def lose_receipt(backend, files):
        if len(host.commits) == stop:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_commit = lose_receipt
    provider = SingleProvider(host)
    first = single_execute(host, provider)
    assert first["receipt"]["status"] == "persistence-unconfirmed", first["receipt"]
    assert first["receipt"]["next_run_can_resume"] is False
    assert first["receipt"]["source_requests"] == (0 if stop <= 2 else 1)
    host.after_commit = None
    original = host.latest()
    assert original["collection"]["reserved_requests"] == (0 if stop == 1 else 1)
    before = host.read().backend.head()
    followup = SingleProvider(host) if stop == 1 else None
    resumed = single_execute(host, followup)
    if stop == 1:
        assert resumed["receipt"]["status"] == "complete" and resumed["receipt"]["source_requests"] == 1
        assert resumed["receipt"]["reserved_requests"] == 1
    else:
        assert resumed["receipt"]["source_requests"] == 0 and host.read().backend.head() == before
        if stop == 4:
            assert resumed["receipt"]["status"] == "already-complete"
            assert resumed["reference"] == original["reference"]
        else:
            assert resumed["receipt"]["reason_code"] == "FTSHARE_IN_FLIGHT_REQUIRES_RECONCILIATION"


def test_single_successor_keeps_prior_failed_owner_and_reservations_out_of_this_generations_cost():
    host = SingleHost()
    first = single_execute(host, SingleProvider(host, fail=RecordError("FTSHARE_HTTP_UNAVAILABLE")))
    assert first["receipt"]["status"] == "failed"
    symbol = next(code for code in host.fixture["context"]["eligible_symbols"]
        if code not in {"000001.SZ", "600000.SH"})
    host.config["generation"] = "g20261016-v2"
    host.config["kernel_sync"].update(base_reference=first["reference"],
        targets=[{"symbol": symbol, "adjust_kind": "none"}])
    second = single_execute(host, SingleProvider(host))
    receipt = second["receipt"]
    assert receipt["status"] == "complete", receipt
    assert receipt["source_requests"] == receipt["reserved_requests"] == receipt["source_budget"] == 1
    assert receipt["prior_generation_reserved_requests"] == 1 and receipt["prior_study_source_requests"] == 6
    assert receipt["slot_counts"]["failed"] == 1 and receipt["new_slot_counts"]["failed"] == 0
    inherited = host.latest()["collection"]["units"]["forward:600000.SH"]
    assert inherited["status"] == "failed" and inherited["request_count"] == 0
    assert single_execute(host)["receipt"]["status"] == "already-complete"


@pytest.mark.parametrize("available", [False, True])
def test_runtime_reads_the_ft_key_only_after_durable_v1_qualification(tmp_path, monkeypatch, capsys, available):
    from decision_kernel.infrastructure import ftshare_http
    host = SingleHost()
    evidence = host.adopted["evidence_reference"]["manifest"]
    if not available:
        host.shared["missing"].add((evidence["commit"], evidence["path"]))
    observed_reads, key_reads, clients = set(), [], []
    host.read_error = lambda _backend, commit, path: observed_reads.add((commit, path))
    required = {tuple(ref["manifest"][key] for key in ("commit", "path"))
        for ref in (host.adopted["evidence_reference"], *host.adopted["capture_references"].values())}
    event, values = single_input(tmp_path)
    class Environment(dict):
        def get(self, key, *args):
            assert key not in {"RELAY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
            if key == "FTSHARE_API_KEY":
                key_reads.append(key)
                assert available and required <= observed_reads
            return super().get(key, *args)
    env = Environment(values)
    path = tmp_path / "single-config.json"
    path.write_bytes(encoded(single_config()))
    monkeypatch.setattr(cli, "CONFIG_FILE", path)
    monkeypatch.setattr(cli, "download_archive", lambda *a, **k: pytest.fail("no temporary archive download"))
    def factory(*, credential):
        clients.append(credential)
        assert credential == values["FTSHARE_API_KEY"] and required <= observed_reads
        return SingleProvider(host)
    monkeypatch.setattr(ftshare_http, "FTShareHTTP", factory)
    def execute(**kwargs):
        assert kwargs["kernel_loader"] is None
        # Only the synthetic fixture's Store locators and clock differ from the
        # fixed hosted binding; the actual core gate and deferred factory run.
        return cli.run_collection(**{**kwargs, "config": host.config, "registry": REGISTRY,
            "open_unit": host.open, "clock": lambda: host.now, "sleep": lambda _: None,
            "guard": lambda _: None})
    out = tmp_path / "single-runtime"
    assert cli.run_runtime(event, env, out, execute=execute) == (0 if available else 2)
    assert key_reads == (["FTSHARE_API_KEY"] if available else [])
    assert len(clients) == (1 if available else 0)
    receipt = json.loads((out / "result.json").read_bytes())
    assert receipt["source_requests"] == (1 if available else 0)
    printed = capsys.readouterr().out
    assert values["FTSHARE_API_KEY"] not in printed and values[cli.TOKEN_ENV] not in printed


def test_single_native_git_unit_reuses_local_snapshot_reads_within_existing_api_budget(tmp_path, monkeypatch):
    from decision_kernel.infrastructure import market_snapshot
    from test_github_store import GitAPI
    from test_market_snapshot import local_fetch
    from test_news_read_budget import transport

    model = GitAPI(tmp_path / "single-native-model.git")
    model.binding = replace(model.binding, repository=cli.REPO, repository_id=1420041744, branch="records")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository, model.git.binding = model.binding.repository, model.binding
    # Existing native fixture transport models GitHub; no source or remote I/O.
    # Seeding is outside the measured work unit, as the adopted records already exist.
    seed_reader = lambda: VersionedStore(copy(model.git))
    fixture = kernel_adoption_fixture(storage=(model.git, VersionedStore(model.git), seed_reader))
    adopted = adopt_kernel(fixture)
    cfg = {**single_config(), "legacy_reference": fixture["legacy_reference"],
        "kernel_adoption": {key: fixture["proof"][key]
            for key in ("run_id", "artifact_id", "sha256", "code_sha")}}
    cfg["kernel_sync"]["base_reference"] = adopted["reference"]
    native, apis, fetches = SingleNativeAPI(), [], []
    model.meta = native.meta
    original_request = model.request
    def request(method, path, payload=None):
        if path == cli.PREFIX + "/git/ref/heads/main" or "/actions/" in path:
            model.requests.append((method, path, deepcopy(payload)))
            return native.request(method, path, payload)
        return original_request(method, path, payload)
    model.request = request
    usage = {"core": 0, "graphql": 0}
    def factory(token):
        api = transport(model)
        wire = api._opener.open
        def observed(request, timeout):
            response = wire(request, timeout)
            resource = "graphql" if request.method == "POST" else "core"
            usage[resource] += 1
            response.headers = {"x-ratelimit-resource": resource, "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(5000 - usage[resource]), "x-ratelimit-reset": "2000000000"}
            return response
        api._opener.open = observed
        apis.append(api)
        return api
    monkeypatch.setattr(cli, "GitHubAPI", factory)
    monkeypatch.setattr(market_snapshot, "_fetch_market_branch", local_fetch(model, fetches))
    event, env = single_input(tmp_path)
    selected = cli.settings(event, env, single_config())
    host = SimpleNamespace(fixture=fixture, now="2026-10-19T12:03:00+00:00")
    def latest():
        reader = seed_reader()
        ref = latest_ftshare_reference(reader, REGISTRY, collection_id=cfg["collection_id"],
            generation=cfg["generation"])
        return restore_ftshare_collection(ref, REGISTRY, reader)
    host.latest = latest
    before_source = []
    class BudgetProvider(SingleProvider):
        def candlesticks(self, *args, **kwargs):
            before_source.append(apis[-1]._requests)
            return super().candlesticks(*args, **kwargs)
    provider = BudgetProvider(host)
    result = cli.run_collection(operation="ftshare-sync", config=cfg, registry=REGISTRY,
        open_unit=cli._open_remote(model.binding, env[cli.TOKEN_ENV], selected, cfg),
        access_confirmed=True, custody_authorized=True, client_factory=lambda: provider,
        clock=lambda: host.now, sleep=lambda _: None,
        guard=lambda api: cli.check_private(api, selected))
    receipt = result["receipt"]
    assert receipt["status"] == "complete", receipt
    assert receipt["source_requests"] == receipt["reserved_requests"] == 1
    assert len(apis) == 1 and len(before_source) == 1 and before_source[0] < cli.SOURCE_ADMISSION_API_REQUESTS
    assert apis[0]._requests == len(model.requests) <= cli.MAX_REQUESTS
    assert receipt["work_units"][0]["store_api_requests"] == apis[0]._requests
    assert model.writes == 4  # Genesis, reservation, truthful original, settlement.
    assert all(call["api"] is apis[0] and not call["destination"].exists() for call in fetches)
    assert not any("/artifacts/" in path or path.endswith("/runs/7001") for _, path, _ in model.requests)
    (tmp_path / "single-native-budget.json").write_bytes(encoded({
        "before_source": before_source[0], "total_api_requests": apis[0]._requests,
        "snapshot_fetches": len(fetches), "store_writes": model.writes,
        "source_requests": receipt["source_requests"]}))
