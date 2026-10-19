"""Finite private FTShare check host; retire with this explicit source check.

Native identity, source requests and exact response bodies stay separate.  The
fixtures are synthetic responses, while the production host and daily qualifier
execute unchanged.  No provider credential, network or store write is needed.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces import ftshare_check_cli as cli


SHA = "a" * 40
RUN_ID = 12345
REPOSITORY_ID = 1420041744
SOURCE_PRIVATE_TEXT = "unpublished source body: do not log 987654321"
SYMBOLS = sorted([f"000{n:03d}.SZ" for n in range(1, 51)]
                 + [f"600{n:03d}.SH" for n in range(50)])
NOW = "2026-10-18T15:00:00+00:00"


def context(tmp_path):
    event = {"inputs": {"operation": "ftshare-check", "code_sha": SHA,
        "selection": json.dumps({"study": cli.STUDY}), "work_units": "1",
        "access_and_custody_confirmed": "true", "public_rights_confirmed": "false"}}
    env = {"GITHUB_REPOSITORY": cli.REPO, "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_REF": cli.REPO + "/" + cli.WORKFLOW + "@refs/heads/main",
        "GITHUB_RUN_ID": str(RUN_ID), "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_TEMP": str(tmp_path)}
    return event, env


class FakeGitHubAPI:
    """Small native GET model; a write or unexpected route fails the test."""

    def __init__(self):
        self.calls = []
        self.metadata = {"id": REPOSITORY_ID, "full_name": cli.REPO,
            "private": True, "archived": False, "default_branch": "main"}
        self.main_sha = SHA
        self.run = {"id": RUN_ID, "head_sha": SHA, "head_branch": "main",
            "run_attempt": 1, "event": "workflow_dispatch", "path": cli.WORKFLOW,
            "display_title": cli.TITLE, "status": "in_progress",
            "repository": {"id": REPOSITORY_ID}}
        self.ci = [{"id": 12300, "head_sha": SHA, "head_branch": "main",
            "event": "push", "path": ".github/workflows/ci.yml",
            "status": "completed", "conclusion": "success"}]
        self.history = [deepcopy(self.run)]
        self.unbounded_inventory = False

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None
        self.calls.append(path)
        parsed = urlsplit(path)
        if parsed.path == cli.PREFIX:
            return deepcopy(self.metadata)
        if parsed.path == cli.PREFIX + "/git/ref/heads/main":
            return {"object": {"sha": self.main_sha}}
        if parsed.path == cli.PREFIX + "/actions/runs/" + str(RUN_ID):
            return deepcopy(self.run)
        if parsed.path == cli.PREFIX + "/actions/workflows/ci.yml/runs":
            assert parse_qs(parsed.query) == {"branch": ["main"], "event": ["push"],
                "head_sha": [SHA], "per_page": ["5"]}
            return {"workflow_runs": deepcopy(self.ci)}
        if parsed.path == cli.PREFIX + "/actions/workflows/ashare-data.yml/runs":
            query = parse_qs(parsed.query)
            assert query["event"] == ["workflow_dispatch"] and query["per_page"] == ["100"]
            if self.unbounded_inventory:
                rows = [{"id": n, "display_title": "other operation"} for n in range(100)]
            else:
                page = int(query["page"][0])
                rows = self.history[(page - 1) * 100:page * 100]
            return {"workflow_runs": deepcopy(rows)}
        raise AssertionError("unexpected native route: " + path)


def baseline(_api):
    return {"reference": cli.BASE_REFERENCE, "symbols": SYMBOLS,
            "scope_count": 4000, "daily": {"synthetic_private_marker": SOURCE_PRIVATE_TEXT}}


def source_bar():
    def millis(clock):
        return int(datetime.fromisoformat("2026-10-16T" + clock + "+08:00").timestamp() * 1000)
    return {"open": "12.00", "high": "12.30", "low": "11.90", "close": "12.10",
            "volume": 123456, "turnover": "1493817.60",
            "ts_millis_open": millis("09:30:00"), "ts_millis": millis("15:00:00")}


class FakeFTShare:
    def __init__(self, *, edit=None, fail_at=None, after_response=None):
        self.requests = 0
        self.calls = []
        self.wire = []
        self.credential_checks = 0
        self.last_response_bytes = None
        self.last_response_meta = None
        self.last_error_meta = None
        self.edit, self.fail_at, self.after_response = edit, fail_at, after_response

    def validate_credential(self):
        self.credential_checks += 1

    def _request(self, api, params):
        self.requests += 1
        self.calls.append((api, deepcopy(params)))
        if self.requests == self.fail_at:
            self.last_error_meta = {"reason": "FTSHARE_HTTP_UNAVAILABLE", "attempts": 1}
            raise RuntimeError(SOURCE_PRIVATE_TEXT)
        if api == "candlesticks":
            body = {"code": 200, "message": "success", "data": [
                [symbol, [source_bar()]] for symbol in params["symbols"]]}
        else:
            body = {"code": 200, "message": "success", "data": []}
        if self.edit:
            self.edit(self.requests, body)
        # Indentation and a terminal newline prove retention of original bytes,
        # instead of a JSON decode/encode reconstruction of the source body.
        raw = json.dumps(body, ensure_ascii=False, indent=3).encode() + b"\n"
        self.last_response_bytes = raw
        self.last_response_meta = {"http_status": 200, "body_bytes": len(raw)}
        self.wire.append(raw)
        if self.after_response:
            self.after_response(self.requests)
        return body

    def candlesticks_batch(self, symbols, *, since, until, adjust_kind):
        return self._request("candlesticks", {"symbols": list(symbols), "since": since,
            "until": until, "adjust_kind": adjust_kind})

    def stock_adjust_factor(self, symbol, *, since, until, page, page_size):
        return self._request("factors", {"symbol": symbol, "since": since, "until": until,
            "page": page, "page_size": page_size})


class Host:
    def __init__(self, tmp_path):
        self.event, self.env = context(tmp_path)
        self.out = tmp_path / "ftshare-check"
        self.api = FakeGitHubAPI()
        self.client = FakeFTShare()
        self.waits = []

    def execute(self, stage, **options):
        return cli.execute(stage, self.event, self.env, self.out, api=self.api,
            baseline_loader=baseline, client_factory=lambda: self.client,
            sleep=self.waits.append, monotonic=lambda: 10.0, clock=lambda: NOW, **options)

    def read(self):
        return json.loads((self.out / "result.json").read_bytes())


@pytest.mark.parametrize("where,key,value", [
    ("env", "GITHUB_REPOSITORY", "someone/Decision-OS-2"),
    ("env", "GITHUB_REF", "refs/heads/feat/10-ftshare-daily"),
    ("env", "GITHUB_EVENT_NAME", "schedule"),
    ("env", "GITHUB_WORKFLOW_REF", "untrusted/host@refs/heads/main"),
    ("env", "GITHUB_SHA", "b" * 40),
    ("env", "GITHUB_RUN_ATTEMPT", "2"),
    ("env", "GITHUB_RUN_ID", "012345"),
    ("input", "code_sha", "main"),
    ("input", "access_and_custody_confirmed", "false"),
    ("input", "public_rights_confirmed", "true"),
    ("input", "work_units", "2"),
    ("input", "repair_request", "restart exhausted task"),
    ("input", "selection", '{"study":"other-study"}'),
    ("input", "base_url", "https://untrusted.invalid"),
])
def test_wrong_host_scope_or_authority_stops_before_any_native_or_source_request(tmp_path, where, key, value):
    host = Host(tmp_path)
    (host.env if where == "env" else host.event["inputs"])[key] = value
    with pytest.raises(RecordError):
        host.execute("prepare")
    assert host.api.calls == [] and host.client.calls == []
    assert not host.out.exists()


@pytest.mark.parametrize("where,key,value", [
    ("metadata", "id", REPOSITORY_ID + 1),
    ("metadata", "private", False),
    ("metadata", "archived", True),
    ("metadata", "default_branch", "other"),
    ("run", "head_sha", "b" * 40),
    ("run", "run_attempt", 2),
    ("run", "path", ".github/workflows/untrusted.yml"),
    ("run", "status", "completed"),
    ("run", "display_title", "other operation"),
    ("run", "repository", {"id": REPOSITORY_ID + 1}),
    ("ci", "conclusion", "failure"),
    ("ci", "head_sha", "b" * 40),
    ("ci", "event", "pull_request"),
])
def test_native_reality_must_match_declared_identity_before_preparation(tmp_path, where, key, value):
    host = Host(tmp_path)
    target = host.api.ci[0] if where == "ci" else getattr(host.api, where)
    target[key] = value
    with pytest.raises(RecordError):
        host.execute("prepare")
    assert host.client.calls == [] and not host.out.exists()


def test_native_check_refuses_moved_main_and_missing_ci(tmp_path):
    host = Host(tmp_path)
    host.api.main_sha = "b" * 40
    with pytest.raises(RecordError, match="FTSHARE_MAIN_MOVED"):
        host.execute("prepare")
    host.api.main_sha = SHA
    host.api.ci = []
    with pytest.raises(RecordError, match="FTSHARE_MAIN_CI_REQUIRED"):
        host.execute("prepare")
    assert host.client.calls == []


def test_prior_dispatch_on_later_page_cannot_reset_finite_check(tmp_path):
    host = Host(tmp_path)
    unrelated = [{"id": n, "display_title": "another operation"} for n in range(99)]
    host.api.history += unrelated + [{**host.api.run, "id": RUN_ID - 1, "status": "completed"}]
    with pytest.raises(RecordError, match="FTSHARE_CHECK_ALREADY_ATTEMPTED"):
        host.execute("prepare")
    pages = [parse_qs(urlsplit(p).query)["page"] for p in host.api.calls
             if "/actions/workflows/ashare-data.yml/runs?" in p]
    assert pages == [["1"], ["2"]] and host.client.calls == []


def test_incomplete_native_inventory_stops_at_finite_page_bound(tmp_path):
    host = Host(tmp_path)
    host.api.unbounded_inventory = True
    with pytest.raises(RecordError, match="FTSHARE_RUN_INVENTORY_INCOMPLETE"):
        host.execute("prepare")
    assert sum("/actions/workflows/ashare-data.yml/runs?" in p for p in host.api.calls) == 10
    assert host.client.calls == []


def test_prepare_does_not_touch_credentials_and_capture_keeps_exact_private_bytes(tmp_path, capsys):
    host = Host(tmp_path)
    assert host.execute("prepare") == 0
    assert host.client.credential_checks == 0 and host.client.calls == []
    assert host.read()["source_requests"] == 0
    assert host.read()["status"] == "prepared-no-source-requests"
    assert host.execute("capture") == 0
    report = host.read()
    assert report["status"] == "validated"
    assert report["source_requests"] == len(host.client.calls) == cli.MAX_REQUESTS == 6
    assert host.waits == [2] * 5
    assert [len(p["symbols"]) for api, p in host.client.calls if api == "candlesticks"] == [2, 2, 100, 50, 50]
    assert [p["adjust_kind"] for api, p in host.client.calls if api == "candlesticks"] == ["none"] * 4 + ["forward"]
    assert report["results"][1]["repeat_equal"] is True
    assert report["results"][-1]["factor_qualification_granted"] is False
    assert report["account_daily_limit_verified"] is False
    assert report["long_term_uptime_certified"] is False
    assert report["active_source_changed"] is False and report["records_written"] is False
    assert report["raw_release"] == "not-released"
    for row, raw in zip(report["results"], host.client.wire, strict=True):
        assert (host.out / row["raw_file"]).read_bytes() == raw
        assert row["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["raw_bytes"] == len(raw)
    assert capsys.readouterr().out == ""
    assert host.execute("release") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["raw_release"] == "private-artifact-only"
    assert "results" not in summary and SOURCE_PRIVATE_TEXT not in json.dumps(summary)
    assert host.read()["raw_release"] == "private-artifact-only"


def test_prepared_plan_or_baseline_mutation_is_refused_before_source(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    request_path = host.out / "request.json"
    original = request_path.read_bytes()
    request = json.loads(original)
    request["plan"].append(deepcopy(request["plan"][0]))
    request_path.write_text(json.dumps(request))
    with pytest.raises(RecordError, match="FTSHARE_PREPARED_INPUT_CHANGED"):
        host.execute("capture")
    request_path.write_bytes(original)
    baseline_path = host.out / "baseline.json"
    changed = json.loads(baseline_path.read_bytes())
    changed["scope_count"] += 1
    baseline_path.write_text(json.dumps(changed))
    with pytest.raises(RecordError, match="FTSHARE_PREPARED_INPUT_CHANGED"):
        host.execute("capture")
    assert host.client.calls == [] and host.client.credential_checks == 0


def test_capture_cannot_repeat_or_reopen_its_consumed_local_state(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.fail_at = 1
    assert host.execute("capture") == 2
    previous = (host.out / "result.json").read_bytes()
    with pytest.raises(RecordError, match="FTSHARE_CHECK_ALREADY_CONSUMED"):
        host.execute("capture")
    assert host.client.requests == 1 and (host.out / "result.json").read_bytes() == previous


def test_transport_failure_stops_remaining_requests_without_source_error_prose(tmp_path, capsys):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.fail_at = 3
    assert host.execute("capture") == 2
    report = host.read()
    assert report["status"] == "partial" and report["source_requests"] == 3
    assert report["results"][2]["error"] == "FTSHARE_CHECK_FAILED"
    assert [row["status"] for row in report["results"][3:]] == ["not-requested"] * 3
    assert len(list(host.out.glob("*.raw.json"))) == 2
    assert SOURCE_PRIVATE_TEXT not in json.dumps(report)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["repeat_changed", "quarantined_row", "all_empty", "known_pair_empty"])
def test_unreliable_data_is_not_a_successful_reliability_check(tmp_path, mode):
    def edit(number, body):
        if mode == "repeat_changed" and number == 2:
            body["data"][0][1][0]["close"] = "12.20"
        elif mode == "quarantined_row" and number == 1:
            body["data"][0][1][0]["close"] = "NaN"
        elif mode == "all_empty":
            for pair in body["data"]:
                pair[1] = []
        elif mode == "known_pair_empty" and number == 1:
            body["data"][0][1] = []
    host = Host(tmp_path)
    host.client.edit = edit
    host.execute("prepare")
    assert host.execute("capture") == 2
    report = host.read()
    failed_at = 2 if mode == "repeat_changed" else 1
    assert report["status"] in ("failed", "partial")
    assert report["source_requests"] == failed_at
    assert report["results"][failed_at - 1]["status"] == "failed"
    assert all(row["status"] == "not-requested" for row in report["results"][failed_at:])
    # A failed qualification still retains the original received bytes privately.
    assert (host.out / report["results"][failed_at - 1]["raw_file"]).read_bytes() == host.client.wire[-1]


def test_visibility_reversal_after_http_stops_raw_write_and_private_release(tmp_path, capsys):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.after_response = lambda _: host.api.metadata.update(private=False)
    assert host.execute("capture") == 2
    report = host.read()
    assert report["source_requests"] == 1 and report["status"] == "failed"
    assert report["results"][0]["error"] == "FTSHARE_PRIVATE_REPOSITORY_REQUIRED"
    assert list(host.out.glob("*.raw.json")) == []
    with pytest.raises(RecordError, match="FTSHARE_PRIVATE_REPOSITORY_REQUIRED"):
        host.execute("release")
    assert host.read()["raw_release"] == "not-released"
    assert capsys.readouterr().out == ""


def test_output_escape_and_symlink_input_are_refused(tmp_path):
    host = Host(tmp_path)
    host.out = tmp_path.parent / "outside-ftshare-check"
    with pytest.raises(RecordError, match="FTSHARE_CHECK_OUTPUT_PATH"):
        host.execute("prepare")
    assert host.api.calls == []
    host.out = tmp_path / "ftshare-check"
    host.execute("prepare")
    request = host.out / "request.json"
    saved = tmp_path / "saved-request.json"
    request.rename(saved)
    request.symlink_to(saved)
    with pytest.raises(RecordError, match="FTSHARE_CHECK_REGULAR_INPUT"):
        host.execute("capture")
    assert host.client.calls == []


def test_nonfresh_client_is_refused_without_a_provider_call(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.requests = 1
    with pytest.raises(RecordError, match="FTSHARE_FRESH_CLIENT_REQUIRED"):
        host.execute("capture")
    assert host.client.calls == []
    assert host.read()["status"] == "prepared-no-source-requests"


def test_missing_original_body_cannot_be_reconstructed_and_released(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.after_response = lambda _: setattr(host.client, "last_response_bytes", None)
    assert host.execute("capture") == 2
    assert host.read()["results"][0]["error"] == "FTSHARE_ORIGINAL_BODY_REQUIRED"
    assert host.client.requests == 1
    assert list(host.out.glob("*.raw.json")) == []


def test_workflow_keeps_finite_check_read_only_and_secret_only_at_capture():
    workflow = yaml.safe_load((Path(__file__).parents[1] / cli.WORKFLOW).read_text())
    # PyYAML's YAML 1.1 resolver reads the GitHub Actions key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert "ftshare-check" in triggers["workflow_dispatch"]["inputs"]["operation"]["options"]
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {"group": "decision-os-relay-source", "cancel-in-progress": False}
    job = workflow["jobs"]["ftshare-check"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert job["timeout-minutes"] <= 30
    for clause in ["github.repository == 'auguspp/Decision-OS-2'", "github.ref == 'refs/heads/main'",
                   "inputs.operation == 'ftshare-check'", "inputs.access_and_custody_confirmed == true",
                   "github.event.repository.private == true", "inputs.public_rights_confirmed == false"]:
        assert clause in job["if"]
    denied = workflow["jobs"]["rejected-sync"]
    for clause in ["inputs.operation == 'ftshare-check'", "inputs.access_and_custody_confirmed != true",
                   "github.event.repository.private != true", "inputs.public_rights_confirmed != false"]:
        assert clause in denied["if"]
    assert denied["permissions"] == {"contents": "read"}
    assert "exit 1" in denied["steps"][0]["run"]
    holders = [(name, step.get("id")) for name, candidate in workflow["jobs"].items()
               for step in candidate["steps"] if "FTSHARE_API_KEY" in json.dumps(step)]
    assert holders == [("ftshare-check", "capture"), ("ftshare-check-continue", "capture"),
                       ("ftshare-check-kernel", "capture"), ("ftshare-sync", "runtime")]
    relay_holders = [(name, step.get("id")) for name, candidate in workflow["jobs"].items()
                     for step in candidate["steps"] if "TUSHARE_PROXY_API_KEY" in json.dumps(step)]
    assert relay_holders == [("sync", None), ("repair", None), ("ftshare-calendar", "runtime")]
    steps = {step["id"]: step for step in job["steps"] if "id" in step}
    assert steps["capture"]["env"]["FTSHARE_API_KEY"] == "${{ secrets.FTSHARE_API_KEY }}"
    assert "FTSHARE_API_KEY" not in workflow.get("env", {})
    assert all("FTSHARE_API_KEY" not in candidate.get("env", {}) for candidate in workflow["jobs"].values())
    for stage in ("prepare", "capture", "release"):
        assert "decision_kernel.interfaces.ftshare_check_cli " + stage in steps[stage]["run"]
    assert all(not step.get("continue-on-error") for step in job["steps"])
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] is False


def test_workflow_uploads_original_private_artifact_only_after_native_release_check():
    workflow = yaml.safe_load((Path(__file__).parents[1] / cli.WORKFLOW).read_text())
    steps = workflow["jobs"]["ftshare-check"]["steps"]
    release_index = next(i for i, step in enumerate(steps) if step.get("id") == "release")
    assert steps[release_index]["if"] == "always() && steps.prepare.outcome == 'success'"
    upload = steps[release_index + 1]
    assert re.fullmatch(r"actions/upload-artifact@[0-9a-f]{40}", upload["uses"])
    assert upload["if"] == "always() && steps.release.outcome == 'success'"
    assert upload["with"]["path"] == "${{ runner.temp }}/dos2-ftshare-check/"
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in upload["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] <= 30


@pytest.mark.parametrize("quota", ["key_remaining", "ip_remaining"])
def test_success_response_with_exhausted_quota_stops_before_second_request(tmp_path, quota):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.after_response = lambda _: host.client.last_response_meta.update(
        rate_limits={quota: {"state": "valid", "remaining": 0}})
    assert host.execute("capture") == 2
    report = host.read()
    assert report["status"] == "partial" and report["source_requests"] == 1
    assert report["results"][0]["status"] == "qualified-response"
    assert report["results"][1]["error"] == "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"
    assert host.waits == [] and host.client.requests == 1
    assert all(row["status"] == "not-requested" for row in report["results"][2:])


@pytest.mark.parametrize("instruction,reason", [
    (("valid", 90), "FTSHARE_WAIT_EXCEEDS_CHECK_BOUND"),
    (("invalid", None), "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"),
    (("withheld", None), "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"),
])
def test_long_or_unusable_retry_instruction_never_becomes_shorter_wait_or_retry(tmp_path, instruction, reason):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.after_response = lambda _: setattr(host.client, "last_retry_after", instruction)
    assert host.execute("capture") == 2
    report = host.read()
    assert report["source_requests"] == host.client.requests == 1
    assert report["results"][1]["error"] == reason
    assert host.waits == []
    assert (host.out / report["results"][0]["raw_file"]).read_bytes() == host.client.wire[0]


def test_valid_short_retry_after_is_honored_between_distinct_planned_requests(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    host.client.after_response = lambda _: setattr(host.client, "last_retry_after", ("valid", 4))
    assert host.execute("capture") == 0
    assert host.waits == [4] * 5 and host.client.requests == 6
    assert host.read()["status"] == "validated"

