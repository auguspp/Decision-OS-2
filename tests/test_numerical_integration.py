"""Actual store/host consumers, with Git-backed model transport; never live issuer research."""
from dataclasses import asdict, replace
import json
import subprocess

from test_numerical_audit import spec


def _public_store(tmp_path, *, timed=False):
    from test_github_store import GitAPI
    from decision_kernel.infrastructure.github_store import GitHubStore
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.domain.records import SourceInput
    api = GitAPI(tmp_path / "remote.git")
    api.binding = replace(api.binding, visibility="public", write_enabled=False)
    anchor = api.git.commit({"store.json": api.binding.marker()}, expected=api.git.head())
    api.binding = replace(api.binding, anchor=anchor)
    api.meta["private"] = False
    store = VersionedStore(GitHubStore(replace(api.binding, write_enabled=True), api))
    csv = "subject,period,scope,unit,revenue,cost\nsynthetic-issuer,2025-FY,consolidated,CNY_10K,600,450\n"
    times = dict(published_at="2026-10-22T09:00:00+08:00",
                 acquired_at="2026-10-23T09:00:00+08:00") if timed else {}
    ref = store.put_source("numeric-source", SourceInput("numbers", "dataset", "Synthetic numbers", "synthetic:numeric", csv, **times))
    payload = spec(); payload["sources"] = [asdict(ref)]
    return api, store, payload, ref


def _timed_spec(payload):
    return {**payload, "schema": "research-numbers-v2",
            "knowledge_cutoff": "2026-10-23T08:00:00Z",
            "evaluated_at": "2026-10-24T08:00:00Z"}


def test_v2_native_host_time_failure_is_read_only_and_preserves_independent_calculation(tmp_path, monkeypatch):
    from decision_kernel.application import versioned_store
    from decision_kernel.interfaces.research_runtime import run_command
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-23T07:00:00Z")
    api, store, old, ref = _public_store(tmp_path, timed=True)
    payload = _timed_spec(old)
    writes, head = api.writes, api.git.head()
    inputs = dict(operation="audit-numbers", code_sha="a" * 40, payload=json.dumps(payload))
    good = run_command(api.binding, api=api, inputs=inputs, code_sha="a" * 40,
                       output=tmp_path / "timed-good", run_id="1", attempt="1")
    assert good["status"] == "completed" and good["result"]["status"] == "checked"
    assert good["result"]["schema"] == "research-numerical-audit-v2"
    assert good["result"]["knowledge_cutoff"] == payload["knowledge_cutoff"]
    payload["knowledge_cutoff"] = "2026-10-23T06:00:00Z"
    context = {key: payload["inputs"][0][key] for key in ("subject", "period", "scope")}
    payload["inputs"].append(dict(id="independent-assumption", value="2", unit="ratio",
        assumption="Synthetic sensitivity assumption; not an observed source", **context))
    payload["calculations"].append(dict(id="independent-result", operation="identity",
        operands=["independent-assumption"], unit="ratio", **context))
    inputs["payload"] = json.dumps(payload)
    bad = run_command(api.binding, api=api, inputs=inputs, code_sha="a" * 40,
                      output=tmp_path / "timed-bad", run_id="2", attempt="1")
    assert bad["status"] == "numerical-not-cleared" and bad["write"] == "not-requested"
    assert bad["result"]["inputs"][0]["status"] == "blocked"
    assert bad["result"]["calculations"][0]["status"] == "blocked"
    assert bad["result"]["calculations"][-1]["display_number"] == "2.00"
    assert bad["result"]["calculations"][-1]["status"] == "conditional"
    assert api.writes == writes and api.git.head() == head


def test_v2_checkpoint_is_recovered_by_fresh_reader_and_does_not_retime_old_evidence(tmp_path, monkeypatch):
    from decision_kernel.application import versioned_store
    from decision_kernel.application.numerical_audit import NumericalAudit
    from decision_kernel.application.research_workflow import ResearchWorkflow
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.domain.records import BlobRef
    from decision_kernel.domain.research_work import ResearchRequest, ResearchProgress
    from decision_kernel.infrastructure.github_store import GitHubStore
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-23T07:00:00Z")
    api, store, old, ref = _public_store(tmp_path, timed=True)
    payload = _timed_spec(old)
    audit = NumericalAudit(store).audit(payload)
    request = ResearchRequest.from_dict(dict(case_id="numeric-time", kind="full", mandate="Synthetic time boundary",
        questions=[dict(question_id="q", text="Synthetic cash sensitivity")]))
    progress = ResearchProgress.from_dict(dict(body="Synthetic narrative" + audit["appendix"],
        findings=[dict(question_id="q", status="open", finding="Still needs analysis",
                       remaining_kind="acquired-unanalysed", next_action="Continue substantive analysis")],
        sources=[asdict(ref)], stop="continue", stop_reason="Time checks do not conclude research"))
    saved = ResearchWorkflow(store).checkpoint(request, progress, submission_id="timed-save", revision_id="r1")
    assert saved["numerical_audit"]["schema"] == "research-numerical-audit-v2"
    assert saved["numerical_audit"]["knowledge_cutoff"] == payload["knowledge_cutoff"]
    assert saved["numerical_audit"]["status"] == "checked"
    head, writes = api.git.head(), api.writes
    monkeypatch.setattr(versioned_store, "_now", lambda: "2035-01-01T00:00:00Z")
    fresh = VersionedStore(GitHubStore(api.binding, api))
    restored = NumericalAudit(fresh).saved(BlobRef(**saved["reference"]))
    assert restored["saved_render"] == "matches-recalculation"
    assert restored["appendix"] == audit["appendix"]
    assert restored["input_sha256"] == audit["input_sha256"]
    assert restored["evaluated_at"] == payload["evaluated_at"]
    assert ResearchWorkflow(store).checkpoint(request, progress, submission_id="timed-save", revision_id="r1") == saved
    assert api.git.head() == head and api.writes == writes


def test_host_audit_is_read_only_and_keeps_failed_numeric_diagnostics(tmp_path):
    from decision_kernel.interfaces.research_runtime import run_command
    api, store, payload, ref = _public_store(tmp_path)
    writes, head = api.writes, api.git.head()
    inputs = dict(operation="audit-numbers", code_sha="a" * 40, payload=json.dumps(payload))
    report = run_command(api.binding, api=api, inputs=inputs, code_sha="a" * 40,
                         output=tmp_path / "good", run_id="1", attempt="1")
    assert report["status"] == "completed" and report["result"]["status"] == "checked"
    assert report["write"] == "not-requested" and api.writes == writes and api.git.head() == head
    payload["inputs"][0]["value"] = "60"
    inputs["payload"] = json.dumps(payload)
    failed = run_command(api.binding, api=api, inputs=inputs, code_sha="a" * 40,
                         output=tmp_path / "bad", run_id="2", attempt="1")
    assert failed["status"] == "numerical-not-cleared" and failed["write"] == "not-requested"
    assert json.loads((tmp_path / "bad/report.json").read_text())["result"]["status"] == "blocked"
    assert api.writes == writes and api.git.head() == head


def test_research_checkpoint_preserves_receipt_separate_from_bad_numerical_display(tmp_path):
    from decision_kernel.application.numerical_audit import NumericalAudit
    from decision_kernel.application.research_workflow import ResearchWorkflow
    from decision_kernel.domain.research_work import ResearchRequest, ResearchProgress
    api, store, payload, ref = _public_store(tmp_path)
    appendix = NumericalAudit(store).audit(payload)["appendix"]
    request = ResearchRequest.from_dict(dict(case_id="numeric", kind="full", mandate="Synthetic only",
        questions=[dict(question_id="q", text="Synthetic cash distinction")]))
    progress = ResearchProgress.from_dict(dict(body="Synthetic narrative" + appendix,
        findings=[dict(question_id="q", status="open", finding="Still needs economics", remaining_kind="acquired-unanalysed",
                       next_action="Continue real analysis, not just arithmetic")],
        sources=[asdict(ref)], stop="continue", stop_reason="A numerical match is not research completion"))
    workflow = ResearchWorkflow(store)
    saved = workflow.checkpoint(request, progress, submission_id="n1", revision_id="r1")
    assert saved["numerical_audit"]["status"] == "checked"
    assert saved["research_assessment"] == "not-performed" and saved["human_acceptance"] == "not-confirmed"
    assert workflow.checkpoint(request, progress, submission_id="n1", revision_id="r1") == saved
    from decision_kernel.domain.records import BlobRef
    later = replace(request, predecessor=BlobRef(**saved["reference"]))
    wrong = replace(progress, body=progress.body.replace("150.00 CNY_10K", "150.00 CNY_100M", 1))
    bad = workflow.checkpoint(later, wrong, submission_id="n2", revision_id="r2")
    assert bad["reference"] != saved["reference"]
    assert bad["numerical_audit"]["status"] == "blocked"
    assert bad["numerical_audit"]["error"] == "SAVED_NUMERICAL_DISPLAY_MISMATCH"
    assert store.read_revision(BlobRef(**bad["reference"]))["body"].startswith(wrong.body)
    assert workflow.checkpoint(later, wrong, submission_id="n2", revision_id="r2") == bad


def test_host_main_returns_nonzero_for_numerical_failure_without_losing_report(tmp_path, monkeypatch, capsys):
    from decision_kernel.interfaces import research_runtime as host
    api, store, payload, ref = _public_store(tmp_path)
    payload["calculations"][0]["claimed"] = "15"
    inputs = dict(operation="audit-numbers", code_sha="a" * 40, payload=json.dumps(payload))
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": api.binding.repository, "GITHUB_REPOSITORY_ID": str(api.binding.repository_id),
        "GITHUB_WORKFLOW_REF": api.binding.repository + "/" + host.WORKFLOW + "@refs/heads/main",
        "GITHUB_SHA": "a" * 40, "GITHUB_EVENT_PATH": "event.json", "DOS2_RESEARCH_MODE": "read"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(host, "_file", lambda path: {"inputs": inputs} if str(path) == "event.json" else asdict(api.binding))
    monkeypatch.setattr(host.GitHubAPI, "from_environment", lambda: api)
    writes = api.writes
    assert host.main(["--out", str(tmp_path / "run")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "numerical-not-cleared"
    assert json.loads((tmp_path / "run/report.json").read_text())["result"]["calculations"][0]["error"] == "CLAIMED_DISPLAY_MISMATCH"
    assert api.writes == writes


def exercise_installed_numbers(command, outside, python, env, executable, store_path):
    """Extend the ONE existing cold-wheel test; do not build another installation."""
    source = {"source_id": "numeric", "kind": "dataset", "title": "Synthetic cash numbers", "origin": "synthetic:installed",
              "text": "subject,period,scope,unit,revenue,cost\nsynthetic-issuer,2025-FY,consolidated,CNY_10K,600,450\n"}
    input_file = outside / "numeric-source.json"
    input_file.write_text(json.dumps(source), encoding="utf-8")
    ref = command("put-source", "--submission-id", "installed-numeric-source", "--input", str(input_file))
    payload = spec(); payload["sources"] = [ref]
    input_file.write_text(json.dumps(payload), encoding="utf-8")
    audit = command("audit-numbers", "--input", str(input_file))
    assert audit["status"] == "checked" and audit["calculations"][0]["display"] == "150.00 CNY_10K"
    request = dict(case_id="numeric", kind="full", mandate="Synthetic installation only",
                   questions=[dict(question_id="q", text="Synthetic economics")])
    request_file = outside / "numeric-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    progress = dict(body="Synthetic report" + audit["appendix"], sources=[ref], stop="continue",
        stop_reason="Continue substantive research, numerical check is not completion",
        findings=[dict(question_id="q", status="open", finding="Synthetic remaining analysis",
                       remaining_kind="acquired-unanalysed", next_action="Review actual economics")])
    progress_file = outside / "numeric-progress.json"
    progress_file.write_text(json.dumps(progress), encoding="utf-8")
    saved = command("checkpoint-research", "--request", str(request_file), "--input", str(progress_file),
                    "--submission-id", "installed-numeric-research", "--revision-id", "r1")
    assert saved["numerical_audit"]["status"] == "checked"
    ref_file = outside / "numeric-revision.json"
    ref_file.write_text(json.dumps(saved["reference"]), encoding="utf-8")
    assert command("audit-numbers", "--ref", str(ref_file))["saved_render"] == "matches-recalculation"
    payload["calculations"][0]["claimed"] = "1500000"
    input_file.write_text(json.dumps(payload), encoding="utf-8")
    failed = subprocess.run([str(executable), "store", "--path", str(store_path), "audit-numbers", "--input", str(input_file)],
                            cwd=outside, env=env, capture_output=True, text=True)
    assert failed.returncode == 2
    assert json.loads(failed.stdout)["calculations"][0]["error"] == "CLAIMED_DISPLAY_MISMATCH"
    assert command("current", "--case-id", "numeric")["reference"] == saved["reference"]

    # The same cold installation receives a time-aware successor. Derive the
    # cutoff from its actual retained manifest, never from a fabricated receipt.
    timed_source = {**source, "source_id": "numeric-timed",
                    "published_at": "2020-01-01T00:00:00Z", "acquired_at": "2020-01-02T00:00:00Z"}
    input_file.write_text(json.dumps(timed_source), encoding="utf-8")
    timed_ref = command("put-source", "--submission-id", "installed-timed-source", "--input", str(input_file))
    source_ref_file = outside / "numeric-timed-source-ref.json"
    source_ref_file.write_text(json.dumps(timed_ref), encoding="utf-8")
    retained_at = command("read-source", "--ref", str(source_ref_file))["manifest"]["stored_at"]
    timed = {**spec(), "schema": "research-numbers-v2", "sources": [timed_ref],
             "knowledge_cutoff": retained_at, "evaluated_at": retained_at}
    input_file.write_text(json.dumps(timed), encoding="utf-8")
    checked = command("audit-numbers", "--input", str(input_file))
    assert checked["status"] == "checked" and checked["schema"] == "research-numerical-audit-v2"
    request_file.write_text(json.dumps({**request, "predecessor": saved["reference"]}), encoding="utf-8")
    progress_file.write_text(json.dumps({**progress, "body": "Synthetic successor" + checked["appendix"],
                                         "sources": [ref, timed_ref]}), encoding="utf-8")
    successor = command("checkpoint-research", "--request", str(request_file), "--input", str(progress_file),
                        "--submission-id", "installed-timed-research", "--revision-id", "r2")
    assert successor["numerical_audit"]["status"] == "checked"
    assert successor["numerical_audit"]["knowledge_cutoff"] == retained_at
    ref_file.write_text(json.dumps(successor["reference"]), encoding="utf-8")
    recovered = command("audit-numbers", "--ref", str(ref_file))
    assert recovered["saved_render"] == "matches-recalculation"
    assert recovered["input_sha256"] == checked["input_sha256"]
    timed["knowledge_cutoff"] = "2020-01-03T00:00:00Z"
    input_file.write_text(json.dumps(timed), encoding="utf-8")
    too_late = subprocess.run([str(executable), "store", "--path", str(store_path), "audit-numbers", "--input", str(input_file)],
                             cwd=outside, env=env, capture_output=True, text=True)
    assert too_late.returncode == 2 and json.loads(too_late.stdout)["inputs"][0]["status"] == "blocked"
    assert command("current", "--case-id", "numeric")["reference"] == successor["reference"]
