"""External entry admission, metadata isolation, and the original durable budget."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from decision_kernel.application import daily_clock as clock
from decision_kernel.application import daily_market as daily
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces import daily_visibility_cli as visibility
from decision_kernel.interfaces import daily_reading_cli as reading
from decision_kernel.interfaces import daily_discovery_cli as discovery
from test_daily_market import NOW
from test_daily_market_schedule import WindowHost, native_runtime
from test_daily_visibility import NativeAPI as VisibilityAPI, context
from test_runtime_publication import grant


def configured(tmp_path, monkeypatch, primary=clock.GITHUB_SCHEDULE):
    approved = {"schema": clock.CLOCK_SCHEMA, "primary": primary}
    path = tmp_path / "clock.json"
    path.write_bytes(encoded(approved))
    monkeypatch.setattr(visibility, "CLOCK_POLICY_FILE", path)
    return approved


def probe_native(tmp_path, monkeypatch):
    approved = json.loads(cli.POLICY_FILE.read_text())
    event, env, api = native_runtime(tmp_path, monkeypatch, approved,
        invocation=clock.CLOCK_PROBE, admitted_at=NOW, actual_at=NOW)
    event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api.run.update(name=clock.PROBE_RUN_TITLE, display_title=clock.PROBE_RUN_TITLE)
    api.main.update(ref="refs/heads/main")
    api.main["object"]["type"] = "commit"
    return event, env, api


@pytest.mark.parametrize("primary,invocation", [
    (clock.GITHUB_SCHEDULE, clock.EXTERNAL_CLOCK), (clock.EXTERNAL_CRON, "schedule"),
])
def test_other_clock_stops_before_native_api_or_source_credentials(tmp_path, monkeypatch, primary, invocation):
    approved_clock = configured(tmp_path, monkeypatch, primary)
    event, env = context(invocation, scheduled=invocation == "schedule")
    if invocation == clock.EXTERNAL_CLOCK:
        event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI(scheduled=invocation == "schedule")
    with pytest.raises(RecordError, match="DAILY_CLOCK_NOT_PRIMARY"):
        visibility.preflight(event, env, api, use_authorization=True, clock=approved_clock)
    assert api.calls == []
    with pytest.raises(RecordError, match="DAILY_CLOCK_NOT_PRIMARY"):
        cli.run_runtime(event, env, tmp_path / "forbidden", api=api,
            execute=lambda **_: pytest.fail("The other clock cannot execute a cycle."))
    assert api.calls == [] and not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("invocation", ["schedule", clock.EXTERNAL_CLOCK])
@pytest.mark.parametrize("fault", ["old-checkout", "failed-main-ci"])
def test_queued_clock_cannot_publish_admission_after_main_or_ci_changes(
        tmp_path, monkeypatch, capsys, invocation, fault):
    configured(tmp_path, monkeypatch,
        clock.GITHUB_SCHEDULE if invocation == "schedule" else clock.EXTERNAL_CRON)
    event, env = context(invocation, scheduled=invocation == "schedule")
    if invocation == clock.EXTERNAL_CLOCK:
        event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI(private=True, scheduled=invocation == "schedule")
    api.run.update(name=clock.run_title(invocation), display_title=clock.run_title(invocation))
    if fault == "old-checkout":
        api.main["object"]["sha"] = "b" * 40
    else:
        api.ci["workflow_runs"][0]["conclusion"] = "failure"
    event_path, output = tmp_path / "event.json", tmp_path / "output"
    event_path.write_bytes(encoded(event))
    env.update(GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(output),
        DECISION_OS_STORE_TOKEN="synthetic-clock-only")
    assert visibility.main(["--use-runtime-authorization"], env=env, api_factory=lambda _: api) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == ("FTSHARE_MAIN_MOVED" if fault == "old-checkout" else "DAILY_MARKET_MAIN_CI")
    assert not output.exists() and report["source_requests"] == 0
    assert report["records_read"] is report["records_written"] is False


@pytest.mark.parametrize("invocation,supplied", [
    *((name, clock.CURRENT_MAIN) for name in sorted(clock.MANUAL_OPERATIONS)),
    (clock.EXTERNAL_CLOCK, "a" * 40), (clock.CLOCK_PROBE, "a" * 40),
])
def test_floating_code_marker_is_not_a_general_manual_permission(tmp_path, monkeypatch, invocation, supplied):
    approved_clock = configured(tmp_path, monkeypatch, clock.EXTERNAL_CRON)
    event, env = context(invocation)
    event["inputs"]["code_sha"] = supplied
    api = VisibilityAPI()
    with pytest.raises(RecordError, match="CODE_SHA"):
        visibility.preflight(event, env, api, clock=approved_clock)
    with pytest.raises(RecordError, match="CODE_SHA"):
        cli.settings(event, env, json.loads(cli.POLICY_FILE.read_text()),
            clock=approved_clock, allow_probe=True)
    assert api.calls == []


@pytest.mark.parametrize("primary", [clock.GITHUB_SCHEDULE, clock.EXTERNAL_CRON])
@pytest.mark.parametrize("private", [False, True])
def test_probe_preflight_never_admits_a_business_job(tmp_path, monkeypatch, primary, private):
    approved = configured(tmp_path, monkeypatch, primary)
    event, env = context(clock.CLOCK_PROBE)
    event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI(private=private)
    api.run.update(name=clock.PROBE_RUN_TITLE, display_title=clock.PROBE_RUN_TITLE)
    monkeypatch.setattr(visibility, "native_publication_basis",
        lambda *a, **k: pytest.fail("Metadata has no business-publication prerequisite."))
    report, code = visibility.preflight(event, env, api, use_authorization=True, clock=approved)
    assert code == 0 and report["status"] == "clock-probe-metadata-only"
    assert report["runtime_authorized"] is report["admitted"] is False
    assert report["source_requests"] == 0 and report["records_read"] is report["records_written"] is False
    assert report["clock_primary"] == primary and report["external_origin_verified"] is False
    assert len(api.calls) == 3


@pytest.mark.parametrize("invocation", [clock.CLOCK_PROBE, clock.EXTERNAL_CLOCK])
@pytest.mark.parametrize("field", ["name", "display_title"])
def test_clock_preflight_rejects_a_title_from_the_other_operation(tmp_path, monkeypatch, invocation, field):
    approved = configured(tmp_path, monkeypatch, clock.EXTERNAL_CRON)
    event, env = context(invocation)
    event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI()
    api.run.update(name=clock.run_title(invocation), display_title=clock.run_title(invocation))
    other = clock.EXTERNAL_CLOCK if invocation == clock.CLOCK_PROBE else clock.CLOCK_PROBE
    api.run[field] = clock.run_title(other)
    with pytest.raises(RecordError, match="DAILY_VISIBILITY_NATIVE_RUN"):
        visibility.preflight(event, env, api, use_authorization=True, clock=approved)
    assert len(api.calls) == 2


@pytest.mark.parametrize("primary", [clock.GITHUB_SCHEDULE, clock.EXTERNAL_CRON])
def test_probe_uses_only_native_metadata_even_without_business_grant(tmp_path, monkeypatch, primary):
    configured(tmp_path, monkeypatch, primary)
    event, env, api = probe_native(tmp_path, monkeypatch)
    api.meta["private"] = False
    api.run["private_payload"] = "FORBIDDEN-PROVIDER-BODY"
    from decision_kernel.infrastructure import runtime_publication
    monkeypatch.setattr(runtime_publication, "authorization",
        lambda: pytest.fail("Probe must not open a business authorization or data binding."))
    monkeypatch.setattr(cli, "remote_units", lambda *a, **k: pytest.fail("No Store may open."))
    monkeypatch.setattr(cli, "RelayRegistry", lambda *a, **k: pytest.fail("No registry is needed."))
    out = tmp_path / "probe"
    assert cli.run_clock_probe(event, env, out, api=api) == 0
    report = json.loads((out / "probe.json").read_bytes())
    assert {p.name for p in out.iterdir()} == {"probe.json", "probe.md"}
    assert report["clock_primary"] == primary and report["native_proof"]["main_ci_run"] == 6001
    assert report["code_sha"] == env["GITHUB_SHA"] and report["event"] == "workflow_dispatch"
    assert report["invocation"] == clock.CLOCK_PROBE and report["external_origin_verified"] is False
    assert report["source_requests"] == report["dispatches"] == report["notifications"] == 0
    assert report["records_read"] is report["records_written"] is report["business_execution"] is False
    assert len(api.calls) == 8
    assert all(path in {cli.PREFIX, cli.PREFIX + "/git/ref/heads/main",
        cli.PREFIX + "/actions/runs/8001"} or path.startswith(cli.PREFIX + "/actions/workflows/ci.yml/runs?")
        for path in api.calls)
    output = (out / "probe.json").read_text() + (out / "probe.md").read_text()
    assert "FORBIDDEN-PROVIDER-BODY" not in output and env[cli.TOKEN_ENV] not in output


@pytest.mark.parametrize("fault", ["main-moved", "wrong-ref", "wrong-object-type", "ci-failed",
                                    "name-mismatch", "legacy-name", "title-mismatch", "changes-before-output"])
def test_probe_cannot_release_stale_native_identity(tmp_path, monkeypatch, fault):
    configured(tmp_path, monkeypatch)
    event, env, api = probe_native(tmp_path, monkeypatch)
    if fault == "main-moved":
        api.main["object"]["sha"] = "b" * 40
    elif fault == "wrong-ref":
        api.main["ref"] = "refs/heads/other"
    elif fault == "wrong-object-type":
        api.main["object"]["type"] = "tag"
    elif fault == "ci-failed":
        api.ci["workflow_runs"][0]["conclusion"] = "failure"
    elif fault == "name-mismatch":
        api.run["name"] = clock.EXTERNAL_RUN_TITLE
    elif fault == "legacy-name":
        api.run["name"] = clock.RUN_TITLE
    elif fault == "title-mismatch":
        api.run["display_title"] = clock.EXTERNAL_RUN_TITLE
    else:
        original = api.request
        def moved(method, path, payload=None):
            if len(api.calls) == 4:
                api.main["object"]["sha"] = "b" * 40
            return original(method, path, payload)
        monkeypatch.setattr(api, "request", moved)
    out = tmp_path / "invalid"
    with pytest.raises(RecordError, match="DAILY_MARKET_NATIVE_RUN"
            if fault in {"name-mismatch", "legacy-name", "title-mismatch"} else None):
        cli.run_clock_probe(event, env, out, api=api)
    assert not out.exists()


@pytest.mark.parametrize("consumer", [cli, reading, discovery])
def test_probe_is_rejected_by_every_business_consumer_before_data_access(tmp_path, monkeypatch, consumer):
    configured(tmp_path, monkeypatch)
    event, env, api = probe_native(tmp_path, monkeypatch)
    with pytest.raises(RecordError, match="DAILY_CLOCK_PROBE_ONLY"):
        consumer.run_runtime(event, env, tmp_path / "forbidden", api=api)
    assert api.calls == [] and not (tmp_path / "forbidden").exists()


def test_missing_clock_file_never_silently_uses_legacy_production_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(visibility, "CLOCK_POLICY_FILE", tmp_path / "missing.json")
    event, env = context("update")
    event_path = tmp_path / "event.json"
    event_path.write_bytes(encoded(event))
    env.update(GITHUB_EVENT_PATH=str(event_path), DECISION_OS_STORE_TOKEN="synthetic-only")
    assert visibility.main(["--use-runtime-authorization"], env=env,
        api_factory=lambda _: pytest.fail("Missing policy cannot open native admission.")) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    with pytest.raises(RecordError):
        cli.run_runtime(event, env, tmp_path / "forbidden")
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("bad", [None, {}, {"schema": clock.CLOCK_SCHEMA, "primary": "both"},
    {"schema": clock.CLOCK_SCHEMA, "primary": []},
    {"schema": clock.CLOCK_SCHEMA, "primary": clock.EXTERNAL_CRON, "source_budget": 200}])
def test_malformed_clock_policy_never_opens_a_route(bad):
    event, env = context(clock.EXTERNAL_CLOCK)
    event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI()
    with pytest.raises(RecordError):
        if bad is None:
            # Omitted pure-helper config is legacy GitHub-only, not an enable switch.
            visibility.preflight(event, env, api)
        else:
            visibility.preflight(event, env, api, clock=bad)
    assert api.calls == []


def invoke_external(host, tmp_path, monkeypatch, number):
    root = tmp_path / str(number)
    root.mkdir()
    event, env, api = native_runtime(root, monkeypatch, host.approved,
        invocation=clock.EXTERNAL_CLOCK, admitted_at=host.now, actual_at=host.now)
    run_id = 8001 + number
    env["GITHUB_RUN_ID"], api.run["id"] = str(run_id), run_id
    original = api.request
    def request(method, path, payload=None):
        if path == cli.PREFIX + "/actions/runs/" + str(run_id):
            path = cli.PREFIX + "/actions/runs/8001"
        return original(method, path, payload)
    monkeypatch.setattr(api, "request", request)
    def execute(**options):
        return host.run(operation=options["operation"], admission_at=options["admission_at"],
            allow_late=options["allow_late"], progress=options["progress"])
    out = root / "daily"
    result = cli.run_runtime(event, env, out, api=api, execute=execute)
    return result, json.loads((out / "result.json").read_bytes())


def test_second_external_run_reuses_completed_cycle_without_source_or_records_replay(tmp_path, monkeypatch):
    host = WindowHost()
    code, first = invoke_external(host, tmp_path, monkeypatch, 1)
    assert code == 0 and first["status"] == "complete"
    head = host.fixture["backend"].head()
    cycle = daily.latest_cycle(host.fixture["fresh"]())
    counts = len(host.calendar_clients), len(host.price_clients)
    code, second = invoke_external(host, tmp_path, monkeypatch, 2)
    assert second["run_id"] != first["run_id"] and code == 0
    assert second["status"] == "already-complete" and second["source_requests"] == 0
    assert (len(host.calendar_clients), len(host.price_clients)) == counts
    assert daily.latest_cycle(host.fixture["fresh"]()) == cycle
    assert host.fixture["backend"].head() == head


def test_failed_external_cycle_stays_held_on_same_and_next_business_day(tmp_path, monkeypatch):
    host = WindowHost()
    host.calendar_error = RecordError("RELAY_AUTH_OR_ENTITLEMENT")
    code, first = invoke_external(host, tmp_path, monkeypatch, 1)
    assert code == 2 and first["source_requests"] == 1
    cycle = daily.latest_cycle(host.fixture["fresh"]())
    assert cycle["cycle"]["status"] == "blocked"
    head = host.fixture["backend"].head()
    counts = len(host.calendar_clients), len(host.price_clients)
    host.calendar_error = None
    for number, at in enumerate([NOW, "2026-10-20T12:10:00+00:00"], 2):
        host.now = at
        code, receipt = invoke_external(host, tmp_path, monkeypatch, number)
        assert code == 2 and receipt["status"] == "source-held" and receipt["source_requests"] == 0
        assert receipt["cycle_date"] == first["cycle_date"]
        assert daily.latest_cycle(host.fixture["fresh"]()) == cycle
        assert host.fixture["backend"].head() == head
        assert (len(host.calendar_clients), len(host.price_clients)) == counts


def test_unknown_external_reservation_never_reopens_same_or_next_day_budget(tmp_path, monkeypatch):
    host = WindowHost()
    original = host.fixture["store"].put_source
    def lost(submission, source, **kwargs):
        value = original(submission, source, **kwargs)
        if source.source_id.endswith("-r001"):
            raise UnknownWrite("SYNTHETIC_RESERVATION_RESPONSE_LOST")
        return value
    monkeypatch.setattr(host.fixture["store"], "put_source", lost)
    code, first = invoke_external(host, tmp_path, monkeypatch, 1)
    assert code == 2 and first["status"] == "persistence-unconfirmed"
    cycle = daily.latest_cycle(host.fixture["fresh"]())
    head = host.fixture["backend"].head()
    monkeypatch.setattr(host.fixture["store"], "put_source", original)
    for number, at in enumerate([NOW, "2026-10-20T12:10:00+00:00"], 2):
        host.now = at
        code, receipt = invoke_external(host, tmp_path, monkeypatch, number)
        assert code == 2 and receipt["status"] == "persistence-unconfirmed"
        assert receipt["reason_code"] == "DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED"
        assert receipt["source_requests"] == 0 and host.calendar_clients == host.price_clients == []
        assert daily.latest_cycle(host.fixture["fresh"]()) == cycle
        assert host.fixture["backend"].head() == head


def test_external_public_route_and_release_use_existing_revocable_authority(tmp_path, monkeypatch, grant):
    approved_clock = configured(tmp_path, monkeypatch, clock.EXTERNAL_CRON)
    event, env = context(clock.EXTERNAL_CLOCK)
    event["inputs"]["code_sha"] = clock.CURRENT_MAIN
    api = VisibilityAPI()
    api.run.update(name=clock.EXTERNAL_RUN_TITLE, display_title=clock.EXTERNAL_RUN_TITLE)
    report, code = visibility.preflight(event, env, api, use_authorization=True, clock=approved_clock)
    assert code == 0 and report["operation"] == "update" and report["invocation"] == clock.EXTERNAL_CLOCK
    assert report["runtime_authorized"] is report["admitted"] is True
    assert report["publication_authority"] == grant[0]["authorization_ref"]
    assert report["native_proof"]["main_ci_run"] == 6001 and len(api.calls) == 7
    event_file = tmp_path / "event.json"
    event_file.write_bytes(encoded(event))
    for name, value in {**env, "GITHUB_EVENT_PATH": str(event_file)}.items():
        monkeypatch.setenv(name, value)
    from test_ftshare_collection_runtime import NativeAPI
    native = NativeAPI()
    native.meta["private"] = False
    cli.check_private(native, {"code_sha": env["GITHUB_SHA"]})
    assert native.calls == [cli.PREFIX, cli.PREFIX + "/git/ref/heads/main"]
    doc, path = grant
    path.write_text(json.dumps({**doc, "enabled": False}))
    report, code = visibility.preflight(event, env, api, use_authorization=True, clock=approved_clock)
    assert code == 2 and report["runtime_authorized"] is report["admitted"] is False


def test_workflow_probe_is_a_separate_read_only_job_with_exact_native_markers():
    workflow = yaml.load(Path(cli.WORKFLOW).read_text(), Loader=yaml.BaseLoader)
    probe = workflow["jobs"][clock.CLOCK_PROBE]
    assert probe["permissions"] == {"contents": "read", "actions": "read"}
    assert probe["needs"] == "visibility" and "needs.visibility.result == 'success'" in probe["if"]
    assert "inputs.operation == 'clock-probe'" in probe["if"]
    assert "secrets." not in json.dumps(probe)
    assert not any(word in json.dumps(probe) for word in ("daily_reading_cli", "daily_discovery_cli", " runtime --out"))
    for name in ("status", "daily"):
        gate = workflow["jobs"][name]["if"]
        assert "needs.visibility.outputs.admitted == 'true'" in gate
        assert "clock-probe" not in gate
    assert clock.EXTERNAL_RUN_TITLE in workflow["run-name"] and clock.PROBE_RUN_TITLE in workflow["run-name"]
    artifact = next(step["with"]["name"] for step in probe["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert artifact == "dos2-daily-clock-${{ github.run_id }}-${{ github.run_attempt }}"
