"""Recover exact native RSS source intent without fetching a publisher again.

Only an explicit known-unwritten CAS conflict can be saved. Unknown writes use
read-only reconciliation. Metadata-only, fixed native archive, existing writer.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import sys
import zipfile

from decision_kernel.application.rss_intake import (
    _assemble, _check_previous, open_capture, read_capture, reference, source_input, verify_capture,
)
from decision_kernel.domain.records import MAX_BYTES, RecordError, SourceInput, digest, encoded, identifier, require
from decision_kernel.interfaces.news_custody import MAX_ARCHIVE, REPOSITORY, verify_native
from decision_kernel.interfaces.research_payload import json_object
from decision_kernel.observations.rss_failure import SCHEMA as FAILURE_SCHEMA

NATIVE_WORKFLOW = ".github/workflows/rss-intake.yml"
MEMBERS = {"inputs.json", "report.json", "source.json", "capture.json", "reading.json"}


def selection(value):
    require(isinstance(value, dict) and set(value) == {"source_run", "artifact_id", "artifact_sha256"},
            "RSS_RECOVERY_EXACT_SELECTION_REQUIRED")
    require(all(isinstance(value[k], str) and re.fullmatch(r"[1-9][0-9]{0,19}", value[k])
                for k in ("source_run", "artifact_id"))
            and isinstance(value["artifact_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["artifact_sha256"]), "RSS_RECOVERY_SELECTION_INVALID")
    return value


def _read(path, maximum=MAX_BYTES):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "RSS_RECOVERY_INPUT_BOUND")
    return raw


def native_metadata(run, artifact, chosen):
    selection(chosen)
    verify_native(run, artifact, chosen, workflow_path=NATIVE_WORKFLOW, artifact_prefix="dos2-rss-")


def source_from_archive(raw, run, artifact, chosen, *, submission_id, reconcile=False):
    native_metadata(run, artifact, chosen)
    identifier(submission_id)
    require(isinstance(raw, bytes) and len(raw) == artifact["size_in_bytes"]
            and digest(raw) == chosen["artifact_sha256"], "RSS_RECOVERY_ARCHIVE_BYTES")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as bundle:
            infos = bundle.infolist()
            require(len(infos) == len(MEMBERS) and {i.filename for i in infos} == MEMBERS
                    and all(not i.is_dir() and not stat.S_ISLNK(i.external_attr >> 16)
                            and not i.flag_bits & 1 and 0 < i.file_size <= MAX_BYTES for i in infos)
                    and sum(i.file_size for i in infos) <= MAX_ARCHIVE, "RSS_RECOVERY_ARCHIVE_MEMBERS")
            parts = {}
            for item in infos:
                with bundle.open(item) as stream:
                    body = stream.read(MAX_BYTES + 1)
                require(len(body) == item.file_size and len(body) <= MAX_BYTES, "RSS_RECOVERY_EXPANSION_BOUND")
                parts[item.filename] = body
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError):
        raise RecordError("RSS_RECOVERY_ARCHIVE_INVALID") from None
    inputs, report = json_object(parts["inputs.json"]), json_object(parts["report.json"])
    require(inputs.get("operation") == report.get("operation") == "capture"
            and inputs.get("code_sha") == report.get("code_sha") == run["head_sha"]
            and report.get("run_id") == chosen["source_run"] and report.get("run_attempt") == 1
            and inputs.get("submission_id") == submission_id, "RSS_RECOVERY_ORIGINAL_IDENTITY")
    require(inputs.get("public_metadata_custody_confirmed") == "true", "RSS_RECOVERY_ORIGINAL_RIGHTS")
    if not reconcile:
        require(run["conclusion"] == "failure" and report.get("status") == "failed"
                and report.get("write") == "not-attempted"
                and report.get("error_code") == "RSS_PLANNED_HEAD_CHANGED",
                "RSS_RECOVERY_REQUIRES_UNWRITTEN_CONFLICT")
    plan = json_object(inputs["plan"].encode("utf-8"))
    require(set(plan) <= {"feed_id", "bootstrap", "predecessor", "retain_original", "rights_basis"}
            and plan.get("retain_original", False) is False and not plan.get("rights_basis"),
            "RSS_RECOVERY_METADATA_ONLY")
    value = verify_capture(json_object(parts["capture.json"]))
    failed = value["schema"] == FAILURE_SCHEMA
    require(value["batch_id"] == "rss-run-" + chosen["source_run"]
            and plan.get("feed_id") == (value["feed_id"] if failed else value["window"]["feed_id"])
            and type(plan.get("bootstrap")) is bool
            and plan["bootstrap"] == (value["predecessor"]["reference"] is None)
            and (reference(plan["predecessor"]) if plan.get("predecessor") is not None else None)
            == value["predecessor"]["reference"], "RSS_RECOVERY_CAPTURE_PLAN_MISMATCH")
    require(value["original"]["status"] == ("not-retained-failed-attempt" if failed else "not-retained-rights-unconfirmed"),
            "RSS_RECOVERY_METADATA_ONLY")
    source = SourceInput.from_dict(json_object(parts["source.json"]))
    require(encoded(asdict(source)) == parts["source.json"]
            and encoded(asdict(source_input(value, submission_id))) == parts["source.json"]
            and source.text.encode("utf-8") == parts["capture.json"]
            and encoded(read_capture(value)) == parts["reading.json"], "RSS_RECOVERY_ORIGINAL_BYTES_DIFFER")
    return source, value, report


def load_original(root, chosen, *, submission_id, reconcile=False):
    run = json_object(_read(root / "run.json"))
    artifact = json_object(_read(root / "artifact.json"))
    source, value, report = source_from_archive(_read(root / "source.zip", MAX_ARCHIVE), run, artifact,
        chosen, submission_id=submission_id, reconcile=reconcile)
    return source, value, {"selection": chosen, "run": run, "artifact": artifact, "original_report": report}


def verify_transition(store, value):
    """Reuse the same one-step replay as open_capture, never source HTTP."""
    if value["schema"] == FAILURE_SCHEMA:
        return  # Failure is not a transition; its missing prior must stay readable.
    prior = value["predecessor"]["reference"]
    previous = open_capture(store, prior) if prior is not None else None
    if previous:
        _check_previous(previous, value["window"]["feed_id"], value["requested_at"], value["batch_id"])
    require(value == _assemble(value["window"], batch_id=value["batch_id"], requested_at=value["requested_at"],
        received_at=value["received_at"], previous=previous, predecessor=prior, original=value["original"]),
        "RSS_RECOVERY_TRANSITION_DIFFERS")


def _chosen_inputs():
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + NATIVE_WORKFLOW + "@refs/heads/main"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "RSS_RECOVERY_MANUAL_HOST_REQUIRED")
    inputs = json_object(_read(os.environ["GITHUB_EVENT_PATH"]))["inputs"]
    require(inputs.get("operation") in ("recover", "reconcile")
            and inputs.get("code_sha") == os.environ.get("GITHUB_SHA"), "RSS_RECOVERY_CODE_OR_OPERATION")
    if inputs["operation"] == "recover":
        require(inputs.get("public_metadata_custody_confirmed") == "true", "RSS_RECOVERY_RIGHTS_REQUIRED")
    return selection(json_object(inputs["plan"].encode("utf-8")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="RSS native archive preflight, never publisher requests")
    parser.add_argument("operation", choices=("select", "preflight"));parser.add_argument("--root",type=Path)
    args = parser.parse_args(argv)
    try:
        chosen = _chosen_inputs()
        if args.operation == "select":
            print(" ".join(chosen[k] for k in ("source_run", "artifact_id", "artifact_sha256")))
        else:
            require(args.root is not None, "RSS_RECOVERY_ROOT_REQUIRED")
            native_metadata(json_object(_read(args.root / "run.json")), json_object(_read(args.root / "artifact.json")), chosen)
            print("verified-native-rss-archive")
        return 0
    except (RecordError,OSError,KeyError,TypeError,UnicodeError) as error:
        print(str(error) if isinstance(error,RecordError) else "RSS_RECOVERY_INPUT_FAILED",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
