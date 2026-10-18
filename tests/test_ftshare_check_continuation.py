"""One original study allowance, original predecessor custody and a finite continuation."""
from copy import deepcopy
from io import BytesIO
import json
import zipfile

import pytest

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.interfaces import ftshare_check_continuation_cli as cli
from test_ftshare_check_cli import FakeFTShare, FakeGitHubAPI, NOW, baseline, context


def documents():
    old_baseline = baseline(None)
    request = {"study": cli.STUDY, "repository": cli.REPO, "code_sha": cli.PREDECESSOR_CODE,
        "run_id": str(cli.PREDECESSOR_RUN), "attempt": 1, "source_budget": 6,
        "plan": cli.check_plan(old_baseline["symbols"]), "baseline_sha256": digest(encoded(old_baseline))}
    result = {**{k: request[k] for k in ("study", "repository", "code_sha", "run_id", "attempt", "source_budget")},
        "schema": "ftshare-daily-check-v1", "status": "failed", "source_requests": 1,
        "records_written": False, "active_source_changed": False, "raw_release": "private-artifact-only",
        "required_candle_checks": 5, "qualified_candle_checks": 0,
        "results": [{"id": "raw-pair", "status": "failed", "error": "FTSHARE_HTTP_403",
            "transport": {"http_status": 403}}] + [
            {"id": spec["id"], "status": "not-requested"} for spec in request["plan"][1:]]}
    return {"baseline.json": old_baseline, "request.json": request, "result.json": result}


def archive(docs, *, extra=None):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, value in docs.items():
            bundle.writestr(name, encoded(value))
        if extra is not None:
            bundle.writestr(*extra)
    return output.getvalue()


class Native(FakeGitHubAPI):
    def __init__(self):
        super().__init__()
        self.run["display_title"] = cli.CONTINUATION_TITLE
        self.history = [deepcopy(self.run), {"id": cli.PREDECESSOR_RUN, "display_title": cli.TITLE}]
        self.old_run = {"id": cli.PREDECESSOR_RUN, "run_attempt": 1, "head_sha": cli.PREDECESSOR_CODE,
            "head_branch": "main", "event": "workflow_dispatch", "path": cli.WORKFLOW,
            "display_title": cli.TITLE, "status": "completed", "conclusion": "failure",
            "repository": {"id": 1420041744}}
        self.old_artifact = {"id": cli.PREDECESSOR_ARTIFACT,
            "name": f"dos2-ftshare-check-{cli.PREDECESSOR_RUN}-1", "expired": False,
            "size_in_bytes": cli.PREDECESSOR_BYTES, "digest": "sha256:" + cli.PREDECESSOR_SHA256,
            "workflow_run": {"id": cli.PREDECESSOR_RUN, "head_sha": cli.PREDECESSOR_CODE,
                "repository_id": 1420041744, "head_repository_id": 1420041744}}

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None
        if path == cli.PREFIX + "/actions/runs/" + str(cli.PREDECESSOR_RUN):
            self.calls.append(path)
            return deepcopy(self.old_run)
        if path == cli.PREFIX + "/actions/artifacts/" + str(cli.PREDECESSOR_ARTIFACT):
            self.calls.append(path)
            return deepcopy(self.old_artifact)
        return super().request(method, path, payload)


class Host:
    def __init__(self, tmp_path):
        self.event, self.env = context(tmp_path)
        self.event["inputs"].update(operation="ftshare-check-continue",
            selection=json.dumps({"study": cli.STUDY, "predecessor_run": cli.PREDECESSOR_RUN}))
        self.out, self.api, self.client = tmp_path / "continuation", Native(), FakeFTShare()
        self.prior = cli._predecessor_documents(documents())
        self.prior_reads = 0
        self.waits = []

    def load_prior(self, api, selected):
        self.prior_reads += 1
        return deepcopy(self.prior)

    def execute(self, stage, **kwargs):
        return cli.execute(stage, self.event, self.env, self.out, api=self.api,
            client_factory=lambda: self.client, predecessor_loader=self.load_prior,
            baseline_loader=baseline, clock=lambda: NOW, monotonic=lambda: 10.0, sleep=self.waits.append, **kwargs)

    def read(self):
        return json.loads((self.out / "result.json").read_bytes())


@pytest.mark.parametrize("where,key,value", [
    ("env", "GITHUB_RUN_ATTEMPT", "2"), ("env", "GITHUB_REF", "refs/heads/feature"),
    ("env", "GITHUB_RUN_ID", str(cli.PREDECESSOR_RUN)), ("input", "operation", "ftshare-check"),
    ("input", "work_units", "2"), ("input", "repair_request", "restart"),
    ("input", "public_rights_confirmed", True), ("input", "access_and_custody_confirmed", 1),
    ("input", "selection", '{"study":"ftshare-daily-qualification-20261018","predecessor_run":99}'),
    ("input", "source_budget", 6),
])
def test_fixed_scope_and_original_attempt_cannot_be_reopened(tmp_path, where, key, value):
    host = Host(tmp_path)
    (host.env if where == "env" else host.event["inputs"])[key] = value
    with pytest.raises(RecordError):
        host.execute("prepare")
    assert host.api.calls == [] and host.prior_reads == host.client.requests == 0


def test_continuation_has_its_own_unique_native_title_not_another_original_check(tmp_path):
    host = Host(tmp_path)
    host.api.history.append({**host.api.run, "id": 777})
    with pytest.raises(RecordError, match="ALREADY_ATTEMPTED"):
        host.execute("prepare")
    assert host.prior_reads == 0


@pytest.mark.parametrize("family,key,value", [
    ("old_run", "run_attempt", 2), ("old_run", "conclusion", "success"),
    ("old_run", "head_sha", "a" * 40), ("old_run", "path", ".github/workflows/other.yml"),
    ("old_artifact", "digest", "sha256:" + "a" * 64), ("old_artifact", "size_in_bytes", 1),
    ("old_artifact", "expired", True),
])
def test_native_predecessor_mismatch_prevents_any_archive_download(tmp_path, family, key, value):
    host = Host(tmp_path)
    getattr(host.api, family)[key] = value
    selected = cli.settings(host.event, host.env)
    downloads = []
    with pytest.raises(RecordError):
        cli.load_predecessor(host.api, selected, archive_loader=lambda *args: downloads.append(args))
    assert downloads == []


def test_native_original_zip_must_match_the_fixed_real_digest_not_supplied_metadata(tmp_path):
    host = Host(tmp_path)
    downloads = []
    def wrong(repository, artifact):
        downloads.append((repository, artifact))
        return archive(documents())
    with pytest.raises(RecordError, match="ARCHIVE_DIGEST"):
        cli.load_predecessor(host.api, cli.settings(host.event, host.env), archive_loader=wrong)
    assert downloads == [(cli.REPO, cli.PREDECESSOR_ARTIFACT)]


def test_bounded_archive_parser_preserves_exact_documents_and_rejects_tamper_or_extra_member():
    docs = documents()
    raw = archive(docs)
    restored = cli._archive_documents(raw, sha256=digest(raw), size=len(raw))
    assert cli._predecessor_documents(restored)["identity"]["source_requests"] == 1
    with pytest.raises(RecordError, match="DIGEST"):
        cli._archive_documents(raw + b"x", sha256=digest(raw), size=len(raw))
    unsafe = archive(docs, extra=("../outside", b"untrusted"))
    with pytest.raises(RecordError, match="MEMBERS"):
        cli._archive_documents(unsafe, sha256=digest(unsafe), size=len(unsafe))


@pytest.mark.parametrize("mutation", ["zero", "two", "other-error", "another-request", "request-budget", "baseline"])
def test_predecessor_document_inspection_cannot_forge_unused_allowance(mutation):
    docs = documents()
    if mutation in {"zero", "two"}:
        docs["result.json"]["source_requests"] = 0 if mutation == "zero" else 2
    elif mutation == "other-error":
        docs["result.json"]["results"][0]["error"] = "FTSHARE_HTTP_429"
    elif mutation == "another-request":
        docs["result.json"]["results"][1]["status"] = "failed"
    elif mutation == "request-budget":
        docs["request.json"]["source_budget"] = 7
    else:
        docs["baseline.json"]["scope_count"] += 1
    with pytest.raises(RecordError):
        cli._predecessor_documents(docs)


def test_prepare_and_release_are_source_free_and_five_successes_use_six_total(tmp_path, capsys):
    host = Host(tmp_path)
    assert host.execute("prepare") == 0
    assert host.client.credential_checks == host.client.requests == 0
    assert host.read()["cumulative_source_requests"] == 1
    assert host.execute("capture") == 0
    report = host.read()
    assert report["schema"] == cli.SCHEMA and report["status"] == "validated"
    assert report["source_requests"] == 5 and report["prior_source_requests"] == 1
    assert report["cumulative_source_requests"] == report["source_budget"] == 6
    assert report["remaining_source_requests"] == 0 and report["continuation_budget"] == 5
    assert len(host.client.calls) == 5 and all(call[0] == "candlesticks" for call in host.client.calls)
    assert report["qualified_candle_checks"] == 5 and report["factor_qualification_granted"] is False
    assert not report["records_written"] and not report["active_source_changed"]
    for row, raw in zip(report["results"], host.client.wire):
        assert (host.out / row["raw_file"]).read_bytes() == raw
        assert row["raw_sha256"] == digest(raw)
    before = host.client.credential_checks
    assert host.execute("release") == 0
    assert host.prior_reads == 3 and host.client.requests == 5 and host.client.credential_checks == before
    printed = capsys.readouterr().out
    assert '"results"' not in printed and "synthetic_private_marker" not in printed
    assert host.read()["raw_release"] == "private-artifact-only"
    assert host.waits == [2, 2, 2, 2]


def test_another_403_is_two_of_six_and_never_reopens_capture(tmp_path):
    host = Host(tmp_path)
    class Forbidden(FakeFTShare):
        def candlesticks_batch(self, *args, **kwargs):
            self.requests += 1
            self.last_error_meta = {"http_status": 403, "classification": "auth-or-entitlement"}
            raise RecordError("FTSHARE_HTTP_403")
    host.client = Forbidden()
    host.execute("prepare")
    assert host.execute("capture") == 2
    result = host.read()
    assert result["source_requests"] == 1 and result["cumulative_source_requests"] == 2
    assert result["remaining_source_requests"] == 4 and result["predecessor"] == cli.predecessor_identity()
    assert result["results"][0]["error"] == "FTSHARE_HTTP_403"
    assert all(row["status"] == "not-requested" for row in result["results"][1:])
    with pytest.raises(RecordError, match="ALREADY_CONSUMED"):
        host.execute("capture")
    assert host.client.requests == 1


def test_same_native_run_cannot_replenish_allowance_by_changing_output_directory(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    assert host.execute("capture") == 0 and host.client.requests == 5
    host.out = tmp_path / "second-output"
    host.client = FakeFTShare()
    assert host.execute("prepare") == 0
    with pytest.raises(RecordError, match="ALREADY_STARTED"):
        host.execute("capture")
    assert host.client.requests == host.client.credential_checks == 0


def test_source_stop_header_keeps_unrequested_rows_and_does_not_wait_for_quota(tmp_path):
    host = Host(tmp_path)
    def exhausted(number):
        host.client.last_response_meta["rate_limits"] = {"key_remaining": {"state": "valid", "remaining": 0}}
    host.client.after_response = exhausted
    host.execute("prepare")
    assert host.execute("capture") == 2
    result = host.read()
    assert result["status"] == "partial" and result["cumulative_source_requests"] == 2
    assert result["stop_reason"] == "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"
    assert result["results"][1]["status"] == "not-requested" and host.waits == []


def test_changed_repeat_or_quarantined_body_stops_five_check_qualification(tmp_path):
    host = Host(tmp_path)
    def changed(number, body):
        if number == 2:
            body["data"][0][1][0]["close"] = "12.20"
    host.client.edit = changed
    host.execute("prepare")
    assert host.execute("capture") == 2
    result = host.read()
    assert result["qualified_candle_checks"] == 1 and result["cumulative_source_requests"] == 3
    assert result["results"][1]["error"] == "FTSHARE_REPEAT_CHANGED"
    assert all(row["status"] == "not-requested" for row in result["results"][2:])


def test_changed_prepared_plan_and_baseline_cannot_reset_or_expand_the_study(tmp_path):
    host = Host(tmp_path)
    host.execute("prepare")
    request = json.loads((host.out / "request.json").read_bytes())
    request["plan"].append(request["plan"][0])
    (host.out / "request.json").write_bytes(encoded(request))
    with pytest.raises(RecordError, match="PREPARED_REQUEST_CHANGED"):
        host.execute("capture")
    assert host.client.requests == 0


def test_privacy_reversal_after_response_does_not_release_raw_bytes(tmp_path):
    host = Host(tmp_path)
    host.client.after_response = lambda number: host.api.metadata.update(private=False)
    host.execute("prepare")
    assert host.execute("capture") == 2
    assert host.read()["cumulative_source_requests"] == 2
    assert not list(host.out.glob("*.raw.json"))
    with pytest.raises(RecordError, match="PRIVATE_REPOSITORY"):
        host.execute("release")
