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
