"""Manual Actions host for reviewed financial and participant sources; no store or research writes."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = "auguspp/Decision-OS-2"
INPUTS = {"operation", "code_sha", "codes", "periods", "apis", "page_size",
          "max_pages", "public_rights_confirmed", "family", "dates", "source_plan", "access_confirmed"}


def settings(event: dict, env: dict) -> dict:
    """Host selection is explicit; this does not authenticate the source's claims."""
    if (env.get("GITHUB_REPOSITORY") != REPO or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise ValueError("RELAY_RUNTIME_MANUAL_MAIN_ONLY")
    if not isinstance(event, dict):
        raise ValueError("RELAY_RUNTIME_EVENT_INVALID")
    data = event.get("inputs")
    if not isinstance(data, dict) or not set(data) <= INPUTS:
        raise ValueError("RELAY_RUNTIME_INPUT_INVALID")
    sha = data.get("code_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != env.get("GITHUB_SHA"):
        raise ValueError("RELAY_RUNTIME_CODE_SHA_MISMATCH")
    operation = data.get("operation", "plan")
    if operation not in ("plan", "capture", "check"):
        raise ValueError("RELAY_RUNTIME_OPERATION_INVALID")
    from decision_kernel.interfaces import pmi_check_commands
    if data.get("family") == pmi_check_commands.FAMILY:
        return pmi_check_commands.settings(data, sha)
    if operation == "check" or data.get("access_confirmed") not in (None, False, "false"):
        raise ValueError("RELAY_RUNTIME_UNUSED_CHECK_MODE")
    declaration = data.get("public_rights_confirmed")
    rights = declaration is True or type(declaration) is str and declaration == "true"
    if operation == "capture" and not rights:
        raise ValueError("RELAY_RUNTIME_PUBLIC_RIGHTS_REQUIRED")
    def positive(name, default):
        value = str(data.get(name, default))
        if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
            raise ValueError("RELAY_RUNTIME_PAGINATION_INVALID")
        return value
    def words(name, pattern, optional=False):
        value = data.get(name, "")
        if not isinstance(value, str):
            raise ValueError("RELAY_RUNTIME_INPUT_INVALID")
        result = value.replace(",", " ").split()
        if (not result and not optional) or any(not re.fullmatch(pattern, x) for x in result):
            raise ValueError("RELAY_RUNTIME_SELECTION_INVALID")
        return result
    family = data.get("family", "financial")
    if family not in ("financial", "participants", "research"):
        raise ValueError("RELAY_RUNTIME_FAMILY_INVALID")
    if family == "research":
        if any(data.get(k, "") for k in ("codes", "periods", "dates", "apis")):
            raise ValueError("RELAY_RUNTIME_UNUSED_SELECTOR")
        from decision_kernel.application.relay_registry import _json, RelayRegistry
        from decision_kernel.application.research_sources import validate_plan
        raw = data.get("source_plan", "")
        if not isinstance(raw, str) or not 0 < len(raw.encode()) <= 60000:
            raise ValueError("RELAY_RUNTIME_SOURCE_PLAN_INVALID")
        plan = _json(raw.encode())
        validate_plan(plan, RelayRegistry(Path("docs/sources/relay-registry.json")))
        return {"operation": operation, "code_sha": sha, "family": family,
                "source_plan": plan, "max_pages": positive("max_pages", "100"), "public_rights_confirmed": rights}
    if data.get("source_plan", ""):
        raise ValueError("RELAY_RUNTIME_UNUSED_SOURCE_PLAN")
    if family == "financial" and data.get("dates", "") or family == "participants" and data.get("periods", ""):
        raise ValueError("RELAY_RUNTIME_UNUSED_DATE_SELECTOR")
    codes = words("codes", r"[0-9]{6}\.(SH|SZ|BJ)")
    periods = words("periods" if family == "financial" else "dates", r"[0-9]{8}")
    for period in periods:
        parsed = date(int(period[:4]), int(period[4:6]), int(period[6:]))
        if family == "financial" and parsed.strftime("%m%d") not in ("0331", "0630", "0930", "1231"):
            raise ValueError("RELAY_RUNTIME_REPORT_PERIOD_INVALID")
    apis = words("apis", r"[a-z][a-z_]*", optional=True)
    result = {"operation": operation, "code_sha": sha, "codes": codes, "periods": periods,
            "apis": apis, "page_size": positive("page_size", "100"),
            "max_pages": positive("max_pages", "100"), "public_rights_confirmed": rights}
    if family == "participants":
        result["family"] = family
        result["dates"] = result.pop("periods")
    return result


def execute(stage: str, event: dict, env: dict, out: Path, *, run=subprocess.run) -> int:
    selected = settings(event, env)
    root = Path(env["RUNNER_TEMP"]).resolve()
    if not out.resolve().is_relative_to(root) or out.resolve() == root or out.is_symlink():
        raise ValueError("RELAY_RUNTIME_OUTPUT_INVALID")
    from decision_kernel.interfaces import pmi_check_commands
    if selected.get("family") == pmi_check_commands.FAMILY:
        return pmi_check_commands.execute(stage, selected, out)
    if selected.get("family") == "research":
        cli = [sys.executable, "-m", "decision_kernel.interfaces.relay_research_cli"]
        plan_path = out / "source-plan.json"
        if stage == "prepare":
            out.mkdir(parents=False, exist_ok=False)
            plan_path.write_text(json.dumps(selected["source_plan"], ensure_ascii=False), encoding="utf-8")
            outcome = run([*cli, "plan", "--plan", str(plan_path), "--output", str(out / "plan.json")], check=False)
            if outcome.returncode:
                return outcome.returncode
            (out / "request.json").write_text(json.dumps(selected, sort_keys=True), encoding="utf-8")
            (out / "result.json").write_text(json.dumps({"status": "planned-not-captured",
                "code_sha": selected["code_sha"], "records_written": False}), encoding="utf-8")
            return 0
        if stage != "capture" or selected["operation"] != "capture":
            raise ValueError("RELAY_RUNTIME_CAPTURE_NOT_SELECTED")
        if (json.loads((out / "request.json").read_text()) != selected
                or json.loads(plan_path.read_text()) != selected["source_plan"]):
            raise ValueError("RELAY_RUNTIME_PLAN_CHANGED")
        outcome = run([*cli, "capture", "--plan", str(plan_path), "--live", "--public-rights-confirmed",
                       "--output", str(out / "capture.jsonl"), "--max-pages", selected["max_pages"]], check=False)
        (out / "result.json").write_text(json.dumps({"status": "capture-returned" if outcome.returncode == 0
            else "capture-incomplete", "exit_code": outcome.returncode, "code_sha": selected["code_sha"],
            "records_written": False, "qualification": "source-use-not-Full-economic-acceptance"}), encoding="utf-8")
        return outcome.returncode
    participant = selected.get("family") == "participants"
    cli = [sys.executable, "-m", "decision_kernel.interfaces.relay_" + ("participant" if participant else "financial") + "_cli"]
    if stage == "prepare":
        out.mkdir(parents=False, exist_ok=False)
        args = [*cli, "plan", "--codes", *selected["codes"], ("--dates" if participant else "--periods"), *selected["dates" if participant else "periods"],
                "--output", str(out / "plan.jsonl")]
        if selected["apis"]:
            args += ["--apis", *selected["apis"]]
        outcome = run(args, check=False)
        if outcome.returncode:
            return outcome.returncode
        # The existing CLI, not this host, owns per-API field/parameter validation.
        (out / "request.json").write_text(json.dumps(selected, sort_keys=True) + "\n", encoding="utf-8")
        (out / "result.json").write_text(json.dumps({"status": "planned-not-captured",
            "code_sha": selected["code_sha"], "records_written": False}) + "\n", encoding="utf-8")
        return 0
    if stage != "capture" or selected["operation"] != "capture":
        raise ValueError("RELAY_RUNTIME_CAPTURE_NOT_SELECTED")
    if json.loads((out / "request.json").read_text(encoding="utf-8")) != selected:
        raise ValueError("RELAY_RUNTIME_PLAN_CHANGED")
    outcome = run([*cli, "capture", "--plan", str(out / "plan.jsonl"), "--live",
                   "--output", str(out / "capture.jsonl"), "--page-size", selected["page_size"],
                   "--max-pages", selected["max_pages"]], check=False)
    (out / "result.json").write_text(json.dumps({"status": "capture-returned" if outcome.returncode == 0
        else "capture-incomplete", "exit_code": outcome.returncode,
        "code_sha": selected["code_sha"], "records_written": False,
        "qualification": "inspect-per-query-results-not-PDF-or-research-acceptance"}) + "\n", encoding="utf-8")
    return outcome.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "capture", "check"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with Path(os.environ["GITHUB_EVENT_PATH"]).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("RELAY_RUNTIME_EVENT_TOO_LARGE")
        return execute(args.stage, json.loads(raw), dict(os.environ), args.out)
    except (ValueError, KeyError, TypeError, OSError):
        # Do not print event data, source response prose, paths containing input, or secrets.
        print('{"status":"rejected-or-interrupted","error":"RELAY_RUNTIME_FAILED_CHECK_INPUTS_AND_RESULTS"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
