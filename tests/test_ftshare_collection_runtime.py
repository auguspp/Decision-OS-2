"""Native admission and finite FTShare execution with real retained source controls."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from decision_kernel.application.ftshare_collection import latest_ftshare_reference, restore_ftshare_collection
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubRateLimits
from decision_kernel.interfaces import ftshare_collection_cli as cli
from test_ftshare_collection import _capture, _setup, STAMP
from test_market_collection_runtime import InventoriedBackend
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY

SHA = "a" * 40
STUDY_SHA = "b" * 40


def config():
    return {"schema": cli.SCHEMA, "collection_id": "ftshare-cn-eod", "generation": "g20261016",
        "legacy_reference": deepcopy(cli.BASE_REFERENCE), "qualification": {
            "run_id": 7001, "artifact_id": 9001, "sha256": "c" * 64, "code_sha": STUDY_SHA}}


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
        self.main = {"object": {"sha": SHA}}
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
