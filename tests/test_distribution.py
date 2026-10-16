"""Cold wheel installation outside the checkout, with no dependency/network access."""
from email.parser import Parser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from packaging.requirements import Requirement

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
        headers = Parser().parsestr(metadata)
        requirements = [Requirement(item) for item in headers.get_all("Requires-Dist", [])]
        # Optional RSS is declared, but installing the base wheel must activate no dependency.
        assert headers.get_all("Provides-Extra", []) == ["rss"]
        assert all(item.marker is not None and not item.marker.evaluate({"extra": ""}) for item in requirements)
        assert {str(item) for item in requirements} == {
            'feedparser==6.0.14; extra == "rss"', 'feedparser-sgmllib==2.1.0; extra == "rss"'}
        assert all(item.marker.evaluate({"extra": "rss"}) for item in requirements)
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
    subprocess.run([str(python), "-I", "-c",
                    "from importlib.util import find_spec; assert find_spec('feedparser') is None; "
                    "from decision_kernel.application.rss_intake import read_capture"],
                   cwd=outside, env=env, check=True, capture_output=True, text=True)

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
                                     "body": "合成工程检查，不是真实投资研究。", "mandate": "synthetic:mandate",
                                     "method": "synthetic:method", "sources": [reference]}, ensure_ascii=False), encoding="utf-8")
    revision = command("submit-revision", "--submission-id", "two", "--input", str(input_file))
    ref_file.write_text(json.dumps(revision), encoding="utf-8")
    assert command("read-revision", "--ref", str(ref_file))["body"] == "合成工程检查，不是真实投资研究。"
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

    # The same installed wheel exposes the host bridge, but a plain process cannot run it.
    runtime_module = "decision_kernel.interfaces.research_runtime"
    runtime_help = subprocess.run([str(python), "-I", "-m", runtime_module, "--help"],
                                  cwd=outside, env=env, check=True, capture_output=True, text=True)
    assert "Explicit public research I/O" in runtime_help.stdout
    no_host = {k: v for k, v in env.items() if not k.startswith(("GITHUB_", "DOS2_", "DECISION_OS_"))}
    rejected = subprocess.run([str(python), "-I", "-m", runtime_module, "--out", str(outside / "host-output")],
                              cwd=outside, env=no_host, capture_output=True, text=True)
    assert rejected.returncode == 2 and json.loads(rejected.stderr)["error"] == "MANUAL_MAIN_RUNTIME_REQUIRED"
    assert not (outside / "host-output").exists()

    # Same cold wheel: full method is usable offline, with no data-store credential.
    method = subprocess.run([str(python), "-I", "-m", "decision_kernel.methods.research", "--kind", "full"],
                            cwd=outside, env=no_host, check=True, capture_output=True, text=True)
    assert json.loads(method.stdout)["id"] == "decision-os-research-full-v2"
    assert prepared["method"]["sha256"] == json.loads(method.stdout)["sha256"]
    assert resumed["continuation"]["actions"][0]["next_action"] == "完成合成计算"
    completed = command("prepare-research", "--case-id", "work")
    assert completed["continuation"]["actions"] == []
    assert completed["continuation"]["research_assessment"] == "not-performed"
    assert completed["continuation"]["review_flags"] == ["DECLARED_CONCLUSION_REQUIRES_SUBSTANTIVE_REVIEW"]

    # Same installed wheel: discover cases without IDs, then open pinned revisions.
    catalogue = command("research-catalogue", "--limit", "1")
    assert catalogue["total_cases"] == 2 and catalogue["items"][0]["case_id"] == "sample"
    assert catalogue["items"][0]["reference"] == newest
    catalogue_cursor = outside / "catalogue-cursor.json"
    catalogue_cursor.write_text(json.dumps(catalogue["next_cursor"]), encoding="utf-8")
    # A new alphabetically earlier case must not displace items on the existing page.
    input_file.write_text(json.dumps({"case_id": "late", "revision_id": "r1", "kind": "quick",
        "body": "较晚保存的合成研究", "mandate": "合成委托", "method": "synthetic", "sources": [reference]}, ensure_ascii=False), encoding="utf-8")
    command("submit-revision", "--submission-id", "late", "--input", str(input_file))
    tail = command("research-catalogue", "--cursor", str(catalogue_cursor))
    assert tail["total_cases"] == 2 and tail["snapshot"] == catalogue["snapshot"]
    card, = tail["items"]
    assert card["case_id"] == "work" and card["reference"] == second_work["reference"]
    assert card["continuation"]["actions"] == []
    assert card["open_operation"] == "prepare" and card["research_assessment"] == "not-performed"
    open_ref = outside / "catalogue-open.json"
    open_ref.write_text(json.dumps(card["reference"]), encoding="utf-8")
    opened = command("prepare-research", "--ref", str(open_ref))
    assert opened["previous_context"]["research"]["reference"] == card["reference"]
    assert command("research-catalogue")["total_cases"] == 3

    # Same cold wheel: a complete large payload packs offline and reaches the host parser intact.
    transport_input = outside / "long-source.json"
    long_source = {**payload, "text": "合成完整资料，保留全部正文。\n" * 10000}
    transport_input.write_text(json.dumps(long_source, ensure_ascii=False), encoding="utf-8")
    packed = subprocess.run([str(python), "-I", "-m", "decision_kernel.interfaces.research_payload",
                             "--input", str(transport_input)], cwd=outside, env=no_host,
                            check=True, capture_output=True, text=True)
    assert transport_input.stat().st_size > 60 * 1024 and len(packed.stdout.encode()) <= 60 * 1024
    wire = outside / "packed-source.json"
    wire.write_text(packed.stdout, encoding="utf-8")
    parsed = subprocess.run([str(python), "-I", "-c",
        "import json,sys; from pathlib import Path; "
        "from decision_kernel.interfaces.research_runtime import parse_inputs; "
        "v=parse_inputs(dict(operation='put-source',payload=Path(sys.argv[1]).read_text(encoding='utf-8'),"
        "code_sha='a'*40,submission_id='long-source',expected_head='b'*40)); "
        "print(json.dumps(v['payload'],ensure_ascii=False))", str(wire)],
        cwd=outside, env=no_host, check=True, capture_output=True, text=True)
    assert json.loads(parsed.stdout) == long_source

    # Same installation: source -> calculation -> report checkpoint -> exact re-audit.
    from test_numerical_integration import exercise_installed_numbers
    exercise_installed_numbers(command, outside, python, env, venv / "bin/decision-os", store)


    # Same cold wheel: complete originals, source registration, and independent readback.
    from test_research_delivery_integration import exercise_installed_delivery
    exercise_installed_delivery(command, outside, python, env, venv / "bin/decision-os", store)

    # Same cold wheel: discover unlinked pool sources and retain their exact refs in research.
    from test_research_evidence_integration import exercise_installed_evidence
    exercise_installed_evidence(command, outside)

    # Same cold wheel: exact Research -> frozen spec -> two prices -> saved readback.
    from test_odds_runtime import exercise_installed_odds
    exercise_installed_odds(command, outside, python, env, venv / "bin/decision-os", store)

    # Same installation: source-use acceptance cannot be inferred from a saved report.
    from test_research_source_use_integration import exercise_installed_source_use
    exercise_installed_source_use(command, outside, python, env, venv / "bin/decision-os", store)

    # Same installation: resumable bounded source scan, not a full remote rescan.
    from test_source_scan import exercise_installed_scan
    exercise_installed_scan(command, outside)

    # Same cold wheel: news -> explicit choice -> saved Brief -> independent read.
    from test_news_delivery_host import exercise_installed_news
    exercise_installed_news(outside, python, env, store)
