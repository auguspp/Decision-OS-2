"""Kernel route replay, two retained refusals, four calls and no quota reset."""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import shutil

import pytest
import yaml

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.interfaces import ftshare_kernel_check_cli as cli
from test_ftshare_check_cli import FakeGitHubAPI, NOW, baseline, context, source_bar
from test_ftshare_check_continuation import documents as first_documents


def prior():
    original = cli.prior_check._predecessor_documents(first_documents())
    selected = {"study": cli.STUDY, "repository": cli.REPO, "code_sha": cli.PREDECESSOR_CODE,
        "run_id": str(cli.PREDECESSOR_RUN), "attempt": 1, "source_budget": 6, "continuation_budget": 5}
    b = original["documents"]["baseline.json"]
    request = cli.prior_check._prepared(selected, original, b)
    result = {**selected, "schema": cli.prior_check.SCHEMA, "predecessor": original["identity"],
        "status": "failed", "source_requests": 1, "reserved_requests": 1, "prior_source_requests": 1,
        "cumulative_source_requests": 2, "qualified_candle_checks": 0, "required_candle_checks": 5,
        "records_written": False, "active_source_changed": False, "raw_release": "private-artifact-only",
        "results": [{"id": "raw-pair", "status": "failed", "source_requests": 1,
            "error": "FTSHARE_HTTP_403", "transport": {"http_status": 403}}] + [
                {"id": spec["id"], "status": "not-requested"} for spec in request["plan"][1:]]}
    docs = {"baseline.json": b, "request.json": request, "result.json": result, "predecessor.json": original}
    return cli.predecessor_documents(docs, original)


def verification(_api=None, _baseline=None):
    # Synthetic declared sessions: production only reads native exchange calendars.
    first, last = date(2026, 7, 1), date(2026, 10, 16)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    sessions = days[-61:]
    return {"close": {"000001.SZ": "12.10", "600000.SH": "12.10"},
        "sessions": {"SSE": sessions, "SZSE": sessions}, "scope_count": 4000}


def bars():
    rows = []
    for day in verification()["sessions"]["SZSE"]:
        row = source_bar()
        since, _ = cli.date_bounds(day.replace("-", ""), day.replace("-", ""))
        row.update(ts_millis_open=since + 34200000, ts_millis=since + 54000000)
        rows.append(row)
    return rows


class Client:
    def __init__(self):
        self.requests = self.credential_checks = 0
        self.calls, self.wire = [], []
        self.last_response_bytes = self.last_response_meta = self.last_error_meta = None
        self.fail_at = self.edit = self.after = None
        self.last_retry_after = ("missing", None)

    def validate_credential(self):
        self.credential_checks += 1

    def candlesticks(self, **args):
        self.requests += 1
        self.calls.append(args)
        if self.requests == self.fail_at:
            self.last_error_meta = {"http_status": 403, "error_diagnostics": {
                "business_code": 403, "category": "permission-denied"}}
            raise RecordError("FTSHARE_HTTP_403")
        payload = {"code": 200, "data": bars()}
        if self.edit:
            self.edit(self.requests, payload)
        raw = json.dumps(payload, indent=3).encode() + b"\n"
        self.wire.append(raw)
        self.last_response_bytes = raw
        self.last_response_meta = {"http_status": 200, "wire_bytes": len(raw)}
        if self.after:
            self.after(self.requests)
        return payload


class Host:
    def __init__(self, tmp_path):
        self.event, self.env = context(tmp_path)
        self.event["inputs"].update(operation="ftshare-check-kernel", selection=json.dumps({
            "study": cli.STUDY, "route": cli.ROUTE, "predecessor_run": cli.PREDECESSOR_RUN}))
        self.api = FakeGitHubAPI()
        self.api.run["display_title"] = cli.TITLE
        self.api.history = [deepcopy(self.api.run)]
        self.out, self.client = tmp_path / "kernel", Client()
        self.prior, self.waits = prior(), []

    def execute(self, stage):
        return cli.execute(stage, self.event, self.env, self.out, api=self.api,
            client_factory=lambda: self.client, predecessor_loader=lambda *_: deepcopy(self.prior),
            baseline_loader=baseline, verification_loader=verification,
            sleep=self.waits.append, clock=lambda: NOW, monotonic=lambda: 1.0)

    def read(self):
        return json.loads((self.out / "result.json").read_bytes())


def test_four_whole_range_requests_retain_close_repeat_and_both_previous_failures(tmp_path, capsys):
    host = Host(tmp_path)
    assert host.execute("prepare") == 0
    assert host.client.requests == host.client.credential_checks == 0
    assert host.execute("capture") == 0
    report = host.read()
    assert report["status"] == "validated"
    assert report["source_requests"] == report["reserved_requests"] == report["qualified_candle_checks"] == 4
    assert report["prior_source_requests"] == 2 and report["cumulative_source_requests"] == report["source_budget"] == 6
    assert report["remaining_source_requests"] == 0
    assert report["predecessor"] == cli.predecessor_identity()
    assert host.client.calls[0] == host.client.calls[1]
    assert [call["symbol"] for call in host.client.calls] == ["000001.SZ", "000001.SZ", "600000.SH", "000001.SZ"]
    assert [call["adjust_kind"] for call in host.client.calls] == ["none", "none", "none", "forward"]
    assert host.waits == [2, 2, 2]
    assert report["results"][1]["repeat_equal"]
    assert all(row["close_matches_baseline"] for row in report["results"][:3])
    for row, raw in zip(report["results"], host.client.wire):
        assert (host.out / row["raw_file"]).read_bytes() == raw
        assert row["raw_sha256"] == digest(raw)
        capture = json.loads((host.out / (row["id"] + ".capture.json")).read_bytes())
        assert capture["body_utf8"].encode() == raw
        assert "/v1/" in capture["request"]["url"] and "/batch" not in capture["request"]["url"]
    assert not report["full_market_coverage_certified"] and not report["long_term_uptime_certified"]
    assert not report["records_written"] and not report["active_source_changed"]
    assert host.execute("release") == 0
    assert host.client.requests == 4 and host.client.credential_checks == 1
    assert host.read()["raw_release"] == "private-artifact-only"
    assert '"results"' not in capsys.readouterr().out


@pytest.mark.parametrize("at", [1, 3, 4])
def test_rejection_stops_later_slots_preserves_real_diagnostic_and_cannot_repeat(tmp_path, at):
    host = Host(tmp_path)
    host.client.fail_at = at
    host.execute("prepare")
    assert host.execute("capture") == 2
    report = host.read()
    assert host.client.requests == at and report["cumulative_source_requests"] == at + 2
    assert report["results"][at - 1]["error"] == "FTSHARE_HTTP_403"
    assert report["results"][at - 1]["transport"]["error_diagnostics"]["business_code"] == 403
    assert all(row["status"] == "not-requested" for row in report["results"][at:])
    with pytest.raises(RecordError, match="CONSUMED"):
        host.execute("capture")
    assert host.client.requests == at


@pytest.mark.parametrize("mutation", ["incomplete", "close", "repeat", "empty"])
def test_real_history_close_and_repeat_requirements_can_each_stop_the_route(tmp_path, mutation):
    host = Host(tmp_path)
    def edit(n, payload):
        if mutation == "incomplete":
            payload["data"].pop(0)
        elif mutation == "empty":
            payload["data"].clear()
        elif mutation == "close":
            payload["data"][-1]["close"] = "12.20"
        elif n == 2:
            payload["data"][0]["close"] = "12.20"
    host.client.edit = edit
    host.execute("prepare")
    assert host.execute("capture") == 2
    assert host.client.requests == (2 if mutation == "repeat" else 1)
    assert (host.out / host.read()["results"][-(4 if mutation != "repeat" else 3)]["raw_file"]).is_file()


@pytest.mark.parametrize("where,key,value", [
    ("env", "GITHUB_RUN_ATTEMPT", "2"), ("env", "GITHUB_REF", "refs/heads/feat/other"),
    ("input", "operation", "ftshare-check-continue"), ("input", "work_units", "2"),
    ("input", "access_and_custody_confirmed", 1), ("input", "public_rights_confirmed", True),
    ("input", "source_budget", 10), ("input", "selection", '{"study":"new-study"}'),
])
def test_scope_is_fixed_before_any_native_or_source_call(tmp_path, where, key, value):
    host = Host(tmp_path)
    (host.env if where == "env" else host.event["inputs"])[key] = value
    with pytest.raises(RecordError):
        host.execute("prepare")
    assert host.api.calls == [] and host.client.requests == 0


def test_second_native_run_and_copied_output_cannot_restore_allowance(tmp_path):
    host = Host(tmp_path)
    host.api.history.append({**host.api.run, "id": 666})
    with pytest.raises(RecordError, match="ALREADY_ATTEMPTED"):
        host.execute("prepare")
    host.api.history.pop()
    host.execute("prepare")
    copied = tmp_path / "copied"
    shutil.copytree(host.out, copied)
    host.client.fail_at = 1
    host.execute("capture")
    host.out = copied
    with pytest.raises(RecordError, match="ALREADY_STARTED"):
        host.execute("capture")
    assert host.client.requests == 1


@pytest.mark.parametrize("field,value", [("source_requests", 0), ("cumulative_source_requests", 1),
                                       ("qualified_candle_checks", 1), ("status", "validated")])
def test_original_rejections_cannot_be_erased_to_fund_new_calls(field, value):
    p = prior()
    p["documents"]["result.json"][field] = value
    with pytest.raises(RecordError):
        cli.predecessor_documents(p["documents"], p["documents"]["predecessor.json"])


def test_changed_price_verification_and_its_prepared_hash_are_rechecked_against_store(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    path = host.out / "verification.json"
    data = json.loads(path.read_bytes())
    data["close"]["000001.SZ"] = "99.00"
    path.write_bytes(encoded(data))
    req = json.loads((host.out / "request.json").read_bytes())
    req["verification_sha256"] = digest(encoded(data))
    (host.out / "request.json").write_bytes(encoded(req))
    with pytest.raises(RecordError, match="VERIFICATION_CHANGED"):
        host.execute("capture")
    assert host.client.requests == host.client.credential_checks == 0


def test_quota_hold_prevents_next_source_even_after_valid_body(tmp_path):
    host = Host(tmp_path)
    host.client.after = lambda _: host.client.last_response_meta.update(
        rate_limits={"key_remaining": {"state": "valid", "remaining": 0}})
    host.execute("prepare")
    assert host.execute("capture") == 2
    assert host.client.requests == 1 and host.read()["stop_reason"] == "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"


