"""Dated metadata host: finite real transport/custody paths over synthetic inputs.

No real supplier, credentials, or remote write. Retire with the one-shot
calendar extension; shared Relay and Store protections retain their owners.
"""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_custody import restore_market_query
from decision_kernel.application.market_relay_inputs import qualified_trade_calendar
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubRateLimits
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import ftshare_calendar_cli as cli
from test_ftshare_check_cli import FakeGitHubAPI, context, NOW
from test_market_collection_inputs import CollectionOpener
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY, Response


PRIVATE = "private supplier message with secret marker 913245670"
KEY = "synthetic-calendar-secret-only"


class CalendarAPI(FakeGitHubAPI):
    def __init__(self):
        super().__init__()
        self.run["display_title"] = cli.CALENDAR_TITLE
        self.history = [deepcopy(self.run)]
        self._requests = 0
        self.rate_limits = GitHubRateLimits()
        self.rate_limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "5000", "x-ratelimit-reset": "1893456000"})

    def request(self, method, path, payload=None):
        self._requests += 1
        self.rate_limits.debit("core")
        return super().request(method, path, payload)


class Host:
    def __init__(self, tmp_path):
        self.event, self.env = context(tmp_path)
        self.event["inputs"].update(operation="ftshare-calendar", selection=json.dumps({"study": cli.STUDY}))
        self.env.update(DECISION_OS_STORE_TOKEN="synthetic-store-secret", TUSHARE_PROXY_API_KEY=KEY)
        self.out = tmp_path / "ftshare-calendar"
        self.api = CalendarAPI()
        self.opener = CollectionOpener(through=cli.END)
        self.client = RelayHTTP(opener=self.opener, credential=KEY, request_timeout_seconds=90)
        self.backend = MemoryBackend()
        self.store = VersionedStore(self.backend)
        self.fresh = lambda: VersionedStore(MemoryBackend(self.backend.shared))
        self.opened, self.created = [], 0
        self.capacity = lambda _: {"admit_work_unit": True}

    def open(self, api):
        self.opened.append(api)
        return self.store, self.fresh

    def create_client(self):
        self.created += 1
        return self.client

    def run(self, **kwargs):
        return cli.run_runtime(self.event, self.env, self.out, api=self.api,
            open_store=self.open, client_factory=self.create_client,
            capacity=self.capacity, clock=lambda: NOW, monotonic=lambda: 10, **kwargs)

    def report(self):
        return json.loads((self.out / "result.json").read_bytes())

    def control(self):
        return json.loads((self.out / "control.json").read_bytes())


@pytest.mark.parametrize("where,key,value", [
    ("env", "GITHUB_REPOSITORY", "someone/other"),
    ("env", "GITHUB_REF", "refs/heads/other"),
    ("env", "GITHUB_EVENT_NAME", "schedule"),
    ("env", "GITHUB_WORKFLOW_REF", "untrusted/workflow"),
    ("env", "GITHUB_SHA", "b" * 40),
    ("env", "GITHUB_RUN_ATTEMPT", "2"),
    ("env", "GITHUB_RUN_ID", "01234"),
    ("env", "DECISION_OS_MARKET_BINDING_JSON", "PRIVATE_BINDING"),
    ("env", "FTSHARE_API_KEY", "unneeded-price-secret"),
    ("input", "operation", "ftshare-sync"),
    ("input", "access_and_custody_confirmed", "false"),
    ("input", "public_rights_confirmed", "true"),
    ("input", "work_units", "2"),
    ("input", "repair_request", {}),
    ("input", "repair_request", "retry"),
    ("input", "selection", '{"study":"another-calendar"}'),
    ("input", "selection", '{"study":"ftshare-calendar-continuity-20261018","end":"20261019"}'),
    ("input", "endpoint", "https://untrusted.invalid"),
])
def test_wrong_scope_has_zero_native_source_and_store_actions(tmp_path, where, key, value):
    host = Host(tmp_path)
    (host.env if where == "env" else host.event["inputs"])[key] = value
    with pytest.raises(RecordError):
        host.run()
    assert host.api.calls == host.opener.calls == host.opened == []
    assert host.created == 0 and not host.out.exists()


@pytest.mark.parametrize("mode", ["public", "main-moved", "old-ci", "wrong-title", "attempt-two", "prior-run"])
def test_native_identity_and_global_one_shot_inventory_precede_store_and_secret(tmp_path, mode):
    host = Host(tmp_path)
    if mode == "public":
        host.api.metadata["private"] = False
    elif mode == "main-moved":
        host.api.main_sha = "b" * 40
    elif mode == "old-ci":
        host.api.ci.insert(0, {**host.api.ci[0], "conclusion": "failure"})
    elif mode == "wrong-title":
        host.api.run["display_title"] = "FTShare daily check 20261018"
    elif mode == "attempt-two":
        host.api.run["run_attempt"] = 2
    else:
        host.api.history.extend([{"id": n, "display_title": "another operation"} for n in range(99)])
        host.api.history.append({**host.api.run, "id": 1, "status": "completed"})
    assert host.run() == 2
    assert host.opener.calls == host.opened == [] and host.created == 0
    assert host.report()["source_requests"] == 0


@pytest.mark.parametrize("mode", ["native-size-unknown", "full", "local-budget", "remote-budget", "quota-unknown"])
def test_capacity_and_shared_quota_reject_before_source_credential(tmp_path, mode):
    host = Host(tmp_path)
    if mode == "native-size-unknown":
        host.capacity = market_capacity
    elif mode == "full":
        host.capacity = lambda _: {"admit_work_unit": False}
    elif mode == "local-budget":
        host.api._requests = cli.SOURCE_ADMISSION_API_REQUESTS
    elif mode == "remote-budget":
        host.api.rate_limits._resources["core"]["remaining"] = 100
    else:
        host.api.rate_limits = GitHubRateLimits()
    assert host.run() == 2
    assert host.opener.calls == [] and host.created == 0
    assert host.report()["reserved_requests"] == host.report()["source_requests"] == 0
    assert host.backend.shared["count"] == 0


def test_two_exact_metadata_calls_preserve_custody_and_full_calendar_without_weekday_inference(tmp_path, capsys):
    host = Host(tmp_path)
    # A deliberately synthetic Sunday opening must survive unchanged. The host
    # accepts explicit source metadata, never infers a weekend closure.
    host.opener.rows["trade_cal", "SZSE"][-1]["is_open"] = 1
    assert host.run() == 0
    report, control = host.report(), host.control()
    assert report["status"] == "retained" and report["private_release"] == "verified-private-only"
    assert report["reserved_requests"] == report["source_requests"] == report["retained_queries"] == 2
    assert report["price_source_requests"] == 0 and report["legacy_collection_changed"] is False
    assert report["retry_allowed"] is False and host.created == 1 and host.opened == [host.api]
    assert set(control["calendar_references"]) == {"SSE", "SZSE"}
    for venue, (api, params) in zip(cli.VENUES, host.opener.calls, strict=True):
        assert api == "trade_cal" and params == {"exchange": venue, "start_date": "20260701",
            "end_date": "20261018", "fields": "exchange,cal_date,is_open", "limit": "5000"}
        ref = control["calendar_references"][venue]
        restored = restore_market_query(ref, REGISTRY, host.fresh())
        assert encoded(restored["reference"]) == encoded(ref)
        assert json.loads(encoded(asdict(SourceRef.from_dict(ref)))) == ref
        assert len(restored["page_references"]) == 1 and restored["original_http_restored"] is False
        days = qualified_trade_calendar(restored["result"], registry=REGISTRY, exchange=venue,
            start=cli.START, end=cli.END)
        assert ("2026-10-18" in days) is (venue == "SZSE")
        assert len(restored["result"]["capture"]["pages"][0]["table"]["items"]) == 110
    assert capsys.readouterr().out == ""
    safe = json.dumps(report)
    assert not any(value in safe for value in (KEY, host.env[cli.TOKEN_ENV], "source-views/", "manifest", PRIVATE))
    assert not any(path.startswith("source-views/market-collection-")
        for path in host.backend.paths(host.backend.head()))
    assert len(list(host.out.iterdir())) == 2


@pytest.mark.parametrize("failure_at,retained", [("SSE", 0), ("SZSE", 1)])
def test_one_transport_attempt_per_venue_and_failure_stops_remaining(tmp_path, failure_at, retained, capsys):
    host = Host(tmp_path)
    host.opener.fail = ("trade_cal", failure_at)
    host.opener.once = True  # Would succeed on a retry; the host may not take it.
    assert host.run() == 2
    report = host.report()
    assert report["retained_queries"] == retained
    assert report["source_requests"] == host.client.requests == len(host.opener.calls) == retained + 1
    assert report["reason_code"] == "RELAY_HTTP_UNAVAILABLE"
    assert set(host.control()["calendar_references"]) == ({"SSE"} if retained else set())
    assert PRIVATE not in json.dumps(report) and capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["missing-last-date", "duplicate-date", "wrong-exchange", "empty", "invalid-open"])
def test_calendar_semantics_fail_before_retention_and_before_second_request(tmp_path, mode):
    host = Host(tmp_path)
    rows = host.opener.rows["trade_cal", "SSE"]
    if mode == "missing-last-date":
        rows.pop()
    elif mode == "duplicate-date":
        rows.append(deepcopy(rows[-1]))
    elif mode == "wrong-exchange":
        rows[-1]["exchange"] = "SZSE"
    elif mode == "empty":
        rows.clear()
    else:
        rows[-1]["is_open"] = "UNKNOWN"
    assert host.run() == 2
    assert len(host.opener.calls) == 1 and host.backend.shared["count"] == 0
    assert host.report()["retained_queries"] == 0 and host.control()["calendar_references"] == {}


def test_reflected_secret_is_rejected_and_private_prose_never_reaches_outputs(tmp_path, capsys):
    host = Host(tmp_path)
    class Reflected:
        def open(self, request, timeout):
            return Response(request.full_url, {"code": 0, "data": {"fields": [], "items": []},
                "message": KEY + PRIVATE})
    host.client = RelayHTTP(opener=Reflected(), credential=KEY)
    assert host.run() == 2
    assert host.report()["source_requests"] == 1 and host.report()["reason_code"] == "RELAY_SECRET_REFLECTED"
    for path in host.out.iterdir():
        text = path.read_text()
        assert KEY not in text and PRIVATE not in text and host.env[cli.TOKEN_ENV] not in text
    assert capsys.readouterr().out == ""


def test_full_first_page_cannot_open_a_second_page_or_the_other_exchange(tmp_path):
    host = Host(tmp_path)
    host.opener.rows["trade_cal", "SSE"] = [deepcopy(host.opener.rows["trade_cal", "SSE"][0])
        for _ in range(5000)]
    assert host.run() == 2
    assert host.report()["reason_code"] == "SOURCE_PAGE_BOUND_REACHED"
    assert host.client.requests == len(host.opener.calls) == 1
    assert host.backend.shared["count"] == 0


def test_ambiguous_store_write_is_not_retried_and_stops_source_budget(tmp_path, monkeypatch):
    host = Host(tmp_path)
    calls = []
    def unknown(*args, **kwargs):
        calls.append(1)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    monkeypatch.setattr(cli, "retain_market_query", unknown)
    assert host.run() == 2
    assert calls == [1] and len(host.opener.calls) == 1
    assert host.report()["status"] == "persistence-unconfirmed"
    assert host.report()["reason_code"] == "REMOTE_WRITE_UNCONFIRMED"
    assert host.control()["calendar_references"] == {}


def test_native_quota_remains_shared_after_first_custody(tmp_path, monkeypatch):
    host = Host(tmp_path)
    original = cli.retain_market_query
    def retained(*args, **kwargs):
        result = original(*args, **kwargs)
        host.api.rate_limits._resources["core"]["remaining"] = 4
        return result
    monkeypatch.setattr(cli, "retain_market_query", retained)
    assert host.run() == 2
    assert host.opened == [host.api] and host.created == 1 and host.client.requests == 1
    assert host.report()["status"] == "partial"
    assert host.report()["reason_code"] == "FTSHARE_CALENDAR_STORE_RATE_LIMIT"
    assert set(host.control()["calendar_references"]) == {"SSE"}


def test_visibility_reversal_after_response_blocks_retention_and_release(tmp_path):
    host = Host(tmp_path)
    host.opener.before = lambda *_: host.api.metadata.update(private=False)
    assert host.run() == 2
    assert host.report()["status"] == "release-blocked"
    assert host.report()["private_release"] == "not-verified"
    assert host.client.requests == 1 and host.backend.shared["count"] == 0


def test_consumed_local_run_cannot_restart_and_output_cannot_escape_runner_temp(tmp_path):
    host = Host(tmp_path)
    host.opener.fail = "denied"
    assert host.run() == 2
    original = (host.out / "result.json").read_bytes()
    with pytest.raises(RecordError, match="FTSHARE_CALENDAR_OUTPUT"):
        host.run()
    assert host.client.requests == 1 and (host.out / "result.json").read_bytes() == original
    host.out = tmp_path / "another-output"
    before = len(host.api.calls)
    with pytest.raises(RecordError, match="FTSHARE_CALENDAR_ALREADY_CONSUMED"):
        host.run()
    assert len(host.api.calls) == before and host.client.requests == 1
    host.out = tmp_path.parent / "outside-runner"
    with pytest.raises(RecordError, match="FTSHARE_CALENDAR_OUTPUT"):
        host.run()
    host.out = tmp_path / "symlink-out"
    host.out.symlink_to(tmp_path / "nonexistent")
    with pytest.raises(RecordError, match="FTSHARE_CALENDAR_OUTPUT"):
        host.run()


def test_real_native_git_capacity_and_fresh_store_restore_both_exact_query_refs(tmp_path):
    host = Host(tmp_path)
    native = tmp_path / "calendar-records.git"
    host.store = VersionedStore(GitStore.init_synthetic(native))
    host.fresh = lambda: VersionedStore(GitStore(native))
    host.capacity = market_capacity
    assert host.run() == 0
    assert host.report()["capacity"]["native_bytes"] > 0
    refs = host.control()["calendar_references"]
    for venue in cli.VENUES:
        restored = restore_market_query(refs[venue], REGISTRY, VersionedStore(GitStore(native)))
        assert encoded(restored["reference"]) == encoded(refs[venue])
        assert restored["result"]["use"]["parameters"]["end_date"] == cli.END
    assert host.client.requests == 2
