"""Complete research via the original runtime/store; synthetic Git-backed transport."""
from copy import deepcopy
import json

import pytest

from decision_kernel.domain.records import Conflict, MissingObject, RecordError, UnknownWrite, digest, encoded
from decision_kernel.domain.research_work import read_workpaper
from decision_kernel.interfaces.research_payload import decode_payload, pack_payload
from decision_kernel.interfaces.research_runtime import run_command
from test_research_runtime import CODE, host, inputs, invoke, request, source, work


def packed_call(host, path, operation, payload, submission="", expected=None):
    value = inputs(operation, payload, submission,
                   host.git.head() if expected is None and operation in ("put-source", "checkpoint") else expected or "")
    value["payload"] = pack_payload(encoded(payload))
    return run_command(host.binding, api=host, inputs=value, code_sha=CODE, output=path)


def test_large_complete_workpaper_is_saved_read_and_resumed_without_truncation(host, tmp_path):
    src = source(text="完整合成来源\n" * 6000)
    ref = packed_call(host, tmp_path / "src", "put-source", src, "src")["result"]["reference"]
    questions = [{"question_id": "q" + str(i), "text": "保留问题" + str(i)} for i in range(8)]
    payload = work(ref, request=request(questions=questions))
    payload["progress"].update(
        body="合成长期研究：利润、现金、资本和归属。\n" * 6000,
        findings=[{"question_id": q["question_id"], "status": "open", "finding": "尚待核算", "evidence": [0],
                   "remaining_kind": "acquired-unanalysed", "next_action": "继续原分析"} for q in questions],
        challenges=[{"question_id": "q0", "argument": "合成竞争解释", "disposition": "unresolved",
                     "response": "继续保留，不删除", "evidence": [0]}])
    with pytest.raises(RecordError, match="DISPATCH_PAYLOAD_TOO_LARGE"):
        invoke(host, tmp_path / "too-large-plain", "checkpoint", payload, "work")
    saved = packed_call(host, tmp_path / "save", "checkpoint", payload, "work")
    row = invoke(host, tmp_path / "read", "resume", {"reference": saved["result"]["reference"]})["result"]
    req, progress = read_workpaper(row["research"])
    assert progress.body == payload["progress"]["body"] and len(req.questions) == 8
    assert progress.challenges[0].argument == "合成竞争解释"
    assert row["source_page"]["items"][0]["view"]["data"] == src["text"]
    resumed = packed_call(host, tmp_path / "prepare", "prepare", {"case_id": "example"})["result"]
    assert encoded(resumed["request"]["questions"]) == encoded(questions)
    actions = resumed["continuation"]["actions"]
    assert len(actions) == 8 and {a["question_id"] for a in actions} == {q["question_id"] for q in questions}
    assert all(a["action"] == "analyse-available-evidence" and a["next_action"] == "继续原分析" for a in actions)
    assert encoded(resumed["continuation"]["unresolved_challenges"]) == encoded(payload["progress"]["challenges"])
    assert saved["payload_sha256"] == digest(encoded(payload))
    assert host.writes == 2


def test_plain_and_compressed_have_same_original_intent(host, tmp_path):
    ref = invoke(host, tmp_path / "src", "put-source", source(), "src")["result"]["reference"]
    payload = work(ref)
    saved = invoke(host, tmp_path / "save", "checkpoint", payload, "work")
    replay = packed_call(host, tmp_path / "replay", "checkpoint", payload, "work")
    assert replay["result"] == saved["result"] and replay["write"] == "replayed" and host.writes == 2
    assert replay["payload_sha256"] == saved["payload_sha256"]
    changed = deepcopy(payload)
    changed["progress"]["body"] += "不同内容"
    with pytest.raises(Conflict):
        packed_call(host, tmp_path / "different", "checkpoint", changed, "work")
    assert host.writes == 2


@pytest.mark.parametrize("operation", ["put-source", "checkpoint"])
def test_lost_receipt_recovers_original_large_payload_without_new_write(host, tmp_path, operation):
    ref = invoke(host, tmp_path / "src", "put-source", source(), "src")["result"]["reference"]
    payload = source(source_id="lost", text="合成原来源" * 20000) if operation == "put-source" else work(ref)
    if operation == "checkpoint":
        payload["progress"]["body"] = "合成原研究" * 20000
    original_head = host.git.head()
    def lose(_):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_write = lose
    with pytest.raises(UnknownWrite):
        packed_call(host, tmp_path / "lost", operation, payload, "original", original_head)
    native = json.loads((tmp_path / "lost/inputs.json").read_text())
    assert encoded(decode_payload(native["payload"])) == encoded(payload)
    published = host.git.head()
    host.after_write = None
    invoke(host, tmp_path / "later", "put-source", source(source_id="later"), "later")
    count = host.writes
    recovered = packed_call(host, tmp_path / "recover", "reconcile-" + ("source" if operation == "put-source" else "checkpoint"), payload, "original", original_head)
    exact = recovered["result"]["reference"]
    assert (exact.get("manifest") or exact)["commit"] == published
    assert recovered["write"] == "reconciled-without-write" and host.writes == count


def test_compression_neither_adds_authority_nor_avoids_original_payload_validation(host, tmp_path):
    before = host.git.head()
    result = packed_call(host, tmp_path / "catalogue", "catalogue", {"limit": 4})
    assert result["write"] == "not-requested" and host.git.head() == before
    with pytest.raises(RecordError, match="INVALID_CATALOGUE_PAYLOAD"):
        packed_call(host, tmp_path / "foreign", "catalogue", {"repository": "other"})
    with pytest.raises(MissingObject, match="SUBMISSION_NOT_FOUND_NO_WRITE"):
        packed_call(host, tmp_path / "absent", "reconcile-source", source(), "absent")
    assert host.writes == 0


def test_corrupt_envelope_fails_before_any_backend_access(host, tmp_path):
    value = inputs("put-source", source(), "one", host.git.head())
    corrupt = json.loads(pack_payload(encoded(source())))
    corrupt["sha256"] = "0" * 64
    value["payload"] = encoded(corrupt).decode()
    host.requests.clear()
    with pytest.raises(RecordError, match="PAYLOAD_DIGEST_MISMATCH"):
        run_command(host.binding, api=host, inputs=value, code_sha=CODE, output=tmp_path / "bad")
    assert host.requests == [] and not (tmp_path / "bad").exists()


def test_large_packed_input_cannot_bypass_method_or_expected_head(host, tmp_path):
    ref = invoke(host, tmp_path / "src", "put-source", source(), "src")["result"]["reference"]
    payload = work(ref)
    payload["progress"]["body"] *= 20000
    payload["method_sha256"] = "0" * 64
    with pytest.raises(RecordError, match="RESEARCH_METHOD_CHANGED"):
        packed_call(host, tmp_path / "method", "checkpoint", payload, "r1")
    with pytest.raises(Conflict, match="PLANNED_HEAD_CHANGED"):
        packed_call(host, tmp_path / "stale", "put-source", source(source_id="new"), "new", "0" * 40)
    assert host.writes == 1
