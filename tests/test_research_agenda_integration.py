"""Real temporary Git through the existing host/Reader and the one installed wheel."""
import json
from pathlib import Path

import pytest
import yaml

from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.interfaces import research_runtime as runtime
from test_research_runtime import host, invoke, source, work, inputs


AT = "2026-10-16T02:00:00Z"
WINDOW = {"variable": "经营现金流", "event": "报告复核", "opens_at": "2026-10-17T01:00:00Z",
          "closes_at": "2026-10-18T01:00:00Z", "required_evidence": "原报表及对应期间", "end_condition": "显式复核"}


def agenda_payload(ref, **changes):
    return {"plan": {"references": [ref], "as_of": AT, **changes}}


def attention(ref, **changes):
    return {"reference": ref, "question_id": "economics", "action": "defer",
            "reason": "合成选择，不是真实用户反馈", "review": WINDOW, **changes}


def prepared(host, tmp_path):
    ref = invoke(host, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]
    research = invoke(host, tmp_path / "research", "checkpoint", work(ref), "research")["result"]["reference"]
    return ref, research


def artifact_payload(built):
    return {"artifact": built["artifact"], "rights_confirmed": True,
            "rights_basis": "合成工程材料；不包含真实公司研究或用户反馈"}


def test_real_git_agenda_save_independent_open_preserves_original_research(host, tmp_path):
    _, ref = prepared(host, tmp_path)
    before = host.git.head()
    payload = {**agenda_payload(ref, changes=[attention(ref)]), "public_feedback_custody_confirmed": True}
    built = invoke(host, tmp_path / "agenda", "agenda", payload)
    assert host.git.head() == before and built["write"] == "not-requested"
    result = built["result"]
    card, = result["cards"]
    assert card["status"] == "readable" and card["mandate"] == "合成有限委托"
    assert card["continuation"]["actions"][0]["next_action"] == "继续核算"
    assert card["agenda"][0]["attention_status"] == "defer"
    saved = invoke(host, tmp_path / "save", "put-artifact", artifact_payload(result), "agenda-one")
    artifact = saved["result"]["reference"]
    writes = host.writes
    reopened = invoke(host, tmp_path / "open", "read-agenda", {"reference": artifact})["result"]
    assert host.writes == writes and encoded(reopened["document"]) == encoded(result["document"])
    assert reopened["cards"] == result["cards"]
    original = invoke(host, tmp_path / "original", "resume", {"reference": ref})["result"]["research"]
    assert original["record"]["input"]["revision_id"] == "r1" and original["reference"] == ref
    assert reopened["human_acceptance"] == "not-established-by-agenda"


def test_new_revision_is_not_suppressed_and_old_choices_remain_recoverable(host, tmp_path):
    src, ref = prepared(host, tmp_path)
    payload = {**agenda_payload(ref, changes=[attention(ref)]), "public_feedback_custody_confirmed": True}
    first = invoke(host, tmp_path / "first", "agenda", payload)["result"]
    baseline = invoke(host, tmp_path / "save", "put-artifact", artifact_payload(first), "agenda-one")["result"]["reference"]
    request = invoke(host, tmp_path / "prepare", "prepare", {"case_id": "example"})["result"]["request"]
    newer = invoke(host, tmp_path / "new", "checkpoint", work(src, request=request, revision_id="r2"), "second")["result"]["reference"]
    following = invoke(host, tmp_path / "following", "agenda", agenda_payload(newer, baseline=baseline))["result"]
    assert following["cards"][0]["agenda"][0]["selection"] is None
    assert following["unselected_attention"] == first["document"]["state"]
    assert following["cards"][0]["reference"] == newer


def test_unconfirmed_public_feedback_fails_before_any_input_artifact_or_store_access(host, tmp_path):
    _, ref = prepared(host, tmp_path)
    writes = host.writes
    output = tmp_path / "must-not-exist"
    with pytest.raises(RecordError, match="PUBLIC_FEEDBACK_NOT_CONFIRMED"):
        invoke(host, output, "agenda", agenda_payload(ref, changes=[attention(ref)]))
    assert not output.exists() and host.writes == writes


def test_missing_selected_research_is_visible_and_cannot_receive_fabricated_choice(host, tmp_path):
    _, ref = prepared(host, tmp_path)
    missing = {**ref, "path": ref["path"].replace("/r1/", "/absent/")}
    result = invoke(host, tmp_path / "partial", "agenda", agenda_payload(ref, references=[ref, missing]))["result"]
    assert result["status"] == "partial" and result["cards"][0]["status"] == "readable"
    assert result["cards"][1]["status"] == "missing"
    with pytest.raises(RecordError, match="CHOICE_RESEARCH_UNREADABLE"):
        invoke(host, tmp_path / "invalid", "agenda", {
            **agenda_payload(missing, changes=[attention(missing)]), "public_feedback_custody_confirmed": True})


def test_agenda_unknown_artifact_ack_uses_original_readonly_reconciliation(host, tmp_path):
    _, ref = prepared(host, tmp_path)
    built = invoke(host, tmp_path / "build", "agenda", agenda_payload(ref))["result"]
    payload, before = artifact_payload(built), host.git.head()
    def lose(_):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", "put-artifact", payload, "same-agenda", before)
    host.after_write = None
    count = host.writes
    saved = invoke(host, tmp_path / "reconcile", "reconcile-artifact", payload, "same-agenda", before)
    assert saved["write"] == "reconciled-without-write" and host.writes == count
    restored = invoke(host, tmp_path / "open", "read-agenda", saved["result"])["result"]
    assert restored["document"] == built["document"]
    assert json.loads((tmp_path / "lost/inputs.json").read_text())["submission_id"] == "same-agenda"


def test_agenda_host_operations_stay_read_only_and_keep_original_write_allowlist():
    assert {"agenda", "read-agenda"} <= runtime.READ_OPERATIONS
    assert not {"agenda", "read-agenda"} & (runtime.WRITE_OPERATIONS | runtime.RECONCILE_OPERATIONS)
    with pytest.raises(RecordError, match="UNEXPECTED_WRITE_FIELDS"):
        runtime.parse_inputs(inputs("agenda", {"plan": {"references": [], "as_of": AT}}, "not-a-write"))
    flow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/research-runtime.yml").read_text())
    trigger = flow.get("on", flow.get(True))
    assert {"agenda", "read-agenda"} <= set(trigger["workflow_dispatch"]["inputs"]["operation"]["options"])
    assert flow["jobs"]["read"]["permissions"]["contents"] == "read"
    assert "agenda" not in flow["jobs"]["write"]["if"]


def exercise_installed_agenda(command, outside, ref):
    """Extend the existing cold-wheel process, not another installation test."""
    plan = {"references": [ref], "as_of": AT, "changes": [
        {"reference": ref, "question_id": "q1", "action": "focus", "reason": "合成显式复核",
         "review": WINDOW, "feedback": {"observation": "合成单位疑问", "implication": "复核原依据", "outcome": "revision-requested"}}]}
    path = outside / "agenda-plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    built = command("research-agenda", "--input", str(path))
    assert built["cards"][0]["agenda"][0]["attention_status"] == "focus"
    path.write_text(json.dumps(artifact_payload(built), ensure_ascii=False), encoding="utf-8")
    saved = command("put-artifact", "--input", str(path), "--submission-id", "installed-agenda")
    assert saved["readback"]["bytes_verified"]
    reference = outside / "agenda-ref.json"
    reference.write_text(json.dumps(saved["reference"]), encoding="utf-8")
    restored = command("read-research-agenda", "--ref", str(reference), "--as-of", "2026-10-18T10:00:00+08:00")
    assert restored["document"] == built["document"]
    assert restored["cards"][0]["agenda"][0]["review_status"] == "overdue-for-review"
    assert restored["execution"] == "not-started" and command("current", "--case-id", "work")["reference"] == ref
