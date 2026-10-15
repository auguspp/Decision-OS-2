"""Cold wheel installation outside the checkout, with no dependency/network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_installs_without_checkout_or_runtime_dependencies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "src/decision_kernel", source / "src/decision_kernel", ignore=shutil.ignore_patterns("__pycache__"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel(" + repr(str(wheels)) + ")"], cwd=source, check=True, capture_output=True)
    wheel, = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert all(name.startswith(("decision_kernel/", "decision_os_2-0.1.0.dist-info/")) for name in archive.namelist())
        metadata = archive.read("decision_os_2-0.1.0.dist-info/METADATA").decode()
        assert "Requires-Dist:" not in metadata
    venv = tmp_path / "installed"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True)
    python = venv / "bin/python"
    subprocess.run(["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)], check=True, capture_output=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([str(venv / "bin/decision-os"), "info", "--json"], cwd=outside, env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["version"] == "0.1.0"
    result = subprocess.run([str(python), "-I", "-c", "import decision_kernel; print(decision_kernel.__file__)"], cwd=outside, env=env, check=True, capture_output=True, text=True)
    assert str(venv) in result.stdout and str(source) not in result.stdout

    # Extend this same real installation consumer; do not build a second wheel/pilot.
    store = outside / "synthetic.git"
    def command(*args):
        completed = subprocess.run([str(venv / "bin/decision-os"), "store", "--path", str(store), *args],
                                   cwd=outside, env=env, check=True, capture_output=True, text=True)
        return json.loads(completed.stdout)
    assert command("init-synthetic")["remote_binding"] == "not-configured"
    payload = {"source_id": "sample", "kind": "content", "title": "合成来源",
               "origin": "synthetic:installed-cli", "text": "仓库外读取的合成材料。"}
    input_file = outside / "input.json"
    input_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    reference = command("put-source", "--submission-id", "one", "--input", str(input_file))
    assert command("put-source", "--submission-id", "one", "--input", str(input_file)) == reference
    ref_file = outside / "ref.json"
    ref_file.write_text(json.dumps(reference), encoding="utf-8")
    assert command("read-source", "--ref", str(ref_file))["data"] == payload["text"]
    input_file.write_text(json.dumps({"case_id": "sample", "revision_id": "r1", "kind": "quick",
                                     "body": "合成工程检查，不是投资研究。", "mandate": "synthetic:mandate",
                                     "method": "synthetic:method", "sources": [reference]}, ensure_ascii=False), encoding="utf-8")
    revision = command("submit-revision", "--submission-id", "two", "--input", str(input_file))
    ref_file.write_text(json.dumps(revision), encoding="utf-8")
    assert command("read-revision", "--ref", str(ref_file))["body"] == "合成工程检查，不是投资研究。"
    assert command("current", "--case-id", "sample")["reference"] == revision

    # Separate installed CLI processes resume and revise the same saved research.
    context = command("resume-research", "--case-id", "sample")
    assert context["research"]["reference"] == revision
    assert context["source_page"]["items"][0]["view"]["data"] == payload["text"]
    input_file.write_text(json.dumps({**payload, "source_id": "second", "text": "第二份合成材料。"}, ensure_ascii=False), encoding="utf-8")
    second = command("put-source", "--submission-id", "three", "--input", str(input_file))
    full = {"case_id": "sample", "revision_id": "r2", "kind": "full", "body": "合成修订，仍有未决问题。",
            "mandate": "synthetic:mandate", "method": "synthetic:method", "sources": [reference, second],
            "predecessor": revision, "unresolved": ["合成待核问题"]}
    input_file.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    full_ref = command("submit-revision", "--submission-id", "four", "--input", str(input_file))
    page = command("resume-research", "--case-id", "sample", "--source-limit", "1")
    assert page["research"]["record"]["input"]["kind"] == "full"
    cursor_file = outside / "cursor.json"
    cursor_file.write_text(json.dumps(page["next_cursor"]), encoding="utf-8")
    input_file.write_text(json.dumps({**full, "revision_id": "r3", "predecessor": full_ref,
                                     "body": "较新合成修订。"}, ensure_ascii=False), encoding="utf-8")
    newest = command("submit-revision", "--submission-id", "five", "--input", str(input_file))
    last = command("resume-research", "--cursor", str(cursor_file))
    assert last["research"]["reference"] == full_ref and last["source_page"]["offset"] == 1
    assert last["source_page"]["items"][0]["view"]["data"] == "第二份合成材料。"
    assert last["next_cursor"] is None
    assert command("resume-research", "--ref", str(ref_file))["research"]["reference"] == revision
    assert command("current", "--case-id", "sample")["reference"] == newest

    # Method/workpaper consumer in the SAME cold wheel, outside the checkout.
    request_file = outside / "work-request.json"
    initial_request = {"case_id": "work", "kind": "full", "mandate": "合成直接Full，不是投资任务",
                       "questions": [{"question_id": "q1", "text": "如何区分解释"}]}
    request_file.write_text(json.dumps(initial_request, ensure_ascii=False), encoding="utf-8")
    prepared = command("prepare-research", "--request", str(request_file))
    assert prepared["method"]["kind"] == "full" and prepared["method"]["sha256"]
    assert prepared["previous_context"] is None and prepared["persistence"] == "not-written"
    partial = {"body": "合成阶段分析", "findings": [{"question_id": "q1", "status": "open", "finding": "仍需核对",
               "evidence": [0], "remaining_kind": "acquired-unanalysed", "next_action": "完成合成计算"}],
               "sources": [reference], "stop": "continue", "stop_reason": "已取得材料还有重要工作"}
    input_file.write_text(json.dumps(partial, ensure_ascii=False), encoding="utf-8")
    checkpoint = command("checkpoint-research", "--request", str(request_file), "--input", str(input_file),
                         "--submission-id", "work-one", "--revision-id", "r1")
    assert checkpoint["research_assessment"] == "not-performed"
    resumed = command("prepare-research", "--case-id", "work")
    assert resumed["request"]["predecessor"] == checkpoint["reference"]
    assert resumed["request"]["questions"] == initial_request["questions"]
    request_file.write_text(json.dumps(resumed["request"], ensure_ascii=False), encoding="utf-8")
    finished = {**partial, "body": "合成修订：论证缩小到证据支持范围",
                "findings": [{"question_id": "q1", "status": "answered", "finding": "限定回答", "evidence": [0]}],
                "challenges": [{"question_id": "q1", "argument": "竞争解释", "disposition": "changed", "response": "已据此调整论证", "evidence": [0]}],
                "stop": "concluded", "stop_reason": "声明范围内已回答，不是系统认证"}
    input_file.write_text(json.dumps(finished, ensure_ascii=False), encoding="utf-8")
    second_work = command("checkpoint-research", "--request", str(request_file), "--input", str(input_file),
                          "--submission-id", "work-two", "--revision-id", "r2")
    assert command("checkpoint-research", "--request", str(request_file), "--input", str(input_file),
                   "--submission-id", "work-two", "--revision-id", "r2") == second_work
    assert command("current", "--case-id", "work")["reference"] == second_work["reference"]
    assert second_work["human_acceptance"] == "not-confirmed"
