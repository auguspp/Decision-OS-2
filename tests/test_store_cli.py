"""Local synthetic CLI consumers and bounded failures. No sources are contacted."""
import json

import pytest

from decision_kernel.domain.records import MAX_BYTES
from decision_kernel.interfaces.cli import main
from decision_kernel.interfaces.store_cli import _load


def test_cli_consumer_roundtrip_idempotency_and_explicit_conflict(tmp_path, capsys):
    store = tmp_path / "local.git"
    def command(*args):
        code = main(["store", "--path", str(store), *args])
        result = capsys.readouterr()
        return code, json.loads(result.out if code == 0 else result.err)
    code, result = command("init-synthetic")
    assert code == 0 and result["remote_binding"] == "not-configured" and result["mode"] == "synthetic"
    payload = {"source_id": "sample", "kind": "content", "title": "合成铜价",
               "origin": "synthetic:cli", "text": "仅合成内容，不是真实行情。"}
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code, reference = command("put-source", "--submission-id", "intent-one", "--input", str(source))
    assert code == 0
    assert command("put-source", "--submission-id", "intent-one", "--input", str(source)) == (0, reference)
    ref = tmp_path / "source-ref.json"
    ref.write_text(json.dumps(reference), encoding="utf-8")
    assert command("read-source", "--ref", str(ref))[1]["data"] == payload["text"]
    assert command("search", "--query", "铜价")[1]["total"] == 1
    research = tmp_path / "revision.json"
    research.write_text(json.dumps({"case_id": "sample-case", "revision_id": "r1", "kind": "quick",
                                   "body": "合成问题，未作研究接受。", "mandate": "synthetic:mandate",
                                   "method": "synthetic:method", "sources": [reference]}), encoding="utf-8")
    code, receipt = command("submit-revision", "--submission-id", "research-one", "--input", str(research))
    assert code == 0
    ref.write_text(json.dumps(receipt), encoding="utf-8")
    assert command("read-revision", "--ref", str(ref))[1]["record"]["human_acceptance"] == "not-confirmed"
    assert command("current", "--case-id", "sample-case")[1]["reference"] == receipt
    assert command("current", "--case-id", "other")[1]["reference"] is None
    source.write_text(json.dumps({**payload, "text": "相同键不同内容"}), encoding="utf-8")
    assert command("put-source", "--submission-id", "intent-one", "--input", str(source)) == (
        2, {"error": "SUBMISSION_CONTENT_CONFLICT"})
    assert command("init-synthetic") == (2, {"error": "STORE_PATH_EXISTS"})


@pytest.mark.parametrize("raw,code", [
    ('{"source_id":"one","source_id":"two"}', "DUPLICATE_JSON_KEY"),
    ('{"value":NaN}', "INVALID_JSON_NUMBER"),
    ('[]', "JSON_OBJECT_REQUIRED"),
    ('{"broken":', "INPUT_UNREADABLE_OR_INVALID"),
])
def test_input_json_rejects_ambiguous_or_invalid_payload(tmp_path, raw, code):
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match=code):
        _load(path)


def test_size_limit_and_unknown_store_are_clear_without_path_or_source_echo(tmp_path, capsys):
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="INPUT_TOO_LARGE"):
        _load(path)
    result = main(["store", "--path", str(tmp_path / "not-there-sensitive-path"), "current", "--case-id", "sample"])
    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "STORE_UNAVAILABLE"}


def test_empty_or_wrong_reference_is_structured_cli_error(tmp_path, capsys):
    store = tmp_path / "local.git"
    assert main(["store", "--path", str(store), "init-synthetic"]) == 0
    capsys.readouterr()
    ref = tmp_path / "ref.json"
    ref.write_text('{}', encoding="utf-8")
    assert main(["store", "--path", str(store), "read-revision", "--ref", str(ref)]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "INVALID_INPUT_SHAPE"}


@pytest.mark.parametrize("command", ["collect", "quick", "full", "decision", "deploy", "delete"])
def test_storage_interface_does_not_enable_unimplemented_authority(command, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["store", "--path", str(tmp_path / "unused"), command])
    assert exc.value.code == 2
    assert not (tmp_path / "unused").exists()
