"""Native AI Brief Room composition, exact wire scope and deferred publication."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from decision_kernel.application import hosted_handoff, reading_ai_brief, reading_room
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.github_reading import _ReadingScope
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import research_runtime as runtime, reading_page, daily_reading_cli, reading_commands
from test_hosted_watch_runtime import public_host, full_brief
from test_hosted_native_runtime import payload as hosted_payload
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs, invoke
from test_reading_odds_book import html_model


@pytest.fixture
def saved_brief(public_host, full_brief, tmp_path):
    api, _, _ = public_host
    doc, _ = full_brief
    saved = invoke(api, tmp_path / "save", "save-hosted-handoff", hosted_payload(doc), "author-brief")
    return doc, saved["result"]["reference"]


def run(api, wire, out, doc, ref=None):
    value = {"plan": {"as_of": doc["cutoff"], "ai_brief": ref or {"select": "latest-ai-brief"}},
             "public_research_custody_confirmed": True, "public_sources_custody_confirmed": True}
    return runtime.run_command(api.binding, api=wire, inputs=inputs("reading-room", value),
                               code_sha=CODE, output=out, run_id="123", attempt="1")


def failed(out, doc):
    report = json.loads((out / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report
    assert not (out / "reading-room.html").exists() and not (out / "inputs.json").exists()
    assert doc["body"] not in (out / "report.json").read_text()


@pytest.mark.parametrize("private", [False, True])
def test_original_native_reader_returns_one_body_and_exact_check_without_writes(public_host, saved_brief,
                                                                                tmp_path, monkeypatch, private):
    api, grant, grant_path = public_host
    doc, ref = saved_brief
    if private:
        api.binding = replace(api.binding, private_upgrade_allowed=True)
        api.meta["private"] = True
        grant["enabled"] = False
        grant_path.write_bytes(encoded(grant))
    wire, output = transport(api), tmp_path / "room"
    original, seen = hosted_handoff.open_handoff, []
    def open_original(store, reference):
        assert type(store.backend) is _ReadingScope and store.backend.api is wire
        assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
        seen.append(store.backend)
        return original(store, reference)
    monkeypatch.setattr(hosted_handoff, "open_handoff", open_original)
    count, head = api.writes, api.git.head()
    report = run(api, wire, output, doc, ref)
    panel = report["result"]["ai_brief"]
    assert panel["view"]["document"] == doc and panel["watch_publication_required"] is True
    assert panel["view"]["watch_evidence_adoption"][0]["result"]["status"] == "cannot-determine"
    assert report["repository_visibility"] == ("private" if private else "public")
    assert html_model((output / "reading-room.html").read_text()) == report["result"]
    assert seen and not seen[0].active and 0 < wire._requests < 512
    assert api.writes == count and api.git.head() == head and report["write"] == "not-requested"


def test_handoff_scope_is_required_before_any_room_read_or_input_export(public_host, saved_brief, tmp_path, monkeypatch):
    api, grant, path = public_host
    doc, ref = saved_brief
    grant["scopes"].remove("handoff")
    path.write_bytes(encoded(grant))
    monkeypatch.setattr(ReadingRoom, "build", lambda *a, **kw: pytest.fail("read before admission"))
    out = tmp_path / "denied"
    with pytest.raises(RecordError, match="HOSTED_PRIVATE_CUSTODY_REQUIRED"):
        run(api, transport(api), out, doc, ref)
    assert not out.exists()


@pytest.mark.parametrize("baseline_only", [False, True])
def test_partial_discovery_preserves_opened_candidate_watch_scope(public_host, saved_brief, tmp_path, baseline_only):
    api, grant, path = public_host
    doc, ref = saved_brief
    if baseline_only:
        doc = {**doc, "schema": "hosted-ai-handoff-v1", "execution_date": "2026-01-04",
               "cutoff": "2026-01-04T05:00:00Z", "baseline": ref}
        doc.pop("watch_adoptions")
        invoke(api, tmp_path / "next", "save-hosted-handoff", hosted_payload(doc), "next")
    bad = {**doc, "execution_date": "2026-01-05", "cutoff": "2026-01-05T05:00:00Z", "body": 123}
    # A deliberately corrupt retained fixture tests incomplete discovery; it
    # is not a replacement author document or a business submission.
    local = VersionedStore(api.git)
    ResearchArtifacts(local).put("bad-candidate", ArtifactInput.from_bytes(
        "ai-brief-20260105", "handoff.json", "application/json", encoded(bad)))
    grant["scopes"].remove("watch");path.write_bytes(encoded(grant))
    out = tmp_path / "partial-denied"
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        run(api, transport(api), out, {**doc, "cutoff": "2026-01-05T06:00:00Z"})
    failed(out, doc)


@pytest.mark.parametrize("scope", ["watch", "sources", "research", "brief"])
@pytest.mark.parametrize("baseline_only", [False, True])
def test_actual_and_baseline_watch_need_original_scopes(public_host, saved_brief, tmp_path, scope, baseline_only):
    api, grant, path = public_host
    doc, ref = saved_brief
    if baseline_only:
        doc = {**doc, "schema": "hosted-ai-handoff-v1", "execution_date": "2026-01-04",
               "cutoff": "2026-01-04T05:00:00Z", "baseline": ref}
        doc.pop("watch_adoptions")
        ref = invoke(api, tmp_path / "next", "save-hosted-handoff", hosted_payload(doc), "next")["result"]["reference"]
    grant["scopes"].remove(scope)
    path.write_bytes(encoded(grant))
    out = tmp_path / "denied-read"
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        run(api, transport(api), out, doc, ref)
    failed(out, doc)


@pytest.mark.parametrize("phase", ["after-read", "after-render"])
@pytest.mark.parametrize("scope", ["handoff", "watch"])
def test_final_revocation_never_releases_original_inputs_html_or_success(public_host, saved_brief,
                                                                        tmp_path, monkeypatch, phase, scope):
    api, grant, path = public_host
    doc, ref = saved_brief
    original, render = runtime._perform, reading_page.render_page
    state = {"read": False, "revoked": False}
    def completed(*args, **kwargs):
        result = original(*args, **kwargs)
        state["read"] = True
        if phase == "after-read":
            grant["scopes"].remove(scope);path.write_bytes(encoded(grant));state["revoked"] = True
        return result
    def rendered(*args, **kwargs):
        page = render(*args, **kwargs)
        if state["read"] and phase == "after-render" and not state["revoked"]:
            grant["scopes"].remove(scope);path.write_bytes(encoded(grant));state["revoked"] = True
        return page
    monkeypatch.setattr(runtime, "_perform", completed)
    monkeypatch.setattr(reading_page, "render_page", rendered)
    out = tmp_path / "revoked"
    with pytest.raises(RecordError, match="HOSTED_.*(REQUIRED|NOT_CONFIRMED)"):
        run(api, transport(api), out, doc, ref)
    assert state["revoked"]
    failed(out, doc)


def test_private_reversal_keeps_original_adapter_latch(public_host, saved_brief, tmp_path, monkeypatch):
    api, _, _ = public_host
    doc, ref = saved_brief
    api.binding = replace(api.binding, private_upgrade_allowed=True)
    api.meta["private"] = True
    original = runtime._perform
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs);api.meta["private"] = False;return result
    monkeypatch.setattr(runtime, "_perform", reverse)
    out = tmp_path / "reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        run(api, transport(api), out, doc, ref)
    failed(out, doc)


@pytest.mark.parametrize("phase", ["missing", "after-render"])
def test_omitted_ai_panel_still_blocks_missing_or_revoked_watch_scope(public_host, saved_brief,
                                                                     tmp_path, monkeypatch, phase):
    api, grant, path = public_host
    doc, ref = saved_brief
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 1)
    original, render = runtime._perform, reading_page.render_page
    state = {"read": False}
    def completed(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result["ai_brief"]["status"] == "not-included"
        assert result["ai_brief"]["watch_publication_required"] is True
        state["read"] = True
        return result
    def rendered(*args, **kwargs):
        page = render(*args, **kwargs)
        if state["read"] and "watch" in grant["scopes"]:
            grant["scopes"].remove("watch")
            path.write_bytes(encoded(grant))
        return page
    monkeypatch.setattr(runtime, "_perform", completed)
    monkeypatch.setattr(reading_page, "render_page", rendered)
    if phase == "missing":
        grant["scopes"].remove("watch")
        path.write_bytes(encoded(grant))
    out = tmp_path / "omitted-denied"
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        run(api, transport(api), out, doc, ref)
    assert state["read"] is (phase == "after-render")
    failed(out, doc)


def test_store_cli_checks_revocation_after_render_before_html(public_host, saved_brief,
                                                            tmp_path, monkeypatch):
    api, grant, path = public_host
    doc, ref = saved_brief
    store = VersionedStore(GitHubStore(api.binding, transport(api)))
    original, render = reading_commands.perform, reading_page.render_page
    state = {"read": False}
    def completed(*args, **kwargs):
        result = original(*args, **kwargs)
        state["read"] = True
        return result
    def rendered(*args, **kwargs):
        page = render(*args, **kwargs)
        if state["read"]:
            grant["scopes"].remove("handoff")
            path.write_bytes(encoded(grant))
        return page
    monkeypatch.setattr(reading_commands, "perform", completed)
    monkeypatch.setattr(reading_page, "render_page", rendered)
    out = tmp_path / "revoked-cli.html"
    args = SimpleNamespace(input=tmp_path / "unused-plan.json", out=out)
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        reading_commands.execute(args, store, lambda _: {"as_of": doc["cutoff"], "ai_brief": ref})
    assert state["read"] and not out.exists()


def test_native_request_limit_is_not_swallowed_as_optional_brief_failure(public_host, saved_brief,
                                                                        tmp_path, monkeypatch):
    api, _, _ = public_host
    doc, ref = saved_brief
    original = reading_ai_brief.read_panel
    def exhausted(store, *args, **kwargs):
        store.backend.api._requests = 512
        return original(store, *args, **kwargs)
    monkeypatch.setattr(reading_ai_brief, "read_panel", exhausted)
    out = tmp_path / "exhausted"
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        run(api, transport(api), out, doc, ref)
    failed(out, doc)


@pytest.mark.parametrize("scope", ["handoff", "watch"])
def test_daily_final_gate_follows_render_and_preserves_hidden_baseline_dependency(public_host, saved_brief,
                                                                                tmp_path, monkeypatch, scope):
    from test_daily_reading import native_run, receipt, AT
    api, grant, path = public_host
    doc, ref = saved_brief
    next_doc = {**doc, "schema": "hosted-ai-handoff-v1", "execution_date": "2026-01-04",
                "cutoff": "2026-01-04T05:00:00Z", "baseline": ref}
    next_doc.pop("watch_adoptions")
    next_ref = invoke(api, tmp_path / "next", "save-hosted-handoff", hosted_payload(next_doc), "next")["result"]["reference"]
    native_store = VersionedStore(GitHubStore(api.binding, transport(api)))
    from decision_kernel.interfaces.reading_commands import perform
    model = perform(native_store, {"plan": {"as_of": next_doc["cutoff"], "ai_brief": next_ref}})
    # Reuse the original daily host fixture for its native main/CI receipt;
    # the final publication check still acts on the actual retained store above.
    event, env, out, daily_api, _, _ = native_run(tmp_path, monkeypatch)
    model["daily_update"] = {"read_as_of": AT, "source_receipt": receipt(), "market_resolution": "unavailable",
                            "market_cycle": None, "market_error": "SYNTHETIC_NO_MARKET"}
    monkeypatch.setattr(daily_reading_cli, "_open_readers", lambda _: (native_store, object()))
    monkeypatch.setattr(daily_reading_cli, "build_reading", lambda *a, **kw: deepcopy(model))
    original = daily_reading_cli.daily_page
    def revoke(*args, **kwargs):
        page = original(*args, **kwargs);grant["scopes"].remove(scope);path.write_bytes(encoded(grant));return page
    monkeypatch.setattr(daily_reading_cli, "daily_page", revoke)
    with pytest.raises(RecordError, match="HOSTED_.*(REQUIRED|NOT_CONFIRMED)"):
        daily_reading_cli.run_runtime(event, env, out, api=daily_api, clock=lambda: AT)
    assert not any((out / name).exists() for name in daily_reading_cli.OUTPUTS)
